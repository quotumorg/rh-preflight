"""Partner headers and bounded before-buy WAIT retries, without real sleeps."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from rh_preflight.card import Card
from rh_preflight.extract import tx_intent
from rh_preflight.hey import Unavailable
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.network import Network, MODULES
from rh_preflight.policy import evaluate
from rh_preflight.settings import Settings
from rh_preflight.sources import toll as module
from rh_preflight.sources.common import Reading
from rh_preflight.sources.toll import Client as Toll, parse_before_buy
from rh_preflight.transactions import local_card
from test_toll_0511 import A, B, CAPTURE, HISS, network_card, rpc, synthetic


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def wait(seconds):
    return rpc(f'Decision: WAIT\nToken: {A}\nCall pre_buy_check again in {seconds} s.')


def client(answers, durations=()):
    clock, calls = Clock(), []
    toll = Toll(clock=clock, sleep=clock.sleep)
    answers, durations = iter(answers), iter(durations)
    def read(path, deadline, payload=None):
        if path.startswith('/api/markets/stock/'):
            return 404, {}
        calls.append((deadline, payload, toll.request_headers(path)))
        clock.now += next(durations, 0)
        return 200, next(answers)
    toll._read_once = read
    return toll, clock, calls


@pytest.mark.parametrize('header,expected', [
    ('x-toll-pass', {'x-toll-pass': 'synthetic-u3-key'}),
    ('X-TOLL-PASS', {'x-toll-pass': 'synthetic-u3-key'}),
    ('Authorization', {'Authorization': 'Bearer synthetic-u3-key'}),
    ('aUtHoRiZaTiOn', {'Authorization': 'Bearer synthetic-u3-key'}),
    ('x-api-key', {'x-toll-pass': 'synthetic-u3-key'}),
    ('Host', {'x-toll-pass': 'synthetic-u3-key'}),
    ('bad\r\nheader', {'x-toll-pass': 'synthetic-u3-key'}),
    (None, {'x-toll-pass': 'synthetic-u3-key'}),
])
@pytest.mark.parametrize('route', ['/api/tx/check', '/mcp'])
def test_partner_header_forms_and_fallback_on_both_routes(monkeypatch, header, expected, route):
    monkeypatch.setattr(module, 'toll_key', lambda: 'synthetic-u3-key')
    toll, clock, calls = client([synthetic('ALLOW')])
    if route == '/api/tx/check':
        def read(path, deadline, payload=None):
            assert path == route
            calls.append((deadline, payload, toll.request_headers(path)))
            return 200, {'x-toll': {}}
        toll._read_once = read
        assert toll.check_tx({}, 104, header) is not None
    else:
        assert toll.before_buy(A, 104, header).decision == 'ALLOW'
    assert calls[0][2] == expected
    assert toll.request_headers('/mcp') == {}
    assert toll.request_headers('/api/markets/stock/' + A) == {}
    toll.tx_headers.key_header = header
    toll.base_url = 'http://127.0.0.1:1234'
    assert toll.request_headers(route) == {}
    assert clock.sleeps == []


@pytest.mark.parametrize('header', ['x-api-key', 'Host', 'bad\r\nheader', None])
def test_doctor_names_fallback_without_the_key_or_raw_header(monkeypatch, header):
    monkeypatch.setattr(module, 'toll_key', lambda: pytest.fail('doctor must not read the key'))
    settings = Settings(toll_key_header=header, sources={name: 'off' for name in MODULES})
    row = Network().doctor(settings)['toll']
    assert row['key_header'] == 'x-toll-pass'
    assert row['warning'] == 'toll_key_header is not supported by Toll; using x-toll-pass'
    assert len(row['warning'].splitlines()) == 1


def test_valid_authorization_doctor_and_context_setting():
    class Context:
        def get_config(self, name, default=None):
            return {'toll_key_header': 'AUTHORIZATION', 'toll_wait_max_seconds': 12,
                    'sources': {name: 'off' for name in MODULES}}.get(name, default)
    settings = Settings.from_context(Context())
    assert settings.toll_wait_max_seconds == 12
    row = Network().doctor(settings)['toll']
    assert row['key_header'] == 'Authorization' and 'warning' not in row


@pytest.mark.parametrize('value', [-1, 120.01, True, '30', None, float('nan'), float('inf')])
def test_wait_ceiling_validation(value):
    with pytest.raises(ValueError, match='toll_wait_max_seconds'):
        Settings(toll_wait_max_seconds=value)


def test_wait_ceiling_defaults_and_bounds():
    assert Settings().toll_wait_max_seconds == 30
    assert Settings(toll_wait_max_seconds=0).toll_wait_max_seconds == 0
    assert Settings(toll_wait_max_seconds=120).toll_wait_max_seconds == 120


def test_maximum_ceiling_and_two_waits_then_final_word():
    toll, clock, calls = client([wait(60), wait(60), synthetic('WARN')])
    reading = toll.before_buy(A, 104, wait_max_seconds=Settings(toll_wait_max_seconds=120).toll_wait_max_seconds)
    assert reading.decision == 'WARN' and reading.line.endswith('after 2 waits totalling 120 s')
    assert clock.sleeps == [60, 60] and len(calls) == 3 and clock.now == 220
    assert [row[0] for row in calls] == [104, 224, 224]


def test_wait_then_allow_retries_with_fresh_budget_and_caches_final(monkeypatch):
    monkeypatch.setattr(module, 'toll_key', lambda: 'synthetic-u3-key')
    toll, clock, calls = client([wait(12), synthetic('ALLOW')], durations=[3, 1])
    reading = toll.buy_check(A, 104, 'authorization')
    assert reading.decision == 'ALLOW'
    assert reading.line.endswith('after one wait of 12 s')
    assert clock.sleeps == [12] and clock.now == 116
    assert [row[0] for row in calls] == [104, 134]
    assert [row[1]['params']['arguments']['budget_ms'] for row in calls] == [3600, 18600]
    assert [row[2] for row in calls] == [{'Authorization': 'Bearer synthetic-u3-key'}] * 2
    assert toll.buy_check(A, 120) == reading and len(calls) == 2
    assert toll.inflight._entries == {}


@pytest.mark.parametrize('answers,ceiling,sleeps,seconds', [
    ([wait(31)], 30, [], 31),
    ([wait(12), wait(19)], 30, [12], 19),
    ([wait(12), wait(18), wait(1)], 30, [12, 18], 1),
    ([wait(0), wait(0), wait(0)], 30, [0, 0], 0),
])
def test_remaining_ceiling_and_three_call_cap_keep_final_wait_line(answers, ceiling, sleeps, seconds):
    toll, clock, calls = client(answers)
    reading = toll.before_buy(A, 104, wait_max_seconds=ceiling)
    baseline = parse_before_buy(wait(seconds), A).line
    assert reading.decision == 'WAIT'
    assert reading.line.split(' · after ')[0] == baseline
    assert clock.sleeps == sleeps and len(calls) == len(sleeps) + 1 <= 3
    assert not getattr(toll, '_before_buys', {})


def test_retry_transport_time_cannot_move_absolute_ceiling():
    toll, clock, calls = client([wait(12), wait(10)], durations=[3, 10])
    reading = toll.before_buy(A, 104)
    assert reading.decision == 'WAIT' and 'ask again in 10 s' in reading.line
    # Ten requested seconds fit the sleep allowance, but not the absolute end.
    assert clock.now == 125 and clock.sleeps == [12] and len(calls) == 2
    assert calls[-1][0] == 134


def test_late_retry_is_silent_and_not_cached():
    toll, clock, calls = client([wait(12), synthetic('ALLOW')], durations=[3, 19])
    assert toll.before_buy(A, 104).line == ''
    assert clock.now == 134 and len(calls) == 2
    assert not getattr(toll, '_before_buys', {}) and toll.inflight._entries == {}


@pytest.mark.parametrize('body', [
    f'Decision: WAIT\nToken: {A}',
    f'Decision: WAIT\nToken: {A}\nCall pre_buy_check again in -1 s.',
    f'Decision: WAIT\nToken: {A}\nCall pre_buy_check again in 1 s.\nCall pre_buy_check again in 2 s.',
])
def test_wait_without_one_valid_seconds_value_is_not_polled(body):
    toll, clock, calls = client([rpc(body)])
    assert toll.before_buy(A, 104).line == 'Toll before buy: WAIT · still reading'
    assert len(calls) == 1 and clock.sleeps == []


def test_zero_ceiling_preserves_captured_wait_bytes_and_does_not_cache():
    response = CAPTURE[HISS]['response']
    toll, clock, calls = client([response, response])
    baseline = parse_before_buy(response, HISS)
    for _ in range(2):
        assert toll.before_buy(HISS, 104, wait_max_seconds=0).line.encode() == baseline.line.encode()
    assert len(calls) == 2 and clock.sleeps == [] and not getattr(toll, '_before_buys', {})


@pytest.mark.parametrize('final', ['ALLOW', 'WAIT', 'failure'])
def test_concurrent_callers_share_one_retry_loop_or_stay_silent(final):
    last = synthetic('ALLOW') if final == 'ALLOW' else wait(31)
    toll, clock, calls = client([wait(12), last])
    sleeping, release, joined = (threading.Event() for _ in range(3))
    acquire = toll.inflight.acquire
    def tracked(key, *, timeout):
        if timeout > 0:
            joined.set()
        return acquire(key, timeout=timeout)
    toll.inflight.acquire = tracked
    def sleep(seconds):
        sleeping.set()
        assert release.wait(3), 'test did not release the fake sleep'
        if final == 'failure':
            raise Unavailable()
        clock.sleep(seconds)
    toll.sleep = sleep
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(toll.before_buy, A, 104)
        try:
            assert sleeping.wait(3)
            follower = pool.submit(toll.before_buy, A, 104)
            assert joined.wait(3)
        finally:
            release.set()
        first, second = owner.result(timeout=3), follower.result(timeout=3)
    if final == 'ALLOW':
        assert first == second and first.decision == 'ALLOW'
    else:
        assert second.line == ''
        assert ('WAIT' in first.line) == (final == 'WAIT')
    assert len(calls) == (1 if final == 'failure' else 2)
    assert toll.inflight._entries == {}


def test_toll_block_after_wait_does_not_change_the_decision():
    toll, clock, calls = client([wait(1), synthetic('BLOCK')])
    reading = toll.before_buy(A, 104)
    assert reading.decision == 'BLOCK' and clock.sleeps == [1]
    for unattended in (False, True):
        for mode in ('ask_when', 'ask', 'note'):
            for allow in (False, True):
                settings = Settings(mode=mode, allow_unattended=allow)
                assert evaluate([network_card(reading)], settings, unattended=unattended) == evaluate(
                    [network_card(Reading('Toll', 'unlisted', ''))], settings, unattended=unattended)


def test_direct_card_retries_only_toll_under_extended_deadline(tmp_path, monkeypatch, controlled_budget):
    toll, clock, calls = client([wait(12), synthetic('ALLOW')])
    settings = Settings(timeout=4, toll_wait_max_seconds=12,
                        sources={name: 'on' if name in {'toll', 'priors'} else 'off' for name in MODULES})
    service = Service(settings, ledger=Ledger(tmp_path))
    service.network.clients['toll'] = toll
    service.client.lookup = lambda *args, **kwargs: Card(A, 'identifier')
    controlled_budget(lambda *args: False)
    monkeypatch.setattr(time, 'monotonic', clock)
    deadlines = []
    def priors(*args, **kwargs):
        deadlines.append(kwargs['deadline'])
        return Reading('Priors', 'unlisted', '')
    service.network.clients['priors'].lookup = priors
    card = service.card(A)
    assert 'Toll before buy: ALLOW' in card.render() and 'after one wait of 12 s' in card.render()
    assert deadlines == [104] and [row[0] for row in calls] == [104, 116]


def test_borro_membership_finishes_while_toll_waits(monkeypatch, controlled_budget):
    from rh_preflight import network as network_module
    toll, clock, calls = client([wait(12), synthetic('ALLOW')])
    sleeping, release = threading.Event(), threading.Event()
    def sleep(seconds):
        sleeping.set()
        assert release.wait(3), 'Borro membership was held behind WAIT'
        clock.sleep(seconds)
    toll.sleep = sleep
    network = Network()
    network.clients['toll'] = toll
    settings = Settings(sources={name: 'on' if name in {'toll', 'borro'} else 'off' for name in MODULES})
    controlled_budget(lambda *args: False)
    monkeypatch.setattr(time, 'monotonic', clock)
    collected = []
    def collect(future, deadline, fallback):
        if future.test_job[0] == 'stock_read':
            assert sleeping.wait(3)
            reading = future.result(timeout=3)
            assert reading.source == 'Borro' and reading.line == '' and not reading.asked
            collected.append(deadline)
            release.set()
            return reading
        return future.result(timeout=3)
    monkeypatch.setattr(network_module, 'budget_result', collect)
    try:
        card = network.enrich([Card(A, 'identifier')], 'read', {}, settings,
                              all_sources=True, deadline=104)[0]
    finally:
        release.set()
    assert collected == [103.995]
    assert 'Toll before buy: ALLOW' in card.render() and 'Borro could not be reached' not in card.render()
    assert len(calls) == 2 and clock.sleeps == [12]


@pytest.mark.parametrize('unattended', [False, True])
def test_hook_keeps_final_word_when_wait_crosses_original_budget(tmp_path, monkeypatch, controlled_budget, unattended):
    toll, clock, calls = client([wait(12), synthetic('BLOCK')])
    settings = Settings(timeout=4, toll_wait_max_seconds=12,
                        sources={name: 'on' if name == 'toll' else 'off' for name in MODULES})
    service = Service(settings, ledger=Ledger(tmp_path), presence=lambda: unattended, clock=clock)
    service.network.clients['toll'] = toll
    service.client.lookup = lambda *args, **kwargs: Card(A, 'identifier')
    controlled_budget(lambda *args: False, service)
    monkeypatch.setattr(time, 'monotonic', clock)
    toll.clock = clock
    # A shape-only swap has no simulation payload; only before-buy can extend.
    args = {'chainId': 4663, 'tokenIn': B, 'tokenOut': A, 'amount': '1'}
    service.pre_tool_call('swap', args)
    output = service.ledger.rows()[0]['cards'][0]['card']
    assert 'Toll before buy: BLOCK' in output and 'after one wait of 12 s' in output
    assert clock.sleeps == [12] and len(calls) == 2
    assert calls[0][0] == pytest.approx(103.98) and calls[1][0] == pytest.approx(115.98)


@pytest.mark.parametrize('retried', [False, True])
@pytest.mark.parametrize('unattended', [False, True])
def test_dispatcher_extends_only_when_toll_started_a_wait(tmp_path, retried, unattended):
    args = {'chainId': 4663, 'tokenIn': B, 'tokenOut': A, 'amount': '1'}
    clock, submitted, waits = Clock(), [], []
    class Executor:
        def submit(self, fn, *args, **kwargs):
            future = {'tx': fn.__name__ == '_tx_cards', 'args': args}
            submitted.append(future)
            return future
    def wait_for(future, deadline, fallback):
        waits.append(deadline)
        at = 110 if future['tx'] else 104
        if future['tx']:
            if retried:
                future['args'][-1].set()
        clock.now = max(clock.now, min(at, deadline))
        if at > deadline:
            return fallback
        if future['tx']:
            card = local_card(tx_intent(args)[0])
            card.data['lines'].append(parse_before_buy(synthetic('BLOCK'), A).line)
            return [card]
        return [Card(A, 'identifier')]
    service = Service(Settings(toll_wait_max_seconds=12), ledger=Ledger(tmp_path),
                      presence=lambda: unattended, clock=clock, executor=Executor(), wait=wait_for)
    service.pre_tool_call('swap', args)
    assert waits == ([104, 104, 116] if retried else [104, 104])
    assert submitted[1]['args'][3] == pytest.approx(103.98)
    assert ('Toll before buy: BLOCK' in service.ledger.rows()[0]['cards'][0]['card']) == retried
