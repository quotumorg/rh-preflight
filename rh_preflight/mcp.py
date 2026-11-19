"""Read-only MCP over newline-delimited stdio, with no Hermes runtime."""

import argparse
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import fields
import json
import math
import os
import re
import sys

from . import __version__, tools
from .extract import bounded_arguments
from .hooks import Service
from .settings import Settings
from .sources.chain import call as rpc_call
from .sources.toll import Client as Toll


PROTOCOL_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
MAX_FRAME_BYTES = 1024 * 1024
ADDRESS = r'^0x[0-9a-fA-F]{40}$'
HASH = r'^0x[0-9a-fA-F]{64}$'
DATA = r'^0x(?:[0-9a-fA-F]{2})*$'
QUANTITY = r'^(?:0|[1-9][0-9]*|0x[0-9a-fA-F]+)$'
TX_SCHEMA = {
    'type': 'object',
    'properties': {
        'to': {'type': 'string', 'pattern': ADDRESS},
        'data': {'type': 'string', 'pattern': DATA, 'maxLength': 262144},
        'value': {'description': 'native value in wei, as an integer, decimal string or hex quantity',
                  'anyOf': [{'type': 'integer', 'minimum': 0, 'maximum': 2**256 - 1},
                            {'type': 'string', 'pattern': QUANTITY, 'maxLength': 78}]},
        'from': {'type': 'string', 'pattern': ADDRESS},
        'chainId': {'type': 'integer', 'const': 4663, 'default': 4663},
        'tx_hash': {'type': 'string', 'pattern': HASH,
                    'description': 'read its unsigned fields from chain 4663; checks use current state'},
    },
    'anyOf': [{'required': ['to', 'data']}, {'required': ['to', 'value']}, {'required': ['tx_hash']}],
    'additionalProperties': False,
}
ANNOTATIONS = {'readOnlyHint': True, 'destructiveHint': False,
               'idempotentHint': True, 'openWorldHint': True}
TOOLS = [
    {'name': tools.SCHEMA['name'], 'description': tools.SCHEMA['description'],
     'inputSchema': tools.SCHEMA['parameters'], 'annotations': ANNOTATIONS},
    {'name': 'tx_check',
     'description': 'read an unsigned transaction intent or transaction hash on chain 4663. '
                    'returns the plugin decision and cards. advisory only; never signs or sends.',
     'inputSchema': TX_SCHEMA, 'annotations': ANNOTATIONS},
]
FAILED_CHECK = ('stop · rh-preflight could not complete this check\n'
                'coverage unavailable for this check\n'
                'rh-preflight could not complete this check')


class ProtocolError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


class ToolError(Exception):
    """A fixed, caller-visible tool failure, without raw upstream details."""


def strict_json(raw):
    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError('non-finite JSON number')
        return parsed
    def constant(value):
        raise ValueError('non-finite JSON number')
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError('duplicate JSON member')
            obj[key] = value
        return obj
    result = json.loads(raw, parse_constant=constant, parse_float=number, object_pairs_hook=pairs)
    bounded_arguments(result)
    return result


def load_settings(overrides=(), environ=None):
    """Use plugin defaults and validation; only explicitly named env settings."""
    environ = os.environ if environ is None else environ
    defaults = Settings()
    values = {}
    names = {field.name for field in fields(Settings)}
    pairs = [(name, environ['RH_PREFLIGHT_' + name.upper()])
             for name in sorted(names) if 'RH_PREFLIGHT_' + name.upper() in environ]
    for entry in overrides:
        name, separator, value = entry.partition('=')
        if not separator or name not in names:
            raise ValueError('use --setting with a plugin setting name and value')
        pairs.append((name, value))
    for name, raw in pairs:
        try:
            value = raw if isinstance(getattr(defaults, name), str) else strict_json(raw)
            if name in {'sources', 'ask_when'} and not isinstance(value, dict):
                raise ValueError()
            values[name] = value
        except (ValueError, TypeError, RecursionError):
            raise ValueError('invalid setting: ' + name) from None
    try:
        return Settings(**values)
    except (ValueError, TypeError, AttributeError, re.error):
        raise ValueError('invalid plugin settings; check names, types and limits') from None


class EnvToll(Toll):
    """MCP credentials come only from the launching user's environment."""

    def __init__(self, *args, environ=None, **kwargs):
        environ = os.environ if environ is None else environ
        super().__init__(*args, key_provider=lambda: environ.get('TOLL_API_KEY'), **kwargs)

    def check_tx(self, payload, deadline, key_header='x-toll-pass'):
        if not self.key_provider():
            return None
        return super().check_tx(payload, deadline, key_header)


def make_service(settings, environ=None):
    service = Service(settings=settings, presence=lambda: False)
    service.network.clients['toll'] = EnvToll(timeout=settings.timeout, environ=environ)
    return service


