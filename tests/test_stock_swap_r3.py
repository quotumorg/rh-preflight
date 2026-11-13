"""Third hostile review: raw argument scope and complete metadata checks."""
from copy import deepcopy
import json

import pytest

from rh_preflight.stock_swap import RULE
from test_stock_swap_above_usd import A, B, WALLET, ROUTER, ASK, app, run
from test_stock_swap_r2 import raw_swap
from test_stock_swap_review import decoded, value_card


@pytest.mark.parametrize('generic', [False, True])
@pytest.mark.parametrize('envelope', ['standalone', 'wallet', 'flat', 'embedded_json', 'wallet_json'])
def test_raw_swap_fields_in_typed_data_are_not_a_swap(tmp_path, envelope, generic):
    action = raw_swap(**({'amountIn': '3'} if generic else {}))
    typed = {'domain': {'chainId': 4663}, 'types': {'Message': []},
             'primaryType': 'Message', 'message': action}
    if envelope in {'wallet', 'wallet_json'}:
        arguments = {'chainId': 4663, 'method': 'eth_signTypedData_v4',
                     'params': [WALLET, json.dumps(typed) if envelope == 'wallet_json' else typed]}
    elif envelope == 'flat':
        arguments = {**typed, **action}
    elif envelope == 'embedded_json':
        typed['message'] = {'example': json.dumps(action)}
        arguments = {'chainId': 4663, 'typed': typed}
    else:
        arguments = {'chainId': 4663, 'typed': typed}
    service, _ = app(tmp_path)
    _, row, printed = run(service, arguments, 'wallet_swap_sign')
    assert all(RULE not in rule for rule in row['rules'])
    assert 'swap value' not in printed


@pytest.mark.parametrize('generic', [False, True])
def test_independent_swap_beside_typed_data_is_still_valued(tmp_path, generic):
    typed = {'domain': {'chainId': 4663}, 'types': {'Message': []},
             'primaryType': 'Message', 'message': raw_swap(amountInRaw='9000000')}
    service, _ = app(tmp_path)
    action = raw_swap(**({'amountIn': '3'} if generic else {}))
    _, row, printed = run(service, {'chainId': 4663, 'calls': [typed, action]})
    assert any(RULE in rule for rule in row['rules'])
    values = [line for line in printed.splitlines() if line.startswith('swap value')]
    assert len(values) == 1 and values[0].startswith('swap value $300.00 ')


@pytest.mark.parametrize('wallet', [False, True])
@pytest.mark.parametrize('independent', [False, True])
def test_transaction_inside_signature_is_not_a_swap_but_independent_send_is(tmp_path, wallet, independent):
    payload = {'chainId': 4663, 'from': WALLET, 'to': ROUTER, 'data': '0x', 'token': A}
    example = {'method': 'eth_sendTransaction', 'params': [payload]} if wallet else payload
    typed = {'domain': {'chainId': 4663}, 'types': {'Message': []},
             'primaryType': 'Message', 'message': {'example': json.dumps(example)}}
    arguments = {'chainId': 4663, 'calls': [typed, example] if independent else [typed]}
    service, _ = app(tmp_path)
    service.network.clients['toll'].check_tx = lambda *a: decoded()
    _, row, printed = run(service, arguments)
    assert any(RULE in rule for rule in row['rules']) == independent
    assert ('swap value $300.00 ' in printed) == independent
    assert 'swap value not read:' not in printed


@pytest.mark.parametrize('independent', [False, True])
def test_literal_cast_inside_signature_does_not_supply_a_swap(tmp_path, independent):
    command = f'cast send {ROUTER} --from {WALLET} --data 0x --chain 4663'
    typed = {'domain': {'chainId': 4663}, 'types': {'Message': []},
             'primaryType': 'Message', 'message': {'command': command}}
    arguments = {'chainId': 4663, 'token': A, 'typed': typed}
    if independent:
        arguments['command'] = command
    service, _ = app(tmp_path)
    service.network.clients['toll'].check_tx = lambda *a: decoded()
    _, row, printed = run(service, arguments)
    assert any(RULE in rule for rule in row['rules']) == independent
    assert ('swap value $300.00 ' in printed) == independent


