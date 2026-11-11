"""Coverage partitions and official Stock Token policy, using saved replies."""
from dataclasses import replace
import json

import pytest

from rh_preflight.card import Card, agent_card
from rh_preflight.hey import TemporaryUnavailable
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.network import MODULES, Network
from rh_preflight.policy import evaluate
from rh_preflight.settings import Settings
from rh_preflight.sources.common import Reading
from rh_preflight.sources.toll import Client as Toll
from demo.r6_replay import NVDA
from demo.captures import capture_path
from test_r8 import ROOT, all_readings, checked_coverage, network_card


def test_unmatched_and_negative_answers_are_different():
    rows = [replace(row, state='unlisted', line='') for row in all_readings()]
    rows = [replace(row, line='Borro has no lending market for NVDA · https://borro.markets')
            if row.source == 'Borro' else row for row in rows]
    card = network_card(rows)
    assert checked_coverage(card) == ('11 sources · 8 answered · '
        '3 not for this target: Hookr, Toll, Sherwood')


def test_empty_buckets_and_settings_precedence():
    assert checked_coverage(network_card(base=Card(NVDA, 'identifier'))) == (
        '11 sources · 11 not for this target: ' + ', '.join((
            'HEY', 'Priors', 'Sentinelhood', 'HoodLock', 'Trnch',
            'Agent Wormhole', 'Hookr', 'Toll', 'Borro', 'Sherwood', 'OffRouter')))
    card = network_card(all_readings() + [Reading('OffRouter', 'unreachable', '', elapsed=2)],
                        offrouter=True, source_settings={'offrouter': 'off'})
    assert checked_coverage(card) == '11 sources · 10 answered · off: OffRouter'
    card = network_card(all_readings() + [Reading('OffRouter', 'timeout', '', elapsed=2)], offrouter=True)
    assert checked_coverage(card) == '11 sources · 10 answered · silent: OffRouter (unreachable; 2.0 s)'


@pytest.mark.parametrize('base_kind', ['missing', 'agent'])
@pytest.mark.parametrize('membership,override,expected', [
    ('official', False, 'pass'), ('official', True, 'ask'),
    ('unreachable', False, 'ask'), ('unlisted', False, 'ask'),
    ('text_only', False, 'ask'), ('unreachable', True, 'ask'),
])
def test_stock_no_record_policy(base_kind, membership, override, expected):
    payload = json.loads(capture_path('hey-builder_status-unknown.json').read_text())
    base = agent_card(NVDA, payload) if base_kind == 'agent' else Card(NVDA, 'missing')
    source = 'Toll'
    reading = Reading(source, 'present' if membership in {'official', 'text_only'} else membership,
                      source + ': official Robinhood Stock Token NVDA · https://example.org',
                      stock_ticker='NVDA' if membership == 'official' else None)
    card = network_card([reading], base=base, is_payment=False)
    settings = Settings(ask_when={'stock_no_hey_record': override})
    result = evaluate([card], settings)
    assert result.action == expected
    assert ('your rule: no HEY record' in result.rules) == (expected == 'ask')
    rendered = replace(card, data={**card.data, 'decision': result}).render()
    assert base.render() in rendered
    assert rendered.count('your rule: no HEY record') == (expected == 'ask')
    assert evaluate([card], replace(settings, ask_no_record=False)).action == 'pass'


def test_stock_exception_keeps_other_rules_and_target_scope():
    official = Reading('Toll', 'present', '', stock_ticker='NVDA')
    card = network_card([official], base=Card(NVDA, 'missing'), is_payment=False)
    assert evaluate([card], Settings(), unattended=True).action == 'block'
    assert evaluate([card], Settings(mode='ask')).action == 'ask'
    assert evaluate([card], Settings(mode='note')).action == 'note'
    assert evaluate([card], Settings(sources={'toll': 'off'})).action == 'ask'
    assert evaluate([card, Card('another target', 'missing')], Settings()).action == 'ask'
    unreadable = network_card([official], base=Card(NVDA, 'unreachable'))
    assert evaluate([unreadable], Settings()).action == 'ask'
    payment = network_card([official, Reading('Priors', 'missing', '')],
                           base=Card(NVDA, 'missing'), is_payment=True)
    assert evaluate([payment], Settings()).rules == ('your rule: payee has no Priors record',)
    quiet = network_card([official], base=Card(NVDA, 'token', {
        'activity_measured': True, 'meaningful_ships_30d': 0}), is_payment=False)
    assert evaluate([quiet], Settings()).rules == ('your rule: no ship in 30 days',)


