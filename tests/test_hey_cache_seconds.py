from dataclasses import asdict, replace
import hashlib

import pytest

from rh_preflight.card import Card
from rh_preflight.hey import Client
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.settings import Settings


def record_client(monkeypatch, **kwargs):
    ticks, reads = [0.0], []
    client = Client(clock=lambda: ticks[0], **kwargs)

    def read(target, deadline, project, required):
        reads.append(target)
        return Card(target, 'missing')

    monkeypatch.setattr(client, '_agent', read)
    return client, ticks, reads


@pytest.mark.parametrize('ttl', [600, 2.5, 86400])
def test_record_cache_window(monkeypatch, ttl):
    kwargs = {} if ttl == 600 else {'ttl': ttl}
    client, ticks, reads = record_client(monkeypatch, **kwargs)
    assert client.ttl == ttl
    client.lookup('example')
    ticks[0] = ttl - .01
    client.lookup('example')
    assert len(reads) == 1
    ticks[0] = ttl
    client.lookup('example')
    assert len(reads) == 2
    assert client.stats()['hits'] == 1
    assert client.stats()['entries'] == 1


def test_zero_never_reads_or_stores_records(monkeypatch):
    client, ticks, reads = record_client(monkeypatch, ttl=0)
    key = ('example', None, ('verified_builder', 'activity_measured', 'meaningful_ships_30d'))
    # Even a pre-existing entry must not be read when caching is disabled.
    client.cache[key] = (0, Card('example', 'contract'))
    assert client.lookup('example').kind == 'missing'
    client.cache.clear()
    for index in range(520):
        client.lookup('example-' + str(index))
    ticks[0] += 1000
    client.lookup('example')
    assert len(reads) == 522
    assert client.stats()['entries'] == client.stats()['hits'] == 0


def lock_card(slug='example'):
    return Card('example', 'agent', {'payload': {'subject': {'project': {'slug': slug}}}})


def test_zero_never_reads_or_stores_snapshot_locks(monkeypatch):
    ticks, reads = [100.0], []
    client = Client(ttl=0, clock=lambda: ticks[0])
    stale = (100.0, {'old': True})
    client.cache[('snapshot-locks', 'example')] = stale

    def read(path, deadline):
        reads.append(path)
        return 200, {'locks': {'read': len(reads)}}

    monkeypatch.setattr(client, '_read', read)
    for expected in (1, 2):
        assert client.attach_locks(lock_card(), ticks[0] + 4).data['locks'] == {'read': expected}
    assert reads == ['/api/projects/example/snapshot'] * 2
    assert dict(client.cache) == {('snapshot-locks', 'example'): stale}
    client.cache.clear()
    client.attach_locks(lock_card(), ticks[0] + 4)
    assert not client.cache


@pytest.mark.parametrize('ttl,window', [(600, 60), (30, 30)])
def test_snapshot_lock_cache_window(monkeypatch, ttl, window):
    ticks, reads = [100.0], []
    client = Client(ttl=ttl, clock=lambda: ticks[0])

    def read(path, deadline):
        reads.append(path)
        return 200, {'locks': {'read': len(reads)}}

    monkeypatch.setattr(client, '_read', read)
    assert client.attach_locks(lock_card(), ticks[0] + 4).data['locks'] == {'read': 1}
    ticks[0] += window - .01
    assert client.attach_locks(lock_card(), ticks[0] + 4).data['locks'] == {'read': 1}
    assert len(reads) == 1
    ticks[0] = 100 + window
    assert client.attach_locks(lock_card(), ticks[0] + 4).data['locks'] == {'read': 2}
    ticks[0] += window + .01
    assert client.attach_locks(lock_card(), ticks[0] + 4).data['locks'] == {'read': 3}
    assert reads == ['/api/projects/example/snapshot'] * 3


def test_snapshot_locks_keep_shared_cache_bounded(monkeypatch):
    client = Client(clock=lambda: 100.0)
    client.cache[('example', None, ())] = (100.0, Card('example', 'missing'))
    monkeypatch.setattr(client, '_read', lambda *args: (200, {'locks': {}}))
    for index in range(513):
        client.attach_locks(lock_card('example-' + str(index)), 104.0)
        assert len(client.cache) <= 512
    assert ('example', None, ()) not in client.cache
    assert ('snapshot-locks', 'example-0') not in client.cache
    assert ('snapshot-locks', 'example-512') in client.cache


@pytest.mark.parametrize('value', [-1, -.5, '600', 'bad', None, True, False,
                                   float('nan'), float('inf'), float('-inf'), [], {}])
def test_invalid_ttl_falls_back(value):
    settings = Settings(hey_cache_seconds=value)
    assert settings.hey_cache_seconds == asdict(settings)['hey_cache_seconds'] == 600


