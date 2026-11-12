"""Execution scope, metadata identity and offline film-path regressions."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from rh_preflight.stock_swap import RULE
from test_stock_swap_above_usd import A, B, WALLET, ROUTER, AS_OF, ASK, app, run
from test_stock_swap_review import decoded, value_card, hookr_request


def raw_swap(**extra):
    return {'chainId': 4663, 'tokenIn': {'address': A, 'decimals': 6},
            'tokenOut': B, 'amountInRaw': '3000000', **extra}


def exact_input_single(side='sold'):
    # SwapRouter02 exactInputSingle((address,address,uint24,address,uint256,uint256,uint160)).
    # This ABI is not decoded locally. Positive tests supply an explicit Toll raw.
    token_in, token_out = (A, B) if side == 'sold' else (B, A)
    words = [int(token_in, 16), int(token_out, 16), 3000, int(WALLET, 16), 3000000, 0, 0]
    return {'chainId': 4663, 'from': WALLET, 'to': ROUTER,
            'data': '0x04e45aaf' + ''.join(format(word, '064x') for word in words)}


@pytest.mark.parametrize('tool,arguments', [
    ('quote_swap', raw_swap()),
    ('estimate_swap', raw_swap()),
    ('research_swap', raw_swap()),
    ('swap', {'chainId': 4663, 'method': 'eth_call', 'params': [raw_swap(), 'latest']}),
    ('swap', {'chainId': 4663, 'method': 'wallet_simulateCalls', 'params': [raw_swap()]}),
])
def test_read_examples_never_fire_threshold(tmp_path, tool, arguments):
    service, _ = app(tmp_path)
    directive, row, printed = run(service, arguments, tool)
    assert directive is None and not row['rules']
    assert 'swap value' not in printed


def test_raw_only_swap_is_kept_beside_an_independent_transaction(tmp_path):
    service, _ = app(tmp_path)
    arguments = {'chainId': 4663, 'calls': [raw_swap(),
        {'from': WALLET, 'to': ROUTER, 'data': '0x', 'value': '0'}]}
    directive, row, printed = run(service, arguments)
    assert directive['action'] == 'approve' and ASK in printed.splitlines()
    assert any(RULE in rule for rule in row['rules'])


def test_raw_only_argument_checks_all_nested_decimals(tmp_path):
    service, _ = app(tmp_path)
    arguments = raw_swap(tokenIn={'address': A, 'decimals': 6,
        'token': {'address': A, 'decimals': 18}})
    directive, _, printed = run(service, arguments)
    assert directive is None
    assert 'swap value not read: decimals' in printed.splitlines()


@pytest.mark.parametrize('location', ['coins', 'warning', 'hookr'])
def test_conflicting_metadata_identity_cannot_supply_decimals(location):
    response = decoded({'address': A, 'raw': '3000000'})
    response['coins'] = []
    bad = {'address': A, 'token': B, 'decimals': 6}
    quote = None
    if location == 'coins':
        response['coins'] = [bad]
    elif location == 'warning':
        response['warnings'] = [{'code': 'coin-careful', 'x-toll-evidence': bad}]
    else:
        quote = {'status': 'ok', 'pays': bad}
    card = value_card(response, hookr=quote)
    assert not card.data.get(RULE)
    assert 'swap value not read: decimals' in card.data['lines']


@pytest.mark.parametrize('shape', ['amountInRaw', 'amountOutRaw', 'sold.raw', 'bought.raw'])
def test_explicit_agent_argument_shapes(tmp_path, shape):
    service, reads = app(tmp_path)
    arguments = {'chainId': 4663, 'tokenIn': B, 'tokenOut': B}
    side = 'In' if shape in {'amountInRaw', 'sold.raw'} else 'Out'
    arguments['token' + side] = {'address': A, 'decimals': 6}
    if shape.endswith('.raw'):
        arguments['token' + side]['raw'] = '3000000'
    else:
        arguments[shape] = '3000000'
    directive, row, printed = run(service, arguments, 'agent_swap')
    assert directive['action'] == 'approve' and ASK in printed.splitlines()
    assert row['rules'] == [RULE + ': your rule: Stock Token swap above $250.00']
    assert sorted(reads) == [A, B]


@pytest.mark.parametrize('envelope', ['unsigned', 'wallet', 'cast'])
@pytest.mark.parametrize('side', ['sold', 'bought'])
def test_transaction_with_explicit_decoded_swap_amount(tmp_path, envelope, side):
    service, reads = app(tmp_path)
    response = decoded({'address': A, 'raw': '3000000'}, side)
    payload = exact_input_single(side)
    service.network.clients['toll'].check_tx = lambda *a: deepcopy(response)
    if envelope == 'wallet':
        arguments = {'chainId': 4663, 'token': A,
                     'method': 'eth_sendTransaction', 'params': [payload]}
        tool = 'wallet_swap_rpc'
    elif envelope == 'cast':
        arguments = {'command': f"cast send {ROUTER} --from {WALLET} --data {payload['data']} --chain 4663",
                     'token': A}
        tool = 'terminal'
    else:
        arguments = {**payload, 'token': A}
        tool = 'wallet_swap'
    directive, _, printed = run(service, arguments, tool)
    assert directive['action'] == 'approve' and ASK in printed.splitlines()
    assert 'swap value $300.00 (amount × Toll chain price read ' + AS_OF + ')' in printed.splitlines()
    assert sorted(reads) == sorted([A, WALLET, ROUTER])


@pytest.mark.parametrize('envelope', ['unsigned', 'wallet', 'cast'])
def test_known_hookr_exact_input_sell_calldata(tmp_path, envelope):
    request, address = hookr_request()
    from test_stock_swap_above_usd import stock
    service, reads = app(tmp_path, stocks={address: stock(address)})
    service.network.clients['toll'].check_tx = lambda *a: {
        'coins': [{'address': address, 'decimals': 6}],
        'x-toll': {'decoded': [], 'simulation': {'status': 'success'}}}
    if envelope == 'wallet':
        arguments = {'chainId': 4663, 'token': address,
                     'method': 'eth_sendTransaction', 'params': [request]}
        tool = 'wallet_swap_rpc'
    elif envelope == 'cast':
        arguments = {'command': f"cast send {request['to']} --from {request['from']} --data {request['data']} --chain 4663",
                     'token': address}
        tool = 'terminal'
    else:
        arguments = {**request, 'token': address}
        tool = 'wallet_swap'
    directive, _, printed = run(service, arguments, tool)
    assert directive['action'] == 'approve' and ASK in printed.splitlines()
    assert 'swap value $300.00 (amount × Toll chain price read ' + AS_OF + ')' in printed.splitlines()
    assert len(reads) == 3 and address in reads


def test_exact_input_single_without_explicit_decoded_raw_stays_unread(tmp_path):
    service, _ = app(tmp_path)
    response = decoded({'address': A})
    service.network.clients['toll'].check_tx = lambda *a: response
    directive, row, printed = run(service, {**exact_input_single(), 'token': A}, 'wallet_swap')
    assert directive is None and not row['rules']
    assert 'swap value not read: amount' in printed.splitlines()


@pytest.mark.parametrize('tool,include_token', [('wallet_swap', False), ('wallet_rpc', True)])
def test_calldata_does_not_add_membership_reads(tmp_path, tool, include_token):
    service, reads = app(tmp_path)
    service.network.clients['toll'].check_tx = lambda *a: decoded()
    arguments = {'chainId': 4663, 'method': 'eth_sendTransaction',
                 'params': [exact_input_single()]}
    if include_token:
        arguments['token'] = A
    directive = service.pre_tool_call(tool, arguments)
    assert directive is None and A not in reads
    row = service.ledger.rows(1)[0]
    assert not row['rules']
    assert all('swap value' not in card['card'] for card in row['cards'])


def test_nvda_buy_film_fixture_end_to_end(tmp_path):
    root = Path(__file__).parent / 'fixtures'
    fixture = json.loads((root / 'stock-swap' / 'nvda-buy-film.json').read_text())
    saved_price = json.loads((root / 'captures' / 'r5' / 'toll-markets-stock-nvda.json').read_text())
    address = saved_price['address'].lower()
    pools = json.loads((root / 'captures' / 'hookr-discover-pools.json').read_text())['pools']
    metadata = next(pool[side] for pool in pools for side in ('currency0', 'currency1')
                    if pool[side]['address'].lower() == address)
    assert fixture['arguments']['tokenOut']['decimals'] == metadata['decimals'] == 18
    service, reads = app(tmp_path, stocks={address: saved_price}, **fixture['settings'])
    directive, row, printed = run(service, fixture['arguments'], fixture['tool'])
    assert directive['action'] == 'approve' and row['disposition'] == 'ask'
    assert fixture['expected_ask'] in printed.splitlines()
    assert fixture['expected_value'] in printed.splitlines()
    assert row['rules'] == [RULE + ': your rule: Stock Token swap above $250.00']
    assert len(reads) == 2 and address in reads


def test_base_units_alias_is_not_a_supported_amount(tmp_path):
    service, _ = app(tmp_path)
    directive, _, printed = run(service, raw_swap(amountInRaw=None, amountInBaseUnits='3000000'))
    assert directive is None
    assert 'swap value not read: amount' in printed.splitlines()
