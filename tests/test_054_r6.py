"""Detector consistency, bounded parsing and incomplete-write regressions."""
import json
import random
import time

import pytest

from rh_preflight.transactions import tx_intents
from test_054_r4 import A, C, RPC, TX, service

SEND = {'chainId': 4663, 'method': 'eth_sendTransaction', 'params': [TX]}


def nested(value, depth):
    for _ in range(depth):
        value = {'next': value}
    return value


@pytest.mark.parametrize('args', [
    nested(SEND, 26),
    json.dumps({**SEND, 'padding': 'x' * 1_048_576}),
    {'chainId': 4663, 'method': 'eth_sendTransaction', 'params': None},
    {'chainId': 4663, 'rpc': {'method': 'eth_sendTransaction', 'params': []}},
    {'chainId': 4663, 'method': 'eth_sendTransaction', 'params': [{'from': C}]},
    {'chainId': 4663, 'method': 'personal_sign', 'params': []},
    {'chainId': 4663, 'method': 'personal_sign', 'params': [{}, C]},
    {'command': f'cast send {A} --chain 4663 "'},
    {'chainId': 4663, 'body': '{"method":"eth_sendTransaction","params":['},
], ids=['deep', 'megabyte', 'null-params', 'empty-params', 'no-target', 'empty-sign', 'malformed-sign', 'cast-quote', 'partial-json'])
def test_unreadable_write_uses_unchecked(tmp_path, args):
    app = service(tmp_path)
    answer = app.pre_tool_call('rpc', args)
    assert answer and answer['action'] == 'approve'
    assert 'transaction could not be checked' in answer['message']
    assert app.ledger.rows()[0]['cards'][0]['kind'] == 'transaction'


@pytest.mark.parametrize('name', ['read_file', 'readFile', 'files__readFile', 'web_search', 'webSearch'])
@pytest.mark.parametrize('serialized', [False, True])
def test_content_reads_normalize_names(tmp_path, name, serialized):
    app = service(tmp_path)
    assert app.pre_tool_call(name, {'content': json.dumps(TX) if serialized else TX}) is None
    assert all(c['kind'] != 'transaction' for r in app.ledger.rows() for c in r['cards'])


@pytest.mark.parametrize('depth', [0, 3, 23, 26])
@pytest.mark.parametrize('serialized', [False, True])
@pytest.mark.parametrize('name', ['rpc', 'get_rpc', 'terminal'])
def test_send_representation_consistency(tmp_path, depth, serialized, name):
    args = nested(SEND, depth)
    args = json.dumps(args) if serialized else args
    if name == 'terminal' and serialized:
        args = {'command': "curl -d '" + args + "' " + RPC}
    answer = service(tmp_path).pre_tool_call(name, args)
    assert answer and answer['action'] == 'approve'


def test_parse_failure_respects_unchecked_settings(tmp_path):
    from dataclasses import replace
    app = service(tmp_path)
    args = nested(SEND, 40)
    app.settings = replace(app.settings, on_unchecked='stop')
    assert app.pre_tool_call('rpc', args)['action'] == 'block'
    app.settings = replace(app.settings, on_unchecked='note')
    assert app.pre_tool_call('rpc', args) is None


def test_bounded_fuzz_and_megabyte_detection():
    rng = random.Random(546)
    start = time.monotonic()
    samples = [nested(SEND, 1500), '[' * 1_048_576,
               '{' * 100_000, json.dumps({**SEND, 'padding': 'x' * 1_048_576})]
    atoms = [None, True, [], {}, '0x', 4663, {'method': []}, {'method': SEND}]
    for _ in range(200):
        sample = {**SEND, 'params': rng.choice(atoms)}
        samples.append(nested(sample, rng.randrange(40)))
    for args in samples:
        assert tx_intents(args, 4663, 'rpc')
    assert time.monotonic() - start < 3


@pytest.mark.parametrize('broken', ['intent_rows', 'json_values', 'cast_sends'])
def test_detector_exceptions_become_unchecked(tmp_path, monkeypatch, broken):
    from rh_preflight import transactions
    def fail(*args, **kwargs):
        raise RuntimeError('private input must not be retained')
    monkeypatch.setattr(transactions, broken, fail)
    app = service(tmp_path)
    answer = app.pre_tool_call('rpc', SEND)
    assert answer['action'] == 'approve'
    assert 'transaction could not be checked' in answer['message']
    assert 'private input' not in json.dumps(app.ledger.rows())


@pytest.mark.parametrize('args', [
    {'chainId': 4663, 'command': 'curl -d "{\\"method\\":\\"eth_sendTransaction\\",\\"params\\":[}"'},
    {'chainId': 4663, 'command': 'curl -d "{\\"method\\":\\"eth_sendTransaction\\",\\"params\\":[}'},
    {'chainId': 4663, 'requests': [SEND] * 1100},
    {'chainId': 4663, 'body': '{' * 100_000},
    {'chainId': 4663, 'padding': 'x' * 2_000_001, 'rpc': SEND},
], ids=['escaped-json', 'escaped-quote', 'row-limit', 'candidate-limit', 'text-limit'])
def test_limits_and_shell_parse_failures_stay_unchecked(tmp_path, args):
    app = service(tmp_path)
    answer = app.pre_tool_call('rpc', args)
    assert answer['action'] == 'approve'
    assert 'transaction could not be checked' in answer['message']


def test_megabyte_send_still_receives_transaction_budget(tmp_path):
    from dataclasses import replace
    app = service(tmp_path)
    app.settings = replace(app.settings, sources={**app.settings.sources, 'toll': 'on'})
    remaining = []
    def check(payload, deadline, *args):
        remaining.append(deadline - time.monotonic())
        return {'x-toll': {'simulation': {'status': 'success', 'block': 1}}}
    app.network.clients['toll'].check_tx = check
    tx = {**TX, 'value': '0'}
    args = json.dumps({'method': 'eth_sendTransaction', 'params': [tx], 'padding': 'x' * 1_048_576})
    assert app.pre_tool_call('rpc', args) is None
    assert len(remaining) == 1
    assert remaining[0] > app.settings.tx_timeout - .5
