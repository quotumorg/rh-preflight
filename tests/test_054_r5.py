"""Execution evidence must survive read-name and simulation exclusions."""
import json

import pytest

from rh_preflight.transactions import tx_intents
from test_054_r4 import A, C, RPC, SWAP, TX, TYPED, service

SEND = {'method': 'eth_sendTransaction', 'params': [TX]}
SIGN = {'method': 'eth_signTypedData_v4', 'params': [C, TYPED]}
RAW = {'method': 'eth_sendRawTransaction', 'params': ['0xdeadbeef']}
READ = {'method': 'eth_call', 'params': [TX, 'latest']}

WRITES = [
    ('get_swap_quote', {**SWAP, 'execute': True}),
    ('get_swap_quote', {**SWAP, 'quoteOnly': False}),
    ('get_swap_quote', {**SWAP, 'mode': 'execute'}),
    ('get_swap_quote', {'transaction': TX}),
    ('get_swap_quote', {'rpc': SEND}),
    ('get_swap_quote', {'rpc': SIGN}),
    ('get_rpc', {'chainId': 4663, **RAW}),
    ('rpc', {'chainId': 4663, 'requests': [READ, SEND]}),
    ('rpc', {'chainId': 4663, 'requests': [READ, SIGN]}),
    ('rpc', {'chainId': 4663, 'requests': [READ, RAW]}),
    ('rpc', {'chainId': 4663, **READ, 'next': SEND}),
    ('rpc', {'chainId': 4663, **READ, 'next': SIGN}),
    ('get_rpc', {'chainId': 4663, 'rpc': RAW}),
    ('rpc', {'chainId': 4663, 'method': 'eth_call', 'params': [{'requests': [SEND, SIGN]}]}),
    ('rpc', {'chainId': 4663, **READ, 'next': json.dumps(SEND)}),
    ('terminal', {'command': 'curl --data "' + json.dumps(RAW).replace('"', '\\"') + '" ' + RPC}),
    ('terminal', {'command': "curl -d '" + json.dumps(SEND) + "' " + RPC}),
    ('terminal', {'command': "curl -d '" + json.dumps(RAW) + "' " + RPC}),
    ('terminal', {'command': "curl -d '" + json.dumps([READ, SEND]) + "' " + RPC}),
    ('terminal', {'command': f'cast call {A} "symbol()(string)" --rpc-url {RPC} | cast send {A} --value 1 --from {C} --chain 4663'}),
]


@pytest.mark.parametrize('name,args', WRITES)
def test_execution_cannot_inherit_read_exemption(tmp_path, name, args):
    app = service(tmp_path)
    assert app.pre_tool_call(name, args)['action'] == 'approve'
    assert app.ledger.rows()[0]['cards'][0]['kind'] == 'transaction'


@pytest.mark.parametrize('extra', [{'execute': False}, {'quoteOnly': True}, {'mode': 'quote'}])
def test_quote_flags_keep_read_behavior(tmp_path, extra):
    app = service(tmp_path)
    assert app.pre_tool_call('get_swap_quote', {**SWAP, **extra}) is None
    assert all(c['kind'] != 'transaction' for r in app.ledger.rows() for c in r['cards'])


def test_raw_broadcast_is_unchecked_without_retaining_signature(tmp_path):
    app = service(tmp_path)
    directive = app.pre_tool_call('rpc', {'chainId': 4663, **RAW})
    assert 'transaction could not be checked' in directive['message']
    assert 'deadbeef' not in json.dumps(app.ledger.rows())


def test_explicit_other_chain_raw_send_stays_outside_scope():
    assert not tx_intents({'chainId': 1, **RAW}, 4663, 'get_rpc')


@pytest.mark.parametrize('name,args', [
    ('get_swap_quote', {'transaction': {**TX, 'value': '0'}}),
    ('rpc', {'chainId': 4663, **READ, 'next': {'method': 'eth_sendTransaction', 'params': [{**TX, 'value': '0'}]}}),
    ('terminal', {'command': "curl -d '" + json.dumps({'method': 'eth_sendTransaction', 'params': [{**TX, 'value': '0'}]}) + "' " + RPC}),
])
@pytest.mark.parametrize('completed', [False, True])
def test_send_requires_completed_toll_before_go(tmp_path, name, args, completed):
    from rh_preflight.settings import Settings
    from rh_preflight.network import MODULES
    app = service(tmp_path)
    app.settings = Settings(sources={n: 'on' if n == 'toll' else 'off' for n in MODULES})
    requests = []
    def check(payload, *unused):
        requests.append(payload)
        return {'x-toll': {'simulation': {'status': 'success', 'block': 1}}} if completed else None
    app.network.clients['toll'].check_tx = check
    directive = app.pre_tool_call(name, args)
    assert requests == [{**TX, 'value': '0'}]
    assert (directive is None) == completed
    if directive:
        assert directive['action'] == 'approve'
        assert 'transaction could not be checked' in directive['message']


@pytest.mark.parametrize('name,args', [
    ('rpc', {'chainId': 4663, **READ, 'next': RAW}),
    ('get_swap_quote', {**SWAP, 'execute': True}),
])
def test_unattended_write_exclusions_stop(tmp_path, name, args):
    app = service(tmp_path)
    app.presence = lambda: True
    assert app.pre_tool_call(name, args)['action'] == 'block'
