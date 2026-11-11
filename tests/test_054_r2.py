"""Hostile offline paths for the Toll fold and Hookr transaction projections."""
from copy import deepcopy
import json
import time

import pytest

from rh_preflight.card import Card, WORDING
from rh_preflight.network import Network, MODULES
from rh_preflight.settings import Settings
from rh_preflight.transactions import project, local_card
from rh_preflight.sources.toll import Client
from test_toll_coin import COIN, WHOLE, rpc, client
from test_054_i3 import fixture, service, capture


@pytest.mark.parametrize('answer', [None, [], {'result': None}, {'result': {'content': [None]}},
                                    {'result': {'content': [{'type': 'text', 'text': 7}]}},
                                    {'result': {'content': 'bad'}}])
def test_unreadable_coin_is_silent(answer):
    toll = Client()
    toll._read = lambda path, *args: (404, {}) if path.startswith('/api/markets') else (200, answer)
    row = toll.lookup(COIN, time.monotonic() + 1, coin_findings='line')
    assert row.state == 'unlisted' and row.line == ''


def test_default_coin_setting_does_not_spend_quota():
    toll, calls = client(WHOLE)
    assert toll.lookup(COIN).line == ''
    assert all(path != '/mcp' for path, _ in calls)


def test_stock_never_calls_check_coin_even_with_line():
    toll = Client()
    calls = []
    toll._read = lambda path, *args: (calls.append(path), (200, {}))[1]
    toll.stock = lambda *args: 'stock'
    assert toll.lookup(COIN, coin_findings='line') == 'stock'
    assert calls == ['/api/markets/stock/' + COIN]


def test_coin_cache_ttl_cap_and_tx_isolation():
    now, calls = [0.0], []
    toll = Client(clock=lambda: now[0])
    def read(path, deadline, payload=None):
        calls.append((path, payload))
        if path.startswith('/api/markets'):
            return 404, {}
        if path == '/api/tx/check':
            return 200, {'x-toll': {'simulation': {'status': 'success'}}}
        address = payload['params']['arguments']['address']
        return 200, rpc(WHOLE.replace(COIN, address))
    toll._read = read
    def coin(address=COIN):
        return toll.lookup(address, 1000, coin_findings='line')
    first = coin()
    now[0] = 599.999
    assert coin().line == first.line
    assert sum(p == '/mcp' for p, _ in calls) == 1
    toll.check_tx({}, 1000)
    toll.check_tx({}, 1000)
    assert sum(p == '/api/tx/check' for p, _ in calls) == 2
    now[0] = 600
    coin()
    assert sum(p == '/mcp' for p, _ in calls) == 2
    for i in range(513):
        coin('0x' + format(i, '040x'))
    assert len(toll._coins) == 512
    assert COIN not in toll._coins


@pytest.mark.parametrize('mode', ['silent', 'line'])
def test_coin_setting_reaches_builder_and_tool_result(tmp_path, mode):
    toll, calls = client(WHOLE)
    app, intent = service(tmp_path, toll_coin_findings=mode)
    network = Network()
    network.clients['toll'] = toll
    settings = Settings(toll_coin_findings=mode, sources={n: 'on' if n == 'toll' else 'off' for n in MODULES})
    card = network.enrich([Card(COIN, 'identifier')], 'swap', {}, settings)[0]
    output = card.render()
    assert ("8 wallets on Toll's flag list bought 5.9%" in output) == (mode == 'line')
    assert not WORDING.search(output)
    app._read_cards = lambda *args, **kw: [card]
    app.pre_tool_call('swap', intent['payload'])
    row = json.dumps(app.ledger.rows()[0])
    tool = app.transform_tool_result('swap', intent['payload'], '{}')
    for text in (row, tool):
        assert 'Verdict:' not in text and 'first launch from this dev' not in text
        assert ("8 wallets on Toll's flag list bought 5.9%" in text) == (mode == 'line')


def test_conflicting_hookr_metadata_does_not_replace_toll():
    intent, response = fixture()
    response['x-toll']['coins'][0]['decimals'] = 18
    trade = deepcopy(capture('check-trade-0xdc36d5.json')['result']['structuredContent'])
    trade['gets']['symbol'] = 'OTHER'
    trade['gets']['decimals'] = 2
    output = project(local_card(intent), intent, response, hookr=trade).render()
    assert output.count('simulated sender balance change: +45704.730444476128839330 $CLEAR') == 1
    assert 'OTHER' not in output and '45650.58' not in output


def test_payout_all_rules_named_and_partial_read_is_tx_coverage(tmp_path):
    app, intent = service(tmp_path, 'payout-other-wallet')
    directive = app.pre_tool_call('swap', intent['payload'])
    row = app.ledger.rows()[0]['cards'][0]
    assert directive['action'] == 'approve'
    assert 'Toll lists findings' not in row['card']
    assert 'Toll could not read all findings for $CLEAR' in row['card']
    for rule in row['rules']:
        assert row['card'].count(rule) == 1


def test_hookr_titles_cannot_relay_classifications(tmp_path):
    app, intent = service(tmp_path)
    client = app.network.clients['hookr']
    original = client._read_once
    def read(*args):
        status, data = original(*args)
        data = deepcopy(data)
        if args[2]['params']['name'] == 'hookr_scan':
            for finding in data['result']['structuredContent']['findings']:
                finding['title'] = 'safe good low risk verdict'
        return status, data
    client._read_once = read
    app.pre_tool_call('swap', intent['payload'])
    output = json.dumps(app.ledger.rows()[0])
    assert 'safe good low risk verdict' not in output
    assert 'Hookr scan: 79/100' in output
    assert not WORDING.search(output)


def test_concurrent_coin_reads_share_one_complete_answer():
    from concurrent.futures import ThreadPoolExecutor
    toll, calls = client(WHOLE)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda _: toll.lookup(COIN, coin_findings='line'), range(16)))
    assert all(row.line == rows[0].line and row.state == 'present' for row in rows)
    assert sum(path == '/mcp' for path, _ in calls) == 1


@pytest.mark.parametrize('unchecked,action', [('ask', 'approve'), ('stop', 'block'), ('note', None)])
def test_hookr_down_respects_user_incomplete_policy(tmp_path, unchecked, action):
    app, intent = service(tmp_path, on_unchecked=unchecked)
    app.network.clients['hookr'].check_trade = lambda *args: None
    directive = app.pre_tool_call('swap', intent['payload'])
    assert (directive['action'] if directive else None) == action
    assert 'Toll: simulated at block' in app.ledger.rows()[0]['cards'][0]['card']
