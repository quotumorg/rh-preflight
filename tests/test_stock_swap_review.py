"""Adversarial regressions for the 0.5.6 Stock Token threshold review."""
import pytest

from rh_preflight.card import Card
from rh_preflight.policy import evaluate
from rh_preflight.settings import Settings
from rh_preflight.sources.toll import Client as Toll
from rh_preflight.stock_swap import RULE, annotate
from test_stock_swap_above_usd import A, B, AS_OF, stock


@pytest.mark.parametrize('rule', [{}, {RULE: None}, {RULE: 250}])
@pytest.mark.parametrize('missing', ['prices', 'chain', 'usd', 'as_of', 'reference', 'updated_at'])
def test_incomplete_toll_prices_preserve_existing_ask(rule, missing):
    data = stock()
    container = data if missing == 'prices' else data['prices'] if missing in {'chain', 'reference'} else (
        data['prices']['reference'] if missing == 'updated_at' else data['prices']['chain'])
    del container[missing]
    client = Toll()
    client._read = lambda *a, **kw: (200, data)
    reading = client.lookup(A)
    card = Card(A, 'network', {'base': Card(A, 'missing'), 'sources': [reading]})
    decision = evaluate([card], Settings(ask_when=rule))
    assert decision.action == 'ask'
    assert 'your rule: no HEY record' in decision.rules
    assert reading.state == 'unreachable'
    assert reading.line == client.unavailable(A).line


def value_card(response=None, hookr=None, arguments=None, address=A, price=None):
    from rh_preflight.transactions import tx_intents, local_card, project
    from test_stock_swap_above_usd import WALLET, ROUTER
    arguments = arguments or {'chainId': 4663, 'from': WALLET, 'to': ROUTER, 'data': '0x', 'token': address}
    intent = tx_intents(arguments)[0]
    card = project(local_card(intent), intent, response, hookr=hookr, stock_swap=True)
    network = Card(address, 'network', {'base': Card(address, 'missing'),
        'sources': [Toll().stock(price or stock(address), address)]})
    settings = Settings(ask_when={RULE: 250}, ask_no_record=False, on_unchecked='note')
    return annotate([card, network], {}, settings)[0]


def decoded(token=None, direction='sold', **extra):
    return {'coins': [{'address': A, 'decimals': 6}], 'x-toll': {
        'decoded': [{'kind': 'swap', direction: [token or {'address': A, 'raw': '3000000'}], **extra}],
        'simulation': {'status': 'success'}}}


@pytest.mark.parametrize('delta', ['-3000000', '3000000'])
@pytest.mark.parametrize('direction', ['sold', 'bought'])
def test_balance_change_never_supplies_swap_amount(delta, direction):
    from test_stock_swap_above_usd import WALLET
    response = decoded({'address': A}, direction)
    response['x-toll']['simulation']['deltas'] = [{'holder': WALLET, 'token': A, 'delta': delta}]
    card = value_card(response)
    assert not card.data.get(RULE)
    assert 'swap value not read: amount' in card.data['lines']


@pytest.mark.parametrize('amount', ['3000000', '3', '3.5', 3])
def test_decoded_amount_without_units_is_not_raw(amount):
    card = value_card(decoded({'address': A, 'amount': amount}))
    assert not card.data.get(RULE)
    assert 'swap value not read: amount' in card.data['lines']


@pytest.mark.parametrize('raw', [True, -3000000, '-3000000', '3.5', 3.0, 2 ** 256])
def test_invalid_raw_never_fires(raw):
    card = value_card(decoded({'address': A, 'raw': raw}))
    assert not card.data.get(RULE)
    assert 'swap value not read: amount' in card.data['lines']


@pytest.mark.parametrize('unit_key', ['amount', 'amountIn'])
@pytest.mark.parametrize('amount', ['3000000', '3', '3.5'])
def test_generic_swap_argument_units_are_not_guessed(tmp_path, unit_key, amount):
    from test_stock_swap_above_usd import app, run
    service, _ = app(tmp_path)
    arguments = {'chainId': 4663, 'tokenIn': {'address': A, 'decimals': 6}, 'tokenOut': B, unit_key: amount}
    directive, row, printed = run(service, arguments)
    assert directive is None and not row['rules']
    assert 'swap value not read: amount' in printed


