"""r7 regressions from saved live cards; only captures and loopback HTTP."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from demo.captures import capture_path
import threading
import time

import pytest

from rh_preflight.card import Card, link, text
from rh_preflight.hey import Unavailable, Refused
from rh_preflight.network import MODULES, Network
from rh_preflight.settings import Settings
from rh_preflight.sources import hookr, geckoterminal, sentinelhood, toll, priors, sherwood, hoodlock
from rh_preflight.sources.common import Reading, field, plural
from demo.capture_replay import capture as r5
from demo.r6_replay import PRIORS, DELTA, capture as r6
from test_r6 import wire

POOL = '0x' + 'ab' * 32
OTHER = '0x' + '12' * 20


def captured(name):
    return json.JSONDecoder().raw_decode(capture_path(name).read_text())[0]


@pytest.mark.parametrize('raw,expected', [
    ('q' * 90, 'q' * 80 + '…'),
    ('a ' + 'b' * 79, 'a…'),
    ('a' * 80 + ' rest', 'a' * 80 + '…'),
    ('short text', 'short text'),
    ('\x00hello\u200b world\n', 'hello world'),
    ('a tokenized stock or ETF issued by Robinhood: a market, not a launch; no deployer history',
     'a tokenized stock or ETF issued by Robinhood: a market, not a launch; no…'),
])
def test_word_boundary_and_hard_cut(raw, expected):
    assert field(raw) == expected


@pytest.mark.parametrize('noun', ['launch', 'pool', 'lock', 'schedule', 'ship', 'loan', 'agent', 'proposal'])
@pytest.mark.parametrize('count', [0, 1, 2, '1'])
def test_count_inflection(noun, count):
    assert plural(count, noun) == (noun if str(count) == '1' else noun + ('es' if noun == 'launch' else 's'))


def test_projected_singular_counts():
    data = r6('sentinel-token-priors.json')
    history = r6('sentinel-deployer-priors.json')
    history['launches'] = 1
    client = sentinelhood.Client()
    client._read = lambda path, *args: (200, history if 'action=deployer' in path else data)
    assert '1 launch by that deployer' in client.lookup(PRIORS).line
    client = priors.Client()
    assert '1 loan repaid' in client.parse({'agents': [{'record': {'loansRepaid': 1}}]}).line
    client = sherwood.Client()
    row = client.parse({}, {'info': {}, 'agents': [{}], 'proposals': [{'stateName': 'Open'}]})
    assert 'open proposal 1' in row.line and '1 agent' in row.line
    assert 'Ship in 30 days: 1' in Card(PRIORS, 'token', {'meaningful_ships_30d': 1}).render()
    client, _ = wire(hoodlock)
    # Keep the captured lock row and reduce both returned identifier lists.
    original = client._read
    def read(path, deadline, payload=None):
        status, data = original(path, deadline, payload)
        if isinstance(payload, list) and payload[0].get('method') == 'eth_call' and len(payload) == 4:
            data = deepcopy(data)
            for index in (0, 2):
                # ABI dynamic array: offset, length, first id.
                data[index]['result'] = '0x' + f'{32:064x}{1:064x}{101:064x}'
        return status, data
    client._read = read
    client.wall_clock = lambda: 1794248544
    line = client.lookup(DELTA).line
    assert '1 lock already withdrawn, 1 vesting schedule' in line


def test_sentinel_non_token_wording():
    client = sentinelhood.Client()
    client._read = lambda *args: (200, {'chainId': 4663, 'contract': True, 'erc20': False, 'block': 42})
    assert client.lookup(PRIORS).line == ('Sentinelhood: contract with no ERC-20 metadata, block 42 · https://sentinelhood.com/?ca=' + PRIORS)


def test_hookr_root_and_integration_capture():
    client = hookr.Client()
    catalog = captured('hookr-hooks-catalog.json')
    integrations = captured('hookr-hooks-integrations.json')
    pools, hooks = client.index(catalog, integrations, captured('hookr-discover-pools.json'))
    line = client.parse(catalog['root']['address'], pools, hooks).line
    assert line == "Hookr: this is the Hookr shared root hook, listed in Hookr's hook catalog · https://hookr.fun/hooks"
    assert catalog['scope'] not in line
    # The saved integrations have no deployments. Add one explicit synthetic
    # deployment to exercise the captured integration name and summary.
    item = integrations['integrations'][0]
    item['deployments'] = [{'chainId': 4663, 'address': OTHER}]
    pools, hooks = client.index(catalog, integrations, captured('hookr-discover-pools.json'))
    assert client.parse(OTHER, pools, hooks).line == f"Hookr: hook {field(item['name'])} listed in Hookr's integrations ({field(item['summary'])}) · https://hookr.fun/hooks"
    assert client.parse('0x' + '34' * 20, pools, hooks).line == ''
    assert client.parse('0x' + 'fe' * 32, pools, hooks).line == ''


@pytest.mark.parametrize('status', [403, 500])
@pytest.mark.parametrize('name,suffix', [
    ('geckoterminal', '/' + PRIORS), ('dexscreener', '/' + PRIORS),
    ('wormhole', '/t/4663/' + PRIORS), ('chain', '/address/' + PRIORS),
    ('sentinelhood', '/?ca=' + PRIORS),
    ('sherwood', '/api/v1/vault/' + PRIORS), ('toll', '/markets/' + PRIORS),
])
def test_failure_links_are_address_specific(name, suffix, status):
    client = MODULES[name].Client()
    def fail(*args, **kw):
        raise Refused(status) if status == 403 else Unavailable()
    client._read = fail
    assert client.lookup(PRIORS).line.endswith(' · ' + client.page + suffix)


def test_gecko_429_retry_after_and_deadline(server):
    origin, state = server
    path = '/api/v2/networks/robinhood/tokens/' + PRIORS
    state['overrides'][path] = (429, b'limit exceeded', {'Retry-After': '120'})
    now = [time.monotonic()]
    client = geckoterminal.Client(origin, clock=lambda: now[0])
    row = client.lookup(PRIORS, deadline=now[0] + 1)
    assert row.state == 'rate_limited'
    assert row.line == 'GeckoTerminal rate-limited this read · ' + client.page + '/' + PRIORS
    assert client.stats()['retry_after_seconds'] == 120
    # Different targets share cooldown; there is no retry within this card.
    assert 'rate-limited' in client.lookup(OTHER).line
    assert len(state['requests']) == 1
    now[0] += 121
    state['overrides'][path] = (200, r5('gecko-token-priors.json'), {})
    assert client.lookup(PRIORS).state == 'present'
    assert len(state['requests']) == 2


@pytest.mark.parametrize('first', ['prefetch', 'lookup'])
@pytest.mark.parametrize('failed', [False, True])
def test_hookr_snapshot_is_order_independent(first, failed):
    client = hookr.Client()
    entered, release = threading.Event(), threading.Event()
    calls = []
    answers = {'/api/hooks/catalog': captured('hookr-hooks-catalog.json'),
               '/api/hooks/integrations': captured('hookr-hooks-integrations.json'),
               '/api/discover/pools': captured('hookr-discover-pools.json')}
    def read(path, deadline, payload=None):
        calls.append(path)
        if path == client.probe_path:
            entered.set()
            assert release.wait(1)
        if failed:
            raise Unavailable()
        return 200, answers[path]
    client._read = read
    end = time.monotonic() + 2
    with ThreadPoolExecutor(2) as pool:
        snapshot = pool.submit(client.prefetch, end) if first == 'prefetch' else None
        def lookup():
            entered.set()
            assert release.wait(1)
            return [client.lookup(t, deadline=end, index_result=snapshot.result()) for t in (POOL, PRIORS)]
        pending = pool.submit(lookup)
        assert entered.wait(1)
        if snapshot is None:
            snapshot = pool.submit(client.prefetch, end)
        release.set()
        readings = pending.result()
    assert [r.state for r in readings] == (['unreachable'] * 2 if failed else ['unlisted'] * 2)
    assert calls.count(client.probe_path) == 1
    assert not client.hook_index if failed else client.index_stamp is not None


@pytest.mark.parametrize('failed', [False, True])
@pytest.mark.parametrize('pool_first', [False, True])
def test_network_hookr_pools_targets_one_index_one_line(failed, pool_first):
    network = Network(timeout=1)
    hooks = network.clients['hookr']
    calls = []
    def read(path, deadline, payload=None):
        calls.append(path)
        if failed:
            raise Unavailable()
        return 200, {'/api/hooks/catalog': captured('hookr-hooks-catalog.json'),
                     '/api/hooks/integrations': captured('hookr-hooks-integrations.json'),
                     '/api/discover/pools': captured('hookr-discover-pools.json')}[path]
    hooks._read = read
    original = hooks.lookup
    arrived = threading.Event()
    def ordered(target, **kw):
        if target:
            if (len(target) == 66) == pool_first:
                arrived.set()
            else:
                assert arrived.wait(1)
        return original(target, **kw)
    hooks.lookup = ordered
    settings = Settings(sources={name: 'on' if name == 'hookr' else 'off' for name in MODULES})
    output = network.enrich([Card(PRIORS, 'identifier')], 'swap', {'poolId': POOL}, settings)[0].render()
    assert output.count('Hookr could not be reached') == int(failed)
    assert ('Hookr could not be reached' in output) == failed
    assert calls.count(hooks.probe_path) == 1


def test_toll_agent_capture_path_and_shared_deadline():
    network = Network(timeout=.5)
    target = '0x33d6112f2f3bcd2a476bd9c13208e4c632be2d0a'
    calls = []
    network.clients['priors']._read = lambda *args: (200, {'agents': [{'agentId': 7622, 'record': {}}]})
    def read(path, deadline, payload=None):
        calls.append((path, deadline, payload))
        if payload:
            return 200, r5('toll-check-agent-7622.json')
        return 404, {}
    network.clients['toll']._read = read
    settings = Settings(sources={name: 'on' if name in ('priors', 'toll') else 'off' for name in MODULES})
    end = time.monotonic() + .5
    output = network.enrich([Card(target, 'identifier')], 'pay', {}, settings, deadline=end)[0].render()
    assert 'Toll: card loads; MCP did not answer; x402 answered HTTP 402. payTo matches registered agentWallet' in output
    assert output.count('Toll:') == 1
    assert all(deadline <= end for _, deadline, _ in calls)
    mcp = next(payload for path, _, payload in calls if path == '/mcp')
    assert mcp['params'] == {'name': 'check_agent', 'arguments': {'id': 7622}}
    assert 'utm_' not in output


@pytest.mark.parametrize('url,expected', [
    ('https://t0ll.xyz/api/hook/' + OTHER + '?format=text&utm_source=mcp', 'https://t0ll.xyz/api/hook/' + OTHER),
    ('https://t0ll.xyz/agents/census?agent=7622&utm_source=mcp', 'https://t0ll.xyz/agents/census?agent=7622'),
    ('https://example.org/path?UTM_MEDIUM=x&keep=y&FORMAT=text#part', 'https://example.org/path?keep=y#part'),
])
def test_link_cleanup_any_source(url, expected):
    assert link(url) == expected
    assert text('Source: ' + url) == 'Source: ' + expected


def test_toll_hook_captured_link_cleanup():
    line = toll.Client().parse(r5('toll-check-hook.json'), 'hook').line
    assert '1 pool on this hook' in line
    assert line.endswith(' · https://t0ll.xyz/api/hook/0x987e330f00b79864a07fe81dfac344d0078abcc0')


def test_hookr_stale_index_does_not_hide_failed_refresh():
    now = [1.0]
    client = hookr.Client(clock=lambda: now[0])
    answers = {'/api/hooks/catalog': captured('hookr-hooks-catalog.json'),
               '/api/hooks/integrations': captured('hookr-hooks-integrations.json'),
               '/api/discover/pools': captured('hookr-discover-pools.json')}
    client._read = lambda path, *args: (200, answers[path])
    old = client.prefetch(deadline=2)
    assert client.lookup(PRIORS, index_result=old).line == ''
    old_stamp = client.index_stamp
    now[0] += 61
    from rh_preflight.hey import TemporaryUnavailable
    calls = []
    def fail(path, *args):
        calls.append(path)
        raise TemporaryUnavailable()
    client._read = fail
    failure = client.prefetch(deadline=64)
    assert client.lookup(PRIORS, index_result=failure).state == 'unreachable'
    assert client.index_stamp == old_stamp
    assert calls == [client.probe_path]
    # The earlier immutable snapshot remains usable on its own card.
    assert client.lookup(PRIORS, index_result=old).line == ''


def test_listed_pool_and_hook_produce_one_hookr_line():
    network = Network()
    catalog = captured('hookr-hooks-catalog.json')
    root = catalog['root']['address']
    answers = {'/api/hooks/catalog': catalog,
               '/api/hooks/integrations': captured('hookr-hooks-integrations.json'),
               '/api/discover/pools': {'pools': [{'poolId': POOL, 'chainId': 4663, 'hook': {'address': root}}]}}
    network.clients['hookr']._read = lambda path, *args: (200, answers[path])
    settings = Settings(sources={name: 'on' if name == 'hookr' else 'off' for name in MODULES})
    output = network.enrich([Card(root, 'identifier')], 'swap', {'poolId': POOL}, settings)[0].render()
    assert output.count('Hookr:') == 1
    assert 'uses hook Hookr shared root' in output


def test_toll_market_tracking_cleanup_keeps_path():
    assert toll.Client().public_page('https://t0ll.xyz/markets/NVDA?utm_source=mcp&format=text') == 'https://t0ll.xyz/markets/NVDA'
