"""Fifth review: explicit action scope, complete pair identity and scoped keys."""
import json
from pathlib import Path

import pytest

from rh_preflight.sources.toll import Client as Toll
from rh_preflight.stock_swap import RULE
from test_054 import scoped_secret
from test_stock_swap_above_usd import A, B, ASK, app, run
from test_stock_swap_r2 import raw_swap
from test_stock_swap_review import decoded, value_card


@pytest.mark.parametrize('generic', [False, True])
@pytest.mark.parametrize('location', ['quote', 'example', 'serialized', 'token_metadata'])
def test_nested_argument_examples_are_not_executable_swaps(tmp_path, generic, location):
    action = raw_swap(**({'amountIn': '3'} if generic else {}))
    arguments = {'chainId': 4663, 'token': A}
    if location == 'serialized':
        arguments['text'] = json.dumps(action)
    elif location == 'token_metadata':
        arguments.update(tokenIn={'address': B, 'metadata': action}, tokenOut=B)
    else:
        arguments[location] = action
    service, _ = app(tmp_path)
    directive, row, printed = run(service, arguments)
    assert directive is None and not row['rules']
    assert 'swap value not read: amount' in printed
    assert 'swap value $' not in printed


@pytest.mark.parametrize('generic', [False, True])
def test_unverified_other_side_identity_invalidates_the_pair(tmp_path, generic):
    service, _ = app(tmp_path)
    action = raw_swap(tokenOut={'address': B, 'token': '0x' + 'e' * 40},
                      **({'amountIn': '3'} if generic else {}))
    directive, row, printed = run(service, action)
    assert directive is None and not row['rules']
    assert 'swap value not read: amount' in printed


@pytest.mark.parametrize('generic', [False, True])
@pytest.mark.parametrize('flag', ['quoteOnly', 'readOnly', 'dryRun', 'simulateOnly'])
def test_explicit_simulation_flags_are_not_swap_amount_evidence(tmp_path, generic, flag):
    service, _ = app(tmp_path)
    action = raw_swap(**{flag: True}, **({'amountIn': '3'} if generic else {}))
    directive, row, printed = run(service, action)
    assert directive is None and not row['rules']
    assert 'swap value not read: amount' in printed


@pytest.mark.parametrize('hint', [{'mode': 'quote'}, {'action': 'simulate'},
                                 {'operation': 'estimate'}, {'quoteOnly': 'true'},
                                 {'dryRun': None}, {'method': None}])
def test_ambiguous_execution_hints_are_unread(tmp_path, hint):
    service, _ = app(tmp_path)
    directive, row, printed = run(service, raw_swap(**hint))
    assert directive is None and not row['rules']
    assert 'swap value not read: amount' in printed


@pytest.mark.parametrize('hint', [{'mode': 'swap'}, {'action': 'execute'},
                                 {'operation': 'send'}, {'quoteOnly': False}])
def test_explicit_execution_hints_keep_supported_shape_usable(tmp_path, hint):
    service, _ = app(tmp_path)
    directive, row, printed = run(service, raw_swap(**hint))
    assert directive['action'] == 'approve' and ASK in printed.splitlines()
    assert any(RULE in rule for rule in row['rules'])


def test_read_only_tool_does_not_borrow_execution_from_a_sibling(tmp_path):
    service, _ = app(tmp_path)
    arguments = {'chainId': 4663, 'calls': [raw_swap(), {'execute': True}]}
    _, row, printed = run(service, arguments, 'quote_swap')
    assert not row['rules'] and 'swap value' not in printed


@pytest.mark.parametrize('path', ['/mcp', '/api/markets/stock/' + A, '/api/tx/check/'])
def test_other_routes_do_not_access_the_secret_scope(monkeypatch, path):
    names = scoped_secret(monkeypatch, 'synthetic-scoped')
    assert Toll().request_headers(path) == {}
    assert names == []


def test_loopback_transport_does_not_access_the_secret_scope(monkeypatch):
    names = scoped_secret(monkeypatch, 'synthetic-scoped')
    assert Toll(base_url='http://127.0.0.1:12345').request_headers('/api/tx/check') == {}
    assert names == []


def test_missing_scoped_key_never_uses_environment(monkeypatch):
    names = scoped_secret(monkeypatch, None)
    monkeypatch.setenv('TOLL_API_KEY', 'synthetic-environment')
    assert Toll().request_headers('/api/tx/check') == {}
    assert names == ['TOLL_API_KEY']


def test_secret_is_resolved_for_each_profile_call(monkeypatch):
    client = Toll()
    scoped_secret(monkeypatch, 'synthetic-profile-one')
    assert client.request_headers('/api/tx/check') == {'x-toll-pass': 'synthetic-profile-one'}
    scoped_secret(monkeypatch, 'synthetic-profile-two')
    assert client.request_headers('/api/tx/check') == {'x-toll-pass': 'synthetic-profile-two'}


def test_unknown_warning_code_cannot_establish_decimals():
    response = decoded()
    response['coins'] = []
    response['warnings'] = [{'code': 'unrecognized-code',
                             'x-toll-evidence': {'token': A, 'decimals': 6}}]
    card = value_card(response)
    assert not card.data.get(RULE)
    assert 'swap value not read: decimals' in card.data['lines']


def test_stock_swap_tool_keeps_the_film_shape(tmp_path):
    root = Path(__file__).parent / 'fixtures'
    fixture = json.loads((root / 'stock-swap/nvda-buy-film.json').read_text())
    price = json.loads((root / 'captures/r5/toll-markets-stock-nvda.json').read_text())
    service, reads = app(tmp_path, stocks={price['address'].lower(): price}, **fixture['settings'])
    directive, row, printed = run(service, fixture['arguments'], 'stock_swap')
    assert directive['action'] == 'approve' and row['disposition'] == 'ask'
    assert fixture['expected_ask'] in printed.splitlines()
    assert fixture['expected_value'] in printed.splitlines()
    assert row['rules'] == [RULE + ': your rule: Stock Token swap above $250.00']
    assert len(reads) == 2