def test_explicit_argument_raw_and_conflicting_raw(tmp_path):
    from test_stock_swap_above_usd import app, run, args
    service, _ = app(tmp_path)
    directive, _, printed = run(service, args(tokenIn={'address': A, 'decimals': 6, 'raw': '4000000'}))
    assert directive is None and 'swap value not read: amount' in printed
    service, _ = app(tmp_path / 'known')
    directive, _, printed = run(service, args(tokenIn={'address': A, 'decimals': 6, 'raw': '3000000'}))
    assert directive['action'] == 'approve' and 'swap value $300.00' in printed


@pytest.mark.parametrize('location', ['nested', 'coins', 'delta', 'hookr'])
def test_conflicting_decimals_are_not_resolved_by_order(location):
    response = decoded({'address': A, 'raw': '3000000000000000000', 'decimals': 18})
    hookr = None
    if location == 'nested':
        response['coins'] = []
        response['x-toll']['decoded'][0]['sold'][0]['token'] = {'address': A, 'decimals': 6}
    elif location == 'delta':
        response['coins'] = []
        response['x-toll']['simulation']['deltas'] = [{'token': A, 'decimals': 6}]
    elif location == 'hookr':
        response['coins'] = []
        hookr = {'status': 'ok', 'pays': {'address': A, 'decimals': 6}}
    card = value_card(response, hookr=hookr)
    assert not card.data.get(RULE)
    assert 'swap value not read: decimals' in card.data['lines']


@pytest.mark.parametrize('ambiguous', ['rows', 'several_sold', 'several_bought', 'unresolved'])
def test_ambiguous_swap_scope_is_not_a_value(ambiguous):
    response = decoded()
    row = response['x-toll']['decoded'][0]
    if ambiguous == 'rows':
        response['x-toll']['decoded'].append({'kind': 'swap', 'sold': [B], 'bought': [A]})
    elif ambiguous == 'unresolved':
        row['unresolved'] = ['path not read']
    else:
        row['sold' if ambiguous == 'several_sold' else 'bought'] = [A, B]
    card = value_card(response)
    assert not card.data.get(RULE)
    assert 'swap value not read: amount' in card.data['lines']


@pytest.mark.parametrize('as_of', [None, '', '   ', 123, '123', 'not recorded', 'nonsense', '2026-11-12'])
def test_price_requires_its_read_time(as_of):
    data = stock()
    data['prices']['chain']['as_of'] = as_of
    card = value_card(decoded(), price=data)
    assert not card.data.get(RULE)
    assert 'swap value not read: Toll chain price read time' in card.data['lines']


def test_stale_price_keeps_its_actual_as_of_without_claiming_freshness():
    data = stock()
    data['prices']['chain']['as_of'] = '2020-01-01T00:00:00Z'
    card = value_card(decoded(), price=data)
    assert card.data[RULE]
    assert 'swap value $300.00 (amount × Toll chain price read 2020-01-01T00:00:00Z)' in card.data['lines']


@pytest.mark.parametrize('value,above', [('249.9999999999999999999999', False), ('250', False),
    ('250.0000000000000000000001', True)])
def test_decimal_boundary_is_compared_before_money_formatting(value, above):
    response = decoded({'address': A, 'raw': 1, 'decimals': 0})
    response['coins'][0]['decimals'] = 0
    card = value_card(response, price=stock(price=value))
    assert bool(card.data.get(RULE)) == above
    assert 'swap value $250.00 (amount × Toll chain price read ' + AS_OF + ')' in card.data['lines']


@pytest.mark.parametrize('official', [A, B])
def test_wrapped_token_is_never_valued_at_underlying_price(official):
    response = decoded({'address': B, 'token': A, 'raw': '3000000', 'decimals': 6})
    card = value_card(response, address=official)
    if official == A:
        assert not card.data.get(RULE) and not any('swap value' in line for line in card.data['lines'])
    else:
        assert not card.data.get(RULE)
        assert 'swap value not read: amount, decimals' in card.data['lines']


