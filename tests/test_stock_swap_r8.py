"""Informational cast invocations cannot establish an executable swap."""
from copy import deepcopy

import pytest

from test_stock_swap_above_usd import A, WALLET, ROUTER, ASK, app, run, stock
from test_stock_swap_review import decoded, hookr_request


@pytest.mark.parametrize('direct_call', [False, True])
@pytest.mark.parametrize('flag', ['--help', '-h', '"--help"', '--version', '-V'])
@pytest.mark.parametrize('amount_source', ['decoded', 'calldata'])
def test_informational_cast_cannot_supply_swap_value(tmp_path, direct_call, flag, amount_source):
    address, target, data, sender = A, ROUTER, '0x', WALLET
    response = decoded()
    if amount_source == 'calldata':
        request, address = hookr_request()
        target, data, sender = request['to'], request['data'], request['from']
        response = {'coins': [{'address': address, 'decimals': 6}],
                    'x-toll': {'simulation': {'status': 'success'}}}
    service, _ = app(tmp_path, stocks={address: stock(address)})
    service.network.clients['toll'].check_tx = lambda *a: deepcopy(response)
    action = {'command': f'cast send {target} --from {sender} --data {data} --chain 4663 {flag}'}
    arguments = {'chainId': 4663, 'token': address}
    arguments.update({'calls': [action]} if direct_call else action)
    directive, row, printed = run(service, arguments, 'terminal')
    assert directive is None and not row['rules']
    assert 'swap value not read: amount' in printed
    assert 'swap value $' not in printed


def test_informational_cast_retains_the_normal_unchecked_ask(tmp_path):
    service, _ = app(tmp_path, on_unchecked='ask')
    service.network.clients['toll'].check_tx = lambda *a: None
    arguments = {'chainId': 4663, 'token': A,
                 'command': f'cast send {ROUTER} --from {WALLET} --data 0x --chain 4663 --help'}
    directive, row, printed = run(service, arguments, 'terminal')
    assert directive['action'] == 'approve' and row['disposition'] == 'ask'
    assert row['rules'] == ['your rule: transaction could not be checked']
    assert ASK not in printed.splitlines()


def test_help_text_as_an_option_value_keeps_a_literal_send(tmp_path):
    service, _ = app(tmp_path)
    service.network.clients['toll'].check_tx = lambda *a: deepcopy(decoded())
    arguments = {'chainId': 4663, 'token': A,
                 'command': f'cast send {ROUTER} --from {WALLET} --data 0x --chain 4663 --password "--help"'}
    directive, row, printed = run(service, arguments, 'terminal')
    assert directive['action'] == 'approve' and row['disposition'] == 'ask'
    assert ASK in printed.splitlines()
