"""Execution hints belong to the supported wallet RPC envelope as well."""
from copy import deepcopy

import pytest

from test_stock_swap_above_usd import A, B, WALLET, ROUTER, ASK, app, run
from test_stock_swap_r2 import exact_input_single
from test_stock_swap_review import decoded


@pytest.mark.parametrize('direct_call', [False, True])
@pytest.mark.parametrize('hint', [
    {'quoteOnly': True}, {'readOnly': True}, {'dryRun': True}, {'simulateOnly': True},
    {'execute': False}, {'send': False}, {'sign': False}, {'submit': False}, {'broadcast': False},
    {'mode': 'quote'}, {'action': 'simulate'}, {'operation': 'read'},
])
def test_nested_rpc_execution_hints_cannot_supply_swap_value(tmp_path, direct_call, hint):
    service, _ = app(tmp_path)
    service.network.clients['toll'].check_tx = lambda *a: deepcopy(decoded())
    action = {'rpc': {'method': 'eth_sendTransaction', 'params': [exact_input_single()], **hint}}
    arguments = {'chainId': 4663, 'token': A}
    arguments.update({'calls': [action]} if direct_call else action)
    directive, row, printed = run(service, arguments)
    assert directive is None and not row['rules']
    assert 'swap value not read: amount' in printed
    assert 'swap value $' not in printed


@pytest.mark.parametrize('hint', [{'quoteOnly': False}, {'execute': True}, {'mode': 'execute'}])
def test_nested_rpc_execution_hints_keep_supported_sends(tmp_path, hint):
    service, _ = app(tmp_path)
    service.network.clients['toll'].check_tx = lambda *a: deepcopy(decoded())
    arguments = {'chainId': 4663, 'token': A,
                 'rpc': {'method': 'eth_sendTransaction', 'params': [exact_input_single()], **hint}}
    directive, row, printed = run(service, arguments)
    assert directive['action'] == 'approve' and row['disposition'] == 'ask'
    assert ASK in printed.splitlines()


@pytest.mark.parametrize('separator', [' ; ', ' && ', ' | ', '\n'])
@pytest.mark.parametrize('direct_call', [False, True])
def test_compound_shell_examples_cannot_supply_swap_value(tmp_path, separator, direct_call):
    service, _ = app(tmp_path)
    def check_tx(payload, *args):
        if payload.get('to') == ROUTER:
            return deepcopy(decoded())
        return {'x-toll': {'simulation': {'status': 'success'}}}
    service.network.clients['toll'].check_tx = check_tx
    command = (f'cast send {B} --from {WALLET} --value 0 --chain 4663' + separator
               + f'echo cast send {ROUTER} --from {WALLET} --data 0x --chain 4663')
    action = {'command': command}
    arguments = {'chainId': 4663, 'token': A}
    arguments.update({'calls': [action]} if direct_call else action)
    directive, row, printed = run(service, arguments, 'terminal')
    assert directive is None and not row['rules']
    assert 'swap value not read: amount' in printed
    assert 'swap value $' not in printed


@pytest.mark.parametrize('wrapper', ['$(echo {example})', '`echo {example}`', 'echo {example}'])
@pytest.mark.parametrize('direct_call', [False, True])
def test_shell_substitution_examples_cannot_supply_swap_value(tmp_path, wrapper, direct_call):
    service, _ = app(tmp_path)
    def check_tx(payload, *args):
        if payload.get('to') == ROUTER:
            return deepcopy(decoded())
        return {'x-toll': {'simulation': {'status': 'success'}}}
    service.network.clients['toll'].check_tx = check_tx
    example = f'cast send {ROUTER} --from {WALLET} --data 0x --chain 4663 --unused foo'
    command = (f'cast send {B} --from {WALLET} --value 0 --chain 4663 --password '
               + wrapper.format(example=example))
    action = {'command': command}
    arguments = {'chainId': 4663, 'token': A}
    arguments.update({'calls': [action]} if direct_call else action)
    directive, row, printed = run(service, arguments, 'terminal')
    assert directive is None and not row['rules']
    assert 'swap value not read: amount' in printed
    assert 'swap value $' not in printed
