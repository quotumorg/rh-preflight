"""Trnch copyOf on captured 2026-11-24 NVDA checks, confirmed against Toll."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from rh_preflight.card import Card, source_line
from rh_preflight.network import MODULES, Network
from rh_preflight.settings import Settings
from rh_preflight.sources import toll
from rh_preflight.sources.common import Reading
from rh_preflight.sources.trnch import Client

ROOT = Path(__file__).resolve().parents[1]
CAPTURE = json.loads((ROOT / 'tests/fixtures' / 'trnch/copyof-nvda-2026-11-24.json').read_text())
COPIES, OFFICIAL = CAPTURE['copies'], CAPTURE['official']
STOCK = json.loads((ROOT / 'tests/fixtures' / 'toll/stock-nvda-2026-11-24.json').read_text())
NVDA = OFFICIAL['token']
CONFIRMED = 'copy of NVDA · official ' + NVDA + ' (per Trnch, confirmed by Toll)'


def trnch_client(rows):
    client = Client()
    by_token = {row['token']: row for row in rows}
    def read(path, deadline, payload=None):
        if payload is not None:
            return 200, {'results': [deepcopy(by_token[t]) for t in payload['tokens']]}
        return 200, deepcopy(by_token[path.rsplit('=', 1)[1]])
    client._read = read
    client._read_once = read
    return client


class Toll:
    """Toll's free stock route, answered from the captured NVDA response."""
    def __init__(self, stock=STOCK):
        self.stock, self.asked = stock, []

    def lookup(self, target, deadline=None, **kwargs):
        self.asked.append(target)
        if self.stock is not None and target == self.stock['address'].lower():
            return toll.Client().stock(deepcopy(self.stock), target)
        return Reading('Toll', 'unlisted', '')

    def unavailable(self, target, **kwargs):
        return Reading('Toll', 'unreachable', '')


def enrich(rows, targets, toll_client=None, toll_on=True):
    net = Network()
    net.clients['trnch'] = trnch_client(rows)
    net.clients['toll'] = toll_client or Toll()
    on = {'trnch', 'toll'} if toll_on else {'trnch'}
    settings = Settings(sources={n: 'on' if n in on else 'off' for n in MODULES})
    cards = net.enrich([Card(t, 'identifier') for t in targets], 'read', {}, settings)
    return {c.target: next(r for r in c.data['sources'] if r.source == 'Trnch') for c in cards}, net


@pytest.mark.parametrize('copy', COPIES, ids=['0xa328', '0x1076'])
def test_copy_names_official_address_confirmed_by_toll(copy):
    rows, net = enrich([copy], [copy['token']])
    row = rows[copy['token']]
    expected = ('Trnch: ' + copy['summary'] + ' · blocked · COPYCAT · ' + CONFIRMED +
                ' · ' + copy['tokenUrl'])
    assert row.line == source_line(row) == expected
    card = Card(copy['token'], 'network', {'base': Card(copy['token'], 'identifier'), 'sources': [row]})
    assert expected in card.render() == card.public()['card']
    assert NVDA in net.clients['toll'].asked
    assert ' use ' not in row.line


def test_official_token_has_no_copy_line_and_no_extra_toll_read():
    rows, net = enrich([OFFICIAL], [NVDA])
    row = rows[NVDA]
    assert ' · copy of ' not in row.line and row.copy_of is None
    assert row.line == source_line(row)
    assert net.clients['toll'].asked == [NVDA]


def test_batch_of_both_copies_and_the_official_token():
    rows, _ = enrich([*COPIES, OFFICIAL], [*(c['token'] for c in COPIES), NVDA])
    assert all(CONFIRMED in rows[c['token']].line for c in COPIES)
    assert 'copy of' not in rows[NVDA].line


@pytest.mark.parametrize('case', ['toll_off', 'not_a_stock', 'other_ticker', 'other_address', 'unofficial_registry'])
def test_unconfirmed_copy_names_the_ticker_without_an_address(case):
    stock = deepcopy(STOCK)
    if case == 'not_a_stock':
        stock = None
    elif case == 'other_ticker':
        stock['ticker'] = 'AAPL'
    elif case == 'other_address':
        stock['address'] = '0x' + '1' * 40
    elif case == 'unofficial_registry':
        stock['registry']['source'] = 'https://example.test/assets'
    copy = COPIES[0]
    rows, _ = enrich([copy], [copy['token']], Toll(stock), toll_on=case != 'toll_off')
    row = rows[copy['token']]
    assert row.line == source_line(row) == ('Trnch: ' + copy['summary'] + ' · blocked · COPYCAT · '
                                           'copy of NVDA per Trnch · ' + copy['tokenUrl'])
    assert NVDA not in row.line


@pytest.mark.parametrize('value', [
    None, 'NVDA', [], {'symbol': 'NVDA'}, {'officialAddress': NVDA},
    {'symbol': 'nvda', 'officialAddress': NVDA}, {'symbol': 'NVDA\n', 'officialAddress': NVDA},
    {'symbol': 'NV DA', 'officialAddress': NVDA}, {'symbol': 'NVDA', 'officialAddress': NVDA[:-1]},
    {'symbol': 'NVDA', 'officialAddress': NVDA + ' '},
    {'symbol': 'NVDA', 'officialAddress': COPIES[0]['token']},
])
def test_malformed_or_self_copy_of_adds_nothing(value):
    copy = {**COPIES[0], 'copyOf': value}
    rows, net = enrich([copy], [copy['token']])
    assert 'copy of NVDA ' not in rows[copy['token']].line
    assert net.clients['toll'].asked == [copy['token']]


@pytest.mark.parametrize('symbol', ['SAFE', 'LEGIT.SCAM', 'RUG', 'NVDA.VERDICT'])
def test_copy_symbol_cannot_carry_wording(symbol):
    copy = {**COPIES[0], 'copyOf': {'symbol': symbol, 'officialAddress': NVDA}}
    rows, net = enrich([copy], [copy['token']])
    row = rows[copy['token']]
    assert ' · copy of ' not in row.line and symbol not in row.line and row.copy_of is None
    assert net.clients['toll'].asked == [copy['token']]


@pytest.mark.parametrize('toll_on', [True, False])
def test_deferred_trnch_read_confirms_with_the_callers_toll_setting(tmp_path, toll_on):
    from demo.fake_network import agent_payload
    from rh_preflight.card import agent_card
    from rh_preflight.hooks import Service
    from rh_preflight.ledger import Ledger
    copy = COPIES[0]
    class Hey:
        def lookup(self, target, **kwargs):
            if target.startswith('0x'):
                return Card(target, 'identifier')
            payload = agent_payload()
            payload['subject']['project']['token'] = {'chainId': 4663, 'address': copy['token']}
            return agent_card(target, payload)
    net = Network()
    net.clients['trnch'] = trnch_client([copy, OFFICIAL])
    net.clients['toll'] = Toll()
    on = {'trnch', 'toll'} if toll_on else {'trnch'}
    settings = Settings(sources={n: 'on' if n in on else 'off' for n in MODULES})
    app = Service(client=Hey(), network=net, settings=settings, ledger=Ledger(tmp_path))
    cards = app._read_cards(['copy project', NVDA], 'swap', {}, settings)
    line = next(r.line for r in cards[0].data['sources'] if r.source == 'Trnch')
    assert (CONFIRMED in line) is toll_on
    assert ('copy of NVDA per Trnch' in line) is not toll_on

