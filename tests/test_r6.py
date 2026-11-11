"""Offline r6 captures, bounded HTTP, dependency routing and header isolation."""
from contextlib import contextmanager
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import pytest

from rh_preflight import __version__
from rh_preflight.card import Card
from rh_preflight.hey import Client as Hey, Refused, TemporaryUnavailable
from rh_preflight.network import Network, MODULES, ORDER
from rh_preflight.settings import Settings
from rh_preflight.sources import sentinelhood, borro, hoodlock, chain
from rh_preflight.sources.common import Reading
from demo.r6_replay import capture, answer, PRIORS, NVDA, DELTA, cards

NEW = [sentinelhood, borro, hoodlock]


def wire(module, mode='present', clock=time.monotonic):
    client = module.Client(clock=clock)
    calls = []
    def read(path, deadline, payload=None):
        calls.append((path, payload, deadline))
        if mode == '402':
            raise Refused(402)
        if mode == 'timeout':
            raise TemporaryUnavailable()
        if mode == 'malformed':
            return 200, {'changed': True}
        if mode == 'absent':
            if module == sentinelhood:
                return 200, capture('sentinel-token-dead.json')
            if module == borro:
                return 200, {'chain': {'id': 4663}, 'stocks': []}
        return answer(module.__name__.split('.')[-1], path, payload)
    client._read = read
    # Isolate process identity cache for each mocked RPC client.
    if module == hoodlock:
        chain.VERIFIED.discard(client.base_url)
    return client, calls


def lookup(module, client, present=True):
    target = NVDA if module == borro else DELTA if module == hoodlock and present else PRIORS
    kw = {'stock_ticker': 'NVDA'} if module == borro else {}
    return client.lookup(target, **kw)


@pytest.mark.parametrize('module', NEW)
@pytest.mark.parametrize('mode', ['present', 'absent', 'malformed', '402', 'timeout'])
def test_source_modes(module, mode):
    client, calls = wire(module, mode)
    result = lookup(module, client, mode != 'absent')
    assert calls
    if mode == 'present':
        assert result.state == 'present', result
        assert result.line.endswith(' · ' + (client.page + '/?ca=' + PRIORS if module == sentinelhood else client.page))
    elif mode == '402':
        assert result.line == ''
    elif mode in ('timeout', 'malformed'):
        assert result.line == f'{client.label} could not be reached · {client.target_page(DELTA if module == hoodlock else PRIORS)}'
    else:
        assert any(word in result.line for word in ('no contract', 'has not seen', 'no lending market', 'holds no token lock or vesting'))


def test_capture_facts():
    s, _ = wire(sentinelhood)
    assert 'ERC-20 PRIORS, launched on pons by 0x9f05…ac0f, 6 launches' in s.lookup(PRIORS).line
    b, _ = wire(borro)
    line = lookup(borro, b).line
    assert 'lending fees 0.000300%/year before BORRO drip, short borrow fees 0.026200%/year' in line
    assert '$176.77 vs feed $176.59 at 2026-11-09T18:22:24Z' in line


def test_hoodlock_batches_and_scope():
    h, calls = wire(hoodlock)
    h.wall_clock = lambda: 1794248544
    row = h.lookup(DELTA)
    assert '10.00% of supply locked until 2027-01-31, 1 of 2 locks already withdrawn, 0 vesting schedules' in row.line
    assert "token locks and vesting only; LP locks in HoodLock's Liquidity Locker and other lockers not read" in row.line
    assert len(calls) == 3
    assert len(calls[1][1]) == 4 and len(calls[2][1]) == 2
    assert calls[1][1][1]['params'][0]['data'] == '0x18160ddd'
    assert all(call['params'][1] == '0x692c84f' for call in calls[2][1])
    h.wall_clock = lambda: 1900000000
    assert '0.00% of supply locked until no future unlock' in h.lookup(DELTA).line


