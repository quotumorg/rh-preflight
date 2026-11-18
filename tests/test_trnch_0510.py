"""Captured 2026-11-19 checks and bounded multi-token reads, without network."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from rh_preflight.card import Card, source_line, text
from rh_preflight.policy import evaluate
from rh_preflight.settings import Settings
from rh_preflight.sources.common import Reading
from rh_preflight.sources.trnch import Client

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/trnch'
HISS = json.loads((FIXTURES / 'check-hiss-2026-11-19.json').read_text())
BATCHES = json.loads((FIXTURES / 'batch-fixture-tokens-2026-11-19.json').read_text())
ROWS = [row for batch in BATCHES for row in batch['results']]
USDG = next(row for row in ROWS if row['token'].startswith('0x5fc5'))
RUGGED = next(row for row in ROWS if row['rugged'])


def single(data):
    client = Client()
    client._read = lambda *args: (200, deepcopy(data))
    return client.lookup(data['token'])


def network_card(row, kind='missing'):
    return Card(HISS['token'], 'network', {'base': Card(HISS['token'], kind), 'sources': [row]})


def test_summary_as_given_with_date_and_link_on_all_surfaces():
    row = single(HISS)
    expected = ('Trnch: ' + HISS['summary'] + ' · first seen ' + HISS['seenAt'] +
                ' · ' + HISS['tokenUrl'])
    assert row.line == source_line(row) == expected
    card = network_card(row)
    assert expected in card.render() == card.public()['card']
    assert card.public()['sources'][4]['link'] == HISS['tokenUrl']
    assert all(key not in row.line for key in ['api/', 'license', 'cardUrl', 'first_launch'])


@pytest.mark.parametrize('data', [USDG, {**HISS, 'verified': True}])
def test_verified_wording_once(data):
    row = single(data)
    assert row.verified is True
    assert source_line(row).lower().count('verified by trnch') == 1


@pytest.mark.parametrize('value', [None, '', False, 1, [], {}, 'https://evil.test/token/' + HISS['token'],
    'http://trnch.fun/token/' + HISS['token'], HISS['cardUrl'],
    HISS['tokenUrl'] + '/extra', HISS['tokenUrl'] + '?api=1', HISS['tokenUrl'] + '#card',
    HISS['tokenUrl'].replace('trnch.fun', 'trnch.fun.evil.test'),
    HISS['tokenUrl'].replace('trnch.fun', 'user@trnch.fun'),
    HISS['tokenUrl'].replace('trnch.fun', 'trnch.fun:443'),
    HISS['tokenUrl'].replace(HISS['token'], USDG['token']),
    HISS['tokenUrl'] + '\n', 'https://trnch.fun/\ntoken/' + HISS['token']])
def test_bad_token_url_uses_constructed_link(value):
    row = single({**HISS, 'tokenUrl': value})
    assert row.line.endswith(' · ' + HISS['tokenUrl'])
    assert HISS['summary'] in row.line


def test_same_address_url_can_preserve_address_case():
    url = HISS['tokenUrl'].replace(HISS['token'][2:], HISS['token'][2:].upper())
    assert source_line(single({**HISS, 'tokenUrl': url})).endswith(' · ' + url)


@pytest.mark.parametrize('value', [None, '', '  ', False, 1, [], {}, 'one\ntwo', 'one\rtwo',
                                   'one\ttwo', 'one\x1btwo', 'one\x00two', 'one\u200btwo',
                                   'one\u2028two', 'one\u2029two'])
@pytest.mark.parametrize('fact', ['rugged', 'dumped'])
def test_malformed_summary_keeps_independent_fact(value, fact):
    row = single({**HISS, 'summary': value, fact: True})
    assert row.state == 'present'
    assert source_line(row) == 'Trnch: ' + fact + ' · ' + HISS['tokenUrl']


@pytest.mark.parametrize('verified', ['true', 1, [], {}, None])
def test_malformed_verified_does_not_drop_summary_or_rugged(verified):
    row = single({**RUGGED, 'verified': verified})
    assert row.verified is None and row.state == 'present'
    assert RUGGED['summary'] in source_line(row)


def test_summary_cap_does_not_drop_link_or_rugged_fact():
    row = single({**RUGGED, 'summary': 'some observation ' * 100})
    assert len(row.line) < 400 and '… · rugged · ' in row.line
    assert source_line(row) == row.line
    assert row.line.endswith(RUGGED['tokenUrl'])


@pytest.mark.parametrize('data', [HISS, USDG, RUGGED, *ROWS[:1]])
def test_new_prose_has_no_approval_effect_and_exception_is_local(data):
    row = single(data)
    legacy = single({k: v for k, v in data.items() if k not in {'summary', 'verified', 'tokenUrl'}})
    for kind in ['identifier', 'missing', 'unreachable']:
        assert evaluate([network_card(row, kind)], Settings()) == evaluate([network_card(legacy, kind)], Settings())
    # An unrelated source never inherits the summary's wording permission.
    hostile = single({**data, 'summary': 'Rugged: liquidity pulled.'})
    assert source_line(hostile) == hostile.line
    assert source_line(Reading('Trnch', 'present', hostile.line)) == text(hostile.line)
    assert source_line(replace(hostile, source='HEY')) == text(hostile.line)
    assert source_line(replace(hostile, line=hostile.line + ' trusted')) == text(hostile.line + ' trusted')


def test_zero_address_verified_unknown_remains_unlisted():
    row = single(ROWS[0])
    assert ROWS[0]['token'] == '0x' + '0' * 40
    assert row.verified is True and row.state == 'unlisted'
    assert 'Verified by trnch. · not screened yet' in source_line(row)
    dead = next(r for r in ROWS if r['token'] == '0x' + '0' * 36 + 'dead')
    assert dead['verified'] is False and single(dead).state == 'unlisted'


def test_kept_out_summary_keeps_category_and_both_adverse_flags():
    row = single({**HISS, 'status': 'kept_out', 'category': 'WASH_TRADING',
                  'rugged': True, 'dumped': True})
    assert ' · rugged · dumped · blocked · WASH_TRADING · ' in source_line(row)


def test_rugged_summary_relays_instead_of_wording_omission():
    row = single(RUGGED)
    assert source_line(row) == 'Trnch: Rugged: liquidity pulled. · ' + RUGGED['tokenUrl']
