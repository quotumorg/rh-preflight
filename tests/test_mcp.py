"""Stdio protocol and shared check parity, with every network socket forbidden."""

from copy import deepcopy
from dataclasses import asdict, fields, replace
from io import BytesIO, StringIO
import json
import re
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from demo.hookr_replay import replay_client
from demo.r6_replay import cards as captured_cards
from rh_preflight import __version__, tools
from rh_preflight.card import Card
from rh_preflight.ledger import Ledger
from rh_preflight.mcp import (Server, EnvToll, MAX_FRAME_BYTES, PROTOCOL_VERSIONS,
                              TOOLS, load_settings, make_service, serve)
from rh_preflight.network import MODULES
from rh_preflight.settings import Settings
from rh_preflight.sources.common import Reading


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / 'fixtures' / 'toll-tx'
A, B = ('0x' + char * 40 for char in 'ab')
TX_HASH = '0x' + 'c' * 64
INTENT = {'to': A, 'data': '0x', 'value': '0', 'from': B, 'chainId': 4663}
INIT = {'protocolVersion': '2025-11-25', 'capabilities': {},
        'clientInfo': {'name': 'offline-test', 'version': '1'}}


@pytest.fixture(autouse=True)
def no_sockets_or_host_keys(monkeypatch):
    attempts = []
    def deny(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('MCP tests must not create sockets or read host secrets')
    monkeypatch.setattr(socket, 'socket', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    monkeypatch.setattr('rh_preflight.sources.toll.toll_key', deny)
    monkeypatch.delenv('TOLL_API_KEY', raising=False)
    yield
    assert attempts == []


def request(server, method, params=None, ident=1):
    msg = {'jsonrpc': '2.0', 'id': ident, 'method': method}
    if params is not None:
        msg['params'] = params
    return server.handle(json.dumps(msg))


def initialized(server=None, version=None):
    server = server or Server()
    assert 'result' in request(server, 'initialize', {**INIT, 'protocolVersion': version or INIT['protocolVersion']})
    assert server.handle(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'})) is None
    return server


def call(server, name, args):
    return request(server, 'tools/call', {'name': name, 'arguments': args})['result']


def fixture_service(name='clean-swap', **changes):
    settings = Settings(sources={key: 'off' for key in MODULES}, **changes)
    settings = replace(settings, sources={**settings.sources, 'toll': 'on', 'hookr': 'on'})
    service = make_service(settings, environ={'TOLL_API_KEY': 'synthetic-mcp-key'})
    response = json.loads((FIXTURES / name / 'response.json').read_text())
    service._read_cards = lambda targets, *a, **kw: [Card(t, 'identifier') for t in targets]
    service.network.clients['hookr'] = replay_client()
    toll = service.network.clients['toll']
    toll._read_once = lambda *a, **kw: (200, deepcopy(response))
    toll.buy_check = lambda *a, **kw: Reading('Toll', 'unlisted', '')
    intent = json.loads((FIXTURES / name / 'request.json').read_text())
    return service, intent


@pytest.mark.parametrize('version', [*PROTOCOL_VERSIONS, '2099-01-01'])
def test_handshake_negotiates_and_advertises_only_tools(version):
    server = Server()
    result = request(server, 'initialize', {**INIT, 'protocolVersion': version}, ident='init')['result']
    assert result['protocolVersion'] == (version if version in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0])
    assert result['serverInfo'] == {'name': 'rh-preflight', 'version': __version__}
    assert result['capabilities'] == {'tools': {'listChanged': False}}
    assert server.service is None
    assert not server.initialized


@pytest.mark.parametrize('method', ['tools/list', 'tools/call'])
def test_both_lifecycle_steps_are_required(method):
    server = Server()
    assert request(server, method)['error']['code'] == -32002
    request(server, 'initialize', INIT)
    assert request(server, method)['error']['code'] == -32002
    assert request(server, 'initialize', INIT)['error']['code'] == -32600


@pytest.mark.parametrize('params', [{}, {'protocolVersion': 5}, {**INIT, 'clientInfo': {}},
                                  {**INIT, 'capabilities': []}, {**INIT, 'clientInfo': None}])
def test_initialize_rejects_bad_params_and_allows_retry(params):
    server = Server()
    assert request(server, 'initialize', params)['error']['code'] == -32602
    assert 'result' in request(server, 'initialize', INIT)


@pytest.mark.parametrize('ident', [0, 7, 'hello', ''])
def test_ping_and_id_round_trip(ident):
    server = Server()
    assert request(server, 'ping', ident=ident) == {'jsonrpc': '2.0', 'id': ident, 'result': {}}
    assert request(initialized(server), 'ping', ident=ident)['result'] == {}


@pytest.mark.parametrize('raw,code', [
    ('{', -32700), ('NaN', -32700), ('{"x":1e999}', -32700),
    ('{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}', -32700),
    (b'\xff', -32700), pytest.param('{"method":"ping"}'.encode('utf-16'), -32700, id='utf16'),
    ('[', -32700), ('[]', -32600), ('null', -32600), ('1', -32600),
    ('{"jsonrpc":"1.0","id":1,"method":"ping"}', -32600),
    ('{"jsonrpc":"2.0","id":null,"method":"ping"}', -32600),
    ('{"jsonrpc":"2.0","id":true,"method":"ping"}', -32600),
    ('{"jsonrpc":"2.0","id":1.5,"method":"ping"}', -32600),
    ('{"jsonrpc":"2.0","id":1,"result":{}}', -32600),
    ('{"jsonrpc":"2.0","id":1,"method":null}', -32600),
    ('{"jsonrpc":"2.0","id":1,"method":"ping","params":[]}', -32602),
    ('{"jsonrpc":"2.0","id":1,"method":"ping","params":null}', -32602),
    pytest.param('[' * 2000 + ']' * 2000, -32700, id='nested-json'),
])
def test_malformed_protocol_is_reported_without_echo(raw, code):
    result = Server().handle(raw)
    assert result['error']['code'] == code
    assert set(result['error']) == {'code', 'message'}


def test_notifications_do_not_reply_or_execute_tools():
    server = initialized()
    for method in ('notifications/initialized', 'notifications/cancelled', 'unknown', 'tools/call'):
        assert server.handle(json.dumps({'jsonrpc': '2.0', 'method': method,
                                         'params': {'name': 'tx_check', 'arguments': INTENT}})) is None
    assert server.service is None
    before = Server()
    before.handle('{"jsonrpc":"2.0","method":"notifications/initialized"}')
    assert not before.initialized


def test_method_tool_and_params_errors_are_distinct():
    server = initialized()
    assert request(server, 'unknown')['error']['code'] == -32601
    for params in ({}, {'name': 'unknown'}, {'name': []}, {'name': 'tx_check', 'arguments': []},
                   {'name': 'tx_check', 'task': {'ttl': 100}}):
        assert request(server, 'tools/call', params)['error']['code'] == -32602
    assert request(server, 'tools/list', {'cursor': 'bad'})['error']['code'] == -32602
    assert request(server, 'notifications/initialized')['error']['code'] == -32601


def test_tools_list_reuses_builder_schema_and_json_round_trips():
    result = request(initialized(), 'tools/list')['result']
    assert json.loads(json.dumps(result)) == result
    assert result['tools'] == TOOLS
    assert result['tools'][0]['inputSchema'] == tools.SCHEMA['parameters']
    for tool in result['tools']:
        assert tool['inputSchema']['type'] == 'object'
        assert tool['inputSchema']['additionalProperties'] is False
        assert tool['annotations']['readOnlyHint'] is True


@pytest.mark.parametrize('tool', TOOLS, ids=lambda tool: tool['name'])
def test_input_schema_against_2020_12_metaschema_when_validator_available(tool):
    # Optional test-only utility. The server and the rest of the suite stay stdlib.
    jsonschema = pytest.importorskip('jsonschema', exc_type=ImportError)
    jsonschema.Draft202012Validator.check_schema(tool['inputSchema'])


@pytest.mark.parametrize('args', [{'address': A}, {'project': 'priors-agents'},
                                  {'repo_url': 'https://github.com/quotumorg/rh-preflight'}])
def test_builder_call_matches_handler_output_with_captures(args):
    service = make_service(Settings(), environ={})
    base = captured_cards()[0]
    service._read_cards = lambda *a, **kw: [base]
    result = call(initialized(Server(service)), 'builder_card', args)
    assert not result['isError']
    assert result['content'][0]['text'] == tools.handler(service, args)
    assert result['structuredContent'] == json.loads(result['content'][0]['text'])
    assert 'HEY Research Lab' in result['structuredContent']['card']
    assert service.pending == {}


@pytest.mark.parametrize('name,expected', [('clean-swap', 'pass'), ('payout-other-wallet', 'ask'),
                                         ('unlimited-approval-eoa', 'block')])
def test_tx_call_reuses_real_transaction_cards_and_matches_hook(name, expected, tmp_path):
    service, intent = fixture_service(name)
    service.ledger = Ledger(tmp_path)
    original = service._tx_cards
    seen = []
    def tracked(*args, **kwargs):
        seen.append(True)
        return original(*args, **kwargs)
    service._tx_cards = tracked
    result = call(initialized(Server(service)), 'tx_check', intent)
    assert not result['isError']
    payload = result['structuredContent']
    assert payload['action'] == expected and payload['advisory'] is True
    assert payload == json.loads(result['content'][0]['text'])
    assert any('coverage' in card for card in payload['cards'])
    assert not list(tmp_path.iterdir()), 'MCP must not write the Hermes ledger'
    assert service.pending == {} and seen == [True]
    directive = service.pre_tool_call('transfer', intent)
    row = service.ledger.rows()[0]
    assert payload['cards'] == row['cards']
    assert payload['rules'] == row['rules']
    assert payload['action'] == row['disposition']
    assert payload['message'] == '\n\n'.join(c['card'] for c in row['cards'])
    if directive:
        assert directive['message'] == payload['message']


def test_note_is_advisory_and_keeps_existing_label():
    service, intent = fixture_service(mode='note')
    payload = call(initialized(Server(service)), 'tx_check', intent)['structuredContent']
    assert payload['action'] == 'note'
    assert payload['message'].startswith('note · ')


@pytest.mark.parametrize('args', [
    {}, {'to': A}, {'data': '0x'}, {'tx_hash': '0x123'}, {'to': 'bad', 'data': '0x'},
    {**INTENT, 'from': 'bad'}, {**INTENT, 'data': '0x1'}, {**INTENT, 'data': 7},
    {**INTENT, 'data': '0x' + '00' * 131072}, {**INTENT, 'chainId': 1},
    {**INTENT, 'chainId': True}, {**INTENT, 'chainId': '4663'},
    {**INTENT, 'value': -1}, {**INTENT, 'value': True}, {**INTENT, 'value': 1.5},
    {**INTENT, 'value': '1 eth'}, {**INTENT, 'value': str(2**256)},
    {**INTENT, 'value': 2**256}, {**INTENT, 'private_key': 'synthetic-do-not-echo'},
    {**INTENT, 'rpc': {'method': 'eth_sendRawTransaction'}},
    {'tx_hash': TX_HASH, 'data': '0x'}, {'tx_hash': TX_HASH, 'from': B},
])
def test_bad_tool_arguments_never_reach_service(args):
    server = initialized()
    result = call(server, 'tx_check', args)
    assert result['isError']
    assert result['structuredContent']['action'] == 'block'
    assert server.service is None
    assert 'synthetic-do-not-echo' not in json.dumps(result)


@pytest.mark.parametrize('args', [{}, {'address': 12}, {'project': []}, {'repo_url': None}, {'unknown': 'x'},
                                  {'address': '0xNOTHEX'}, {'address': 'pons'}, {'address': '0x' + 'a' * 41}])
def test_builder_invalid_schema_inputs_are_tool_errors(args):
    server = initialized()
    assert call(server, 'builder_card', args)['isError']
    assert server.service is None


def test_builder_preserves_handler_semantic_errors():
    service = make_service(Settings(), environ={})
    result = call(initialized(Server(service)), 'builder_card', {'address': A, 'repo_url': 'x'})
    assert result['isError']
    assert result['content'][0]['text'] == tools.handler(service, {'address': A, 'repo_url': 'x'})


@pytest.mark.parametrize('value', [0, '0', '0x0', 2**256 - 1, str(2**256 - 1), hex(2**256 - 1)])
def test_native_value_inputs_keep_existing_extraction(value):
    service, _ = fixture_service()
    result = call(initialized(Server(service)), 'tx_check', {'to': A, 'value': value})
    assert not result['isError']
    assert 'sending wallet not recorded' in result['structuredContent']['message']


def test_key_unset_skips_toll_simulation_and_keeps_gap():
    service, intent = fixture_service()
    service.network.clients['toll'] = EnvToll(environ={})
    toll = service.network.clients['toll']
    toll.buy_check = lambda *a, **kw: Reading('Toll', 'unlisted', '')
    toll._read = lambda *a, **kw: pytest.fail('unset key must not call the tx route')
    result = call(initialized(Server(service)), 'tx_check', intent)
    assert not result['isError']
    assert result['structuredContent']['action'] == 'ask'
    assert 'Toll: not simulated' in result['structuredContent']['message']


@pytest.mark.parametrize('header', ['x-toll-pass', 'Authorization'])
def test_env_key_header_and_budget_never_enter_output(header):
    service, intent = fixture_service(toll_key_header=header)
    toll = service.network.clients['toll']
    seen = []
    raw = toll._read_once
    def inspect(path, deadline, payload):
        seen.append((path, toll.request_headers(path), payload['budgetMs']))
        return raw(path, deadline, payload)
    toll._read_once = inspect
    result = call(initialized(Server(service)), 'tx_check', intent)
    assert seen and seen[0][0] == '/api/tx/check'
    assert seen[0][1] == {header: ('Bearer ' if header == 'Authorization' else '') + 'synthetic-mcp-key'}
    assert 0 < seen[0][2] <= 15000
    assert 'synthetic-mcp-key' not in json.dumps(result)
    assert toll.request_headers('/api/markets/stock/NVDA') == {}
    toll.base_url = 'https://t0ll.xyz.example.org'
    assert toll.request_headers('/api/tx/check') == {}


@pytest.mark.parametrize('unchecked,action', [('ask', 'ask'), ('stop', 'block'), ('note', 'pass')])
def test_unset_key_obeys_existing_unchecked_policy(unchecked, action):
    service, intent = fixture_service(on_unchecked=unchecked)
    toll = EnvToll(environ={})
    toll.buy_check = lambda *a, **kw: Reading('Toll', 'unlisted', '')
    service.network.clients['toll'] = toll
    result = call(initialized(Server(service)), 'tx_check', intent)
    assert result['structuredContent']['action'] == action


def hash_service(row=None):
    service, _ = fixture_service()
    service.settings = replace(service.settings, sources={**service.settings.sources, 'chain': 'on'})
    row = row if row is not None else {'hash': TX_HASH, 'to': A, 'from': B, 'input': '0x', 'value': '0x0',
                                     'r': 'synthetic-signature', 's': 'synthetic-signature', 'v': '0x1'}
    seen = []
    chain = service.network.clients['chain']
    chain.verify = lambda deadline: seen.append(('verify', deadline))
    def rpc(payload, deadline):
        seen.append((payload, deadline))
        return {'result': row}
    chain.rpc = rpc
    return service, seen


@pytest.mark.parametrize('with_intent', [False, True])
def test_hash_resolves_only_unsigned_fields_then_uses_shared_check(with_intent):
    service, seen = hash_service()
    args = {'tx_hash': TX_HASH, **(INTENT if with_intent else {})}
    payload = call(initialized(Server(service)), 'tx_check', args)['structuredContent']
    assert payload['tx_hash'] == TX_HASH and 'current state' in payload['scope']
    assert seen[0][0] == 'verify'
    assert seen[1][0] == {'jsonrpc': '2.0', 'id': 1, 'method': 'eth_getTransactionByHash', 'params': [TX_HASH]}
    assert seen[0][1] == seen[1][1]
    assert 'synthetic-signature' not in json.dumps(payload)
    assert 'error' not in payload


@pytest.mark.parametrize('row', [{}, {'hash': TX_HASH},
    {'hash': TX_HASH, 'to': None, 'from': B, 'input': '0x', 'value': '0'},
    {'hash': TX_HASH, 'to': A, 'from': B, 'input': '0x', 'value': '0', 'chainId': '0x1'},
    {'hash': '0x' + 'd' * 64, 'to': A, 'from': B, 'input': '0x', 'value': '0'}])
def test_hash_unreadable_or_wrong_chain_is_explicit(row):
    service, _ = hash_service(row)
    result = call(initialized(Server(service)), 'tx_check', {'tx_hash': TX_HASH})
    assert result['isError'] and result['structuredContent']['action'] == 'block'


def test_hash_conflict_and_disabled_chain_are_not_silently_ignored():
    service, _ = hash_service()
    server = initialized(Server(service))
    assert 'disagree' in call(server, 'tx_check', {**INTENT, 'value': '1', 'tx_hash': TX_HASH})['structuredContent']['error']
    service.settings = replace(service.settings, sources={'chain': 'off'})
    assert 'off' in call(server, 'tx_check', {'tx_hash': TX_HASH})['structuredContent']['error']


def test_internal_tool_failure_is_redacted_and_protocol_recovers(capsys):
    service, _ = fixture_service()
    def fail(*a, **kw):
        raise RuntimeError('synthetic-private-detail')
    service.check = fail
    server = initialized(Server(service))
    result = call(server, 'tx_check', INTENT)
    assert result['isError']
    assert result['structuredContent']['message'].startswith('stop · ')
    assert 'synthetic-private-detail' not in json.dumps(result) + capsys.readouterr().err
    assert request(server, 'ping')['result'] == {}


def test_internal_protocol_failure_is_redacted(monkeypatch, capsys):
    server = initialized()
    def fail(*args):
        raise RuntimeError('synthetic-private-detail')
    monkeypatch.setattr(server, 'dispatch', fail)
    result = request(server, 'ping')
    assert result['error']['code'] == -32603
    assert 'synthetic-private-detail' not in json.dumps(result) + capsys.readouterr().err


@pytest.mark.parametrize('version', PROTOCOL_VERSIONS)
def test_old_clients_get_text_without_newer_fields(version):
    service, intent = fixture_service()
    server = initialized(Server(service), version)
    result = call(server, 'tx_check', intent)
    assert ('structuredContent' in result) == (version >= '2025-06-18')
    assert json.loads(result['content'][0]['text'])['action'] == 'pass'
    tool = request(server, 'tools/list')['result']['tools'][0]
    assert ('annotations' in tool) == (version >= '2025-03-26')


def test_settings_match_plugin_defaults_and_argument_precedence():
    assert load_settings(environ={}) == Settings()
    # Compare every manifest default without adding a YAML runtime dependency.
    manifest = (ROOT / 'plugin.yaml').read_text().split('config_schema:\n', 1)[1]
    for name, value in asdict(Settings()).items():
        lines = manifest.split('  ' + name + ':\n', 1)[1].splitlines()
        block = []
        for line in lines:
            if line.startswith('  ') and not line.startswith('    '):
                break
            block.append(line)
        default = next((line.removeprefix('    default: ') for line in block
                        if line.startswith('    default: ')), None)
        if name == 'assume_chain':
            assert value is None and default is None
        elif name == 'tool_patterns':
            assert [line.removeprefix('      - ') for line in block if line.startswith('      - ')] == value
        else:
            try:
                parsed = json.loads(default)
            except ValueError:
                parsed = default
            assert parsed == value, name
    settings = load_settings(['timeout=6', 'sources={"toll":"off"}'], environ={
        'RH_PREFLIGHT_TIMEOUT': '3', 'RH_PREFLIGHT_TX_TIMEOUT': '10',
        'RH_PREFLIGHT_ASK_QUIET': 'false', 'RH_PREFLIGHT_MODE': 'note'})
    assert (settings.timeout, settings.tx_timeout, settings.ask_quiet, settings.mode) == (6, 10, False, 'note')
    assert settings.sources == {'toll': 'off'}


@pytest.mark.parametrize('override', ['unknown=1', 'timeout', 'timeout=0', 'timeout=31',
    'tx_timeout=26', 'timeout=NaN', 'timeout=1e999', 'ask_quiet=1', 'sources=[]',
    'ask_when=[]', 'mode=invalid', 'sources={"toll":"maybe"}', 'toll_wait_max_seconds=121',
    'tool_patterns=["["]', 'assume_chain=[]', 'TOLL_API_KEY=synthetic-key'])
def test_settings_errors_do_not_echo_values(override):
    with pytest.raises(ValueError) as error:
        load_settings([override], environ={})
    assert 'synthetic-key' not in str(error.value)


def test_stdio_is_only_frames_even_when_dependency_prints(capsys):
    service, intent = fixture_service()
    original = service.check
    def noisy(*args):
        print('synthetic dependency log')
        return original(*args)
    service.check = noisy
    messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': INIT},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                 'params': {'name': 'tx_check', 'arguments': intent}},
                {'jsonrpc': '2.0', 'id': 4, 'method': 'ping'}]
    stdin = BytesIO(('\n'.join(json.dumps(row) for row in messages) + '\n').encode())
    stdout = StringIO()
    serve(Server(service), stdin, stdout)
    frames = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [frame['id'] for frame in frames] == [1, 2, 3, 4]
    assert frames[2]['result']['structuredContent']['action'] == 'pass'
    assert 'synthetic dependency log' in capsys.readouterr().err


def test_oversized_and_malformed_frames_recover_and_eof_exits():
    stdin = BytesIO(b'x' * (MAX_FRAME_BYTES + 20) + b'\n{\n' +
                    b'{"jsonrpc":"2.0","id":3,"method":"ping"}\n')
    stdout = StringIO()
    serve(Server(), stdin, stdout)
    frames = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [frame.get('error', {}).get('code') for frame in frames] == [-32600, -32700, None]
    assert frames[-1]['result'] == {}


def test_module_entry_point_with_process_socket_guard():
    # -S prevents third-party and user site startup code. The subprocess installs
    # its guard before importing the server; the parent fixture covers this process.
    program = '''
import runpy, sys
def guard(event, args):
    if event == 'socket.__new__':
        raise AssertionError('stdio must not create a socket')
sys.addaudithook(guard)
runpy.run_module('rh_preflight.mcp', run_name='__main__')
'''
    frames = '\n'.join(json.dumps(row) for row in [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': INIT},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'tx_check', 'arguments': {}}},
    ]) + '\n'
    result = subprocess.run([sys.executable, '-S', '-c', program], cwd=ROOT,
                            input=frames, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ''
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [row['id'] for row in rows] == [1, 2, 3]
    assert rows[-1]['result']['isError']


@pytest.mark.parametrize('flag', ['--help', '--version', '--invalid'])
def test_cli_diagnostics_never_use_stdout(flag):
    result = subprocess.run([sys.executable, '-S', '-m', 'rh_preflight.mcp', flag], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.stdout == '' and result.stderr
    assert result.returncode == (2 if flag == '--invalid' else 0)


def test_packaging_has_no_runtime_dependencies_and_keeps_version_dynamic():
    import tomllib
    data = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    assert data['project']['name'] == 'rh-preflight-mcp'
    assert data['project']['dependencies'] == []
    assert data['project']['scripts'] == {'rh-preflight-mcp': 'rh_preflight.mcp:main'}
    assert data['tool']['setuptools']['dynamic']['version']['attr'] == 'rh_preflight.__version__'
    draft = json.loads((ROOT / ('server' + '.json')).read_text())
    assert draft['name'] == 'io.github.quotumorg/rh-preflight'
    assert draft['packages'][0]['transport'] == {'type': 'stdio'}
    assert draft['packages'][0]['identifier'] == data['project']['name']


def test_readme_mcp_names_match_code():
    readme = (ROOT / 'README.md').read_text()
    section = readme.split('## mcp over stdio\n', 1)[1].split('\n## ', 1)[0]
    table = section.split('| setting name | environment variable | default |', 1)[1].split('\n\nthe additional environment', 1)[0]
    rows = re.findall(r'^\| `([a-z][a-z0-9_]*)` \| `(RH_PREFLIGHT_[A-Z0-9_]+)` \|', table, re.M)
    settings = {field.name for field in fields(Settings)}
    assert len(rows) == len(settings)
    assert {name for name, _ in rows} == settings
    assert all(env == 'RH_PREFLIGHT_' + name.upper() for name, env in rows)
    env_names = set(re.findall(r'\b(?:RH_PREFLIGHT_[A-Z][A-Z0-9_]*|TOLL_API_KEY)\b', section))
    assert env_names == {env for _, env in rows} | {'TOLL_API_KEY'}
    tool_names = set(re.findall(r'^- `([a-z][a-z0-9_]*)`:', section, re.M))
    assert tool_names == {tool['name'] for tool in TOOLS}


def test_registry_environment_and_version_match_code():
    draft = json.loads((ROOT / ('server' + '.json')).read_text())
    package = draft['packages'][0]
    assert draft['version'] == package['version'] == __version__
    variables = package['environmentVariables']
    expected = {'RH_PREFLIGHT_' + field.name.upper() for field in fields(Settings)} | {'TOLL_API_KEY'}
    assert len(variables) == len(expected)
    assert {entry['name'] for entry in variables} == expected
    assert all(not entry['isRequired'] for entry in variables)
    assert {entry['name'] for entry in variables if entry['isSecret']} == {'TOLL_API_KEY'}
