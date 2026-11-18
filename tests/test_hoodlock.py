"""Offline HoodLock pagination and the saved 2026-11-13 LP API response."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time

import pytest

from rh_preflight.card import Card
from rh_preflight.hey import Refused, RateLimited, TemporaryUnavailable, Unavailable
from rh_preflight.network import Network
from rh_preflight.policy import evaluate
from rh_preflight.settings import Settings
from rh_preflight.sources import chain, hoodlock


SAMPLE = Path(__file__).parent / 'fixtures' / 'hoodlock' / 'lp-locks-2026-11-13.json'
TOKEN = '0x' + '12' * 20
SCOPE = 'token locks and vesting only; other lockers not read'
# deeper than any supported CPython json decoder accepts; 600 decodes everywhere
DEEP = 100_000


def abi(*values):
    return '0x' + ''.join(format(value, '064x') for value in values)


@pytest.fixture
def sample():
    return json.loads(SAMPLE.read_text())


def wire(sample, count=0, target=TOKEN, withdrawn=0):
    # start on the real monotonic clock: Network's own deadline and budget use it
    ticks, calls = [time.monotonic()], []
    client = hoodlock.Client(clock=lambda: ticks[0], wall_clock=lambda: sample['timestamp'])
    client.verify = lambda deadline: None

    def read(path, deadline, payload=None):
        calls.append((path, payload, deadline))
        if path == hoodlock.LP_PATH:
            return 200, deepcopy(sample)
        if payload[0]['params'][0]['data'].startswith('0x03091773'):
            return 200, [{'id': 1, 'result': abi(32, count, *range(1, count + 1))},
                         {'id': 2, 'result': abi(1000)},
                         {'id': 3, 'result': abi(32, 0)}, {'id': 4, 'result': '0x123'}]
        return 200, [{'id': row['id'], 'result': abi(1, int(target, 16), 10,
                      sample['timestamp'] + int(row['params'][0]['data'][10:], 16) * 86400,
                      int(int(row['params'][0]['data'][10:], 16) <= withdrawn))}
                     for row in payload]

    client._read = read
    return client, calls, ticks


def details(calls):
    return [payload for path, payload, _ in calls if path != hoodlock.LP_PATH
            and payload[0]['params'][0]['data'].startswith('0xf4dadc61')]


def test_saved_sample_missing_value_and_aggregate_before_rounding(sample):
    shi = sample['locks'][0]['token1']
    client, _, _ = wire(sample, target=shi)
    row = client.lookup(shi)
    assert row.line.splitlines()[1] == ('HoodLock LP: 1 locked position in WETH/SHI, 1 of 1 in range holding 100% of active pool liquidity, first unlock 2027-10-05'
                                      ' · https://hoodlock.tech/proof/position/1')
    lock = sample['locks'][-1]['token1']
    client, _, _ = wire(sample, target=lock)
    assert client.lookup(lock).line.splitlines()[1] == (
        'HoodLock LP: 3 locked positions in WETH/LOCK, $0.02 locked, none in range, first unlock 2026-12-11'
        ' · https://hoodlock.tech/proof/position/14')


def test_multiple_pools_are_separate_and_addresses_match_case_insensitively(sample):
    weth = sample['locks'][0]['token0'].upper().replace('0X', '0x')
    client, _, _ = wire(sample, target=weth)
    lines = client.lookup(weth).line.splitlines()[1:]
    assert len(lines) == 3
    assert '1 locked position in WETH/SHI' in lines[0]
    assert '6 locked positions in WETH/HISS' in lines[1]
    assert '3 locked positions in WETH/LOCK' in lines[2]


@pytest.mark.parametrize('mutation', ['v5', 'withdrawn', 'past', 'now', 'other', 'empty'])
def test_only_matching_future_locked_v2_v3_positions(sample, mutation):
    row = deepcopy(sample['locks'][0])
    target = row['token1']
    if mutation == 'v5':
        row['version'] = 'v5'
    elif mutation == 'withdrawn':
        row['status'] = 'withdrawn'
    elif mutation in ('past', 'now'):
        row['unlockTime'] = sample['timestamp'] - (mutation == 'past')
    elif mutation == 'other':
        row['token1'] = TOKEN
    sample['locks'] = [] if mutation == 'empty' else [row]
    client, _, _ = wire(sample, target=target)
    assert 'HoodLock LP:' not in client.lookup(target).line


def test_v2_and_incomplete_api_are_informational(sample):
    sample['locks'] = [sample['locks'][0]]
    sample['locks'][0].update(version='v2', valueUsd=1234.56)
    sample['complete'] = False
    target = sample['locks'][0]['token1']
    client, _, _ = wire(sample, target=target)
    row = client.lookup(target)
    assert ('HoodLock LP: 1 locked position in WETH/SHI, $1,234.56 locked, 1 of 1 in range holding 100% of active pool liquidity, first unlock 2027-10-05'
            '; incomplete API result') in row.line
    assert row.token_lock is False
    base = Card(target, 'identifier')
    with_lp = Card(target, 'network', {'base': base, 'sources': [row]})
    assert evaluate([with_lp], Settings()) == evaluate([base], Settings())
    assert 'HoodLock LP: 1 locked position' in with_lp.render().splitlines()[-1]
    assert client.lookup(TOKEN).line.splitlines()[-1] == (
        'HoodLock LP: incomplete API result')


@pytest.mark.parametrize('bad', [None, [], {}, {'chainId': 1, 'complete': True, 'locks': []},
                               {'chainId': 4663, 'complete': 'false', 'locks': []},
                               {'chainId': 4663, 'complete': True, 'locks': {}},
                               {'chainId': 4663, 'complete': True, 'locks': [None]}])
def test_malformed_api_preserves_token_line(sample, bad):
    client, _, _ = wire(sample)
    original = client._read
    client._read = lambda path, deadline, payload=None: ((200, bad) if path == hoodlock.LP_PATH
                                                       else original(path, deadline, payload))
    row = client.lookup(TOKEN)
    assert row.state == 'present' and 'holds no token lock or vesting' in row.line
    assert 'HoodLock LP:' not in row.line


@pytest.mark.parametrize('key,value', [('unlockTime', 'soon'), ('unlockTime', True),
                                      ('unlockTime', 10**17),
                                      ('pool', None), ('token0', '0x12')])
def test_malformed_matched_position_is_not_reported(sample, key, value):
    sample['locks'] = [sample['locks'][0]]
    target = sample['locks'][0]['token1']
    sample['locks'][0][key] = value
    client, _, _ = wire(sample, target=target)
    assert 'HoodLock LP:' not in client.lookup(target).line


def test_lp_timestamp_os_error_preserves_completed_token_facts(sample, monkeypatch):
    sample['locks'] = [sample['locks'][0]]
    target = sample['locks'][0]['token1']
    client, _, _ = wire(sample, count=1, target=target)
    timestamp = datetime.fromtimestamp

    class Dates:
        @staticmethod
        def fromtimestamp(value, tz):
            if value == sample['locks'][0]['unlockTime']:
                raise OSError('timestamp outside platform range')
            return timestamp(value, tz)

    monkeypatch.setattr(hoodlock, 'datetime', Dates)
    row = client.lookup(target)
    assert row.state == 'present' and '1.00% of supply locked' in row.line
    assert row.token_lock is True and 'HoodLock LP:' not in row.line


@pytest.mark.parametrize('value', [None, 'NaN', 'Infinity', -1, True])
def test_unknown_value_is_omitted(sample, value):
    sample['locks'] = [sample['locks'][0]]
    sample['locks'][0]['valueUsd'] = value
    target = sample['locks'][0]['token1']
    client, _, _ = wire(sample, target=target)
    line = client.lookup(target).line.splitlines()[-1]
    assert line.startswith('HoodLock LP: 1 locked position') and '$' not in line


def test_api_cache_is_shared_across_targets_and_expires_after_sixty_seconds(sample):
    client, calls, ticks = wire(sample)
    client.lookup(TOKEN)
    ticks[0] += 59
    client.lookup(sample['locks'][0]['token1'])
    assert sum(path == hoodlock.LP_PATH for path, _, _ in calls) == 1
    ticks[0] += 2
    client.lookup(TOKEN)
    assert sum(path == hoodlock.LP_PATH for path, _, _ in calls) == 2


@pytest.mark.parametrize('failure', [Unavailable(), Refused(402), Refused(403), RateLimited()])
def test_lp_failure_preserves_token_facts(sample, failure):
    client, _, _ = wire(sample, count=1)
    original = client._read

    def read(path, deadline, payload=None):
        if path == hoodlock.LP_PATH:
            raise failure
        return original(path, deadline, payload)

    client._read = read
    row = client.lookup(TOKEN)
    assert row.state == 'present' and '1.00% of supply locked' in row.line
    assert 'HoodLock LP:' not in row.line


def test_lp_http_404_preserves_token_facts_including_cached_404(sample):
    client, calls, _ = wire(sample, count=1)
    original = client._read
    client._read = lambda path, deadline, payload=None: ((404, {}) if path == hoodlock.LP_PATH
                                                       else original(path, deadline, payload))
    for _ in range(2):
        row = client.lookup(TOKEN)
        assert row.state == 'present' and '1.00% of supply locked' in row.line
        assert 'HoodLock LP:' not in row.line


def test_lp_transport_uses_hoodlock_origin_and_same_deadline(monkeypatch):
    client = hoodlock.Client(clock=lambda: 100)
    calls = []
    monkeypatch.setattr(client.lp_http, '_read', lambda path, deadline: calls.append(
        (client.lp_http.base_url + path, deadline)) or (200, {}))
    assert client._read(hoodlock.LP_PATH, 101) == (200, {})
    assert calls == [('https://hoodlock.tech/api/v1/lp-locks', 101)]


def test_source_switch_disables_both_rpc_and_http(sample):
    network = Network()
    client, calls, _ = wire(sample)
    network.clients['hoodlock'] = client
    settings = Settings(sources={name: 'off' for name in network.clients})
    base = Card(TOKEN, 'identifier')
    network.enrich([base], 'read', {}, settings)
    assert not calls
    settings.sources['hoodlock'] = 'on'
    target = sample['locks'][0]['token1']
    enriched = network.enrich([Card(target, 'identifier')], 'read', {}, settings)[0]
    assert any(path == hoodlock.LP_PATH for path, _, _ in calls)
    assert 'HoodLock LP: 1 locked position in WETH/SHI' in enriched.render()
    assert evaluate([enriched], settings) == evaluate([base], settings)


def test_non_address_does_not_read_rpc_or_api(sample):
    client, calls, _ = wire(sample)
    assert client.lookup('SHI').state == 'unlisted'
    assert not calls


@pytest.mark.parametrize('timestamp_error', [None, OSError, ValueError, OverflowError])
def test_unrenderable_token_date_keeps_lp_read_independent(sample, monkeypatch, timestamp_error):
    sample['locks'] = [sample['locks'][0]]
    target = sample['locks'][0]['token1']
    client, calls, _ = wire(sample, count=21, target=target, withdrawn=20)
    original = client._read

    def read(path, deadline, payload=None):
        status, data = original(path, deadline, payload)
        if (payload and payload[0]['params'][0]['data'].startswith('0xf4dadc61')
                and len(payload) == 1):
            data[0]['result'] = abi(1, int(target, 16), 10, 10**17, 0)
        return status, data

    class Dates:
        @staticmethod
        def fromtimestamp(value, tz):
            if value == 10**17 and timestamp_error:
                raise timestamp_error('timestamp outside platform range')
            return datetime.fromtimestamp(value, tz)

    client._read = read
    monkeypatch.setattr(hoodlock, 'datetime', Dates)
    row = client.lookup(target)
    assert [len(batch) for batch in details(calls)] == [20, 1]
    assert row.state == 'unreachable' and row.token_lock is None
    assert row.line.splitlines()[0] == f'HoodLock could not be reached · {client.page}'
    assert 'HoodLock LP: 1 locked position in WETH/SHI' in row.line


@pytest.mark.parametrize('value', ['9e999999', '1e9999999999999999999', 10**400])
def test_out_of_range_lp_values_keep_positions_and_token_facts(sample, value):
    position = deepcopy(sample['locks'][0])
    position['valueUsd'] = value
    # distinct positions in the same pool exercise aggregation as well as parsing.
    sample['locks'] = [position, dict(position, lockId=100, tokenId='100')]
    target = position['token1']
    client, _, _ = wire(sample, count=1, target=target)
    row = client.lookup(target)
    assert row.state == 'present' and row.token_lock is True
    assert '1.00% of supply locked' in row.line
    assert row.line.splitlines()[-1] == (
        'HoodLock LP: 2 locked positions in WETH/SHI, 2 of 2 in range, first unlock 2027-10-05'
        ' · https://hoodlock.tech/proof/position/1')


@pytest.mark.parametrize('status', [200, 429])
def test_lp_http_get_cache_and_cooldown_preserve_token_facts(sample, server, status):
    origin, state = server
    state['overrides'][hoodlock.LP_PATH] = (status, sample, {'Retry-After': '60'})
    client, _, _ = wire(sample)
    client.lp_http = hoodlock.LPHTTP(base_url=origin, clock=client.clock)
    original = client._read

    def read(path, deadline, payload=None):
        if path == hoodlock.LP_PATH:
            return hoodlock.Client._read(client, path, deadline, payload)
        return original(path, deadline, payload)

    client._read = read
    for target in (TOKEN, sample['locks'][0]['token1']):
        row = client.lookup(target)
        assert row.state == 'present' and row.token_lock is False
        assert 'holds no token lock or vesting' in row.line
        assert ('HoodLock LP: 1 locked position' in row.line) == (status == 200 and target != TOKEN)
    assert len(state['requests']) == 1
    path, headers = state['requests'][0]
    assert path == hoodlock.LP_PATH
    headers = {key.lower(): value for key, value in headers.items()}
    assert not {'authorization', 'x-api-key', 'x-hey-integration'} & headers.keys()
    assert not any('payment' in key for key in headers)


@pytest.mark.parametrize('depth', [600, DEEP])
@pytest.mark.parametrize('through_network', [False, True])
def test_nested_lp_http_and_cached_json_preserve_token_facts(sample, server, depth, through_network):
    origin, state = server
    # The decoder rejects DEEP levels. At 600 it succeeds, but the cache's
    # defensive deepcopy exceeds the recursion limit on the second lookup.
    raw = (json.dumps(sample).encode()[:-1] + b', "unused":' + b'[' * depth
           + b'0' + b']' * depth + b'}')
    assert len(raw) < 2_000_000
    state['overrides'][hoodlock.LP_PATH] = (200, raw, {})
    target = sample['locks'][0]['token1']
    client, _, _ = wire(sample, count=1, target=target)
    client.lp_http = hoodlock.LPHTTP(base_url=origin, clock=client.clock)
    original = client._read

    def read(path, deadline, payload=None):
        if path == hoodlock.LP_PATH:
            return hoodlock.Client._read(client, path, deadline, payload)
        return original(path, deadline, payload)

    client._read = read
    network = Network()
    network.clients['hoodlock'] = client
    settings = Settings(sources={name: 'off' for name in network.clients})
    settings.sources['hoodlock'] = 'on'
    for index in range(2):
        if through_network:
            card = network.enrich([Card(target, 'identifier')], 'read', {}, settings)[0]
            row, = card.data['sources']
            assert '1.00% of supply locked' in card.render()
        else:
            row = client.lookup(target)
        assert row.state == 'present' and row.token_lock is True
        assert '1.00% of supply locked' in row.line
        assert ('HoodLock LP: 1 locked position in WETH/SHI' in row.line) == (index == 0 and depth == 600)
    assert len(state['requests']) == (1 if depth == 600 else 2)


@pytest.mark.parametrize('stage', ['verify', 'index', 'details'])
@pytest.mark.parametrize('depth', [600, DEEP])
@pytest.mark.parametrize('through_network', [False, True])
def test_nested_token_rpc_keeps_lp_independent(sample, rpc_server, monkeypatch, stage, depth, through_network):
    origin, state = rpc_server
    target = sample['locks'][0]['token1']
    client, _, _ = wire(sample, count=1, target=target)
    original = client._read
    if stage == 'verify':
        response = {'id': 1, 'result': '0x1237'}
        monkeypatch.setattr(chain, 'VERIFIED', set())
        client.verify = hoodlock.RPC.verify.__get__(client)
        raw = json.dumps(response).encode()[:-1] + b', "unused":' + b'[' * depth + b'0' + b']' * depth + b'}'
    else:
        if stage == 'index':
            response = [{'id': 1, 'result': abi(32, 1, 1)}, {'id': 2, 'result': abi(1000)},
                        {'id': 3, 'result': abi(32, 0)}, {'id': 4, 'result': '0x123'}]
        else:
            response = [{'id': 1, 'result': abi(1, int(target, 16), 10, sample['timestamp'] + 86400, 0)}]
        raw = json.dumps(response).encode()[:-2] + b', "unused":' + b'[' * depth + b'0' + b']' * depth + b'}]'
    assert len(raw) < 2_000_000
    state['raw'] = raw
    transport = hoodlock.RPC(base_url=origin, clock=client.clock)

    def read(path, deadline, payload=None):
        is_stage = (isinstance(payload, dict) if stage == 'verify' else
                    bool(payload) and payload[0]['params'][0]['data'].startswith(
                        '0x03091773' if stage == 'index' else '0xf4dadc61'))
        if path != hoodlock.LP_PATH and is_stage:
            return transport._read(path, deadline, payload)
        return original(path, deadline, payload)

    client._read = read
    network = Network()
    network.clients['hoodlock'] = client
    settings = Settings(sources={name: 'off' for name in network.clients})
    settings.sources['hoodlock'] = 'on'
    for index in range(2):
        if stage == 'verify':
            # reverify so both decoder and cached-copy paths are read.
            chain.VERIFIED.clear()
        if through_network:
            card = network.enrich([Card(target, 'identifier')], 'read', {}, settings)[0]
            row, = card.data['sources']
            assert 'HoodLock LP: 1 locked position in WETH/SHI' in card.render()
        else:
            row = client.lookup(target)
        assert 'HoodLock LP: 1 locked position in WETH/SHI' in row.line
        if index == 0 and depth == 600:
            assert row.state == 'present' and row.token_lock is True
            assert '1.00% of supply locked' in row.line
        else:
            assert row.state == 'unreachable' and row.token_lock is None
            assert 'of supply locked' not in row.line
    assert len(state['requests']) == (1 if depth == 600 else 2)
    # both failure paths must release the cache stripe and HTTP capacity.
    for _ in range(3):
        assert transport.slots.acquire(blocking=False)
    for _ in range(3):
        transport.slots.release()
    client.cache.clear()
    if stage == 'verify':
        chain.VERIFIED.clear()
    state['raw'] = json.dumps(response).encode()
    row = client.lookup(target)
    assert row.state == 'present' and row.token_lock is True
    assert '1.00% of supply locked' in row.line


@pytest.mark.parametrize('mutation', ['duplicate', 'duplicate_other_pool', 'other_pair',
                                      'missing_id', 'boolean_id', 'string_id', 'negative_id'])
def test_lp_identity_errors_do_not_invent_pool_totals(sample, mutation):
    position = deepcopy(sample['locks'][0])
    position['valueUsd'] = 125
    target = position['token0']
    second = dict(position, lockId=100, tokenId='100', valueUsd=75)
    if mutation.startswith('duplicate'):
        second['lockId'] = position['lockId']
        if mutation == 'duplicate_other_pool':
            second['pool'] = '0x' + '34' * 20
    elif mutation == 'other_pair':
        second['token1'] = TOKEN
    elif mutation == 'missing_id':
        del second['lockId']
    else:
        second['lockId'] = {'boolean_id': True, 'string_id': '100', 'negative_id': -1}[mutation]
    sample['locks'] = [position, second]
    client, _, _ = wire(sample, count=1, target=target)
    for _ in range(2):
        row = client.lookup(target)
        assert row.state == 'present' and row.token_lock is True
        assert '1.00% of supply locked' in row.line
        assert 'HoodLock LP:' not in row.line


def test_lp_distinct_positions_in_same_pool_keep_aggregate(sample):
    position = deepcopy(sample['locks'][0])
    position['valueUsd'] = 125
    second = dict(position, lockId=100, tokenId='100', valueUsd=75,
                  pool=position['pool'].upper().replace('0X', '0x'),
                  token0=position['token0'].upper().replace('0X', '0x'))
    sample['locks'] = [position, second]
    client, _, _ = wire(sample, target=position['token0'])
    assert 'HoodLock LP: 2 locked positions in WETH/SHI, $200.00 locked, 2 of 2 in range, first unlock 2027-10-05' in client.lookup(position['token0']).line


@pytest.mark.parametrize('malformed', [False, True])
def test_concurrent_lp_reads_collapse_and_release_cache_lock(sample, server, blocked, malformed):
    origin, state = server
    target = sample['locks'][0]['token1']
    raw = b'{"unused":' + b'[' * DEEP + b'0' + b']' * DEEP + b'}' if malformed else sample
    state['overrides'][hoodlock.LP_PATH] = (200, raw, {})
    client, _, _ = wire(sample, count=1, target=target)
    client.lp_http = hoodlock.LPHTTP(base_url=origin, clock=client.clock)
    original_read, original_transport = client.read, client._read
    arriving, arrivals_lock, arrivals = threading.Event(), threading.Lock(), []

    def read(path, deadline, **kwargs):
        if path == hoodlock.LP_PATH:
            with arrivals_lock:
                arrivals.append(path)
                if len(arrivals) == 2:
                    arriving.set()
        return original_read(path, deadline, **kwargs)

    def transport(path, deadline, payload=None):
        if path == hoodlock.LP_PATH:
            return hoodlock.Client._read(client, path, deadline, payload)
        return original_transport(path, deadline, payload)

    client.read, client._read = read, transport
    with ThreadPoolExecutor(max_workers=2) as pool:
        with blocked() as (gate, entered, _):
            state['response_gates'] = {hoodlock.LP_PATH: gate}
            first = pool.submit(client.lookup, target)
            assert entered.wait(2)
            second = pool.submit(client.lookup, target)
            assert arriving.wait(2)
        for future in (first, second):
            row = future.result(timeout=3)
            assert row.state == 'present' and row.token_lock is True
            assert '1.00% of supply locked' in row.line
            assert ('HoodLock LP: 1 locked position in WETH/SHI' in row.line) is not malformed
    assert len(state['requests']) == (2 if malformed else 1)
    # a decoder failure is not cached, and a new valid reply must be readable.
    state['overrides'][hoodlock.LP_PATH] = (200, sample, {})
    assert 'HoodLock LP: 1 locked position in WETH/SHI' in client.lookup(target).line


@pytest.mark.parametrize('status', [200, 403, 404, 429])
def test_oversized_lp_body_preserves_facts_and_releases_slots(sample, server, status):
    origin, state = server
    state['overrides'][hoodlock.LP_PATH] = (status, b'{"unused":"' + b'x' * 2_000_000 + b'"}', {})
    target = sample['locks'][0]['token1']
    client, _, ticks = wire(sample, count=1, target=target)
    client.lp_http = hoodlock.LPHTTP(base_url=origin, clock=client.clock)
    original = client._read

    def read(path, deadline, payload=None):
        if path == hoodlock.LP_PATH:
            return hoodlock.Client._read(client, path, deadline, payload)
        return original(path, deadline, payload)

    client._read = read
    row = client.lookup(target)
    assert row.state == 'present' and row.token_lock is True
    assert '1.00% of supply locked' in row.line and 'HoodLock LP:' not in row.line
    assert len(state['requests']) == 1
    for _ in range(3):
        assert client.lp_http.slots.acquire(blocking=False)
    for _ in range(3):
        client.lp_http.slots.release()
    ticks[0] += 61  # expire a 429 cooldown before testing recovery.
    state['overrides'][hoodlock.LP_PATH] = (200, sample, {})
    assert 'HoodLock LP: 1 locked position in WETH/SHI' in client.lookup(target).line


@pytest.fixture
def rpc_server():
    state = {'raw': b'{}', 'requests': []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            state['requests'].append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            raw = state['raw']
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.fixture
def current_sample():
    return json.loads((SAMPLE.parent / 'lp-locks-2026-11-18.json').read_text())


@pytest.mark.parametrize('complete', [False, True])
@pytest.mark.parametrize('symbol', ['HISS', 'LOCK', 'UNMATCHED'])
def test_successful_lp_read_never_keeps_not_read_caveat(current_sample, complete, symbol):
    current_sample['complete'] = complete
    target = next((r['token1'] for r in current_sample['locks'] if r['symbol1'] == symbol), TOKEN)
    client, _, _ = wire(current_sample, target=target)
    reading = client.lookup(target)
    assert 'LP locks in HoodLock' not in reading.line
    assert 'other lockers not read' in reading.line
    assert ('HoodLock LP:' in reading.line) == (symbol != 'UNMATCHED' or not complete)
    assert ('incomplete API result' in reading.line) is not complete


def test_hiss_v4_pools_keep_all_nine_positions_and_api_values(current_sample):
    target = '0x444ce8ede8e38e1ec18aeec45d415c1d298a8bcf'
    client, _, _ = wire(current_sample, target=target)
    lines = client.lookup(target).line.splitlines()[1:]
    assert lines == [
        'HoodLock LP: 6 locked positions in WETH/HISS, $35,537.08 locked,'
        ' 2 of 6 in range holding 100% of active pool liquidity, first unlock 2026-11-18'
        ' · https://hoodlock.tech/proof/position/12',
        'HoodLock LP: 3 locked positions in HISS/USDG, $15,213.13 locked,'
        ' none in range, pool has no active liquidity, first unlock 2026-11-18'
        ' · https://hoodlock.tech/proof/position/4']
    # Out-of-range positions stay counted and are named, not dropped.
    assert any(not r['inRange'] for r in current_sample['locks'] if target in (r['token0'], r['token1']))


@pytest.mark.parametrize('version', ['v2', 'v3', 'v4'])
@pytest.mark.parametrize('hook_status', ['none', 'verified', 'safe', 'unknown'])
def test_versions_hooks_and_managers_do_not_drop_locked_positions(current_sample, version, hook_status):
    row = deepcopy(current_sample['locks'][0])
    row.update(version=version, hooks='0x' + '12' * 20, hookStatus=hook_status,
               positionManager='0x' + '34' * 20, inRange=False)
    if version == 'v4':
        row['pool'] = '0x' + '56' * 32
    current_sample['locks'] = [row]
    client, _, _ = wire(current_sample, target=row['token1'])
    assert 'HoodLock LP: 1 locked position in WETH/SHI' in client.lookup(row['token1']).line


@pytest.mark.parametrize('proof', [None, '', 'http://hoodlock.tech/proof/position/1',
    'https://hoodlock.tech.evil/proof/position/1', 'https://evil/hoodlock.tech/proof/position/1',
    'https://user@hoodlock.tech/proof/position/1', 'https://hoodlock.tech/api/v1/lp-locks',
    'https://hoodlock.tech/proof/position/', 'https://hoodlock.tech/proof/position/../1',
    'https://hoodlock.tech/proof/position/1?next=evil', 'https://hoodlock.tech/proof/position/1\n'])
def test_invalid_proof_omits_link_without_losing_lp_facts(current_sample, proof):
    current_sample['locks'] = [dict(current_sample['locks'][0], proof=proof)]
    target = current_sample['locks'][0]['token1']
    client, _, _ = wire(current_sample, target=target)
    assert client.lookup(target).line.splitlines()[-1] == (
        'HoodLock LP: 1 locked position in WETH/SHI, 1 of 1 in range holding 100% of active pool liquidity, first unlock 2027-10-05')


def test_first_unlock_proof_is_one_link_and_not_an_api_url(current_sample):
    target = current_sample['locks'][-1]['token1']
    client, _, _ = wire(current_sample, target=target)
    line = client.lookup(target).line.splitlines()[-1]
    assert line == ('HoodLock LP: 3 locked positions in WETH/LOCK, $0.01 locked, none in range, first unlock 2026-12-11'
                    ' · https://hoodlock.tech/proof/position/14')
    assert line.count('https://') == 1 and '/api/' not in line
    current_sample['locks'].reverse()
    client, _, _ = wire(current_sample, target=target)
    assert client.lookup(target).line.splitlines()[-1] == line


def test_lp_timeout_keeps_released_caveat_and_token_facts(current_sample):
    client, _, ticks = wire(current_sample, count=1)
    original = client._read
    def read(path, deadline, payload=None):
        if path == hoodlock.LP_PATH:
            ticks[0] = deadline
            raise TemporaryUnavailable()
        return original(path, deadline, payload)
    client._read = read
    reading = client.lookup(TOKEN)
    assert '1.00% of supply locked' in reading.line
    assert "LP locks in HoodLock's Liquidity Locker and other lockers not read" in reading.line
    assert 'HoodLock LP:' not in reading.line


def test_offline_cards_keep_captured_non_hoodlock_bytes():
    from demo.r6_replay import lp_cards
    cards = lp_cards()
    for symbol, name in [('HISS', 'card-lpbranch-hiss-1118.txt'), ('LOCK', 'cards-058-lock-usdg-1118.txt')]:
        original = (SAMPLE.parent / name).read_text().split('===== ')[1].split('\n', 1)[1]
        original = original.split('===== ')[0].rstrip() + '\n'
        original = original.replace('if Hermes swapped this now:', 'if Hermes Agent swapped this now:')
        unchanged = lambda text: [line for line in text.splitlines() if not line.startswith('HoodLock')]
        assert unchanged(cards[symbol]) == unchanged(original)
        assert 'LP locks in HoodLock' not in cards[symbol]
        assert 'HoodLock LP:' in cards[symbol]


def lp_line(sample, target):
    client, _, _ = wire(sample, target=target)
    return client.lookup(target).line.splitlines()[-1]


@pytest.mark.parametrize('status, note', [
    ('unverified', ', hook not reviewed by HoodLock and can act when liquidity is removed'),
    ('rugged', ', hook status not recognised'),
    (None, ', hook status not recognised'),
    ('none', ''), ('verified', ''), ('safe', '')])
def test_hook_status_from_hoodlock_is_shown_unless_quiet(current_sample, status, note):
    row = dict(current_sample['locks'][0], hookStatus=status)
    current_sample['locks'] = [row]
    line = lp_line(current_sample, row['token1'])
    assert line.startswith('HoodLock LP: 1 locked position in WETH/SHI')
    assert line.endswith('first unlock 2027-10-05' + note + ' · https://hoodlock.tech/proof/position/1')


def test_unverified_hook_in_one_position_marks_the_pool(current_sample):
    target = '0x444ce8ede8e38e1ec18aeec45d415c1d298a8bcf'
    rows = [r for r in current_sample['locks'] if r['symbol0'] == 'WETH' and r['symbol1'] == 'HISS']
    rows[-1]['hookStatus'] = 'unverified'
    client, _, _ = wire(current_sample, target=target)
    weth, usdg = client.lookup(target).line.splitlines()[1:]
    assert 'hook not reviewed by HoodLock' in weth and 'hook' not in usdg


@pytest.mark.parametrize('holder', ['0x' + '12' * 20, '', None])
def test_position_not_held_by_liquidity_locker_is_not_counted(current_sample, holder):
    row = current_sample['locks'][0]
    current_sample['locks'] = [dict(row, holder=holder), dict(row, lockId=99, holder=row['holder'].upper().replace('0X', '0x'))]
    line = lp_line(current_sample, row['token1'])
    assert line.startswith('HoodLock LP: 1 locked position in WETH/SHI')


def test_api_naming_another_lp_locker_drops_lp_lines_but_keeps_token_facts(current_sample):
    current_sample['contracts']['lpLocker'] = '0x' + '12' * 20
    client, _, _ = wire(current_sample, count=1)
    reading = client.lookup(TOKEN)
    assert '1.00% of supply locked' in reading.line and 'HoodLock LP:' not in reading.line


@pytest.mark.parametrize('liquidity, pool, text', [
    ('50', '200', ', 1 of 1 in range holding 25% of active pool liquidity'),
    ('1999', '2000', ', 1 of 1 in range holding 99.9% of active pool liquidity'),
    ('201', '200', ', 1 of 1 in range'),
    ('-1', '200', ', 1 of 1 in range'),
    ('50', 'x', ', 1 of 1 in range'),
    ('50', '0', ', 1 of 1 in range')])
def test_active_share_is_floored_and_omitted_when_inconsistent(current_sample, liquidity, pool, text):
    row = dict(current_sample['locks'][0], liquidity=liquidity, poolActiveLiquidity=pool)
    current_sample['locks'] = [row]
    assert text + ', first unlock' in lp_line(current_sample, row['token1'])


def test_missing_in_range_flags_omit_range_text(current_sample):
    row = dict(current_sample['locks'][0])
    del row['inRange']
    current_sample['locks'] = [row]
    line = lp_line(current_sample, row['token1'])
    assert 'in range' not in line and 'first unlock 2027-10-05' in line


def test_stale_api_snapshot_is_dated(current_sample):
    row = current_sample['locks'][0]
    client, _, _ = wire(current_sample, target=row['token1'])
    client.wall_clock = lambda: current_sample['timestamp'] + 7200
    line = client.lookup(row['token1']).line.splitlines()[-1]
    assert '; API data from 2026-11-18 10:25Z' in line
    assert 'incomplete' not in line


def test_proof_link_follows_earliest_unlock_not_position_order_or_size(current_sample):
    base = current_sample['locks'][0]
    now = current_sample['timestamp']
    rows = []
    for ident, days, usd in [(1, 900, 9000.0), (2, 700, 8000.0), (7, 30, 10.0), (5, 30, 20.0), (3, 400, 50.0)]:
        rows.append(dict(base, lockId=ident, unlockTime=now + days * 86400, valueUsd=usd,
                         proof=f'https://hoodlock.tech/proof/position/{ident}'))
    current_sample['locks'] = rows
    target = base['token1']
    client, _, _ = wire(current_sample, target=target)
    line = client.lookup(target).line.splitlines()[-1]
    until = datetime.fromtimestamp(now + 30 * 86400, timezone.utc).strftime('%Y-%m-%d')
    # earliest unlock wins over #1/#2 and the largest position; on a tie the larger lock links
    assert f'first unlock {until}' in line
    assert line.endswith(' · https://hoodlock.tech/proof/position/5')
    current_sample['locks'] = list(reversed(rows))
    client, _, _ = wire(current_sample, target=target)
    assert client.lookup(target).line.endswith(' · https://hoodlock.tech/proof/position/5')
