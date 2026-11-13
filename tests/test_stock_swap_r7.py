"""Execution hints belong to the supported wallet RPC envelope as well."""
from copy import deepcopy

import pytest

from test_stock_swap_above_usd import A, ASK, app, run
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
