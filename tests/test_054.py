"""Offline transaction replay and synthetic signature/budget cases for 0.5.4."""
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import socket
import time
from urllib.error import HTTPError

import pytest

from rh_preflight.card import Card, WORDING
from rh_preflight.extract import tx_intent
from rh_preflight.hey import TemporaryUnavailable, Unavailable
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.policy import evaluate
from rh_preflight.presentation import present
from rh_preflight.settings import Settings
from rh_preflight.sources.common import Reading
from rh_preflight.sources.toll import Client as Toll
from rh_preflight.transactions import local_card, project, PERMIT2, MAX

FIXTURES = Path(__file__).parent / 'fixtures' / 'toll-tx'
TYPED = Path(__file__).parent / 'fixtures' / 'typed-data'
A, B, C = ('0x' + c * 40 for c in 'abc')


def fixture_card(case):
    folder = FIXTURES / case
    request = json.loads((folder / 'request.json').read_text())
    response = json.loads((folder / 'response.json').read_text())
    intent = tx_intent(request)[0]
    return project(local_card(intent), intent, response)


@pytest.mark.parametrize('case,expected', [('clean-swap', 'pass'), ('replayed-swap', 'ask'),
    ('unlimited-approval-eoa', 'block'), ('payout-other-wallet', 'ask')])
def test_fixture_card_byte_stable_and_own_decision(case, expected):
    card = fixture_card(case)
    decision = evaluate([card], Settings())
    printed = present(card, decision).render() + '\n'
    assert decision.action == expected
    assert printed == (FIXTURES / case / 'expected-card.txt').read_text()
    assert not WORDING.search(printed)
    assert 'careful' not in printed and 'drainer' not in printed
    assert 'source text omitted' not in printed
    # Changing their classification and every warning message changes nothing.
    response = json.loads((FIXTURES / case / 'response.json').read_text())
    response['action'] = 'BLOCK' if expected == 'pass' else 'ALLOW'
    response['scanners'] = [{'verdict': 'BLOCK'}]
    for row in response['warnings']:
        row['message'] = 'not our card copy'
    intent = tx_intent(json.loads((FIXTURES / case / 'request.json').read_text()))[0]
    assert project(local_card(intent), intent, response).data == card.data


@pytest.mark.parametrize('shape', [lambda tx: tx, lambda tx: {'nested': [tx]},
    lambda tx: {'args': json.dumps(tx)}, lambda tx: {'command': "wallet send '" + json.dumps(tx) + "'"},
    lambda tx: {'rpc': {'method': 'eth_sendTransaction', 'params': [tx]}}])
def test_content_detection_unknown_tool_shapes(shape):
    tx = {'to': A, 'data': '0x', 'chainId': '0x1237', 'from': B, 'value': '0x1'}
    intents = tx_intent(shape(tx))
    assert len(intents) == 1 and intents[0]['payload'] == tx


@pytest.mark.parametrize('chain', [1, 'garbage', True])
def test_explicit_other_chain_does_not_use_assumption(chain):
    assert not tx_intent({'chainId': chain, 'to': A, 'data': '0x'}, 4663)


def test_existing_chain_context_rpc_and_no_data_shape():
    assert tx_intent({'to': A, 'data': '0x', 'rpc_url': 'https://rpc.mainnet.chain.robinhood.com'})
    assert not tx_intent({'to': A, 'data': '0x'})
    shape = {'chainId': 4663, 'tokenIn': A, 'tokenOut': B, 'amount': '2', 'recipient': C, 'from': A}
    card = local_card(tx_intent(shape)[0])
    assert card.data['payout_other'] and 'without calldata' in card.data['lines'][0]


@pytest.mark.parametrize('case,expected', [('permit-eoa', 'block'), ('permit2-single', 'ask'),
    ('signin', 'pass'), ('wrong-chain', 'ask')])