@pytest.mark.parametrize('silent,override,expected', [
    (False, False, 'go'), (True, False, 'ask'), (False, True, 'ask'),
])
def test_saved_official_membership_through_read_hook_and_ledger(tmp_path, silent, override, expected):
    source = 'toll'
    class Hey:
        def lookup(self, target, **kwargs):
            return agent_card(target, json.loads(capture_path('hey-builder_status-unknown.json').read_text()))
    settings = Settings(sources={name: 'on' if name == source else 'off' for name in MODULES},
                        ask_when={'stock_no_hey_record': override})
    net = Network()
    paths = []
    def read(path, deadline, payload=None):
        paths.append(path)
        if silent:
            raise TemporaryUnavailable()
        raw = capture_path('r5/toll-markets-stock-nvda.json').read_text()
        return 200, json.JSONDecoder().raw_decode(raw)[0]
    net.clients[source]._read = read
    app = Service(settings=settings, client=Hey(), network=net, ledger=Ledger(tmp_path), presence=lambda: False)
    card = app.card(NVDA)
    assert card.render().startswith('if Hermes swapped this now: ' + expected + ' · ')
    checked_coverage(card)
    assert 'HEY Research Lab: no published project' in card.render()
    args = {'chain': 4663, 'token': NVDA}
    directive = app.pre_tool_call('swap', args)
    stored = app.ledger.rows()[0]['cards'][0]
    assert stored['answer'].startswith(expected + ' · ')
    assert stored['card'].count('your rule: no HEY record') == (expected == 'ask')
    if expected == 'ask':
        assert directive['message'] == stored['card']
    else:
        assert directive is None
    assert paths and all(path == '/api/markets/stock/' + NVDA.lower()
                         for path in paths)


def test_stock_setting_validation_and_docs():
    with pytest.raises(ValueError):
        Settings(ask_when={'stock_no_hey_record': 'false'})
    assert Settings().ask_when.get('stock_no_hey_record', False) is False
    assert 'ask_when.stock_no_hey_record' in (ROOT / 'README.md').read_text()
    assert 'stock_no_hey_record defaults to false' in (ROOT / 'plugin.yaml').read_text()


@pytest.mark.parametrize('field,value', [
    ('ticker', None), ('ticker', 'invalid ticker'), ('isin', None),
    ('status', 'ASSET_STATUS_INACTIVE'), ('address', 'unreadable'),
    ('registry', {}), ('registry', {'source': 'https://example.invalid'}),
])
def test_toll_membership_requires_registry_identity(field, value):
    data = json.loads(capture_path('r5/toll-markets-stock-nvda.json').read_text())
    client = Toll()
    confirmed = client.stock(data, NVDA)
    assert confirmed.stock_ticker == 'NVDA'
    assert 'official Robinhood Stock Token NVDA, ISIN US67066G1040, status ASSET_STATUS_ACTIVE' in confirmed.line
    data[field] = value
    unconfirmed = client.stock(data, NVDA)
    assert unconfirmed.stock_ticker is None
    assert 'official Robinhood Stock Token' not in unconfirmed.line
    card = network_card([unconfirmed], base=Card(NVDA, 'missing'), is_payment=False)
    assert evaluate([card], Settings()).rules == ('your rule: no HEY record',)


MISMATCH = ('Toll: response address differs from requested address; '
            'official Stock Token status not established · https://t0ll.xyz')
OTHER = '0x000000000000000000000000000000000000dead'


@pytest.mark.parametrize('address', [NVDA, NVDA[:2] + NVDA[2:].upper()])
@pytest.mark.parametrize('target', [NVDA, NVDA[:2] + NVDA[2:].upper()])
def test_toll_stock_address_match_is_case_insensitive(address, target):
    data = json.loads(capture_path('r5/toll-markets-stock-nvda.json').read_text())
    data['address'] = address
    client = Toll()
    client._read = lambda *args: (200, data)
    reading = client.lookup(target)
    assert reading.stock_ticker == 'NVDA'
    assert 'official Robinhood Stock Token NVDA' in reading.line