@pytest.mark.parametrize('first,later', [('amount', 'amountIn'), ('amountIn', 'amountOut'),
                                         ('amountOut', 'allowance')])
@pytest.mark.parametrize('mixed', [False, True])
def test_null_first_generic_amount_does_not_drop_raw_swap(tmp_path, first, later, mixed):
    action = raw_swap(**{first: None, later: '3'})
    arguments = {'chainId': 4663, 'calls': [action,
        {'from': WALLET, 'to': ROUTER, 'data': '0x', 'value': '0'}]} if mixed else action
    service, _ = app(tmp_path)
    directive, row, printed = run(service, arguments)
    assert directive and directive['action'] == 'approve'
    assert ASK in printed.splitlines() and any(RULE in rule for rule in row['rules'])
    assert sum(line.startswith('swap value $300.00 ') for line in printed.splitlines()) == 1


@pytest.mark.parametrize('generic', [False, True])
@pytest.mark.parametrize('conflict', ['identity', 'decimals'])
def test_deep_argument_metadata_conflicts_are_unread(tmp_path, generic, conflict):
    inner = {'address': B} if conflict == 'identity' else {'address': A, 'decimals': 18}
    token = {'address': A, 'decimals': 6, 'token': {'address': A, 'token': inner}}
    arguments = raw_swap(tokenIn=token, **({'amountIn': '3'} if generic else {}))
    service, _ = app(tmp_path)
    directive, row, printed = run(service, arguments)
    assert directive is None and not row['rules']
    assert 'swap value not read:' in printed and 'decimals' in printed


@pytest.mark.parametrize('location', ['coins', 'warning', 'hookr', 'delta', 'side'])
def test_deep_transaction_metadata_identity_is_unread(location):
    response = decoded({'address': A, 'raw': '3000000'})
    response['coins'] = []
    bad = {'address': A, 'decimals': 6, 'token': {'address': A, 'token': B}}
    quote = None
    if location == 'coins':
        response['coins'] = [bad]
    elif location == 'warning':
        response['warnings'] = [{'code': 'coin-careful', 'x-toll-evidence': bad}]
    elif location == 'hookr':
        quote = {'status': 'ok', 'pays': bad}
    elif location == 'delta':
        response['x-toll']['simulation']['deltas'] = [bad]
    else:
        response['x-toll']['decoded'][0]['sold'][0].update(bad)
    card = value_card(response, hookr=quote)
    assert not card.data.get(RULE)
    assert any(line.startswith('swap value not read:') and 'decimals' in line
               for line in card.data['lines'])


@pytest.mark.parametrize('kind', ['swap', 'transfer'])
@pytest.mark.parametrize('conflict', ['identity', 'decimals'])
def test_all_decoded_row_metadata_is_checked(kind, conflict):
    response = decoded({'address': A, 'raw': '3000000'})
    bad = {'kind': kind, 'address': A, 'decimals': 18}
    if conflict == 'identity':
        bad.update(address=B, token=A, decimals=6)
    if kind == 'swap':
        response['x-toll']['decoded'][0].update(bad)
    else:
        response['x-toll']['decoded'].append(bad)
    card = value_card(response)
    assert not card.data.get(RULE)
    assert 'swap value not read: decimals' in card.data['lines']


def test_consistent_deep_metadata_remains_usable(tmp_path):
    token = {'address': A, 'token': {'address': A, 'token': {'address': A, 'decimals': 6}}}
    service, _ = app(tmp_path)
    directive, _, printed = run(service, raw_swap(tokenIn=deepcopy(token)))
    assert directive and directive['action'] == 'approve' and ASK in printed.splitlines()
    card = value_card(decoded({**token, 'raw': '3000000'}))
    assert card.data[RULE]