def validate_arguments(name, args):
    if not isinstance(args, dict):
        raise ToolError('arguments must be an object')
    schema = tools.SCHEMA['parameters'] if name == 'builder_card' else TX_SCHEMA
    if args.keys() - schema['properties'].keys():
        raise ToolError('unknown argument; only public identifiers and unsigned fields are accepted')
    if name == 'builder_card':
        if any(not isinstance(value, str) for value in args.values()):
            raise ToolError('builder_card arguments must be strings')
        if not any(key in args for key in ('address', 'project', 'repo_url')):
            raise ToolError('provide an address, project or repo_url')
        return
    if 'chainId' in args and (type(args['chainId']) is not int or args['chainId'] != 4663):
        raise ToolError('chainId must be 4663')
    for name, pattern in [('to', ADDRESS), ('from', ADDRESS), ('data', DATA), ('tx_hash', HASH)]:
        if name in args and (not isinstance(args[name], str) or not re.fullmatch(pattern, args[name])):
            raise ToolError('invalid ' + name)
    if len(args.get('data', '')) > 262144:
        raise ToolError('data exceeds the input limit')
    if 'value' in args:
        value = args['value']
        if isinstance(value, str) and len(value) <= 78 and re.fullmatch(QUANTITY, value):
            value = int(value, 16 if value.startswith('0x') else 10)
        if type(value) is not int or not 0 <= value < 2**256:
            raise ToolError('value must be a uint256 quantity in wei')
    if 'tx_hash' not in args or any(k in args for k in ('to', 'data', 'value', 'from')):
        if 'to' not in args or not any(k in args for k in ('data', 'value')):
            raise ToolError('provide to and data or value, or a tx_hash')


def transaction_args(service, args):
    """Resolve a hash with read-only RPC, then use the shared intent detector."""
    intent = {key: value for key, value in args.items() if key != 'tx_hash'}
    intent.setdefault('chainId', 4663)
    if 'tx_hash' not in args:
        return intent
    settings = service._settings()
    if settings.sources.get('chain', 'on') == 'off':
        raise ToolError('transaction hash could not be read: chain source is off')
    chain = service.network.clients['chain']
    deadline = service.clock() + min(settings.timeout, 25.0)
    try:
        chain.verify(deadline)
        response = chain.rpc(rpc_call('eth_getTransactionByHash', [args['tx_hash']]), deadline)
        row = response['result']
        if not isinstance(row, dict) or str(row.get('hash', '')).lower() != args['tx_hash'].lower():
            raise ValueError()
        resolved = {'chainId': 4663, 'to': row['to'], 'from': row['from'],
                    'data': row['input'], 'value': row['value']}
        if 'chainId' in row and row['chainId'] not in (4663, '0x1237'):
            raise ValueError()
        validate_arguments('tx_check', resolved)
    except Exception:
        raise ToolError('transaction hash could not be read as an unsigned intent on chain 4663') from None
    for key in ('to', 'from', 'data', 'value'):
        if key not in intent:
            continue
        left, right = intent[key], resolved[key]
        if key == 'value':
            def quantity(value):
                return int(value, 16 if value.startswith('0x') else 10) if isinstance(value, str) else value
            matches = quantity(left) == quantity(right)
        else:
            matches = left.lower() == right.lower()
        if not matches:
            raise ToolError('transaction hash and unsigned intent disagree')
    return resolved


def tool_result(payload, is_error=False, text=None):
    return {'content': [{'type': 'text', 'text': text if text is not None else json.dumps(payload)}],
            'structuredContent': payload, 'isError': is_error}


