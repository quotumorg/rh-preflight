"""Fourth hostile review: signature scope and original metadata sources."""
from copy import deepcopy
import json

import pytest

from rh_preflight.stock_swap import RULE
from test_stock_swap_above_usd import A, B, WALLET, ROUTER, app, run
from test_stock_swap_r2 import raw_swap
from test_stock_swap_review import decoded, value_card


@pytest.mark.parametrize('side', ['sold', 'bought'])
@pytest.mark.parametrize('conflict', ['identity', 'decimals'])
def test_non_swap_decoded_sides_cannot_hide_metadata_conflicts(side, conflict):
    response = decoded()
    bad = {'address': A, 'decimals': 18}
    if conflict == 'identity':
        response['coins'] = []
        bad = {'address': B, 'token': A, 'decimals': 6}
    response['x-toll']['decoded'].append({'kind': 'transfer', side: [bad]})
    card = value_card(response)
    assert not card.data.get(RULE)
    assert 'swap value not read: decimals' in card.data['lines']


@pytest.mark.parametrize('side', ['sold', 'bought'])
def test_consistent_metadata_from_non_swap_decoded_sides_remains_usable(side):
    response = decoded()
    response['coins'] = []
    response['x-toll']['decoded'].append({'kind': 'transfer', side: [
        {'token': {'address': A, 'decimals': 6}}]})
    assert value_card(response).data[RULE]


@pytest.mark.parametrize('method', ['eth_signTypedData_v4', 'personal_sign'])
@pytest.mark.parametrize('independent', [False, True])
def test_signature_response_is_not_a_swap(tmp_path, method, independent):
    typed = {'domain': {'chainId': 4663}, 'types': {'Message': []},
             'primaryType': 'Message', 'message': {'token': A}}
    params = [WALLET, typed] if method == 'eth_signTypedData_v4' else ['sign in', WALLET]
    signature = {'method': method, 'params': params}
    calls = [signature]
    if independent:
        calls.append({'chainId': 4663, 'from': WALLET, 'to': ROUTER, 'data': '0x'})
    service, _ = app(tmp_path)
    service.network.clients['toll'].check_tx = lambda *a: deepcopy(decoded())
    _, row, printed = run(service, {'chainId': 4663, 'token': A, 'calls': calls}, 'wallet_swap_sign')
    assert any(RULE in rule for rule in row['rules']) == independent
    assert sum(line.startswith('swap value') for line in printed.splitlines()) == int(independent)


@pytest.mark.parametrize('method', ['eth_signTypedData_v4', 'personal_sign'])
@pytest.mark.parametrize('shape', ['raw', 'generic', 'unsigned', 'wallet', 'cast'])
@pytest.mark.parametrize('independent', [False, True])
def test_signature_rpc_content_does_not_become_a_swap(tmp_path, method, shape, independent):
    action = raw_swap(**({'amountIn': '3'} if shape == 'generic' else {}))
    payload = {'chainId': 4663, 'from': WALLET, 'to': ROUTER, 'data': '0x'}
    if shape == 'unsigned':
        message = json.dumps(payload)
    elif shape == 'wallet':
        message = json.dumps({'method': 'eth_sendTransaction', 'params': [payload]})
    elif shape == 'cast':
        message = f'cast send {ROUTER} --from {WALLET} --data 0x --chain 4663'
    else:
        message = json.dumps(action)
    # personal_sign accepts arbitrary text. Even an unreadable typed-data
    # request is a signature request, never an execution of its example.
    params = [message, WALLET] if method == 'personal_sign' else [WALLET, {'message': message}]
    signature = {'method': method, 'params': params}
    service, _ = app(tmp_path)
    service.network.clients['toll'].check_tx = lambda *a: deepcopy(decoded())
    arguments = {'chainId': 4663, 'token': A,
                 'calls': [signature, action] if independent else [signature]}
    _, row, printed = run(service, arguments, 'wallet_swap_sign')
    assert any(RULE in rule for rule in row['rules']) == independent
    assert sum(line.startswith('swap value') for line in printed.splitlines()) == int(independent)


@pytest.mark.parametrize('warning', [None, 'provider notice', 42])
def test_unstructured_warning_does_not_break_optional_valuation(tmp_path, warning):
    service, _ = app(tmp_path)
    response = decoded()
    response['warnings'] = [warning]
    service.network.clients['toll'].check_tx = lambda *a: deepcopy(response)
    directive = service.pre_tool_call('wallet_swap', {'chainId': 4663, 'from': WALLET,
        'to': ROUTER, 'data': '0x', 'token': A})
    assert directive['action'] == 'approve'
    row = service.ledger.rows(1)[0]
    assert row['disposition'] == 'ask'
    assert any(RULE in rule for rule in row['rules'])
    assert 'swap value $300.00 ' in directive['message']
