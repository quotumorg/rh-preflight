"""Plausible wallet and terminal paths, with all remote reads replaced."""
from copy import deepcopy
import json

import pytest

from rh_preflight.card import Card, WORDING
from rh_preflight.extract import tx_intent
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.policy import evaluate
from rh_preflight.settings import Settings
from rh_preflight.sources.common import Reading
from rh_preflight.transactions import local_card, project, MAX, PERMIT2

A, B, C = ('0x' + c * 40 for c in 'abc')
RPC = 'https://rpc.mainnet.chain.robinhood.com'
OK = {'x-toll': {'simulation': {'status': 'success', 'block': 10}}}


def service_at(tmp_path, response=None, **settings):
    service = Service(settings=Settings(**settings), ledger=Ledger(tmp_path), presence=lambda: False)
    service._read_cards = lambda targets, *a, **kw: [Card(t, 'identifier') for t in targets]
    service.network.clients['chain'].lookup = lambda *a, **kw: Reading('Robinhood Chain', 'no_code', '')
    service.network.clients['toll'].check_tx = lambda *a, **kw: response
    return service


@pytest.mark.parametrize('value', ['1', '0x1', 1])
@pytest.mark.parametrize('shape', ['object', 'json', 'params', 'rpc'])
def test_native_transfer_without_data_uses_unchecked(tmp_path, value, shape):
    tx = {'to': A, 'value': value, 'from': B}
    args = {'chainId': '0x1237', 'transaction': tx}
    if shape == 'json':
        args = {'arguments': json.dumps(args)}
    elif shape in {'params', 'rpc'}:
        envelope = {'method': 'eth_sendTransaction', 'params': [tx]}
        args = {'chainId': '4663', **(envelope if shape == 'params' else {'rpc': envelope})}
    intents = tx_intent(args)
    assert len(intents) == 1 and intents[0]['payload']['data'] == '0x'
    service = service_at(tmp_path, on_unchecked='stop', ask_payout_other=False)
    result = service.pre_tool_call('unknown_wallet', args)
    assert result['action'] == 'block' and 'transaction could not be checked' in result['message']


@pytest.mark.parametrize('field', ['data', 'input', 'calldata'])
def test_missing_sender_cannot_claim_success(tmp_path, field):
    service = service_at(tmp_path, OK, on_unchecked='stop')
    result = service.pre_tool_call('wallet', {'chainId': '0x1237', 'to': A, field: '0x12345678'})
    assert result['action'] == 'block'
    assert 'sending wallet not recorded' in result['message']


@pytest.mark.parametrize('wrap', [lambda r: r, lambda r: {'params': r}, lambda r: {'arguments': json.dumps(r)}])
def test_direct_rpc_signature_envelopes_gate_dai(tmp_path, wrap):
    typed = {'domain': {'name': 'DAI', 'chainId': '0x1237', 'verifyingContract': C},
             'types': {'Permit': []}, 'primaryType': 'Permit',
             'message': {'holder': A, 'spender': B, 'allowed': True, 'nonce': '0', 'expiry': '0'}}
    args = wrap({'method': 'eth_signTypedData_v4', 'params': [A, json.dumps(typed)]})
    service = service_at(tmp_path, OK)
    result = service.pre_tool_call('wallet_request', args)
    assert result['action'] == 'block'
    assert 'amount unlimited, deadline no expiry' in result['message']


@pytest.mark.parametrize('amount', [MAX, str(MAX), hex(MAX), '17', '0x11'])
@pytest.mark.parametrize('deadline', [0, '0', hex(MAX)])
def test_permit_numeric_representations(amount, deadline):
    typed = {'domain': {'chainId': '4663', 'verifyingContract': C}, 'types': {'Permit': []},
             'primaryType': 'Permit', 'message': {'spender': B, 'value': amount, 'deadline': deadline}}
    card = project(local_card(tx_intent({'payload': typed})[0]), tx_intent({'payload': typed})[0], kinds={B: 'no code'})
    assert card.data['approvals'][0]['unlimited'] == (amount in [MAX, str(MAX), hex(MAX)])
    assert evaluate([card], Settings()).action == ('block' if card.data['approvals'][0]['unlimited'] else 'ask')
    assert ('deadline no expiry' if deadline == hex(MAX) else 'deadline 1970-01-01T00:00:00Z') in card.render()


def test_permit2_batch_preserves_each_token_and_uint160_amount():
    typed = {'domain': {'chainId': '0x1237', 'verifyingContract': PERMIT2},
             'types': {'PermitBatch': []}, 'primaryType': 'PermitBatch',
             'message': {'spender': B, 'sigDeadline': str(MAX), 'details': [
                 {'token': A, 'amount': hex(2 ** 160 - 1), 'nonce': '1', 'expiration': '0'},
                 {'token': C, 'amount': '17', 'nonce': '2', 'expiration': '0'}]}}
    intent = tx_intent(typed)[0]
    card = project(local_card(intent), intent, kinds={B: 'no code'})
    assert [(a['token'], a['unlimited']) for a in card.data['approvals']] == [(A, True), (C, False)]
    assert evaluate([card], Settings()).action == 'block'


