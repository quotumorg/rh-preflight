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
    HISS['tokenUrl'] + '/extra', HISS['tokenUrl'].replace('/token/', '/TOKEN/'), HISS['tokenUrl'] + '?api=1', HISS['tokenUrl'] + '#card',
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


def token_rows(count):
    return [{**HISS, 'token': '0x' + format(i + 1, '040x')} for i in range(count)]


def batch_client(rows, response=None, clock=None):
    client = Client(**({'clock': clock} if clock else {}))
    calls = []
    by_address = {row['token']: row for row in rows}
    def read(path, deadline, payload=None):
        calls.append((path, payload, deadline))
        if response:
            return response(path, deadline, payload)
        if payload is None:
            return 200, by_address[path.split('token=')[1]]
        return 200, {'results': [by_address[t] for t in reversed(payload['tokens'])]}
    client._read = read
    return client, calls


@pytest.mark.parametrize('count,sizes', [(2, [2]), (20, [20]), (21, [20, 1]), (41, [20, 20, 1])])
def test_batch_chunks_match_by_address_without_cache(count, sizes):
    rows = token_rows(count)
    client, calls = batch_client(rows)
    client.cache['poison'] = (0, {})
    readings = client.lookup_many([row['token'] for row in rows] + [rows[0]['token'].upper().replace('0X', '0x')])
    assert [len(call[1]['tokens']) for call in calls] == sizes
    assert all(call[0] == '/api/bot/v1/check/batch' for call in calls)
    for row in rows:
        assert readings[row['token']].line.endswith('/token/' + row['token'])
        assert readings[row['token']].state == 'present'
    assert list(client.cache) == ['poison'] and client.hits == 0
    assert all(len(json.dumps(call[1]).encode()) < 8192 for call in calls)


def test_batch_one_token_uses_single_and_no_token_does_not_read():
    client, calls = batch_client([HISS])
    assert client.lookup_many(['not an address'])['not an address'].asked is False
    assert not calls
    client.lookup_many([HISS['token'], HISS['token']])
    assert len(calls) == 1 and calls[0][1] is None


def test_batch_per_token_errors_and_missing_duplicate_or_foreign_rows_are_local():
    rows = token_rows(5)
    payload = {'results': [rows[3], rows[0], rows[0], {**rows[1], 'error': 'not readable'}, HISS]}
    client, calls = batch_client(rows, lambda *args: (200, payload))
    readings = client.lookup_many([r['token'] for r in rows])
    assert [readings[r['token']].state for r in rows] == [
        'unreachable', 'unreachable', 'unreachable', 'present', 'unreachable']
    assert len(calls) == 1 and HISS['token'] not in readings


@pytest.mark.parametrize('failure', [500, 413, 'transport', 'malformed'])
def test_failed_batch_falls_back_to_singles(failure):
    from rh_preflight.hey import TemporaryUnavailable
    rows = token_rows(2)
    def answer(path, deadline, payload):
        if payload is None:
            return 200, next(r for r in rows if path.endswith(r['token']))
        if failure == 'transport':
            raise TemporaryUnavailable()
        return (200, {}) if failure == 'malformed' else (failure, {})
    client, calls = batch_client(rows, answer)
    assert all(r.state == 'present' for r in client.lookup_many([r['token'] for r in rows]).values())
    assert len(calls) == 3 and calls[0][1] == {'tokens': [r['token'] for r in rows]}
    assert all(call[1] is None for call in calls[1:])


@pytest.mark.parametrize('raised', [False, True])
def test_429_stops_remaining_batches_and_singles(raised):
    from rh_preflight.hey import RateLimited
    rows = token_rows(41)
    def answer(*args):
        if raised:
            raise RateLimited()
        return 429, {}
    client, calls = batch_client(rows, answer)
    readings = client.lookup_many([r['token'] for r in rows])
    assert len(calls) == 1 and all(r.state == 'rate_limited' for r in readings.values())


def test_single_fallback_rate_limit_stops_all_remaining_reads():
    from rh_preflight.hey import RateLimited
    rows = token_rows(21)
    def answer(path, deadline, payload):
        if payload:
            return 500, {}
        raise RateLimited()
    client, calls = batch_client(rows, answer)
    assert all(r.state == 'rate_limited' for r in client.lookup_many([r['token'] for r in rows]).values())
    assert len(calls) == 2


