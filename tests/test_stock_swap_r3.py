"""Third hostile review: raw argument scope and complete metadata checks."""
import pytest

from rh_preflight.stock_swap import RULE
from test_stock_swap_above_usd import A, B, WALLET, ROUTER, ASK, app, run
from test_stock_swap_r2 import raw_swap


@pytest.mark.parametrize('envelope', ['standalone', 'wallet', 'flat'])
def test_raw_swap_fields_in_typed_data_are_not_a_swap(tmp_path, envelope):
    typed = {'domain': {'chainId': 4663}, 'types': {'Message': []},
             'primaryType': 'Message', 'message': raw_swap()}
    if envelope == 'wallet':
        arguments = {'chainId': 4663, 'method': 'eth_signTypedData_v4',
                     'params': [WALLET, typed]}
    elif envelope == 'flat':
        arguments = {**typed, **raw_swap()}
    else:
        arguments = {'chainId': 4663, 'typed': typed}
    service, _ = app(tmp_path)
    _, row, printed = run(service, arguments, 'wallet_swap_sign')
    assert all(RULE not in rule for rule in row['rules'])
    assert 'swap value' not in printed


def test_independent_swap_beside_typed_data_is_still_valued(tmp_path):
    typed = {'domain': {'chainId': 4663}, 'types': {'Message': []},
             'primaryType': 'Message', 'message': raw_swap(amountInRaw='9000000')}
    service, _ = app(tmp_path)
    _, row, printed = run(service, {'chainId': 4663, 'calls': [typed, raw_swap()]})
    assert any(RULE in rule for rule in row['rules'])
    values = [line for line in printed.splitlines() if line.startswith('swap value')]
    assert len(values) == 1 and values[0].startswith('swap value $300.00 ')


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


