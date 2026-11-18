"""Captured 2026-11-19 before-buy words, strict projection and unchanged rules."""
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import time
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from rh_preflight.card import Card, source_line
from rh_preflight.extract import tx_intent
from rh_preflight.hey import Unavailable, TemporaryUnavailable
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.network import Network, MODULES
from rh_preflight.policy import evaluate
from rh_preflight.presentation import present
from rh_preflight.settings import Settings
from rh_preflight.sources.common import Reading
from rh_preflight.sources.toll import Client as Toll, parse_before_buy
from rh_preflight.transactions import project, local_card, bought_address

FIXTURES = Path(__file__).parent / 'fixtures'
CAPTURE = json.loads((FIXTURES / 'toll/pre-buy-check-2026-11-19.json').read_text())
SECOND = json.loads((FIXTURES / 'toll/pre-buy-check-2026-11-19-r2.json').read_text())
HISS = '0x444ce8ede8e38e1ec18aeec45d415c1d298a8bcf'
OFFROUTER = '0x47b537f7c2c2183967fe5ba883291b8ada490db6'
A, B = ('0x' + c * 40 for c in 'ab')


def rpc(body):
    return {'result': {'content': [{'type': 'text', 'text': body}]}}


def synthetic(word='BLOCK', reason='synthetic finding against this coin'):
    return rpc(f'Decision: {word}\nToken: {A}\nReasons:\n- {reason}\nFull report: https://t0ll.xyz/check/{A}?utm_source=mcp')


def stub(answer, clock=None):
    toll, calls = Toll(**({'clock': clock} if clock else {})), []
    def read(path, deadline, payload=None):
        calls.append((path, payload))
        if path.startswith('/api/markets/stock/'):
            return 404, {}
        return 200, deepcopy(answer)
    toll._read_once = read
    return toll, calls


def network_card(reading):
    return Card(A, 'network', {'base': Card(A, 'identifier'), 'sources': [reading]})


@pytest.mark.parametrize('capture', [CAPTURE, SECOND])
def test_all_captured_answers_render_only_validated_fields(capture):
    for address, row in capture.items():
        reading = parse_before_buy(row['response'], address)
        rendered = network_card(reading).render()
        assert reading.line in rendered
        assert 'Toll before buy: ' in rendered
        for omitted in ('Next:', 'Telegram', 'watch_coin', 't.me', 'utm_source', 'First look', 'DexScreener'):
            assert omitted not in rendered
    if OFFROUTER in capture:
        assert '+1 more' in parse_before_buy(capture[OFFROUTER]['response'], OFFROUTER).line
        assert 'from a read 9 s ago' in parse_before_buy(capture[OFFROUTER]['response'], OFFROUTER).line


@pytest.mark.parametrize('word', ['BLOCK', 'WARN', 'ALLOW', 'WAIT'])
def test_each_word_and_approval_parity(word):
    data = synthetic(word)
    if word == 'WAIT':
        data['result']['content'][0]['text'] += '\nCall pre_buy_check again in 23 s.'
    reading = parse_before_buy(data, A)
    card = network_card(reading)
    assert f'Toll before buy: {word}' in card.render()
    if word == 'ALLOW':
        assert 'nothing against was found in what was read' in card.render()
    if word == 'WAIT':
        assert 'still reading, ask again in 23 s' in card.render()
    for mode in ('ask_when', 'ask', 'note'):
        for unchecked in ('ask', 'stop', 'note'):
            settings = Settings(mode=mode, on_unchecked=unchecked)
            assert evaluate([card], settings) == evaluate([network_card(Reading('Toll', 'unlisted', ''))], settings)


@pytest.mark.parametrize('mutate', [
    lambda s: s.replace('Token: ' + A, 'Token: ' + B),
    lambda s: s + '\nDecision: ALLOW',
    lambda s: s.replace('Decision: BLOCK', 'Decision: block'),
    lambda s: s.replace('Decision: BLOCK', 'Decision: BLOCKED'),
    lambda s: s.replace('synthetic finding', '\x1b[31msynthetic finding'),
    lambda s: s.replace('synthetic finding', '\u200bsynthetic finding'),
    lambda s: s.replace('synthetic finding', '\rsynthetic finding'),
    lambda s: s.replace('synthetic finding', 'https://evil.example/finding'),
    lambda s: s.replace('https://t0ll.xyz/check/' + A, 'https://evil.example/check/' + A),
    lambda s: s.replace('https://t0ll.xyz/check/' + A, 'https://t0ll.xyz/check/' + B),
    lambda s: s + '\nToken: ' + A,
    lambda s: s.replace('https://t0ll.xyz/check/', 'https://t0ll.xyz@evil.example/check/'),
    lambda s: s + '\nMade from our last whole read, yesterday',
])
def test_malformed_or_injected_answer_is_silent(mutate):
    data = synthetic()
    data['result']['content'][0]['text'] = mutate(data['result']['content'][0]['text'])
    toll, _ = stub(data)
    assert toll.buy_check(A, time.monotonic() + 5).line == ''