@pytest.mark.parametrize('address', [OTHER, None, '', 'unreadable'])
def test_toll_stock_mismatch_or_missing_address_cannot_qualify(address):
    data = json.loads(capture_path('r5/toll-markets-stock-nvda.json').read_text())
    if address is None:
        data.pop('address')
    else:
        data['address'] = address
    client = Toll()
    client._read = lambda *args: (200, data)
    reading = client.lookup(NVDA)
    assert reading.stock_ticker is None and reading.line == MISMATCH
    card = network_card([reading], base=Card(NVDA, 'missing'), is_payment=False)
    assert evaluate([card], Settings()).rules == ('your rule: no HEY record',)


@pytest.mark.parametrize('mode,unattended,expected', [
    ('ask_when', False, 'ask'), ('ask', False, 'ask'), ('note', False, 'note'),
    ('ask_when', True, 'block'), ('ask', True, 'block'), ('note', True, 'note'),
])
def test_substituted_nvda_response_preserves_rules_and_skips_borro(mode, unattended, expected):
    # Labelled substitution: NVDA's saved response is returned for OTHER too.
    net = Network()
    net.clients['toll']._read = lambda *args: (
        200, json.loads(capture_path('r5/toll-markets-stock-nvda.json').read_text()))
    borro_targets = []
    def borro_read(target, **kwargs):
        borro_targets.append((target, kwargs['stock_ticker']))
        return Reading('Borro', 'present', 'Borro: captured market', stock_ticker='NVDA')
    net.clients['borro'].lookup = borro_read
    settings = Settings(mode=mode, sources={name: 'on' if name in {'toll', 'borro'} else 'off'
                                          for name in MODULES})
    cards = net.enrich([Card(NVDA, 'missing'), Card(OTHER, 'missing')], 'swap', {}, settings)
    assert borro_targets == [(NVDA, 'NVDA')]
    assert next(r for r in cards[0].data['sources'] if r.source == 'Toll').stock_ticker == 'NVDA'
    assert MISMATCH in cards[1].render()
    borro = next(r for r in cards[1].data['sources'] if r.source == 'Borro')
    assert borro.asked is False and borro.line == ''
    result = evaluate(cards, settings, unattended=unattended)
    assert result.action == expected
    assert 'your rule: no HEY record' in result.rules
    if mode == 'ask_when' and not unattended:
        assert evaluate([cards[0]], settings).action == 'pass'
        assert evaluate([cards[1]], settings).action == 'ask'


def test_nvda_replay_card_stays_exactly_as_documented():
    from demo.r6_replay import cards
    assert cards()[1].render() in (ROOT / 'README.md').read_text()


def test_ticker_lookup_keeps_prices_without_establishing_address_membership():
    data = json.loads(capture_path('r5/toll-markets-stock-nvda.json').read_text())
    client = Toll()
    client._read = lambda *args: (200, data)
    reading = client.lookup('NVDA')
    assert reading.stock_ticker is None
    assert 'official Robinhood Stock Token' not in reading.line
    assert 'chain price $176.80' in reading.line


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('override,toll_on,expected', [
    (False, True, []), (True, True, ['your rule: no HEY record']),
    (False, False, ['your rule: no HEY record'])])
def test_batch_explanations_isolate_toll_exception(reverse, override, toll_on, expected):
    from demo.gate_replay import INSTANT
    from rh_preflight.explanations import target_rules
    from rh_preflight.presentation import present
    data = json.loads(capture_path('r5/toll-markets-stock-nvda.json').read_text())
    stock = network_card([Toll().stock(data, NVDA)], base=Card(NVDA, 'missing'), is_payment=False)
    other = network_card([Toll().stock(data, OTHER)], base=Card(OTHER, 'missing'), is_payment=False)
    stock, other = replace(stock, target=NVDA), replace(other, target=OTHER)
    cards = [stock, other][::(-1 if reverse else 1)]
    settings = Settings(sources={'toll': 'on' if toll_on else 'off'},
                        ask_when={'stock_no_hey_record': override})
    aggregate = evaluate(cards, settings, now=INSTANT)
    associations = target_rules(cards, settings, INSTANT)
    for card, rules in zip(cards, associations):
        assert [item['rule'] for item in rules] == (expected if card.target == NVDA else ['your rule: no HEY record'])
        shown = present(card, aggregate, rule_targets=rules)
        assert shown.public()['rules'] == list(aggregate.rules) == ['your rule: no HEY record']
        assert ('rule target:' in shown.render()) == bool(rules)