def test_signing_message_is_not_fabricated_as_an_unsigned_native_transfer():
    typed = {'domain': {'chainId': '4663', 'verifyingContract': C},
             'types': {'TransferWithAuthorization': []}, 'primaryType': 'TransferWithAuthorization',
             'message': {'from': A, 'to': B, 'value': '17'}}
    args = {'chainId': 4663, 'method': 'eth_signTypedData_v4', 'params': [A, json.dumps(typed)]}
    intents = tx_intent(args)
    assert len(intents) == 1
    assert intents[0]['payload'] == {'rpc': {'method': 'eth_signTypedData_v4', 'params': args['params']}}


@pytest.mark.parametrize('allowed', [True, False])
def test_dai_calldata_allowed_boolean_is_not_the_expiry(allowed):
    data = '0x8fcbaf0c' + A[2:].zfill(64) + B[2:].zfill(64) + f'{0:064x}{MAX:064x}{int(allowed):064x}'
    intent = tx_intent({'chainId': 4663, 'from': A, 'to': C, 'data': data})[0]
    card = project(local_card(intent), intent, OK, kinds={B: 'no code'})
    assert card.data['approvals'][0]['unlimited'] is allowed
    assert evaluate([card], Settings()).action == ('block' if allowed else 'ask')


def test_rpc_send_can_omit_both_default_value_and_data():
    intents = tx_intent({'chainId': '0x1237', 'method': 'eth_sendTransaction', 'params': [{'to': B, 'from': A}]})
    assert len(intents) == 1 and intents[0]['payload']['data'] == '0x'


@pytest.mark.parametrize('raw', [False, True])
def test_literal_cast_approve_stops_before_terminal(tmp_path, raw):
    data = '0x095ea7b3' + B[2:].zfill(64) + f'{MAX:064x}'
    call = f'--data {data}' if raw else f"'approve(address,uint256)' {B} {MAX}"
    args = {'command': f'cast send {C} {call} --from {A} --rpc-url {RPC}'}
    service = service_at(tmp_path)
    result = service.pre_tool_call('terminal', args)
    assert result['action'] == 'block' and 'spender no code' in result['message']


@pytest.mark.parametrize('value,expected', [('1', '1'), ('0x1', '0x1'), ('0.1ether', '100000000000000000')])
def test_literal_cast_native_value_and_no_secrets(tmp_path, value, expected):
    args = {'command': f'cast send {B} --value {value} --chain=4663 --private-key synthetic-secret'}
    intent = tx_intent(args)[0]
    assert intent['payload'] == {'chainId': 4663, 'to': B, 'data': '0x', 'value': expected}
    service = service_at(tmp_path, OK, on_unchecked='stop')
    result = service.pre_tool_call('terminal', args)
    assert result['action'] == 'block'
    assert 'synthetic-secret' not in json.dumps(service.ledger.rows()) + result['message']


def test_cast_call_stays_read_only_and_other_chain_send_is_ignored():
    assert not tx_intent({'command': f"cast call {C} 'approve(address,uint256)' {B} {MAX} --rpc-url {RPC}"})
    assert not tx_intent({'command': f'cast send {B} --value 1 --chain 1'}, 4663)


def test_unknown_cast_function_uses_unchecked(tmp_path):
    service = service_at(tmp_path, on_unchecked='stop')
    result = service.pre_tool_call('terminal', {'command': f"cast send {C} 'withdraw(uint256)' 17 --rpc-url {RPC}"})
    assert result['action'] == 'block' and 'transaction could not be checked' in result['message']


def test_local_approval_for_all_is_a_collection_grant(tmp_path):
    args = {'command': f"cast send {C} 'setApprovalForAll(address,bool)' {B} true --from {A} --chain 4663"}
    result = service_at(tmp_path).pre_tool_call('terminal', args)
    assert result['action'] == 'block' and f'all items in collection {C}' in result['message']
    assert 'approves unlimited' not in result['message']


def test_local_approval_for_all_false_revokes_collection_authority():
    data = '0xa22cb465' + B[2:].zfill(64) + f'{0:064x}'
    intent = tx_intent({'chainId': 4663, 'from': A, 'to': C, 'data': data})[0]
    card = project(local_card(intent), intent, OK, kinds={B: 'no code'})
    assert evaluate([card], Settings()).action == 'pass'
    assert f'revokes approval for all items in collection {C}' in card.render()


@pytest.mark.parametrize('evidence', [{}, {'token': C, 'spender': B}])
def test_remote_approval_for_all_wording_and_no_prose_on_any_surface(tmp_path, evidence):
    response = deepcopy(OK)
    markers = ['action-prose-private', 'scanner-prose-private', 'warning-prose-private']
    response.update(action=markers[0], scanners=[{'verdict': markers[1]}], warnings=[{
        'code': 'approval-for-all-to-eoa', 'message': markers[2], 'x-toll-evidence': evidence}])
    service = service_at(tmp_path, response)
    args = {'chainId': 4663, 'to': A, 'from': B, 'data': '0x12345678'}
    result = service.pre_tool_call('wallet', args)
    rows = service.ledger.rows()
    output = json.dumps(rows) + json.dumps(result) + service.transform_tool_result('wallet', args, '{}')
    assert all(marker not in output for marker in markers)
    assert 'all items in' in result['message'] and 'approves unlimited' not in result['message']
    assert not WORDING.search(rows[0]['cards'][0]['card'])