@pytest.mark.parametrize('answer', [{}, {'result': {'isError': True}}, {'result': {'content': None}},
                                   {'result': {'content': [{'type': 'text', 'text': None}]}},
                                   {'result': {'content': ['bad']}}])
def test_absent_or_iserror_is_silent(answer):
    toll, _ = stub(answer)
    assert toll.buy_check(A, time.monotonic() + 5).line == ''


@pytest.mark.parametrize('left,expected', [(4, 3600), (.1, 300), (100, 25000)])
def test_budget_clamped_from_deadline(left, expected):
    toll, calls = stub(synthetic(), clock=lambda: 10)
    assert toll.buy_check(A, 10 + left).line
    arguments = calls[-1][1]['params']['arguments']
    assert arguments == {'address': A, 'budget_ms': expected}


def test_expired_deadline_does_not_call():
    toll, calls = stub(synthetic(), clock=lambda: 10)
    assert toll.before_buy(A, 10).line == ''
    assert calls == []


def test_whole_cache_is_independent_case_insensitive_bounded_and_expires():
    ticks = [10]
    toll, calls = stub(synthetic(), clock=lambda: ticks[0])
    first = toll.buy_check(A, 20)
    assert toll.buy_check(A.upper().replace('0X', '0x'), 20) == first
    assert len([p for p, body in calls if p == '/mcp']) == 1
    ticks[0] += 601
    toll.buy_check(A, 620)
    assert len([p for p, body in calls if p == '/mcp']) == 2
    # Keep the two method caches separate.
    assert '_coins' not in toll.__dict__
    toll._before_buys = {str(i): (ticks[0], first) for i in range(512)}
    toll.buy_check(A, 620)
    assert len(toll._before_buys) == 512


def test_wait_has_one_call_each_time_and_is_never_cached():
    toll, calls = stub(CAPTURE[HISS]['response'])
    for _ in range(2):
        assert 'WAIT' in toll.buy_check(HISS, time.monotonic() + 5).line
    assert len([p for p, body in calls if p == '/mcp']) == 2
    assert not getattr(toll, '_before_buys', {})
    assert all(key[0] != '/mcp' for key in toll.cache)


@pytest.mark.parametrize('error', [Unavailable, TemporaryUnavailable])
def test_timeout_or_5xx_one_attempt_silent(error):
    toll = Toll()
    calls = []
    def fail(path, deadline, payload=None):
        calls.append(path)
        raise error()
    toll._read_once = fail
    assert toll.before_buy(A, time.monotonic() + 5).line == ''
    assert calls == ['/mcp']


def test_real_transport_429_cooldown_and_key(monkeypatch):
    from rh_preflight.sources import toll as module
    monkeypatch.setattr(module, 'toll_key', lambda: 'synthetic-test-key')
    ticks, seen = [10], []
    toll = Toll(clock=lambda: ticks[0])
    def open(request, timeout):
        seen.append(request)
        raise HTTPError(request.full_url, 429, 'limited', {'Retry-After': '60'}, BytesIO(b'{}'))
    toll.opener = SimpleNamespace(open=open)
    assert toll.before_buy(A, 15).line == ''
    assert toll.before_buy(A, 15).line == ''
    assert len(seen) == 1
    assert seen[0].get_header('X-toll-pass') == 'synthetic-test-key'
    assert toll.cooldown == 70
    assert toll.request_headers('/mcp') == {}
    toll.base_url = 'http://127.0.0.1:1'
    toll.tx_headers.key_header = 'x-toll-pass'
    assert toll.request_headers('/mcp') == {}


