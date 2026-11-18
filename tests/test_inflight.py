"""Independent cache keys never consume each other's transport deadline."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from rh_preflight import hey
from rh_preflight.card import Card
from rh_preflight.sources import common, hookr, toll


@pytest.fixture(params=['source', 'keyless', 'hey', 'hookr', 'coin', 'before_buy'])
def reader(request, monkeypatch):
    # Force all old stripes to collide, independently of Python's hash seed.
    for module in (hey, common, hookr, toll):
        monkeypatch.setattr(module, 'hash', lambda key: 0, raising=False)
    kind = request.param
    client = (common.Source if kind == 'source' else common.Keyless if kind == 'keyless'
              else hey.Client if kind == 'hey' else hookr.Client if kind == 'hookr'
              else toll.Client)()
    entered, release = threading.Event(), threading.Event()
    calls = []
    a, b = '0x' + 'a' * 40, '0x' + 'b' * 40

    def transport(key, *args):
        calls.append(key)
        if key == a:
            entered.set()
            assert release.wait(3), 'test did not release gated transport'
        if kind == 'hey':
            return Card(key, 'identifier')
        if kind == 'hookr':
            return 200, {'result': {'structuredContent': {'address': key}}}
        if kind in ('coin', 'before_buy'):
            body = (f'Decision: WARN\nToken: {key}\nReasons:\n- synthetic finding'
                    if kind == 'before_buy' else f'Token: {key}\n- Maker\'s launches: 2 launches')
            return 200, {'result': {'content': [{'type': 'text', 'text': body}]}}
        return 200, {'key': key}

    if kind == 'hey':
        monkeypatch.setattr(client, '_agent', transport)
    else:
        def read(path, deadline, payload=None):
            key = (payload['params']['arguments']['address'] if kind in ('hookr', 'coin', 'before_buy')
                   else payload['key'] if kind == 'keyless' else path)
            return transport(key)
        monkeypatch.setattr(client, '_read_once' if kind == 'hookr' else '_read', read)

    def invoke(key, deadline):
        if kind == 'source':
            return client.read(key, deadline)
        if kind == 'keyless':
            return client.read('/rpc', deadline, payload={'key': key})
        if kind == 'hey':
            return client.lookup(key, deadline=deadline)
        if kind == 'hookr':
            return client.mcp('hookr_hook', {'address': key}, deadline, cached=True)
        return getattr(client, kind)(key, deadline)

    def success(result):
        if kind == 'hey':
            return result.kind == 'identifier'
        if kind in ('coin', 'before_buy'):
            return result.state == 'present'
        return isinstance(result, dict)

    yield client, invoke, success, a, b, entered, release, calls
    release.set()


def test_unrelated_keys_do_not_wait_on_gated_read(reader):
    client, invoke, success, a, b, entered, release, calls = reader
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke, a, time.monotonic() + 5)
        try:
            assert entered.wait(1)
            second = pool.submit(invoke, b, time.monotonic() + .5)
            assert success(second.result(timeout=1))
            assert not first.done()
            assert calls == [a, b]
        finally:
            release.set()
        assert success(first.result(timeout=1))
    assert client.inflight._entries == {}


def test_same_key_timeout_preserves_owner_and_cache_collapse(reader, monkeypatch):
    client, invoke, success, a, _, entered, release, calls = reader
    registered = threading.Event()
    acquire = client.inflight.acquire

    def track(key, *, timeout):
        registered.set()
        return acquire(key, timeout=timeout)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke, a, time.monotonic() + 5)
        try:
            assert entered.wait(1)
            active = dict(client.inflight._entries)
            try:
                result = invoke(a, time.monotonic() + .01)
            except hey.Unavailable:
                pass
            else:
                assert not success(result)
            assert client.inflight._entries == active
            monkeypatch.setattr(client.inflight, 'acquire', track)
            second = pool.submit(invoke, a, time.monotonic() + 5)
            assert registered.wait(1)
            assert not second.done()
        finally:
            release.set()
        assert success(first.result(timeout=1))
        assert success(second.result(timeout=1))
    assert calls == [a]
    assert client.inflight._entries == {}


def test_inflight_memory_tracks_only_active_owners_and_waiters():
    from rh_preflight.inflight import KeyedLocks
    locks = KeyedLocks()
    for key in range(10_000):
        assert locks.acquire(key, timeout=0)
        locks.release(key)
    assert locks._entries == {}
    assert locks.acquire('held', timeout=0)
    assert not locks.acquire('held', timeout=0)
    assert len(locks._entries) == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(locks.acquire, 'held', timeout=2) for _ in range(2)]
        end = time.monotonic() + 1
        while time.monotonic() < end:
            with locks._guard:
                if locks._entries['held'][1] == 3:
                    break
            time.sleep(.001)
        assert locks._entries['held'][1] == 3
        locks.release('held')
        end = time.monotonic() + 1
        while not any(f.done() for f in results) and time.monotonic() < end:
            time.sleep(.001)
        assert sum(f.done() for f in results) == 1
        assert len(locks._entries) == 1
        assert not locks.acquire('held', timeout=0)
        locks.release('held')
        assert all(f.result(timeout=1) for f in results)
        locks.release('held')
    assert locks._entries == {}
    with pytest.raises(OverflowError):
        locks.acquire('invalid-timeout', timeout=float('inf'))
    assert locks._entries == {}