@pytest.mark.parametrize('shape', ['object', 'json', 'command', 'wallet'])
def test_synthetic_typed_data(case, expected, shape):
    typed = json.loads((TYPED / (case + '.json')).read_text())['payload']
    args = {'chainId': 4663, 'payload': typed}
    if shape == 'json':
        args['payload'] = json.dumps(typed)
    elif shape == 'command':
        args = {'command': "wallet sign --chain 4663 '" + json.dumps(typed) + "'"}
    elif shape == 'wallet':
        args = {'chainId': 4663, 'rpc': {'method': 'eth_signTypedData_v4', 'params': [A, json.dumps(typed)]}}
    intents = tx_intent(args)
    assert len(intents) == 1
    intent = intents[0]
    card = project(local_card(intent), intent, kinds={B: 'no code'})
    # This test targets local policy rather than remote simulation availability.
    local_only = Card(card.target, card.kind, {**card.data, 'unchecked': False})
    decision = evaluate([local_only], Settings())
    assert decision.action == expected
    if case == 'permit2-single':
        assert any('Permit2' in line for line in card.data['lines'])
    if shape == 'object':
        assert present(card, evaluate([card], Settings())).render() + '\n' == (TYPED / (case + '.txt')).read_text()


def tx_card(**changes):
    base = {'lines': [], 'approvals': [], 'permit': False, 'wrong_chain': False,
            'unchecked': False, 'payout_other': False}
    return Card(A, 'transaction', {**base, **changes})


@pytest.mark.parametrize('fact,action', [
    ({'approvals': [{'unlimited': True, 'kind': 'no code'}]}, 'block'),
    ({'approvals': [{'unlimited': True, 'kind': 'contract with code'}]}, 'ask'),
    ({'approvals': [{'unlimited': True, 'kind': 'contract with code', 'known': True}]}, 'pass'),
    ({'payout_other': True}, 'ask'), ({'reverted': True}, 'ask'),
    ({'expired': True}, 'ask'), ({'short_eth': True}, 'ask'),
    ({'unchecked': True, 'moves': True}, 'ask'), ({}, 'pass'),
    ({'permit': True}, 'ask'), ({'wrong_chain': True}, 'ask')])
def test_each_policy_row(fact, action):
    assert evaluate([tx_card(**fact)], Settings()).action == action


@pytest.mark.parametrize('setting,action', [('ask', 'ask'), ('stop', 'block'), ('note', 'pass')])
def test_on_unchecked_transaction_and_builder(setting, action):
    for card in [tx_card(unchecked=True), Card(A, 'unreachable')]:
        assert evaluate([card], Settings(on_unchecked=setting)).action == action


def test_payout_switch_and_unattended_hard_stop():
    assert evaluate([tx_card(payout_other=True)], Settings(ask_payout_other=False)).action == 'pass'
    for allow in [False, True]:
        assert evaluate([tx_card(approvals=[{'unlimited': True, 'kind': 'no code'}])], Settings(allow_unattended=allow), unattended=True).action == 'block'
        assert evaluate([tx_card(permit=True)], Settings(allow_unattended=allow), unattended=True).action == ('pass' if allow else 'block')


def app(tmp_path, **settings):
    service = Service(settings=Settings(**settings), ledger=Ledger(tmp_path), presence=lambda: False)
    service._read_cards = lambda targets, *a, **kw: [Card(t, 'identifier') for t in targets]
    service.network.clients['chain'].lookup = lambda *a, **kw: Reading('Robinhood Chain', 'no_code', '')
    service.network.clients['toll'].check_tx = lambda *a, **kw: None
    return service


def test_local_decoding_precedes_toll_and_content_hook(tmp_path, monkeypatch):
    service = app(tmp_path)
    args = {'chainId': 4663, 'rpc': {'method': 'eth_signTypedData_v4', 'params': [A,
        json.loads((TYPED / 'permit-eoa.json').read_text())['payload']]}}
    order = []
    import rh_preflight.hooks as hooks
    original = hooks.local_card
    monkeypatch.setattr(hooks, 'local_card', lambda intent: (order.append('local'), original(intent))[1])
    service.network.clients['toll'].check_tx = lambda *a: order.append('toll')
    assert service.pre_tool_call('unknown_tool', args)['action'] == 'block'
    assert order == ['local', 'toll']
    assert service.ledger.rows()[0]['cards'][0]['kind'] == 'transaction'