def test_stock_or_unestablished_membership_never_calls_mcp():
    for status, answer in [(200, {}), (200, {'address': B}), (429, {})]:
        toll, calls = Toll(), []
        def read(path, deadline, payload=None):
            calls.append(path)
            return status, answer
        toll._read_once = read
        assert toll.buy_check(A, time.monotonic() + 5).line == ''
        assert calls == ['/api/markets/stock/' + A]


def test_freshness_minutes_and_cache_age():
    data = synthetic()
    data['result']['content'][0]['text'] += '\nMade from our last whole read, 6 min ago; a newer read is running.'
    ticks = [10]
    toll, _ = stub(data, clock=lambda: ticks[0])
    assert 'from a read 6 min ago' in toll.buy_check(A, 20).line
    ticks[0] += 120
    assert 'from a read 8 min ago' in toll.buy_check(A, 140).line


def test_reason_cap_count_and_toll_wording_exception_is_narrow():
    data = synthetic(reason='rugged ' + 'x' * 400)
    reading = parse_before_buy(data, A)
    assert len(reading.line.split(' · ')[1]) == 160
    assert 'rugged' in network_card(reading).render()
    ordinary = Reading('Toll', 'present', reading.line)
    assert source_line(ordinary) == 'source text omitted by the wording rule'


@pytest.mark.parametrize('action', ['ALLOW', 'WARN', 'BLOCK', None, 'safe', 'ALLOW\nBLOCK', ['WARN']])
def test_check_tx_action_only_changes_display(action):
    intent = tx_intent({'chainId': 4663, 'to': A, 'from': B, 'data': '0x'})[0]
    response = {'x-toll': {'simulation': {'status': 'success', 'block': 10}}}
    before = project(local_card(intent), intent, response)
    after = project(local_card(intent), intent, {**response, 'action': action})
    assert evaluate([before], Settings()) == evaluate([after], Settings())
    valid = action in ('ALLOW', 'WARN', 'BLOCK')
    assert [s for s in after.data['lines'] if s in ('Toll: ALLOW', 'Toll: WARN', 'Toll: BLOCK')] == (
        ['Toll: ' + action] if valid else [])


def test_detect_bought_side_without_using_simulation_deltas_or_signatures():
    request = json.loads((FIXTURES / 'toll-tx/clean-swap/request.json').read_text())
    response = json.loads((FIXTURES / 'toll-tx/clean-swap/response.json').read_text())
    intent = tx_intent(request)[0]
    expected = response['x-toll']['decoded'][0]['bought'][0]
    assert bought_address(intent) == expected
    assert bought_address(intent, response) == expected
    assert bought_address({**intent, 'typed_scope': True}, response) is None
    assert bought_address({'payload': {'to': A, 'data': '0x'}, 'sender': B},
                          {'x-toll': {'simulation': {'deltas': [{'token': A, 'delta': '100'}]}}}) is None
    shape = tx_intent({'chainId': 4663, 'tokenIn': B, 'tokenOut': A, 'amount': '1'})[0]
    assert bought_address(shape) == A


def isolated(tmp_path, answer):
    settings = Settings(sources={name: 'on' if name == 'toll' else 'off' for name in MODULES})
    network = Network()
    toll, calls = stub(answer)
    network.clients['toll'] = toll
    service = Service(settings, network=network, ledger=Ledger(tmp_path))
    return service, calls


def test_direct_address_card_and_source_switch(tmp_path):
    service, calls = isolated(tmp_path, synthetic())
    service.client.lookup = lambda target, **kw: Card(target, 'identifier')
    card = service.card(A)
    assert 'Toll before buy: BLOCK' in card.render()
    assert len([p for p, body in calls if p == '/mcp']) == 1
    calls.clear()
    service.settings = replace(service.settings, sources={'toll': 'off'})
    assert 'Toll before buy:' not in service.card(A).render()
    assert calls == []


def test_swap_only_bought_address_and_no_approval_change(tmp_path):
    service, calls = isolated(tmp_path, synthetic())
    intents = tx_intent({'chainId': 4663, 'tokenIn': B, 'tokenOut': A, 'amount': '1'})
    local = [local_card(i) for i in intents]
    cards = service._tx_cards(intents, local, service.settings, time.monotonic() + 5)
    assert 'Toll before buy: BLOCK' in cards[0].render()
    assert calls[0][0] == '/api/markets/stock/' + A
    assert len([p for p, body in calls if p == '/mcp']) == 1
    assert evaluate(cards, service.settings) == evaluate(local, service.settings)


