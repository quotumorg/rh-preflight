"""Offline Hookr capture projections, wallet rules, cache and shared tx budgets."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import threading
import time

import pytest

from demo.hookr_replay import capture, replay_client, cards, HOOK
from rh_preflight.card import Card, WORDING
from rh_preflight.hey import RateLimited, Unavailable
from rh_preflight.hooks import Service
from rh_preflight.hookr_swap import decode, keccak256, ZERO, ROUTER
from rh_preflight.ledger import Ledger
from rh_preflight.network import Network, MODULES
from rh_preflight.policy import evaluate
from rh_preflight.presentation import present
from rh_preflight.settings import Settings
from rh_preflight.sources.hookr import Client
from rh_preflight.transactions import tx_intents, local_card, project

ROOT = Path(__file__).parent / 'fixtures'
TOKEN = '0x41b57f6273b5a78cf9125ce6800f415738e0bba0'
POOL = '0xc3dc7a1cc0bab42b271cae20dfbc0d2ca3fd519fc8192c6230533a61baa88592'
A = '0x' + 'a' * 40


def fixture(name='clean-swap'):
    folder = ROOT / 'toll-tx' / name
    request = json.loads((folder / 'request.json').read_text())
    return tx_intents(request)[0], json.loads((folder / 'response.json').read_text())


def service(tmp_path, name='clean-swap', **settings):
    intent, response = fixture(name)
    app = Service(settings=Settings(sources={'chain': 'off'}, **settings), ledger=Ledger(tmp_path), presence=lambda: False)
    app._read_cards = lambda targets, *args, **kw: [Card(t, 'identifier') for t in targets]
    app.network.clients['hookr'] = replay_client()
    app.network.clients['toll'].check_tx = lambda *args: response
    return app, intent


def text_result(data):
    return {'result': {'content': [{'type': 'text', 'text': json.dumps(data)}]}}


@pytest.mark.parametrize('name,action,phrase', [
    ('clean-swap', 'pass', "Hookr: this pool's rules allow this wallet"),
    ('payout-other-wallet', 'ask', "Hookr: this pool's rules refuse this wallet")])
def test_capture_service_and_persistent_card(tmp_path, name, action, phrase):
    app, intent = service(tmp_path, name)
    app.pre_tool_call('swap', intent['payload'])
    row = app.ledger.rows()[0]
    output = row['cards'][0]['card']
    assert row['disposition'] == action and phrase in output
    assert 'Hookr scan: 79/100, automated screening' in output
    assert '+2 more' in output
    assert not WORDING.search(output)
    assert not any(word in json.dumps(row) for word in ('review-signals', 'good', 'capReasons', 'headline'))
    transformed = app.transform_tool_result('swap', intent['payload'], '{}')
    assert phrase in transformed and not WORDING.search(transformed)


def test_trade_metadata_keeps_toll_amount_not_independent_quote():
    intent, response = fixture()
    trade = capture('check-trade-0xdc36d5.json')['result']['structuredContent']
    card = project(local_card(intent), intent, response, hookr=trade)
    assert 'simulated sender balance change: +45704.730444476128839330 $CLEAR' in card.data['lines']
    assert '45650.58' not in card.render()  # Different timestamp and amount in the quote capture.
    assert 'quoted price impact' not in card.render()
    assert 'Under 0.01%' not in card.render()


@pytest.mark.parametrize('setting,action', [(True, 'ask'), (False, 'pass')])
def test_refusal_rule_is_user_owned(setting, action):
    intent, _ = fixture()
    trade = capture('check-trade-0x7fbd93.json')['result']['structuredContent']
    card = project(local_card(intent), intent, {'x-toll': {'simulation': {'status': 'success'}}}, hookr=trade)
    assert evaluate([card], Settings(ask_hookr_refused=setting)).action == action
    assert "This wallet holds no ERC-8004 agent identity and is not linked as one's agent wallet." in card.render()
    assert 'hookr_agent_gate' not in card.render()
    assert card.data['hookr_refused']


@pytest.mark.parametrize('kind,label', [('shared-root', 'shared root'), ('own-hook', 'own hook from Hookr factory'), ('other', 'unknown')])
def test_hook_kind_and_titles_only(kind, label):
    client = replay_client()
    transport = client._read_once
    def read(path, deadline, payload):
        status, data = transport(path, deadline, payload)
        data = deepcopy(data)
        if payload['params']['name'] == 'hookr_hook':
            data = text_result({'kind': kind, 'note': 'good verdict'})
        else:
            data['result']['structuredContent'].update(verdict='unsafe', headline='good', score={'score': 0, 'band': 'good', 'capReasons': ['safe']})
        return status, data
    client._read_once = read
    row = client.screen(HOOK, time.monotonic() + 1)
    assert 'hook kind ' + label in row.line
    assert '0/100, automated screening · 5 findings:' in row.line
    assert 'Inline assembly needs manual review, Unchecked arithmetic block, Delegated execution to code fixed at construction, +2 more' in row.line
    assert 'Block time' not in row.line and 'good' not in row.line and not WORDING.search(row.line)
    network = Card(HOOK, 'network', {'base': Card(HOOK, 'identifier'), 'sources': [row]})
    assert evaluate([network], Settings()).action == 'pass'


def test_six_fresh_scans_per_minute_and_ten_minute_cache():
    now = [10.0]
    client = replay_client()
    client.clock = lambda: now[0]
    transport = client._read_once
    calls = []
    def read(*args):
        params = args[2]['params']
        calls.append(params['name'])
        status, reply = transport(*args)
        data = deepcopy(reply['result']['structuredContent'])
        data['address'] = params['arguments']['address']
        return status, text_result(data)
    client._read_once = read
    for index in range(6):
        assert '79/100' in client.screen('0x' + format(index+1, '040x'), 1000).line
    assert calls.count('hookr_scan') == 6
    link = client.screen(A, 1000)
    assert link.line == 'Hookr scan: https://hookr.fun/scan?hook=' + A
    assert client.screen('0x' + format(1, '040x'), 1000).line.count('79/100') == 1
    now[0] = 70
    assert '79/100' in client.screen(A, 1000).line
    now[0] = 609.999
    client.screen('0x' + format(1, '040x'), 1000)
    assert calls.count('hookr_scan') == 7
    now[0] = 610
    client.screen('0x' + format(1, '040x'), 1000)
    assert calls.count('hookr_scan') == 8


def test_concurrent_scan_cache_collapses_same_hook():
    from concurrent.futures import ThreadPoolExecutor
    client = replay_client()
    transport, calls = client._read_once, []
    client._read_once = lambda *args: (calls.append(args[2]['params']['name']), transport(*args))[1]
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda _: client.screen(HOOK, time.monotonic() + 2), range(8)))
    assert all('79/100' in row.line for row in rows)
    assert calls.count('hookr_scan') == calls.count('hookr_hook') == 1


@pytest.mark.parametrize('failure', [RateLimited, Unavailable, ValueError, RuntimeError])
def test_scan_failure_or_rate_limit_only_link(failure):
    client = Client()
    def fail(*args):
        raise failure()
    client._read_once = fail
    assert client.screen(HOOK, time.monotonic() + 1).line == 'Hookr scan: ' + client.scan_link(HOOK)


@pytest.mark.parametrize('reply', [{'error': {'code': -32000}}, {'result': {'isError': True}},
                                    {'result': {'content': []}}, text_result([])])
def test_mcp_error_never_becomes_fact(reply):
    client = Client()
    client._read_once = lambda *args: (200, reply)
    assert client.screen(HOOK, time.monotonic() + 1).line == 'Hookr scan: ' + client.scan_link(HOOK)


def test_mcp_text_fallback_and_exact_payload():
    client = Client()
    calls = []
    client._read_once = lambda *args: (calls.append(args), (200, text_result({'kind': 'other'})))[1]
    assert client.mcp('hookr_hook', {'address': HOOK}, 999999999) == {'kind': 'other'}
    path, deadline, payload = calls[0]
    assert path == '/api/mcp' and deadline == 999999999
    assert payload == {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'hookr_hook', 'arguments': {'address': HOOK}}}


def test_keccak_ethereum_vectors_and_captured_pool_key():
    assert keccak256(b'') == 'c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470'
    assert keccak256(b'abc') == '4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45'
    intent, _ = fixture()
    spec = decode(intent)
    assert spec == {'poolId': POOL, 'sold': ZERO, 'bought': TOKEN, 'raw': 480569378599765, 'hook': HOOK, 'wallet': intent['sender']}
    assert spec['poolId'] == capture('pools-clear.json')['result']['structuredContent']['pools'][0]['poolId']


def change_word(intent, index, value):
    intent = deepcopy(intent)
    raw = intent['payload']['data']
    intent['payload']['data'] = raw[:10 + index*64] + format(value % (1 << 256), '064x') + raw[10 + (index+1)*64:]
    return intent


@pytest.mark.parametrize('index,value', [(0, 1 << 160), (1, 0), (2, 1 << 24), (3, 1 << 23), (5, 2), (9, 1 << 160), (12, 0), (13, 10000)])
def test_malformed_router_abi_not_guessed(index, value):
    assert decode(change_word(fixture()[0], index, value)) is None


def test_known_router_trade_starts_without_pools_or_toll():
    client = replay_client()
    original, calls = client._read_once, []
    client._read_once = lambda *a: (calls.append(a[2]['params']), original(*a))[1]
    intent, _ = fixture()
    assert client.check_trade(intent, time.monotonic() + 1)['status'] == 'ok'
    assert calls == [{'name': 'hookr_check_trade', 'arguments': {'poolId': POOL, 'side': 'buy', 'amount': '0.000480569378599765', 'wallet': intent['sender']}}]


def test_sell_uses_pool_token_decimals_and_actual_sender():
    client, calls = replay_client(), []
    transport = client._read_once
    client._read_once = lambda *a: (calls.append(a[2]['params']), transport(*a))[1]
    intent = change_word(change_word(fixture()[0], 5, 0), 6, -123000000000000000000)
    assert client.check_trade(intent, time.monotonic() + 1)['status'] == 'ok'
    assert calls[-1]['arguments'] == {'poolId': POOL, 'side': 'sell', 'amount': '123.000000000000000000', 'wallet': intent['sender']}


def test_unknown_selector_uses_first_pool_and_native_input():
    client, calls = replay_client(), []
    transport = client._read_once
    client._read_once = lambda *a: (calls.append(a[2]['params']), transport(*a))[1]
    intent, response = fixture()
    intent['payload']['data'] = '0x12345678'
    assert client.check_trade(intent, time.monotonic() + 1, response)['status'] == 'ok'
    assert calls[0] == {'name': 'hookr_pools', 'arguments': {'token': TOKEN}}
    assert calls[1]['arguments']['poolId'] == POOL


@pytest.mark.parametrize('change', ['other-router', 'missing-sender', 'exact-output'])
def test_uncheckable_trade_skips(change):
    client = replay_client()
    intent, _ = fixture()
    if change == 'other-router':
        intent['payload']['to'] = A
    elif change == 'missing-sender':
        intent['sender'] = None
    else:
        intent = change_word(intent, 6, 100)
    client.mcp = lambda *a, **kw: pytest.fail('must not call a provider')
    assert client.check_trade(intent, time.monotonic() + 1) is None


def test_toll_and_hookr_trade_are_parallel(tmp_path, monkeypatch, controlled_budget):
    monkeypatch.setattr(time, 'monotonic', lambda: 100.0)
    app, intent = service(tmp_path)
    controlled_budget(lambda name, args: False, app)
    toll_started, hookr_started = threading.Event(), threading.Event()
    response = fixture()[1]
    client = app.network.clients['hookr']
    client.clock = lambda: 100.0
    original = client._read_once
    def toll(*args):
        toll_started.set()
        assert hookr_started.wait(10)
        return response
    def hookr(*args):
        if args[2]['params']['name'] == 'hookr_check_trade':
            hookr_started.set()
            assert toll_started.wait(10)
        return original(*args)
    app.network.clients['toll'].check_tx = toll
    client._read_once = hookr
    app.pre_tool_call('swap', intent['payload'])
    output = app.ledger.rows()[0]['cards'][0]['card']
    assert 'Toll: simulated at block' in output and "rules allow this wallet" in output


@pytest.mark.parametrize('failure', ['slow', 'error', 'unknown'])
def test_hookr_failure_asks_and_toll_survives(tmp_path, failure, monkeypatch):
    from concurrent.futures import Future
    import rh_preflight.hooks as hooks
    app, intent = service(tmp_path)
    original_pool, original_result = hooks.Pool, hooks.budget_result
    pending = Future()
    deadlines = []
    class ControlledPool(original_pool):
        def submit(self, fn, *args, **kwargs):
            if fn.__name__ == 'check_trade' and failure == 'slow':
                return pending
            return super().submit(fn, *args, **kwargs)
    def result(future, deadline, fallback):
        if future is pending:
            deadlines.append(deadline)
            return fallback  # Inject the wait's deadline outcome, without sleeping.
        return original_result(future, deadline, fallback)
    def check(*args):
        if failure == 'error':
            raise Unavailable()
        return {'status': 'unknown', 'reason': 'good verdict'}
    app.network.clients['hookr'].check_trade = check
    monkeypatch.setattr(hooks, 'Pool', ControlledPool)
    monkeypatch.setattr(hooks, 'budget_result', result)
    directive = app.pre_tool_call('swap', intent['payload'])
    assert directive['action'] == 'approve'
    output = app.ledger.rows()[0]['cards'][0]['card']
    assert 'Toll: simulated at block' in output
    assert 'Hookr: wallet pool rules could not be checked' in output
    assert 'transaction could not be checked' in output
    assert 'rules allow' not in output and 'rules refuse' not in output
    assert 'good' not in output
    assert bool(deadlines) == (failure == 'slow')


def test_decoded_toll_hook_scanned_even_unknown_router_selector(tmp_path):
    app, intent = service(tmp_path)
    intent['payload']['data'] = '0x12345678'
    app.pre_tool_call('swap', intent['payload'])
    output = app.ledger.rows()[0]['cards'][0]['card']
    assert 'Hookr scan: 79/100' in output and "rules allow this wallet" in output


@pytest.mark.parametrize('setting', ['source', 'tx'])
def test_hookr_disabled(tmp_path, setting):
    app, intent = service(tmp_path)
    app.settings = replace(app.settings, **({'sources': {'hookr': 'off', 'chain': 'off'}} if setting == 'source' else {'tx_check': 'off'}))
    app.network.clients['hookr']._read_once = lambda *a: pytest.fail('disabled source')
    app.pre_tool_call('swap', intent['payload'])
    assert 'Hookr' not in app.ledger.rows()[0]['cards'][0]['card']


@pytest.mark.parametrize('mode', ['silent', 'line'])
def test_coin_findings_neutral_and_optional(mode):
    intent, response = fixture()
    card = project(local_card(intent), intent, response, coin_findings=mode)
    output = card.render()
    assert ('Toll lists findings for $CLEAR · https://t0ll.xyz/check/' + TOKEN in output) == (mode == 'line')
    assert not WORDING.search(output)
    assert not any(word in output for word in ('careful', 'against', 'clear:', 'classification'))
    assert evaluate([card], Settings()).action == 'pass'


def test_coin_findings_without_public_link_and_without_findings():
    intent, response = fixture()
    response['x-toll']['coins'][0].pop('check')
    assert 'Toll lists findings for $CLEAR' in project(local_card(intent), intent, response, coin_findings='line').data['lines']
    response['x-toll']['coins'][0]['tally'] = {'clear': 5, 'careful': 0, 'against': 0}
    response['warnings'] = []
    assert 'Toll lists findings' not in project(local_card(intent), intent, response, coin_findings='line').render()


@pytest.mark.parametrize('kwargs', [{'ask_hookr_refused': 'on'}, {'ask_hookr_refused': 1}, {'toll_coin_findings': 'careful'}, {'toll_coin_findings': None}])
def test_new_setting_validation(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_capture_wording_and_replay_outputs():
    outputs = list(cards())
    assert len(outputs) == 3
    for _, output in outputs:
        assert not WORDING.search(output)
        assert not any(word in output for word in ('review-signals', 'good', 'capReasons', 'headline'))


def test_http_rate_limit_no_retry_or_sleep_and_cached_hook_still_works(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError
    client = replay_client()
    client.clock = lambda: 10
    assert '79/100' in client.screen(HOOK, 20).line
    calls = []
    class Opener:
        def open(self, request, **kw):
            calls.append(request)
            raise HTTPError(request.full_url, 429, 'rate limit', {'Retry-After': '60'}, BytesIO(b'{}'))
    client.opener = Opener()
    client._read_once = Client._read_once.__get__(client)
    monkeypatch.setattr(time, 'sleep', lambda *a: pytest.fail('rate limit must not sleep'))
    assert client.screen(A, 20).line == 'Hookr scan: ' + client.scan_link(A)
    assert len(calls) == 1 and client.cooldown == 70
    assert '79/100' in client.screen(HOOK, 20).line
    assert len(calls) == 1


@pytest.mark.parametrize('data', [{'score': []}, {'score': {'score': 10}, 'findings': 7}, {'address': A}])
def test_malformed_or_mismatched_scan_only_link(data):
    client = replay_client()
    original = client._read_once
    def read(*args):
        if args[2]['params']['name'] == 'hookr_scan':
            return 200, text_result(data)
        return original(*args)
    client._read_once = read
    assert client.screen(HOOK, time.monotonic() + 1).line == 'Hookr scan: ' + client.scan_link(HOOK)


def test_coin_warning_can_supply_neutral_line_without_tally():
    intent, response = fixture()
    response['x-toll']['coins'][0]['tally'] = {}
    response['warnings'] = [{'code': 'coin-against', 'message': 'unsafe remote verdict',
                             'x-toll-evidence': {'token': TOKEN}}]
    card = project(local_card(intent), intent, response, coin_findings='line')
    assert card.render().count('Toll lists findings for $CLEAR') == 1
    assert not WORDING.search(card.render())


def test_hook_from_args_uses_mcp_when_catalog_cannot_be_read():
    network = Network()
    client = network.clients['hookr'] = replay_client()
    client.prefetch = lambda *args: None
    settings = Settings(sources={name: 'on' if name == 'hookr' else 'off' for name in MODULES})
    card = network.enrich([Card(HOOK, 'identifier')], 'swap', {'PoolKey': {'hooks': HOOK}}, settings)[0]
    output = card.render()
    assert 'hook kind shared root' in output and '79/100, automated screening' in output
    assert not WORDING.search(output)


def test_coin_findings_setting_reaches_service(tmp_path):
    app, intent = service(tmp_path, toll_coin_findings='line')
    app.pre_tool_call('swap', intent['payload'])
    assert 'Toll lists findings for $CLEAR' in app.ledger.rows()[0]['cards'][0]['card']


def test_hookr_transport_failure_does_not_suppress_toll(tmp_path):
    app, intent = service(tmp_path)
    def fail(*args):
        raise Unavailable()
    app.network.clients['hookr']._read_once = fail
    app.pre_tool_call('swap', intent['payload'])
    output = app.ledger.rows()[0]['cards'][0]['card']
    assert 'Toll: simulated at block' in output
    assert 'rules allow' not in output and 'rules refuse' not in output
    assert 'Hookr scan: https://hookr.fun/scan?hook=' in output