@pytest.mark.parametrize('value,expected', [(0, 0), (.25, .25), (600, 600),
                                           (86400, 86400), (86401, 86400),
                                           (1e100, 86400), (10**1000, 86400)])
def test_ttl_bounds(value, expected):
    assert Settings(hey_cache_seconds=value).hey_cache_seconds == expected


@pytest.mark.parametrize('value,expected', [(2.5, 2.5), (0, 0), ('bad', 600), (86401, 86400)])
def test_config_reaches_client_and_doctor(monkeypatch, tmp_path, value, expected):
    class Context:
        def get_config(self, name, default=None):
            return value if name == 'hey_cache_seconds' else default

    settings = Settings.from_context(Context())
    app = Service(settings=settings, context=Context(), ledger=Ledger(tmp_path))
    assert app.client.ttl == expected
    # Doctor reports the effective cache setting without contacting any source.
    monkeypatch.setattr(app.client, 'probe', lambda: {'reachable': True})
    monkeypatch.setattr(app.network, 'doctor', lambda settings: {})
    assert app.doctor()['hey_cache_seconds'] == expected
    assert all(client.ttl == 600 for client in app.network.clients.values())


def test_setting_default_and_declared_schema():
    from pathlib import Path
    import re

    assert Settings().hey_cache_seconds == 600
    manifest = (Path(__file__).parents[1] / 'plugin.yaml').read_text()
    schema = re.search(r'^config_schema:\n((?:[ \t].*\n|\n)*)', manifest, re.M).group(1)
    keys = re.findall(r'^  (hey_cache_\w+):$', schema, re.M)
    assert keys == ['hey_cache_seconds']
    setting = re.search(r'^  hey_cache_seconds:\n((?:    .*\n)*)', schema, re.M).group(1)
    assert re.search(r'^    default: 600$', setting, re.M)


def hey_card(kind):
    from rh_preflight.card import agent_card

    target = '0x' + '1' * 40
    if kind == 'agent':
        return agent_card(target, {'status': 'not_found', 'asOf': '2026-11-12T00:00:00Z'})
    return Card(target, kind, {'as_of': '2026-11-12T00:00:00Z'})


@pytest.mark.parametrize('seconds,age', [(59.9, 'under 1 min ago'), (119.9, '1 min ago'),
                                        (479.9, '7 min ago'), (599.9, '9 min ago')])
@pytest.mark.parametrize('kind', ['agent', 'token', 'contract', 'missing'])
def test_cached_card_age_at_render(monkeypatch, seconds, age, kind):
    ticks = [100.0]
    client = Client(clock=lambda: ticks[0])
    monkeypatch.setattr(client, '_agent', lambda *args: hey_card(kind))
    fresh = client.lookup('example')
    cached = client.lookup('example')
    # Rendering happens after the cache hit, including time spent on other sources.
    ticks[0] += seconds
    assert '(cached)' not in fresh.render()
    label = f'HEY record read {age} (cached)'
    assert cached.render().count(label) == 1
    assert any(label in line and ('asOf:' in line or line.startswith('Freshness'))
               for line in cached.render().splitlines())
    assert '(cached)' not in client.cache[next(iter(client.cache))][1].render()
    ticks[0] = 700.0
    assert '(cached)' not in client.lookup('example').render()


def test_zero_never_labels_cards(monkeypatch):
    settings = Settings(hey_cache_seconds=0)
    client, ticks, reads = record_client(monkeypatch, ttl=settings.hey_cache_seconds)
    for seconds in (0, 60, 420, 540):
        ticks[0] = seconds
        assert '(cached)' not in client.lookup('example').public()['card']
    assert len(reads) == 4
    assert not client.cache


@pytest.mark.parametrize('minutes', [0, 1, 7])
@pytest.mark.parametrize('kind', ['agent', 'token', 'contract', 'missing'])
@pytest.mark.parametrize('surface', ['base', 'network', 'presented'])
def test_digest_ignores_cached_age(monkeypatch, minutes, kind, surface):
    from rh_preflight.policy import evaluate
    from rh_preflight.presentation import present

    ticks = [100.0]
    client = Client(clock=lambda: ticks[0])
    monkeypatch.setattr(client, '_agent', lambda *args: hey_card(kind))
    fresh, cached = client.lookup('example'), client.lookup('example')
    ticks[0] += minutes * 60
    if surface != 'base':
        fresh, cached = [Card(card.target, 'network', {'base': card, 'sources': []})
                         for card in (fresh, cached)]
    settings = Settings()
    assert evaluate([fresh], settings) == evaluate([cached], settings)
    if surface == 'presented':
        fresh, cached = [present(card, evaluate([card], settings), read_only=True)
                         for card in (fresh, cached)]
    expected = hashlib.sha256(fresh.render().encode()).hexdigest()
    assert fresh.digest == fresh.public()['digest'] == expected
    assert cached.digest == cached.public()['digest'] == expected
    age = f'{minutes} min ago' if minutes else 'under 1 min ago'
    label = f' · HEY record read {age} (cached)'
    assert cached.public()['card'].count(label) == 1
    assert cached.public()['card'].replace(label, '') == fresh.public()['card']
    assert replace(cached, target='different', kind='missing', data={}).digest != expected