@pytest.mark.parametrize('method,params', [('personal_sign', ['0x1234', A]),
    ('eth_sendTransaction', [{'to': A, 'data': '0x', 'from': B}])])
def test_wallet_request_projection(method, params):
    intent = tx_intent({'chainId': 4663, 'rpc': {'method': method, 'params': params}})[0]
    assert intent['payload'] == ({'chainId': 4663, **params[0]} if method == 'eth_sendTransaction'
                                 else {'rpc': {'method': method, 'params': params}})


@pytest.mark.parametrize('slow', ['builder', 'toll'])
@pytest.mark.parametrize('setting,expected', [('ask', 'approve'), ('stop', 'block'), ('note', None)])
def test_budget_contains_slow_stub_and_on_unchecked(tmp_path, slow, setting, expected):
    service = app(tmp_path, timeout=.04, tx_timeout=.04, on_unchecked=setting, ask_payout_other=False)
    def wait(*a, **kw):
        time.sleep(.15)
        return None
    if slow == 'builder':
        service._read_cards = wait
        args = {'chainId': 4663, 'token': A}
        tool = 'swap'
    else:
        service.network.clients['toll'].check_tx = wait
        args = {'chainId': 4663, 'to': A, 'data': '0x', 'from': B, 'value': '1'}
        tool = 'unknown'
    start = time.monotonic()
    directive = service.pre_tool_call(tool, args)
    assert time.monotonic() - start < .12
    assert (directive['action'] if directive else None) == expected


def test_internal_error_stays_fail_closed_even_note(tmp_path):
    service = app(tmp_path, mode='note')
    def fail(*a, **kw):
        raise RuntimeError('synthetic error')
    service._read_cards = fail
    assert service.pre_tool_call('swap', {'chainId': 4663, 'token': A})['action'] == 'block'


def test_switch_off_keeps_local_permit_policy(tmp_path):
    service = app(tmp_path, tx_check='off')
    service.network.clients['toll'].check_tx = lambda *a: pytest.fail('disabled')
    typed = json.loads((TYPED / 'permit2-single.json').read_text())['payload']
    assert service.pre_tool_call('unknown', {'payload': typed})['action'] == 'approve'


@pytest.mark.parametrize('failure', [Unavailable, TemporaryUnavailable, socket.timeout])
def test_transport_unavailable_projects_not_simulated(failure):
    client = Toll()
    # Normal transport wraps socket errors before the source adapter sees them.
    def fail(*a, **kw):
        raise TemporaryUnavailable() from failure()
    client._read = fail
    intent = tx_intent({'chainId': 4663, 'to': A, 'data': '0x', 'value': '1'})[0]
    card = project(local_card(intent), intent, client.check_tx(intent['payload'], time.monotonic() + 1))
    assert 'Toll: not simulated' in card.data['lines']
    assert evaluate([card], Settings()).action == 'ask'


def test_429_uses_process_cooldown_and_never_retries():
    client = Toll(clock=lambda: 10)
    calls = []
    def limited(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 429, 'limit', {'Retry-After': '60'}, BytesIO(b'{}'))
    client.opener.open = limited
    client._error_body = lambda *a: {}
    assert client.check_tx({}, 12) is None
    assert client.check_tx({'another': 'request'}, 12) is None
    assert len(calls) == 1 and client.cooldown == 70


def test_one_retry_within_same_deadline_only():
    client = Toll(clock=lambda: 10)
    calls = []
    def read(path, deadline, payload):
        calls.append(deadline)
        if len(calls) == 1:
            raise TemporaryUnavailable()
        return 200, {'x-toll': {}}
    client._read_once = read
    assert client.check_tx({}, 14) == {'x-toll': {}}
    assert calls == [14, 14]
    calls.clear()
    def fail(*args):
        calls.append(args)
        raise TemporaryUnavailable()
    client._read_once = fail
    assert client.check_tx({}, 14) is None and len(calls) == 2


