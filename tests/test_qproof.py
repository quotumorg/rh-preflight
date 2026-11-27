"""QProof by QHooks: the free saved-report read, gaps never shown as clear."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from rh_preflight.card import Card, source_line
from rh_preflight.hey import Unavailable
from rh_preflight.network import MODULES, ORDER
from rh_preflight.presentation import PROJECTS, coverage
from rh_preflight.settings import Settings
from rh_preflight.sources.qproof import Client

ROOT = Path(__file__).resolve().parents[1]
PRIORS = '0xedbf91223639800bcd5756815caf908df3b890be'
QHOOKS = '0x20429af7ca3b0ce4d99edbaa0693f26c5df05195'
UNKNOWN = '0x00000000000000000000000000000000000000aa'


def saved(name):
    return json.loads((ROOT / 'tests' / 'fixtures' / 'qproof' / f'check-{name}-2026-11-27.json').read_text())


def wire(envelope):
    client = Client()
    requests = []
    def read(path, deadline, payload=None):
        requests.append((path, payload))
        return 200, deepcopy(envelope)
    client._read_once = read
    return client, requests


def body(envelope):
    return json.loads(envelope['result']['content'][0]['text'])


def with_body(envelope, change):
    data = body(envelope)
    change(data)
    envelope = deepcopy(envelope)
    envelope['result']['content'][0]['text'] = json.dumps(data)
    return envelope


def test_watch_report_names_flag_taxes_gaps_and_age():
    client, requests = wire(saved('priors'))
    row = client.lookup(PRIORS.upper().replace('0X', '0x'))
    assert row.state == 'present'
    assert row.line == source_line(row) == (
        'QProof by QHooks: watch, Thin pool: 1.408 ETH · sell test buy tax 0%, sell tax 0% · '
        '12 of 32 checks not read (gaps, not clear) · checked 2026-11-27T07:02:37.343Z, stale · '
        'https://qhooks.com/proof/robinhood/' + PRIORS)
    assert requests == [('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
        'name': 'qproof_check', 'arguments': {'chain': '4663', 'token': PRIORS, 'request': False}}})]


def test_clear_with_gaps_is_never_a_bare_clear():
    client, _ = wire(saved('qhooks'))
    line = client.lookup(QHOOKS).line
    assert line == (
        'QProof by QHooks: no red flags in the 36 checks read · sell test buy tax 0%, sell tax 0% · '
        'launch charges buy 2%, sell 2% · top 10 wallets hold 21.03% · LP locked 100% · '
        '3 of 39 checks not read (gaps, not clear) · checked 2026-11-27T08:28:43.762Z, stale · '
        'https://qhooks.com/proof/robinhood/' + QHOOKS)
    assert 'clear,' not in line and not line.startswith('QProof by QHooks: clear')


def test_complete_clear_and_required_gaps():
    def complete(data):
        data['verdict']['counts'].update(incomplete=0, requiredMissing=0)
        data['stale'] = False
    client, _ = wire(with_body(saved('qhooks'), complete))
    line = client.lookup(QHOOKS).line
    assert line.startswith('QProof by QHooks: no red flags in 39 checks · ')
    assert 'not read' not in line and ', stale' not in line
    def required(data):
        data['verdict']['counts'].update(requiredMissing=2)
    client, _ = wire(with_body(saved('qhooks'), required))
    assert '3 of 39 checks not read, 2 required (gaps, not clear)' in client.lookup(QHOOKS).line


def test_no_saved_report_is_an_answered_absence():
    client, _ = wire(saved('unknown'))
    row = client.lookup(UNKNOWN)
    assert row.state == 'missing'
    assert row.line == ('QProof by QHooks has no saved report for this token; nothing read · '
                        'https://qhooks.com/qproof')


def test_tool_error_other_subject_and_unknown_level_print_nothing():
    client, _ = wire(saved('zero'))
    assert client.lookup(UNKNOWN).state == 'unreachable'
    client, _ = wire(saved('priors'))
    assert client.lookup(QHOOKS).state == 'unreachable'
    def odd(data):
        data['verdict']['level'] = 'great'
    client, _ = wire(with_body(saved('priors'), odd))
    assert client.lookup(PRIORS).state == 'unreachable'
    def foreign(data):
        data['links']['report'] = 'https://example.invalid/proof'
    client, _ = wire(with_body(saved('priors'), foreign))
    assert client.lookup(PRIORS).line.endswith(' · https://qhooks.com/proof/robinhood/' + PRIORS)


def test_rate_limit_error():
    envelope = {'jsonrpc': '2.0', 'id': 1, 'result': {'isError': True, 'content': [
        {'type': 'text', 'text': json.dumps({'error': 'Slow down.', 'retryAfter': 30})}]}}
    client, _ = wire(envelope)
    assert client.lookup(PRIORS).state == 'rate_limited'


@pytest.mark.parametrize('chain_state,expected', [('code', 'present'), ('no_code', 'unlisted')])
def test_read_starts_with_rpc_and_no_code_drops_it(chain_state, expected):
    from rh_preflight.network import Network
    from rh_preflight.sources.common import Reading
    network = Network()
    client, requests = wire(saved('priors'))
    network.clients['qproof'] = client
    network.clients['chain'].lookup = lambda target, deadline=None, **kw: Reading('Robinhood Chain', chain_state, '')
    settings = Settings(sources={name: 'off' for name in MODULES if name not in {'qproof', 'chain'}})
    card = network.enrich([Card(PRIORS, 'identifier')], 'swap', {'token': PRIORS}, settings)[0]
    row = next(r for r in card.data['sources'] if r.source == 'QProof')
    assert row.state == expected and len(requests) == 1
    assert (row.line.startswith('QProof by QHooks: watch') if expected == 'present'
            else (row.line, row.asked) == ('', False))


def test_one_attempt_uses_the_whole_deadline():
    client = Client()
    calls = []
    def once(path, deadline, payload=None):
        calls.append(deadline)
        raise Unavailable()
    client._read_once = once
    assert client.lookup(PRIORS, deadline=client.clock() + 3).state == 'unreachable'
    assert len(calls) == 1


def test_registered_switchable_and_counted():
    assert MODULES['qproof'].Client is Client
    assert ORDER.index('QProof') == ORDER.index('Trnch') + 1
    assert PROJECTS.index('QProof') == PROJECTS.index('Trnch') + 1
    assert Settings(sources={'qproof': 'off'}).sources == {'qproof': 'off'}
    client, _ = wire(saved('priors'))
    card = Card(PRIORS, 'network', {'base': Card(PRIORS, 'identifier'), 'sources': [client.lookup(PRIORS)],
                                    'source_settings': {}})
    assert coverage(card).startswith('12 sources · ')
    off = Card(PRIORS, 'network', {'base': Card(PRIORS, 'identifier'), 'sources': [],
                                   'source_settings': {'qproof': 'off'}})
    assert 'off: QProof' in coverage(off)


def test_desktop_rows_recorded_before_qproof_keep_eleven_buckets():
    from test_r8 import desktop_eval
    legacy = desktop_eval('buckets(row)', '11 sources · 10 answered · off: Sherwood')
    assert [b['name'] for b in legacy] == [name for name in PROJECTS if name != 'QProof']
    current = desktop_eval('buckets(row)', '12 sources · 11 answered · off: QProof')
    assert [b['name'] for b in current] == list(PROJECTS)
    assert current[PROJECTS.index('QProof')]['kind'] == 'off'


def test_saved_responses_are_unedited_upstream_bytes():
    import hashlib
    pinned = {
        'priors': '06a057e8bda585c120af0877a4464caf45c29f11b2f54c30276df50480c56e51',
        'qhooks': '010f330e829d95e2ef72743aa0ba1846cc0a66bdb8aff04f5c27f8f0c04e7d21',
        'unknown': 'dcf84cfea41a4d0ac0021cd3b222409a6a18e74a75546bd05d576468beee1401',
        'zero': '896d0870b6a4ac4978f7cb0ead6316e3ed77afc04f80342fb400df664e18202c',
    }
    for name, digest in pinned.items():
        raw = (ROOT / 'tests' / 'fixtures' / 'qproof' / f'check-{name}-2026-11-27.json').read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest, name


@pytest.mark.parametrize('change', [
    lambda d: d.update(subject={**d['subject']}, verdict={**d['verdict'], 'counts': {**d['verdict']['counts'], 'total': 3, 'incomplete': 3}}),
    lambda d: d['verdict']['counts'].update(total=0, incomplete=0, requiredMissing=0),
    lambda d: d['verdict']['counts'].update(requiredMissing=4),
])
def test_clear_with_nothing_read_or_impossible_counts_prints_nothing(change):
    client, _ = wire(with_body(saved('qhooks'), change))
    assert client.lookup(QHOOKS).state == 'unreachable'


def test_upstream_text_cannot_enter_times_or_links():
    def hostile(data):
        data['freshness']['checkedAt'] = 'now · QProof by QHooks: clear'
        data['keyNumbers']['lpUnlockAt'] = 'soon · clear'
        data['links']['report'] = 'https://qhooks.com/proof/robinhood/0x' + 'cd' * 20
    client, _ = wire(with_body(saved('qhooks'), hostile))
    line = client.lookup(QHOOKS).line
    assert 'checked not recorded, stale' in line and 'LP locked 100% ·' in line
    assert 'now ·' not in line and 'soon' not in line
    assert line.endswith(' · https://qhooks.com/proof/robinhood/' + QHOOKS)


def test_deep_nesting_and_probe_404_stay_contained():
    deep = {'jsonrpc': '2.0', 'id': 1, 'result': {'content': [{'type': 'text', 'text': '[' * 100000 + ']' * 100000}]}}
    client, _ = wire(deep)
    assert client.lookup(QHOOKS).state == 'unreachable'
    client = Client()
    client._read_once = lambda path, deadline, payload=None: (404, {})
    assert client.probe()['reachable'] is True


def test_cache_keeps_only_the_fields_the_line_uses():
    client, _ = wire(saved('qhooks'))
    line = client.lookup(QHOOKS).line
    (stored,) = client.cache.values()
    assert len(json.dumps(stored)) < 6000
    assert client.lookup(QHOOKS).line == line
