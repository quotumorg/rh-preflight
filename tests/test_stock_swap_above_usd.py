"""Offline Stock Token swap thresholds, existing reads and gate behavior."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from rh_preflight.card import Card
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.network import MODULES
from rh_preflight.policy import evaluate
from rh_preflight.settings import Settings
from rh_preflight.sources.common import Reading
from rh_preflight.sources.toll import Client as Toll
from rh_preflight.stock_swap import RULE, PREFIX, annotate
from rh_preflight.transactions import local_card, project, tx_intents

A, B, WALLET, ROUTER = ('0x' + c * 40 for c in 'abcd')
AS_OF = '2026-11-12T12:00:00Z'
ASK = 'ask · your rule: Stock Token swap above $250.00'
ROOT = Path(__file__).parent / 'fixtures'


def stock(address=A, price=100):
    return {'address': address, 'ticker': 'NVDA', 'isin': 'US67066G1040',
            'status': 'ASSET_STATUS_ACTIVE', 'registry': {
                'source': 'https://api.robinhood.com/rhj/assets', 'as_of': AS_OF},
            'prices': {'chain': {'usd': price, 'as_of': AS_OF},
                       'reference': {'usd': 999999, 'updated_at': AS_OF}}}


def args(amount='3000000', **extra):
    return {'chainId': 4663, 'tokenIn': {'address': A, 'decimals': 6},
            'tokenOut': B, 'amountIn': amount, 'amountInRaw': amount, **extra}


def app(tmp_path, stocks=None, **options):
    settings = {'ask_when': {RULE: 250}, 'ask_no_record': False,
                'on_unchecked': 'note', 'sources': {s: 'off' for s in MODULES if s != 'toll'}}
    settings.update(options)
    service = Service(settings=Settings(**settings), ledger=Ledger(tmp_path), presence=lambda: False)
    service.client.lookup = lambda target, **kw: Card(target, 'missing')
    stocks = {A: stock()} if stocks is None else stocks
    reads = []
    def lookup(target, **kw):
        reads.append(target)
        return Toll().stock(stocks[target], target) if target in stocks else Reading('Toll', 'unlisted', '')
    service.network.clients['toll'].lookup = lookup
    service.network.clients['toll'].check_tx = lambda *a: {'warnings': [], 'x-toll': {'simulation': {'status': 'success'}}}
    return service, reads


def run(service, arguments, tool='swap'):
    directive = service.pre_tool_call(tool, arguments)
    row = service.ledger.rows(1)[0]
    return directive, row, '\n\n'.join(card['card'] for card in row['cards'])


@pytest.mark.parametrize('value', [1, 250.5, None])
def test_valid_settings(value):
    assert Settings(ask_when={RULE: value}).ask_when[RULE] == value


@pytest.mark.parametrize('value', [True, False, 0, -1, float('nan'), float('inf'), -float('inf'), '250', [], {}])
def test_invalid_settings(value):
    with pytest.raises(ValueError, match=r'ask_when.stock_swap_above_usd must be a positive finite number or null'):
        Settings(ask_when={RULE: value})


@pytest.mark.parametrize('rule', ['stock_no_hey_record', 'payee_no_priors_record', 'vault_strategy_inactive', 'unknown'])
def test_other_validation_unchanged(rule):
    with pytest.raises(ValueError, match='ask_when must map rule names to booleans'):
        Settings(ask_when={rule: 250})


@pytest.mark.parametrize('amount,disposition', [('2500001', 'ask'), ('2500000', 'pass'), ('2499999', 'pass')])
def test_strict_threshold_and_exact_card_line(tmp_path, amount, disposition):
    service, reads = app(tmp_path)
    directive, row, printed = run(service, args(amount))
    assert row['disposition'] == disposition
    assert (directive is not None) == (disposition == 'ask')
    if directive:
        assert ASK in printed.splitlines()
        assert row['rules'] == [RULE + ': ' + PREFIX + '250.00']
    else:
        assert not row['rules']
    assert sorted(reads) == [A, B]  # Only existing per-address membership reads.
    assert 'amount × Toll chain price read ' + AS_OF in printed


@pytest.mark.parametrize('ask_when', [{}, {RULE: None}])
def test_default_off(tmp_path, ask_when):
    service, _ = app(tmp_path, ask_when=ask_when)
    directive, row, printed = run(service, args())
    assert directive is None and row['disposition'] == 'pass'
    assert 'swap value' not in printed


@pytest.mark.parametrize('missing,detail', [('price', 'Toll chain price'), ('amount', 'amount'), ('decimals', 'decimals')])
@pytest.mark.parametrize('other_rule', [False, True])
def test_missing_data_is_silent_for_policy(tmp_path, missing, detail, other_rule):
    data, arguments = stock(), args()
    if missing == 'price':
        del data['prices']['chain']['usd']
    elif missing == 'amount':
        del arguments['amountIn']
        del arguments['amountInRaw']
    else:
        del arguments['tokenIn']['decimals']
    service, _ = app(tmp_path, stocks={A: data}, ask_no_record=other_rule,
                     ask_when={RULE: 250, 'stock_no_hey_record': other_rule})
    directive, row, printed = run(service, arguments)
    assert (directive is not None) == other_rule
    assert 'swap value not read: ' + detail in printed.splitlines()
    assert all(RULE not in rule for rule in row['rules'])


@pytest.mark.parametrize('price', [None, True, 0, -1, 'nan', 'inf'])
def test_unusable_price_never_uses_reference(tmp_path, price):
    service, _ = app(tmp_path, stocks={A: stock(price=price)})
    directive, _, printed = run(service, args())
    assert directive is None
    assert 'swap value not read: Toll chain price' in printed


def test_non_stock_swap_is_silent(tmp_path):
    service, _ = app(tmp_path, stocks={})
    directive, row, printed = run(service, args())
    assert directive is None and not row['rules']
    assert 'swap value' not in printed


@pytest.mark.parametrize('larger', ['In', 'Out'])
def test_both_stock_sides_use_larger_value(tmp_path, larger):
    service, _ = app(tmp_path, stocks={A: stock(A), B: stock(B)})
    arguments = args('1000000', tokenOut={'address': B, 'decimals': 6}, amountOut='1000000', amountOutRaw='1000000')
    arguments['amount' + larger + 'Raw'] = '4000000'
    directive, _, printed = run(service, arguments)
    assert directive['action'] == 'approve'
    assert 'swap value $400.00 (amount × Toll chain price read ' + AS_OF + ')' in printed.splitlines()


def test_both_stock_sides_need_data(tmp_path):
    service, _ = app(tmp_path, stocks={A: stock(A), B: stock(B)})
    directive, _, printed = run(service, args())
    assert directive is None
    assert 'swap value not read: amount, decimals' in printed


@pytest.mark.parametrize('mode,disposition', [('ask_when', 'ask'), ('note', 'note'), ('ask', 'ask')])
def test_modes(tmp_path, mode, disposition):
    service, _ = app(tmp_path, mode=mode)
    directive, row, printed = run(service, args())
    assert row['disposition'] == disposition
    assert (directive is not None) == (disposition == 'ask')
    assert any(RULE in rule for rule in row['rules'])
    if mode == 'note':
        assert ASK.replace('ask ·', 'note ·') in printed.splitlines()


@pytest.mark.parametrize('mode', ['ask_when', 'note', 'ask'])
def test_hard_stop_wins(tmp_path, mode):
    service, _ = app(tmp_path, mode=mode)
    original = service._tx_cards
    def stopped(*a, **kw):
        cards = original(*a, **kw)
        return [replace(card, data={**card.data, 'approval_eoa': True}) for card in cards]
    service._tx_cards = stopped
    directive, row, _ = run(service, args())
    assert directive['action'] == 'block' and row['disposition'] == 'block'
    assert any(RULE in rule for rule in row['rules'])


@pytest.mark.parametrize('allow,disposition', [(False, 'block'), (True, 'pass')])
def test_cron_matches_other_rules(tmp_path, allow, disposition):
    service, _ = app(tmp_path, allow_unattended=allow)
    service.presence = lambda: True
    _, row, _ = run(service, args())
    assert row['disposition'] == disposition
    assert any(RULE in rule for rule in row['rules'])


@pytest.mark.parametrize('side', ['sold', 'bought'])
def test_decoded_swap_uses_existing_metadata_and_chain_price(tmp_path, side):
    service, reads = app(tmp_path)
    response = {'coins': [{'address': A, 'decimals': 6}], 'x-toll': {
        'decoded': [{'kind': 'swap', side: [{'address': A, 'raw': '3000000'}]}],
        'simulation': {'status': 'success'}}}
    service.network.clients['toll'].check_tx = lambda *a: response
    arguments = {'chainId': 4663, 'from': WALLET, 'to': ROUTER, 'data': '0x', 'token': A}
    directive, _, printed = run(service, arguments, tool='wallet_swap')
    assert directive['action'] == 'approve' and ASK in printed.splitlines()
    assert 'swap value $300.00' in printed
    assert sorted(reads) == [A, WALLET, ROUTER]


def test_existing_simulation_delta_does_not_establish_swap_amount(tmp_path):
    service, _ = app(tmp_path)
    response = {'x-toll': {'decoded': [{'kind': 'swap', 'sold': [A]}],
        'simulation': {'status': 'success', 'deltas': [{'holder': WALLET, 'token': A, 'delta': '-3000000', 'decimals': 6}]}}}
    service.network.clients['toll'].check_tx = lambda *a: response
    directive, _, printed = run(service, {'chainId': 4663, 'from': WALLET, 'to': ROUTER, 'data': '0x', 'token': A})
    assert directive is None and 'swap value not read: amount' in printed


def test_hookr_quote_does_not_establish_swap_amount():
    request = json.loads((ROOT / 'toll-tx/clean-swap/request.json').read_text())
    intent = tx_intents(request)[0]
    address = '0x41b57f6273b5a78cf9125ce6800f415738e0bba0'
    card = project(local_card(intent), intent, stock_swap=True, hookr={'status': 'ok',
        'gets': {'address': address, 'raw': '3000000', 'amount': '3', 'decimals': 6}})
    network = Card(address, 'network', {'base': Card(address, 'missing'),
        'sources': [Toll().stock(stock(address), address)]})
    settings = Settings(ask_when={RULE: 250}, ask_no_record=False, on_unchecked='note')
    cards = annotate([card, network], {}, settings)
    assert evaluate(cards, settings).action == 'pass'
    assert 'swap value not read: amount' in cards[0].data['lines']


def test_membership_must_match_address(tmp_path):
    service, _ = app(tmp_path, stocks={A: stock(B)})
    directive, _, printed = run(service, args())
    assert directive is None and 'swap value' not in printed


def test_no_swap_no_threshold(tmp_path):
    service, _ = app(tmp_path)
    directive, _, printed = run(service, {'chainId': 4663, 'token': A, 'raw': '3000000'}, tool='transfer')
    assert directive is None and 'swap value' not in printed


def test_exact_ask_line_with_another_triggered_rule(tmp_path):
    service, _ = app(tmp_path, on_unchecked='ask')
    directive, row, printed = run(service, args())
    assert directive['action'] == 'approve' and len(row['rules']) == 2
    assert ASK in printed.splitlines()
    assert 'your rule: transaction could not be checked' in row['rules']


@pytest.mark.parametrize('key', ['prices', 'chain'])
def test_missing_price_container_retains_membership(tmp_path, key):
    data = stock()
    if key == 'prices':
        del data['prices']
    else:
        del data['prices']['chain']
    service, _ = app(tmp_path, stocks={A: data}, ask_no_record=True)
    directive, row, printed = run(service, args())
    # Incomplete prices retain the old source state and existing HEY asks.
    assert 'Toll could not be reached' in printed
    assert 'swap value not read: Toll chain price' in printed
    assert all(RULE not in rule for rule in row['rules'])
    assert directive['action'] == 'approve'


@pytest.mark.parametrize('decimals', [None, True, -1, 256, '6'])
def test_unusable_decimals_never_guessed(tmp_path, decimals):
    service, _ = app(tmp_path)
    directive, _, printed = run(service, args(tokenIn={'address': A, 'decimals': decimals}))
    assert directive is None and 'swap value not read: decimals' in printed


def test_decoded_nested_token_metadata(tmp_path):
    service, _ = app(tmp_path)
    service.network.clients['toll'].check_tx = lambda *a: {'x-toll': {
        'decoded': [{'kind': 'swap', 'bought': [{'token': {'address': A, 'decimals': 6}, 'raw': '3000000'}]}],
        'simulation': {'status': 'success'}}}
    directive, _, printed = run(service, {'chainId': 4663, 'from': WALLET, 'to': ROUTER, 'data': '0x', 'token': A})
    assert directive['action'] == 'approve' and 'swap value $300.00' in printed


def test_disabled_toll_has_no_swap_value_rule(tmp_path):
    service, reads = app(tmp_path, sources={s: 'off' for s in MODULES})
    directive, _, printed = run(service, args())
    assert directive is None and not reads and 'swap value' not in printed
