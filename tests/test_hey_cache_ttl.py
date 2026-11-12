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
    settings = Settings(hey_cache_ttl=value)
    assert settings.hey_cache_ttl == asdict(settings)['hey_cache_ttl'] == 600


@pytest.mark.parametrize('value,expected', [(0, 0), (.25, .25), (600, 600),
                                           (86400, 86400), (86401, 86400),
                                           (1e100, 86400), (10**1000, 86400)])
def test_ttl_bounds(value, expected):
    assert Settings(hey_cache_ttl=value).hey_cache_ttl == expected


@pytest.mark.parametrize('value,expected', [(2.5, 2.5), (0, 0), ('bad', 600), (86401, 86400)])
def test_config_reaches_client_and_doctor(monkeypatch, tmp_path, value, expected):
    class Context:
        def get_config(self, name, default=None):
            return value if name == 'hey_cache_ttl' else default

    settings = Settings.from_context(Context())
    app = Service(settings=settings, context=Context(), ledger=Ledger(tmp_path))
    assert app.client.ttl == expected
    # Doctor reports the effective cache setting without contacting any source.
    monkeypatch.setattr(app.client, 'probe', lambda: {'reachable': True})
    monkeypatch.setattr(app.network, 'doctor', lambda settings: {})
    assert app.doctor()['hey_cache_ttl'] == expected
    assert all(client.ttl == 600 for client in app.network.clients.values())