def test_original_capture_bytes():
    import hashlib
    hashes = {'llms-2026-11-19.txt': '953a36e32a8523065c01632766daa5a35c104a2af1c78ad0e0794da90ba4ab30', 'mcp-tools-list-2026-11-19.txt': 'c975dd65ac4535801f0ac3ab026834a206dbd81b63ab641253ddbe30dd78d850', 'openapi-2026-11-19.json': 'c045657706a3f2fdf23ff138ce2eef8aed6a42e53582db57b549a477a3a899cb', 'pre-buy-check-2026-11-19-r2.json': 'cf65b5635ce80ebde0013d02494a6275356b43cacae5cec460b603d8601cf047', 'pre-buy-check-2026-11-19.json': 'bd0743f05135ec3e7bc88300bc64683b90f3d8e81da2cd42318caa95beb6e9e7'}
    for name, expected in hashes.items():
        assert hashlib.sha256((FIXTURES / "toll" / name).read_bytes()).hexdigest() == expected


def test_failed_before_buy_preserves_existing_coin_figures():
    from test_toll_coin import WHOLE, COIN
    toll = Toll()
    def read(path, deadline, payload=None):
        if path.startswith('/api/markets/stock/'):
            return 404, {}
        if payload['params']['name'] == 'check_coin':
            return 200, rpc(WHOLE)
        raise Unavailable()
    toll._read_once = read
    baseline = toll.lookup(COIN, time.monotonic() + 5, coin_findings='line')
    changed = toll.lookup(COIN, time.monotonic() + 5, coin_findings='line', before_buy=True)
    assert changed == baseline and 'maker has 1 launch' in changed.line


@pytest.mark.parametrize('word', ['WARN', 'BLOCK', 'ALLOW'])
def test_multiple_swap_intents_share_one_before_buy_call(tmp_path, word):
    service, calls = isolated(tmp_path, synthetic(word))
    intents = tx_intent({'chainId': 4663, 'tokenIn': B, 'tokenOut': A, 'amount': '1'})
    cards = service._tx_cards(intents * 2, [local_card(intents[0])] * 2,
                              service.settings, time.monotonic() + 5)
    assert all(f'Toll before buy: {word}' in card.render() for card in cards)
    assert len([p for p, body in calls if p == '/mcp']) == 1


@pytest.mark.parametrize('sse', [False, True])
def test_streamable_http_json_or_sse_uses_one_budgeted_request(sse):
    toll, seen = Toll(clock=lambda: 10), []
    data = {'jsonrpc': '2.0', 'id': 1, **synthetic('WARN')}
    raw = json.dumps(data).encode()
    if sse:
        raw = b'event: message\ndata: ' + raw + b'\n\n'
    def open(request, timeout):
        seen.append((request, timeout))
        body = BytesIO(raw)
        body.isclosed = lambda: False
        body.fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=lambda value: None)))
        return body
    toll.opener = SimpleNamespace(open=open)
    assert 'Toll before buy: WARN' in toll.before_buy(A, 15).line
    assert len(seen) == 1
    request, timeout = seen[0]
    assert request.full_url == 'https://t0ll.xyz/mcp'
    assert timeout == 5
    assert json.loads(request.data)['params']['arguments'] == {'address': A, 'budget_ms': 4600}


def test_late_answer_is_silent_and_not_cached():
    ticks = [10]
    toll = Toll(clock=lambda: ticks[0])
    def read(*args):
        ticks[0] = 20
        return 200, synthetic()
    toll._read_once = read
    assert toll.before_buy(A, 15).line == ''
    assert not getattr(toll, '_before_buys', {})


def test_stock_swap_does_not_start_before_buy(tmp_path):
    service, calls = isolated(tmp_path, synthetic())
    def read(path, deadline, payload=None):
        calls.append((path, payload))
        assert path == '/api/markets/stock/' + A
        return 200, {'address': A}
    service.network.clients['toll']._read_once = read
    intents = tx_intent({'chainId': 4663, 'tokenIn': B, 'tokenOut': A, 'amount': '1'})
    card = service._tx_cards(intents, [local_card(intents[0])], service.settings, time.monotonic() + 5)[0]
    assert 'Toll before buy:' not in card.render()
    assert len(calls) == 1
