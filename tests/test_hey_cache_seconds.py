from dataclasses import asdict

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


def test_cached_label_on_read_surfaces_and_stored_card(monkeypatch, tmp_path, capsys):
    import hashlib
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
    app = SimpleNamespace(card=lambda *args, **kwargs: card)
    label = 'HEY record read 7 min ago (cached)'
    assert label in commands.card_command(app, 'example')
    result = json.loads(tools.handler(app, {'project': 'example'}))
    assert label in result['card']
    assert result['digest'] == hashlib.sha256(result['card'].encode()).hexdigest()
    args = SimpleNamespace(rh_preflight_command='card', target='example', project=None)
    assert cli.run(app, args) == 0
    assert label in capsys.readouterr().out
    ledger = Ledger(tmp_path)
    ledger.add('', 'swap', [card], [], 'ask_when', 'ask')
    ticks[0] = 540
    assert label in ledger.rows()[0]['cards'][0]['card']
    assert '(cached)' not in fresh.render()
