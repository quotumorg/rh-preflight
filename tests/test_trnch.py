"""Trnch's written exception: attributed token checks without a dataset cache."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from demo.r6_replay import capture, cards, PRIORS
from rh_preflight.card import Card, source_line, text, token_card
from rh_preflight.hey import Refused, RateLimited, TemporaryUnavailable
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.network import MODULES, Network
from rh_preflight.policy import evaluate
from rh_preflight.presentation import coverage
from rh_preflight.settings import Settings
from rh_preflight.sources.common import Reading, field
from rh_preflight.sources.trnch import Client, TrnchReading

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = json.loads((ROOT / 'tests' / 'fixtures' / 'trnch-synthetic.json').read_text())


def wire(data):
    client = Client()
    requests = []
    def read(path, deadline, payload=None):
        assert payload is None
        requests.append(path)
        return 200, deepcopy(data)
    client._read = read
    return client, requests


def samples():
    return [capture('trnch-check-nxra.json'), capture('trnch-check-priors.json'),
            *SYNTHETIC['responses'], capture('trnch-check-dead.json')]


@pytest.mark.parametrize('data,phrase', list(zip(samples(), [
    'passed the rug screen, live in the Heat',
    'no rug, no dump on record · first seen 2026-10-14T11:50:25.981Z',
    'blocked · WASH_TRADING', 'rugged', 'dumped', 'not screened yet',
])))
def test_exact_attributed_lines(data, phrase):
    client, requests = wire(data)
    row = client.lookup(data['token'].upper().replace('0X', '0x'))
    expected = 'Trnch: ' + phrase + ' · https://trnch.fun/token/' + data['token'].lower()
    assert isinstance(row, TrnchReading)
    assert row.line == source_line(row) == expected
    assert requests == ['/api/bot/v1/check?token=' + data['token'].lower()]
    rendered = Card(data['token'], 'network', {'base': Card(data['token'], 'identifier'),
                                             'sources': [row]}).render()
    assert expected in rendered
    assert not client.cache
    assert row.state == ('unlisted' if data['status'] == 'unknown' else 'present')


@pytest.mark.parametrize('rugged,dumped,expected', [
    (True, True, 'rugged'), (True, False, 'rugged'), (False, True, 'dumped'),
])
@pytest.mark.parametrize('status', ['in_heat', 'seen', 'kept_out', 'unknown'])
def test_boolean_observations_override_lifecycle_status(rugged, dumped, expected, status):
    data = {**capture('trnch-check-priors.json'), 'status': status,
            'rugged': rugged, 'dumped': dumped, 'category': 'WASH_TRADING'}
    client, _ = wire(data)
    assert client.lookup(data['token']).line.startswith('Trnch: ' + expected + ' · ')


@pytest.mark.parametrize('concurrent', [False, True])
def test_two_checks_same_token_make_two_requests_without_cache(tmp_path, concurrent):
    data = capture('trnch-check-priors.json')
    net = Network()
    client, requests = wire(data)
    net.clients['trnch'] = client
    # A poisoned old cache must never be read, updated or serve either check.
    client.cache[('old', 'entry')] = (0, {'not': 'the requested token'})
    before = deepcopy(client.cache)
    class Hey:
        def lookup(self, target, **kwargs):
            return Card(target, 'identifier')
    app = Service(client=Hey(), network=net, ledger=Ledger(tmp_path),
                  settings=Settings(sources={n: 'on' if n == 'trnch' else 'off' for n in MODULES}))
    if concurrent:
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(app.card, [data['token']] * 2))
    else:
        result = [app.card(data['token']), app.card(data['token'])]
    assert len(requests) == 2
    assert requests == ['/api/bot/v1/check?token=' + data['token']] * 2
    assert all('Trnch: no rug, no dump on record' in c.render() for c in result)
    assert client.cache == before and client.hits == 0


def test_trnch_has_no_bulk_prefetch_or_tokenless_doctor_read():
    client, requests = wire(capture('trnch-check-priors.json'))
    client.prefetch()
    assert client.probe() == {'reachable': None, 'latency_ms': None}
    assert client.lookup('agent #7622').asked is False
    assert requests == [] and not client.cache
    net = Network()
    net.clients['trnch'] = client
    settings = Settings(sources={n: 'on' if n == 'trnch' else 'off' for n in MODULES})
    assert net.doctor(settings)['trnch']['reachable'] is None
    assert requests == []


def test_unknown_is_answered_no_record_and_has_no_policy_rule():
    data = capture('trnch-check-dead.json')
    client, _ = wire(data)
    row = client.lookup(data['token'])
    assert row.state == 'unlisted' and row.asked
    card = Card(data['token'], 'network', {'base': Card(data['token'], 'identifier'), 'sources': [row],
        'source_settings': {n: 'off' for n in MODULES if n != 'trnch'}})
    assert coverage(card).startswith('11 sources · 1 answered · 1 not for this target: HEY · off:')
    for data in samples():
        client, _ = wire(data)
        row = client.lookup(data['token'])
        for kind in ['identifier', 'missing']:
            base = Card(data['token'], kind)
            card = Card(data['token'], 'network', {'base': base, 'sources': [row]})
            assert evaluate([card], Settings()) == evaluate([base], Settings())
    assert Settings().sources.get('trnch', 'on') == 'on'
    assert Settings(sources={'trnch': 'off'}).sources['trnch'] == 'off'
    with pytest.raises(ValueError):
        Settings(ask_when={'trnch': True})


@pytest.mark.parametrize('key,value', [
    ('token', '0x' + 'ef' * 20), ('chainId', 1), ('status', 'unrecognized'),
    ('rugged', 'false'), ('dumped', 1),
])
def test_bad_identity_or_schema_does_not_project_a_statement(key, value):
    data = capture('trnch-check-priors.json')
    client, _ = wire({**data, key: value})
    result = client.lookup(data['token'])
    assert result.state == 'unreachable'
    assert result.line.endswith('/token/' + data['token'])
    assert not client.cache


@pytest.mark.parametrize('error,state', [(Refused(402), 'payment_required'),
    (Refused(403), 'unreachable'), (RateLimited(), 'rate_limited'),
    (TemporaryUnavailable(), 'unreachable')])
def test_transport_failure_does_not_project_a_statement(error, state):
    client = Client()
    with patch.object(client, '_read', side_effect=error) as read:
        result = client.lookup(PRIORS)
    assert read.call_count == 1 and result.state == state and not client.cache
    assert result.line == '' if state == 'payment_required' else result.line.endswith('/token/' + PRIORS)


@pytest.mark.parametrize('value', ['SCAM', 'safe', 'blocked', 'rugged', 'dumped',
                                 'hello\nworld\u200b\x1b', 'A' * 200])
def test_dynamic_category_keeps_the_ordinary_field_filter(value):
    data = {**capture('trnch-check-priors.json'), 'category': value, 'status': 'kept_out'}
    client, _ = wire(data)
    row = client.lookup(data['token'])
    assert field(value) in row.line
    assert source_line(row) == row.line
    assert '\n' not in row.line and '\u200b' not in row.line and '\x1b' not in row.line


@pytest.mark.parametrize('data', samples())
def test_ordinary_strings_and_other_clients_receive_no_trnch_exception(data):
    client, _ = wire(data)
    row = client.lookup(data['token'])
    if data['status'] == 'unknown':
        return
    omitted = 'source text omitted by the wording rule'
    assert text(row.line) == omitted
    assert source_line(Reading('Trnch', 'present', row.line)) == omitted
    assert source_line(Reading('Other', 'present', row.line)) == omitted
    assert source_line(replace(row, source='HEY')) == omitted
    assert omitted in token_card(data['token'], {'hey_project_name': row.line}).render()


@pytest.mark.parametrize('key,value', [
    ('seenAt', None), ('seenAt', ''), ('seenAt', True), ('seenAt', 123),
    ('seenAt', {}), ('seenAt', []), ('seenAt', 'not a date'),
    ('seenAt', '2026-02-30T11:50:25Z'),
    ('category', None), ('category', ''), ('category', '  '), ('category', '\u200b\x1b'),
    ('category', False), ('category', 1), ('category', {}), ('category', []),
    ('chainId', 4663.0),
])
@pytest.mark.parametrize('rugged,dumped', [(False, False), (True, False), (False, True), (True, True)])
def test_malformed_fields_fail_closed_to_silent(key, value, rugged, dumped):
    data = {**capture('trnch-check-priors.json'), key: value, 'rugged': rugged, 'dumped': dumped}
    if key == 'category':
        data['status'] = 'kept_out'
    client, _ = wire(data)
    row = client.lookup(PRIORS)
    assert row.state == 'unreachable' and not isinstance(row, TrnchReading)
    card = Card(PRIORS, 'network', {'base': Card(PRIORS, 'identifier'), 'sources': [row]})
    assert 'silent: Trnch (unreachable;' in coverage(card)
    assert row.line.endswith('/token/' + PRIORS) and not client.cache


@pytest.mark.parametrize('status', [None, '', 'unrecognized', [], {}])
@pytest.mark.parametrize('rugged,dumped', [(True, True), (True, False), (False, True)])
def test_boolean_precedence_does_not_hide_malformed_status(status, rugged, dumped):
    data = {**capture('trnch-check-priors.json'), 'status': status,
            'rugged': rugged, 'dumped': dumped}
    client, _ = wire(data)
    assert client.lookup(PRIORS).state == 'unreachable'


@pytest.mark.parametrize('missing', ['token', 'chainId', 'status', 'rugged', 'dumped', 'seenAt'])
def test_missing_required_fields_are_silent(missing):
    data = capture('trnch-check-priors.json')
    del data[missing]
    client, _ = wire(data)
    assert client.lookup(PRIORS).state == 'unreachable'


@pytest.mark.parametrize('data', [None, [], 'seen', True, 4663])
def test_non_object_answers_are_silent(data):
    client, _ = wire(data)
    assert client.lookup(PRIORS).state == 'unreachable'


def test_wormhole_absence_exception_cannot_carry_external_prose():
    line = ('Agent Wormhole has not observed this token; that is not a verdict'
            ' · https://dashboard.agentwormhole.com/t/4663/' + PRIORS)
    assert source_line(Reading('Agent Wormhole', 'missing', line)) == line
    for altered in [line + ' rug', line.replace(PRIORS, 'rug')]:
        assert source_line(Reading('Agent Wormhole', 'missing', altered)) == 'source text omitted by the wording rule'


@pytest.mark.parametrize('word', ['rug', 'blocked', 'rugged', 'dump', 'dumped'])
def test_hostile_token_name_and_other_source_lines_cannot_use_exception(word):
    omitted = 'source text omitted by the wording rule'
    assert text(word) == field(word) == omitted
    assert source_line(Reading('Other', 'present', 'Other: ' + word)) == omitted
    assert omitted in token_card(PRIORS, {'hey_project_name': word}).render()
    data = {**capture('trnch-check-priors.json'), 'name': word, 'symbol': word}
    client, _ = wire(data)
    assert client.lookup(PRIORS).line == (
        'Trnch: no rug, no dump on record · first seen 2026-10-14T11:50:25.981Z'
        ' · https://trnch.fun/token/' + PRIORS)


@pytest.mark.parametrize('mutation', [
    lambda s: s.replace('Trnch: ', ''), lambda s: 'Other: ' + s,
    lambda s: s + ' trusted', lambda s: s.replace('screen,', 'screen safely,'),
    lambda s: s.replace('https://trnch.fun/token/', 'https://example.org/token/'),
    lambda s: s.replace('0x1d4b', '0x1D4B'),
])
def test_only_exact_trnch_phrases_and_token_links_are_excepted(mutation):
    data = capture('trnch-check-nxra.json')
    client, _ = wire(data)
    row = client.lookup(data['token'])
    assert source_line(replace(row, line=mutation(row.line))) == 'source text omitted by the wording rule'


def test_priors_and_nvda_docs_are_current_socket_free_outputs():
    with patch('socket.socket', side_effect=AssertionError('no sockets')):
        priors, nvda = cards()
    readme = (ROOT / 'README.md').read_text()
    assert priors.render() in readme and nvda.render() in readme
    assert 'Trnch: no rug, no dump on record' in priors.render()
    assert 'Trnch: ' not in nvda.render()  # No NVDA Trnch capture is bundled.
