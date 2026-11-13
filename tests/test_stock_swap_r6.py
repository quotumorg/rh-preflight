"""Finite execution shapes for the converged Stock Token threshold."""
from copy import deepcopy
import json

import pytest

from rh_preflight.stock_swap import RULE
from test_stock_swap_above_usd import A, WALLET, ROUTER, ASK, app, run
from test_stock_swap_r2 import exact_input_single, raw_swap
from test_stock_swap_review import decoded


@pytest.mark.parametrize('location', ['example', 'quote', 'serialized', 'nested_calls'])
@pytest.mark.parametrize('wallet', [False, True])
def test_nested_transactions_cannot_supply_swap_value(tmp_path, location, wallet):
    payload = exact_input_single()
    action = {'method': 'eth_sendTransaction', 'params': [payload]} if wallet else payload
    arguments = {'chainId': 4663, 'token': A}
    if location == 'serialized':
        arguments['text'] = json.dumps(action)
    elif location == 'nested_calls':
        arguments['calls'] = [{'calls': [action]}]
    else:
        arguments[location] = action
    service, _ = app(tmp_path)
    service.network.clients['toll'].check_tx = lambda *a: deepcopy(decoded())
    directive, row, printed = run(service, arguments)
    assert directive is None and not row['rules']
    assert 'swap value not read: amount' in printed
    assert 'swap value $' not in printed


@pytest.mark.parametrize('flag', ['quoteOnly', 'readOnly', 'dryRun', 'simulateOnly'])
@pytest.mark.parametrize('shape', ['unsigned', 'wallet', 'cast'])
def test_simulation_flags_cannot_supply_decoded_swap_value(tmp_path, flag, shape):
    payload = exact_input_single()
    if shape == 'wallet':
        arguments = {'chainId': 4663, 'token': A,
                     'method': 'eth_sendTransaction', 'params': [payload]}
    elif shape == 'cast':
        arguments = {'chainId': 4663, 'token': A,
                     'command': f"cast send {ROUTER} --from {WALLET} --data {payload['data']} --chain 4663"}
    else:
        arguments = {**payload, 'token': A}
    arguments[flag] = True
    service, _ = app(tmp_path)
    service.network.clients['toll'].check_tx = lambda *a: deepcopy(decoded())
    directive, row, printed = run(service, arguments)
    assert directive is None and not row['rules']
    assert 'swap value not read: amount' in printed
    assert 'swap value $' not in printed


@pytest.mark.parametrize('method', ['eth_call', 'wallet_simulateCalls', 'personal_sign', 'eth_signTypedData_v4'])
def test_rpc_envelopes_do_not_execute_a_calls_example(tmp_path, method):
    service, _ = app(tmp_path)
    arguments = {'chainId': 4663, 'method': method, 'calls': [raw_swap()]}
    _, row, printed = run(service, arguments)
    assert all(RULE not in rule for rule in row['rules'])
    assert 'swap value $' not in printed


@pytest.mark.parametrize('envelope', ['unsigned', 'wallet', 'rpc', 'cast'])
def test_direct_calls_transaction_shapes_remain_supported(tmp_path, envelope):
    payload = exact_input_single()
    if envelope in {'wallet', 'rpc'}:
        action = {'method': 'eth_sendTransaction', 'params': [payload]}
        if envelope == 'rpc':
            action = {'rpc': action}
    elif envelope == 'cast':
        action = {'command': f"cast send {ROUTER} --from {WALLET} --data {payload['data']} --chain 4663"}
    else:
        action = payload
    service, _ = app(tmp_path)
    service.network.clients['toll'].check_tx = lambda *a: deepcopy(decoded())
    directive, row, printed = run(service, {'chainId': 4663, 'token': A, 'calls': [action]})
    assert directive['action'] == 'approve' and ASK in printed.splitlines()
    assert row['rules'] == [RULE + ': your rule: Stock Token swap above $250.00']


def test_unsupported_shape_retains_the_normal_ask_path(tmp_path):
    service, _ = app(tmp_path, on_unchecked='ask')
    directive, row, printed = run(service, {'chainId': 4663, 'example': raw_swap(amountIn='3')})
    assert directive['action'] == 'approve' and row['disposition'] == 'ask'
    assert row['rules'] == ['your rule: transaction could not be checked']
    assert 'swap value not read: amount' in printed


def test_supported_action_ignores_serialized_examples(tmp_path):
    service, _ = app(tmp_path)
    directive, _, printed = run(service, raw_swap(text=json.dumps(raw_swap(amountInRaw='9000000'))))
    assert directive['action'] == 'approve' and ASK in printed.splitlines()
    assert sum(line.startswith('swap value') for line in printed.splitlines()) == 1
    assert 'swap value $300.00 ' in printed