def scoped_secret(monkeypatch, value):
    import sys, types
    names = []
    module = types.ModuleType('agent.secret_scope')
    module.get_secret = lambda name, default=None: names.append(name) or value
    monkeypatch.setitem(sys.modules, 'agent', types.ModuleType('agent'))
    monkeypatch.setitem(sys.modules, 'agent.secret_scope', module)
    return names


def test_key_comes_from_hermes_secret_scope_not_environment(monkeypatch):
    import sys
    monkeypatch.setenv('TOLL_API_KEY', 'synthetic-env-only')
    monkeypatch.setitem(sys.modules, 'agent.secret_scope', None)
    assert Toll().request_headers('/api/tx/check') == {}
    names = scoped_secret(monkeypatch, 'synthetic-scoped')
    assert Toll().request_headers('/api/tx/check') == {'x-api-key': 'synthetic-scoped'}
    assert names == ['TOLL_API_KEY']


def test_key_header_only_and_absent_from_outputs(tmp_path, monkeypatch, capsys):
    secret = 'synthetic-partner-secret-054'
    scoped_secret(monkeypatch, secret)
    client = Toll()
    assert client.request_headers('/api/tx/check') == {'x-api-key': secret}
    assert client.request_headers('/mcp') == {}
    client.base_url = 'http://localhost:1234'
    assert client.request_headers('/api/tx/check') == {}
    client.base_url = 'https://t0ll.xyz.example.org'
    assert client.request_headers('/api/tx/check') == {}
    service = app(tmp_path)
    args = json.loads((FIXTURES / 'clean-swap' / 'request.json').read_text())
    response = json.loads((FIXTURES / 'clean-swap' / 'response.json').read_text())
    response['warnings'][0]['message'] = secret
    service.network.clients['toll'].check_tx = lambda *a: response
    service.pre_tool_call('unknown', args)
    rendered = service.transform_tool_result('unknown', args, '{}')
    outputs = json.dumps(service.ledger.rows()) + rendered + json.dumps(client.stats()) + str(capsys.readouterr())
    assert secret not in outputs
    assert secret.encode() not in (tmp_path / 'checks.db').read_bytes()
    for path in FIXTURES.rglob('*'):
        if path.is_file():
            assert secret not in path.read_text()


@pytest.mark.parametrize('field,value', [('tx_check', 'bad'), ('ask_payout_other', 'on'), ('on_unchecked', 'pass')])
def test_new_setting_validation(field, value):
    with pytest.raises(ValueError):
        Settings(**{field: value})


def test_configured_key_header_is_sent_only_to_check_route(monkeypatch):
    scoped_secret(monkeypatch, 'synthetic-header-054')
    client = Toll()
    seen = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def opened(request, timeout):
        seen.append(dict((k.lower(), v) for k, v in request.header_items()))
        return Response()
    client.opener.open = opened
    client._body = lambda *args: b'{"x-toll":{"simulation":{"status":"success"}}}'
    assert client.check_tx({}, time.monotonic() + 1, 'x-partner-key') is not None
    assert seen[-1]['x-partner-key'] == 'synthetic-header-054'
    client._read('/mcp', time.monotonic() + 1, {})
    assert all('synthetic-header-054' not in value for value in seen[-1].values())


@pytest.mark.parametrize('header', ['Host', 'Content-Length', 'bad\r\nheader', '', 'x' * 65])
def test_key_header_validation(header):
    with pytest.raises(ValueError):
        Settings(toll_key_header=header)