class Server:
    def __init__(self, service=None, settings=None):
        self.service = service
        self.settings = settings or Settings()
        self.protocol = None
        self.initialized = False

    def call_tool(self, name, args):
        try:
            validate_arguments(name, args)
            if self.service is None:
                self.service = make_service(self.settings)
            if name == 'builder_card':
                raw = tools.handler(self.service, args)
                payload = json.loads(raw)
                return tool_result(payload, 'error' in payload, text=raw)
            intent = transaction_args(self.service, args)
            # A hypothetical transfer runs the existing action-target detector,
            # while calldata still determines approvals, swaps and local rules.
            checked = self.service.check('transfer', intent)
            if checked is None:
                raise ToolError('transaction intent could not be detected')
            cards, decision = checked
            payload = {'action': decision.action, 'advisory': True,
                       'message': '\n\n'.join(card.render() for card in cards),
                       'cards': [card.public() for card in cards], 'rules': list(decision.rules)}
            if 'tx_hash' in args:
                payload['tx_hash'] = args['tx_hash']
                payload['scope'] = 'unsigned fields from chain 4663; checks use current state, not historical execution'
            return tool_result(payload)
        except ToolError as error:
            payload = {'error': str(error)}
        except Exception:
            # Never echo exception text, upstream data, arguments or credentials.
            print('rh-preflight: tool check could not complete', file=sys.stderr)
            payload = {'error': 'rh-preflight could not complete this read'}
        if name == 'tx_check':
            payload.update(action='block', advisory=True, message=FAILED_CHECK, cards=[], rules=[])
        return tool_result(payload, True)

    def dispatch(self, method, params):
        if not isinstance(params, dict):
            raise ProtocolError(-32602, 'params must be an object')
        if method == 'ping':
            return {}
        if method == 'initialize':
            if self.protocol is not None:
                raise ProtocolError(-32600, 'already initialized')
            info = params.get('clientInfo')
            if (not isinstance(params.get('protocolVersion'), str)
                    or not isinstance(params.get('capabilities'), dict)
                    or not isinstance(info, dict)
                    or not all(isinstance(info.get(k), str) for k in ('name', 'version'))):
                raise ProtocolError(-32602, 'invalid initialize parameters')
            requested = params['protocolVersion']
            self.protocol = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return {'protocolVersion': self.protocol, 'capabilities': {'tools': {'listChanged': False}},
                    'serverInfo': {'name': 'rh-preflight', 'version': __version__},
                    'instructions': 'read-only checks on chain 4663. tx_check actions are advisory; '
                                    'the caller decides whether to continue. never signs or sends.'}
        if method not in {'tools/list', 'tools/call'}:
            raise ProtocolError(-32601, 'method not found')
        if not self.initialized:
            raise ProtocolError(-32002, 'send initialize and notifications/initialized first')
        if method == 'tools/list':
            if 'cursor' in params:
                raise ProtocolError(-32602, 'tools/list has no continuation cursor')
            listed = deepcopy(TOOLS)
            if self.protocol == '2024-11-05':
                for tool in listed:
                    tool.pop('annotations')
            return {'tools': listed}
        name, args = params.get('name'), params.get('arguments', {})
        if not isinstance(name, str) or not isinstance(args, dict) or 'task' in params:
            raise ProtocolError(-32602, 'expected a tool name and arguments object; tasks are not supported')
        if name not in {'builder_card', 'tx_check'}:
            raise ProtocolError(-32602, 'unknown tool')
        result = self.call_tool(name, args)
        if self.protocol in {'2024-11-05', '2025-03-26'}:
            result.pop('structuredContent')
        return result

    def handle(self, raw):
        ident = None
        try:
            try:
                if isinstance(raw, bytes):
                    raw = raw.decode('utf-8')
                message = strict_json(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise ProtocolError(-32700, 'parse error') from None
            if not isinstance(message, dict):
                raise ProtocolError(-32600, 'invalid request; batching is not supported')
            if type(message.get('id')) in (int, str):
                ident = message['id']
            if (message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str)
                    or message.keys() - {'jsonrpc', 'id', 'method', 'params'}
                    or 'id' in message and type(message['id']) not in (int, str)):
                raise ProtocolError(-32600, 'invalid request')
            params = message.get('params', {})
            if 'id' not in message:
                # Notifications never produce a response or execute a tool.
                if (message['method'] == 'notifications/initialized' and self.protocol is not None
                        and isinstance(params, dict)):
                    self.initialized = True
                return None
            return {'jsonrpc': '2.0', 'id': ident,
                    'result': self.dispatch(message['method'], params)}
        except ProtocolError as error:
            return {'jsonrpc': '2.0', 'id': ident,
                    'error': {'code': error.code, 'message': error.message}}
        except Exception:
            print('rh-preflight: protocol request could not complete', file=sys.stderr)
            return {'jsonrpc': '2.0', 'id': ident,
                    'error': {'code': -32603, 'message': 'internal error'}}


def serve(server, stdin=None, stdout=None):
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    # Preserve the protocol stream even if an underlying adapter prints.
    with redirect_stdout(sys.stderr):
        while True:
            raw = stdin.readline(MAX_FRAME_BYTES + 1)
            if not raw:
                return
            if len(raw) > MAX_FRAME_BYTES:
                # Drain only this line, with bounded reads, then recover.
                while raw and not raw.endswith(b'\n'):
                    raw = stdin.readline(MAX_FRAME_BYTES + 1)
                response = {'jsonrpc': '2.0', 'id': None,
                            'error': {'code': -32600, 'message': 'message exceeds the input limit'}}
            else:
                response = server.handle(raw)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=True, allow_nan=False) + '\n')
                stdout.flush()


class Parser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        if message:
            sys.stderr.write(message)


def main(argv=None):
    parser = Parser(description='rh-preflight read-only MCP server over stdio')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--setting', action='append', default=[], metavar='NAME=VALUE',
                        help='override a plugin setting; non-string values use JSON; repeatable')
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.setting)
    except ValueError as error:
        parser.error(str(error))
    try:
        serve(Server(settings=settings))
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