def test_hoodlock_cap_and_malformed_rpc():
    h, _ = wire(hoodlock)
    original = h._read
    def read(path, deadline, payload=None):
        status, data = original(path, deadline, payload)
        if isinstance(payload, list) and len(payload) == 4:
            data[0]['result'] = '0x' + ''.join(format(n, '064x') for n in [32, 25, *range(25)])
        elif isinstance(payload, list):
            assert len(payload) == 20
            row = capture('hoodlock-rpc-delta-locks.json')['response'][0]
            data = [{**row, 'id': i+1} for i in range(20)]
        return status, data
    h._read = read
    assert 'first 20 of 25' in h.lookup(DELTA).line
    h, _ = wire(hoodlock)
    h._read = lambda *args: (200, [{'id': 1, 'result': '0x'}, {'id': 1, 'result': '0x'}])
    assert h.lookup(DELTA).state == 'unreachable'


def test_cache_is_sixty_seconds_and_collapses_concurrent_reads():
    now = [10.0]
    b, calls = wire(borro, clock=lambda: now[0])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: lookup(borro, b), range(4)))
    assert len(calls) == 2
    now[0] += 59
    lookup(borro, b)
    assert len(calls) == 2
    now[0] += 2
    lookup(borro, b)
    assert len(calls) == 4


@contextmanager
def local_server():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.answer()
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.answer()
        def answer(self):
            requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
            self.send_response(402)
            self.send_header('Content-Length', '11')
            self.end_headers()
            self.wfile.write(b'not JSON!!!')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('name', MODULES)
def test_every_source_stops_on_payment_and_sends_no_integration_or_payment_header(name):
    with local_server() as (origin, requests):
        client = MODULES[name].Client(base_url=origin)
        kw = {'stock_ticker': 'NVDA'} if name == 'borro' else {}
        result = client.lookup(PRIORS, **kw)
        assert result.line == ''
        assert len(requests) == 1
        path, headers = requests[0]
        assert 'x-hey-integration' not in headers
        assert not any('payment' in h or 'authorization' in h or 'api-key' in h for h in headers)
        assert not any(p in path for p in ('/report', '/api/x402', '/quote', '/buy', '/swap', '/verdict', '/pulse'))
        assert headers['user-agent'].startswith('rh-preflight/' + __version__)