def test_slow_simulation_preserves_local_code_less_stop(tmp_path):
    service = app(tmp_path, timeout=.04, tx_timeout=.04)
    args = json.loads((FIXTURES / 'unlimited-approval-eoa' / 'request.json').read_text())
    def slow(*args):
        time.sleep(.15)
    service.network.clients['toll'].check_tx = slow
    directive = service.pre_tool_call('unknown', args)
    assert directive['action'] == 'block'
    assert 'spender no code' in directive['message'] and 'Toll: not simulated' in directive['message']


def test_registry_contract_and_tx_above_builder_cards(tmp_path):
    service = app(tmp_path)
    service.network.clients['chain'].lookup = lambda *a, **kw: Reading('Robinhood Chain', 'code', '')
    service.network.clients['hookr'].lookup = lambda *a, **kw: Reading('Hookr', 'present', '')
    service.network.clients['priors'].lookup = lambda *a, **kw: Reading('Priors', 'missing', '')
    service.network.clients['toll'].check_tx = lambda *a: {'x-toll': {'simulation': {'status': 'success', 'block': 10}}}
    args = json.loads((FIXTURES / 'unlimited-approval-eoa' / 'request.json').read_text())
    assert service.pre_tool_call('approve', args) is None
    cards = service.ledger.rows()[0]['cards']
    assert cards[0]['kind'] == 'transaction' and cards[1]['kind'] == 'network'
    assert 'contract with code' in cards[0]['card']


def test_sender_deltas_omit_all_other_holders():
    card = fixture_card('clean-swap')
    deltas = [line for line in card.data['lines'] if line.startswith('simulated sender balance')]
    assert len(deltas) == 2
    assert '-0.000480569378599765 ETH' in deltas[0]
    assert '+45704730444476128839330' in deltas[1]


def test_no_retry_when_deadline_has_no_time_remaining():
    ticks = [10.0]
    client = Toll(clock=lambda: ticks[0])
    calls = []
    def fail(*args):
        calls.append(args)
        ticks[0] = 14.0
        raise TemporaryUnavailable()
    client._read_once = fail
    assert client.check_tx({}, 14) is None
    assert len(calls) == 1


def test_explicit_other_call_chain_does_not_use_signing_domain():
    typed = json.loads((TYPED / 'signin.json').read_text())['payload']
    assert not tx_intent({'chainId': 1, 'payload': typed}, 4663)
    assert tx_intent({'payload': typed})


def test_personal_sign_sender_is_not_a_twenty_byte_message():
    intent = tx_intent({'chainId': 4663, 'rpc': {'method': 'personal_sign', 'params': [B, A]}})[0]
    assert intent['sender'] == A


def test_double_quoted_command_json_decodes_locally():
    typed = json.loads((TYPED / 'permit-eoa.json').read_text())['payload']
    command = 'wallet sign --chain 4663 ' + json.dumps(json.dumps(typed))
    intents = tx_intent({'command': command})
    assert len(intents) == 1 and local_card(intents[0]).data['permit']


def test_permit2_uint160_max_is_unlimited_but_erc2612_uint160_is_bounded():
    typed = json.loads((TYPED / 'permit2-single.json').read_text())['payload']
    typed['message']['details']['amount'] = str(2 ** 160 - 1)
    assert local_card(tx_intent({'payload': typed})[0]).data['approvals'][0]['unlimited']
    erc = json.loads((TYPED / 'permit-eoa.json').read_text())['payload']
    erc['message']['value'] = str(2 ** 160 - 1)
    assert not local_card(tx_intent({'payload': erc})[0]).data['approvals'][0]['unlimited']


def test_simulation_discovered_unlimited_approval_checks_spender_code(tmp_path):
    service = app(tmp_path)
    service.network.clients['toll'].check_tx = lambda *a: {'x-toll': {'simulation': {
        'status': 'success', 'block': 10, 'approvals': [{'token': C, 'spender': B, 'amount': str(MAX)}]}}}
    directive = service.pre_tool_call('unknown', {'chainId': 4663, 'to': A, 'data': '0x12345678'})
    assert directive['action'] == 'block' and 'spender no code' in directive['message']