def test_batch_limit_ten_per_minute_shared_between_checks():
    clock = [100.0]
    rows = token_rows(2)
    client, calls = batch_client(rows, clock=lambda: clock[0])
    for i in range(10):
        assert all(r.state == 'present' for r in client.lookup_many([r['token'] for r in rows]).values())
    assert all(r.state == 'rate_limited' for r in client.lookup_many([r['token'] for r in rows]).values())
    assert len(calls) == 10
    clock[0] += 60
    assert all(r.state == 'present' for r in client.lookup_many([r['token'] for r in rows]).values())
    assert len(calls) == 11


def test_large_batch_never_reads_an_eleventh_page():
    rows = token_rows(201)
    client, calls = batch_client(rows)
    result = client.lookup_many([r['token'] for r in rows])
    assert len(calls) == 10
    assert result[rows[-1]['token']].state == 'rate_limited'


@pytest.mark.parametrize('failure', [True, False])
def test_deadline_stops_next_page_or_single_fallback(failure):
    clock = [100.0]
    rows = token_rows(21)
    def answer(path, deadline, payload):
        assert deadline == 101.0
        clock[0] = 102.0
        return (500, {}) if failure else (200, {'results': rows[:20]})
    client, calls = batch_client(rows, answer, clock=lambda: clock[0])
    result = client.lookup_many([r['token'] for r in rows], deadline=101.0)
    assert len(calls) == 1 and result[rows[-1]['token']].state == 'unreachable'
    assert result[rows[0]['token']].state == ('unreachable' if failure else 'present')


def test_network_enrich_batches_swaps_and_lists_and_respects_off_switch():
    from rh_preflight.network import MODULES, Network
    rows = token_rows(21)
    net = Network()
    client, calls = batch_client(rows)
    net.clients['trnch'] = client
    settings = Settings(sources={n: 'on' if n == 'trnch' else 'off' for n in MODULES})
    for count, tool in [(2, 'swap'), (21, 'read')]:
        calls.clear()
        cards = net.enrich([Card(r['token'], 'identifier') for r in rows[:count]], tool, {}, settings)
        assert len(calls) == (1 if count == 2 else 2)
        assert all(c.data['sources'][0].line.endswith(c.target) for c in cards)
        assert all(c.public()['sources'][4]['bucket'] == 'answered' for c in cards)
    calls.clear()
    net.enrich([Card(r['token'], 'identifier') for r in rows], 'swap', {},
               replace(settings, sources={**settings.sources, 'trnch': 'off'}))
    assert not calls


@pytest.mark.parametrize('targets', ['literal', 'mixed', 'resolved'])
def test_service_groups_literal_and_resolved_tokens_under_original_deadline(tmp_path, targets):
    import time
    from rh_preflight.hooks import Service
    from rh_preflight.ledger import Ledger
    from rh_preflight.network import MODULES, Network
    from demo.fake_network import agent_payload
    from rh_preflight.card import agent_card
    rows = token_rows(2)
    names = [r['token'] for r in rows]
    if targets in {'mixed', 'resolved'}:
        names[0] = 'first project'
    if targets == 'resolved':
        names[1] = 'second project'
    class Hey:
        def lookup(self, target, **kwargs):
            if target.startswith('0x'):
                return Card(target, 'identifier')
            payload = agent_payload()
            payload['subject']['project']['token'] = {
                'chainId': 4663, 'address': rows[names.index(target)]['token']}
            return agent_card(target, payload)
    net = Network()
    client, calls = batch_client(rows)
    net.clients['trnch'] = client
    settings = Settings(sources={n: 'on' if n == 'trnch' else 'off' for n in MODULES})
    app = Service(client=Hey(), network=net, settings=settings, ledger=Ledger(tmp_path))
    deadline = time.monotonic() + 1
    cards = app._read_cards(names, 'swap', {}, settings, deadline=deadline)
    assert len(calls) == 1 and calls[0][1] == {'tokens': [r['token'] for r in rows]}
    assert calls[0][2] <= deadline
    assert all(len([r for r in c.data['sources'] if r.source == 'Trnch']) == 1 for c in cards)
    assert all(c.public()['sources'][4]['bucket'] == 'answered' for c in cards)


def test_batch_real_transport_one_post_attempt_then_single_fallback():
    from rh_preflight.hey import TemporaryUnavailable
    rows = token_rows(2)
    client = Client()
    calls = []
    def once(path, deadline, payload=None):
        calls.append((path, payload))
        if payload:
            raise TemporaryUnavailable()
        return 200, next(r for r in rows if path.endswith(r['token']))
    client._read_once = once
    result = client.lookup_many([r['token'] for r in rows])
    assert len(calls) == 3 and all(r.state == 'present' for r in result.values())