def test_cached_label_on_read_surfaces_and_stored_card(monkeypatch, tmp_path, capsys):
    import json
    from types import SimpleNamespace
    from rh_preflight import cli, commands, tools
    from rh_preflight.presentation import present
    from rh_preflight.policy import evaluate

    ticks = [0.0]
    client = Client(clock=lambda: ticks[0])
    monkeypatch.setattr(client, '_agent', lambda *args: hey_card('agent'))
    fresh = client.lookup('example')
    cached = client.lookup('example')
    ticks[0] = 420
    card = present(cached, evaluate([cached], Settings()), read_only=True)
    fresh_card = present(fresh, evaluate([fresh], Settings()), read_only=True)
    app = SimpleNamespace(card=lambda *args, **kwargs: card)
    label = 'HEY record read 7 min ago (cached)'
    assert label in commands.card_command(app, 'example')
    result = json.loads(tools.handler(app, {'project': 'example'}))
    assert label in result['card']
    assert result['digest'] == card.digest == fresh_card.digest
    assert result['digest'] != hashlib.sha256(result['card'].encode()).hexdigest()
    args = SimpleNamespace(rh_preflight_command='card', target='example', project=None)
    assert cli.run(app, args) == 0
    assert label in capsys.readouterr().out
    ledger = Ledger(tmp_path)
    ledger.add('', 'swap', [card], [], 'ask_when', 'ask')
    ticks[0] = 540
    stored = ledger.rows()[0]['cards'][0]
    assert label in stored['card']
    assert stored['digest'] == fresh_card.digest
    assert '(cached)' not in fresh.render()


def test_cached_digest_through_hooks_approvals_and_desktop_api(monkeypatch, tmp_path):
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path
    import sys
    from types import SimpleNamespace
    from rh_preflight.policy import evaluate
    from rh_preflight.presentation import present

    ticks = [100.0]
    client = Client(clock=lambda: ticks[0])
    monkeypatch.setattr(client, '_agent', lambda *args: hey_card('agent'))
    fresh, cached = client.lookup('example'), client.lookup('example')
    ledger = Ledger(tmp_path)
    app = Service(client=client, ledger=ledger, presence=lambda: False, clock=client.clock,
                  wait=lambda future, deadline, fallback: future.result(timeout=30))
    monkeypatch.setattr(app, '_read_cards', lambda *args, **kwargs: [cached])
    expected = present(fresh, evaluate([fresh], app.settings)).public()
    target = fresh.target
    args = {'chainId': 4663, 'token': target}
    keys, displays = [], []
    for minutes in (0, 1, 7):
        ticks[0] = 100 + minutes * 60
        directive = app.pre_tool_call('swap', args, tool_call_id=str(minutes))
        assert directive['action'] == 'approve'
        keys.append(directive['rule_key'])
        app.post_approval_response(pattern_key='plugin_rule:' + directive['rule_key'], choice='once')
        row = next(row for row in ledger.rows() if directive['rule_key'] == 'rh-preflight:' + row['id'])
        stored = row['cards'][0]
        assert row['decision'] == 'once'
        assert stored['digest'] == expected['digest']
        assert stored['rules'] == expected['rules']
        assert stored['card'] == directive['message']
        assert '(cached)' in stored['card']
        assert stored['card'] in app.transform_tool_result('swap', args, 'result', tool_call_id=str(minutes))
        displays.append(stored['card'])
    assert len(set(keys)) == len(set(displays)) == 3

    spec = spec_from_file_location('cached_card_api', Path(__file__).parents[1] / 'dashboard' / 'plugin_api.py')
    api = module_from_spec(spec)
    # The host supplies the router; exercise our adapter without a host install.
    router = SimpleNamespace(get=lambda path: lambda handler: handler)
    monkeypatch.setitem(sys.modules, 'fastapi', SimpleNamespace(APIRouter=lambda: router))
    spec.loader.exec_module(api)
    monkeypatch.setattr(api.module, 'Ledger', lambda: ledger)
    rows = api.checks()['checks']
    assert len(rows) == 3
    assert {row['cards'][0]['digest'] for row in rows} == {expected['digest']}
    assert {row['cards'][0]['card'] for row in rows} == set(displays)