def test_hey_rest_header_uses_package_version_and_exact_host_only():
    # Capture the Request at the transport boundary without opening a socket.
    for origin, expected in [('https://heyresearch.xyz', True), ('http://127.0.0.1:9', False)]:
        client = Hey(base_url=origin)
        seen = []
        def open_request(request, timeout):
            seen.append(request)
            raise OSError('offline')
        client.opener.open = open_request
        for path, payload in [('/api/agent/builder_status?address=' + PRIORS, None), ('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})]:
            with pytest.raises(TemporaryUnavailable):
                client._read(path, time.monotonic()+1, payload)
        for req in seen:
            headers = {k.lower(): v for k, v in req.header_items()}
            assert headers.get('x-hey-integration') == (f'quotum-hermes/{__version__}' if expected else None)


def test_dependencies_order_rpc_fallback_and_deadline():
    network = Network(timeout=.3)
    calls = []
    deployer = capture('sentinel-token-priors.json')['pons']['deployer']
    def lookup(name, target, deadline=None, **kw):
        calls.append((name, target, deadline, kw))
        if name == 'sentinelhood':
            return Reading('Sentinelhood', 'present', 'Sentinelhood: captured · https://sentinelhood.com', deployer=deployer)
        if name == 'toll':
            return Reading('Toll', 'present', 'Toll: captured · https://t0ll.xyz', stock_ticker='NVDA')
        if name == 'borro':
            assert kw['stock_ticker'] == 'NVDA'
        if name == 'chain':
            return Reading('Robinhood Chain', 'code', 'RPC fallback · https://robinhoodchain.blockscout.com')
        return Reading(MODULES[name].Client.label, 'present', name + ' · https://example.org')
    for name, client in network.clients.items():
        client.lookup = lambda target, name=name, **kw: lookup(name, target, **kw)
        client.prefetch = lambda deadline: None
    result = network.enrich([Card(PRIORS, 'identifier')], 'read', {}, Settings(), all_sources=True)[0]
    sources = result.data['sources']
    assert [ORDER.index(r.source) for r in sources] == sorted(ORDER.index(r.source) for r in sources)
    assert 'RPC fallback' not in result.render()
    assert len({row[2] for row in calls}) == 1
    assert all(row[1] == PRIORS for row in calls)
    network.clients['sentinelhood'].lookup = lambda *args, **kw: Reading('Sentinelhood', 'unreachable', 'Sentinelhood could not be reached · https://sentinelhood.com')
    result = network.enrich([Card(PRIORS, 'identifier')], 'read', {}, Settings(), all_sources=True)[0]
    assert 'RPC fallback' in result.render()


def test_borro_requires_toll_membership():
    network = Network()
    enabled = {'toll', 'borro'}
    settings = Settings(sources={name: 'off' for name in MODULES if name not in enabled})
    network.clients['toll'].lookup = lambda *a, **k: Reading('Toll', 'unlisted', '')
    called = []
    network.clients['borro'].lookup = lambda *a, **k: called.append(k) or Reading('Borro', 'present', 'Borro · https://borro.markets')
    network.enrich([Card(PRIORS, 'identifier')], 'read', {}, settings)
    assert not called
    network.clients['toll'].lookup = lambda *a, **k: Reading('Toll', 'present', 'Toll · https://t0ll.xyz', stock_ticker='NVDA')
    network.enrich([Card(NVDA, 'identifier')], 'read', {}, settings)
    assert called == [{'deadline': called[0]['deadline'], 'stock_ticker': 'NVDA'}]


def test_snapshot_disagreement_keeps_both_and_snapshot_cache():
    base = Card(DELTA, 'agent', {'payload': {'subject': {'project': {'slug': 'delta', 'url': 'https://heyresearch.xyz/project/delta'}}}})
    client = Hey()
    reads = []
    client._read = lambda path, deadline: reads.append(path) or (200, capture('hey-snapshot-delta.json'))
    enriched = client.attach_locks(base, time.monotonic()+1)
    client.attach_locks(base, time.monotonic()+1)
    assert reads == ['/api/projects/delta/snapshot']
    direct = Reading('HoodLock', 'present', 'HoodLock holds no token lock or vesting for this token · https://hoodlock.tech/lock-checker', token_lock=False)
    output = Network.annotate(enriched, [direct])
    assert len(output) == 2 and output[0].source == 'HEY locks'
    assert 'supplyPct 10' in output[0].line


def test_demo_priors_and_nvda_applicable_lines():
    priors, nvda = cards()
    output = priors.render()
    for name in ('Sentinelhood', 'HoodLock', 'Trnch', 'Agent Wormhole', 'DexScreener', 'GeckoTerminal'):
        assert name in output
    assert 'Borro:' not in output
    assert 'official Robinhood Stock Token NVDA' in nvda.render()
    assert 'Borro: NVDA lending market present' in nvda.render()


@pytest.mark.parametrize('module', [sentinelhood, borro, hoodlock])
def test_secondary_payment_stops_source_without_partial_facts(module):
    client, _ = wire(module)
    original = client._read
    def read(path, deadline, payload=None):
        second = ('action=deployer' in path or path == '/prices.json'
                  or isinstance(payload, list) and payload[0].get('params', [{}])[0].get('data', '').startswith('0xf4dadc61'))
        if second:
            raise Refused(402)
        return original(path, deadline, payload)
    client._read = read
    assert lookup(module, client).line == ''


def test_matching_hey_hoodlock_read():
    client, _ = wire(hoodlock)
    client.wall_clock = lambda: 1794248544
    own = client.lookup(DELTA)
    base = Card(DELTA, 'identifier', {'locks': capture('hey-snapshot-delta.json')['locks']})
    assert Network.annotate(base, [own]) == [own]