def hookr_request(signed_amount=-3000000):
    import json
    from pathlib import Path
    request = json.loads((Path(__file__).parent / 'fixtures' / 'toll-tx/clean-swap/request.json').read_text())
    data = request['data']
    words = [data[i:i + 64] for i in range(10, len(data), 64)]
    address = '0x' + '1' * 40
    words[0] = address[2:].zfill(64)
    words[6] = format(signed_amount % (2 ** 256), '064x')
    return {**request, 'data': data[:10] + ''.join(words)}, address


def test_known_exact_input_calldata_is_base_units_and_quote_cannot_override_it():
    arguments, address = hookr_request()
    response = {'coins': [{'address': address, 'decimals': 6}], 'x-toll': {'decoded': []}}
    hookr = {'status': 'ok', 'pays': {'address': address, 'raw': '4000000', 'decimals': 6}}
    card = value_card(response, hookr=hookr, arguments=arguments, address=address)
    assert card.data[RULE]
    assert 'swap value $300.00 (amount × Toll chain price read ' + AS_OF + ')' in card.data['lines']


@pytest.mark.parametrize('direction', ['sold', 'bought'])
def test_calldata_disagreement_with_decoded_sides_is_not_a_value(direction):
    arguments, address = hookr_request()
    response = {'coins': [{'address': address, 'decimals': 6}], 'x-toll': {'decoded': [
        {'kind': 'swap', direction: [{'address': address, 'raw': '4000000'}],
         'bought' if direction == 'sold' else 'sold': [B]}]}}
    card = value_card(response, arguments=arguments, address=address)
    assert not card.data.get(RULE)
    assert 'swap value not read: amount' in card.data['lines']


def test_exact_output_calldata_does_not_turn_input_bound_into_amount():
    arguments, address = hookr_request(3000000)
    card = value_card({'coins': [{'address': address, 'decimals': 6}]}, arguments=arguments, address=address)
    assert not card.data.get(RULE)
    assert 'swap value not read: amount' in card.data['lines']


def test_other_chain_shape_does_not_use_robinhood_membership(tmp_path):
    from test_stock_swap_above_usd import args
    arguments = {'chainId': 4663, 'token': A, 'example': args(chainId=1)}
    card = Card(A, 'network', {'base': Card(A, 'missing'), 'sources': [Toll().stock(stock(), A)]})
    cards = annotate([card], arguments, Settings(ask_when={RULE: 250}))
    assert cards[0] == card


def test_rebased_stock_uses_chain_token_units_without_an_underlying_multiplier():
    import json
    from pathlib import Path
    data = json.loads((Path(__file__).parent / 'fixtures' / 'captures/r5/toll-markets-stock-nvda.json').read_text())
    address = data['address'].lower()
    assert data['registry']['multiplier'] != 1
    response = {'coins': [{'address': address, 'decimals': 6}], 'x-toll': {
        'decoded': [{'kind': 'swap', 'sold': [{'address': address, 'raw': '3000000'}]}]}}
    card = value_card(response, address=address, price=data)
    assert card.data[RULE]
    assert any(line.startswith('swap value $530.41 ') for line in card.data['lines'])
    response['coins'] = []
    card = value_card(response, address=address, price=data)
    assert not card.data.get(RULE)
    assert 'swap value not read: decimals' in card.data['lines']


def test_price_identity_is_the_verified_address_even_on_a_project_card():
    from rh_preflight.transactions import tx_intents, local_card, project
    from test_stock_swap_above_usd import args
    intent = tx_intents(args())[0]
    card = project(local_card(intent), intent, stock_swap=True)
    target = 'https://github.com/example/stock-project'
    network = Card(target, 'network', {'base': Card(target, 'missing'),
        'sources': [Toll().stock(stock(), A)]})
    settings = Settings(ask_when={RULE: 250})
    cards = annotate([card, network], args(), settings)
    assert cards[1].data[RULE]


def test_invalid_decoded_raw_is_not_silently_replaced_by_calldata():
    arguments, address = hookr_request()
    response = {'coins': [{'address': address, 'decimals': 6}], 'x-toll': {'decoded': [
        {'kind': 'swap', 'sold': [{'address': address, 'raw': '-4000000'}]}]}}
    card = value_card(response, arguments=arguments, address=address)
    assert not card.data.get(RULE)
    assert 'swap value not read: amount' in card.data['lines']
