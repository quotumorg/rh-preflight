"""Read-only tools must not acquire transaction approval prompts."""
import json

import pytest

from rh_preflight.card import Card
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.network import MODULES
from rh_preflight.settings import Settings
from rh_preflight.transactions import tx_intents

A, B, C = ('0x' + c * 40 for c in 'abc')
RPC = 'https://rpc.mainnet.chain.robinhood.com'
SWAP = {'chainId': 4663, 'tokenIn': A, 'tokenOut': B, 'amountIn': '10', 'recipient': C}
APPROVAL = {'chainId': 4663, 'token': A, 'spender': B, 'amount': '10'}
PAYMENT = {'chainId': 4663, 'recipient': B, 'amount': '10'}
TX = {'chainId': 4663, 'from': C, 'to': A, 'data': '0x', 'value': '1'}
TYPED = {'domain': {'chainId': 4663}, 'types': {'SignIn': []},
         'primaryType': 'SignIn', 'message': {'to': A, 'value': '10'}}
READS = [
    ('get_swap_quote', SWAP), ('quote_swap', SWAP), ('dex__getQuote', SWAP),
    ('estimate_swap', SWAP), ('price_lookup', SWAP), ('research', SWAP),
    ('get_allowance', APPROVAL), ('read_allowance', APPROVAL),
    ('search_payments', PAYMENT), ('fetch_payment', PAYMENT),
    ('read_token_page', {'url': 'https://ponsfamily.com/launchpad/' + A}),
    ('web_search', {'query': json.dumps(SWAP)}),
    ('research', {'example': json.dumps(TYPED)}),
    ('read_file', {'content': json.dumps(TX)}),
    ('terminal', {'command': f"cast call {A} 'balanceOf(address)' {C} --rpc-url {RPC}"}),
    ('terminal', {'command': f"cast call {A} 'approve(address,uint256)' {B} 10 --rpc-url {RPC}"}),
    ('terminal', {'command': 'hermes plugins install https://github.com/example/plugin'}),
    ('plugins_install', {'repo': 'https://github.com/example/plugin', 'config': SWAP}),
    ('rpc', {'chainId': 4663, 'method': 'eth_call', 'params': [TX, 'latest']}),
    ('rpc', {'chainId': 4663, 'rpc': {'method': 'eth_estimateGas', 'params': [TX]}}),
    ('terminal', {'command': "curl -d '" + json.dumps({'method': 'eth_call', 'params': [TX, 'latest']}) + "' " + RPC}),
]


def service(tmp_path):
    app = Service(settings=Settings(sources={n: 'off' for n in MODULES}),
                  ledger=Ledger(tmp_path), presence=lambda: False)
    app._read_cards = lambda targets, *a, **kw: [Card(t, 'identifier') for t in targets]
    app.network.clients['toll'].check_tx = lambda *a: pytest.fail('read must not simulate')
    return app


@pytest.mark.parametrize('name,args', READS)
def test_read_only_shapes_do_not_ask(tmp_path, name, args):
    app = service(tmp_path)
    assert app.pre_tool_call(name, args) is None
    rows = app.ledger.rows()
    assert all(row['disposition'] == 'pass' for row in rows)
    assert all(card['kind'] != 'transaction' for row in rows for card in row['cards'])


@pytest.mark.parametrize('name,args', [
    ('swap', SWAP), ('approve', APPROVAL), ('transfer', PAYMENT),
    ('wallet_action', SWAP), ('wallet_sign', TYPED),
    ('terminal', {'command': f'cast send {A} --value 1 --chain 4663'}),
    ('rpc', {'method': 'eth_sendTransaction', 'params': [TX]}),
    ('get_quote_and_execute', SWAP), ('estimate_then_swap', SWAP),
    ('get_send_transaction', TX),
    ('get_rpc', {'method': 'eth_sendTransaction', 'params': [TX]}),
    ('get_rpc', {'chainId': 4663, 'method': 'eth_signTypedData_v4', 'params': [C, TYPED]}),
])
def test_write_shapes_still_ask(tmp_path, name, args):
    app = service(tmp_path)
    assert app.pre_tool_call(name, args)['action'] == 'approve'
    assert app.ledger.rows()[0]['cards'][0]['kind'] == 'transaction'


def test_read_rpc_in_batch_does_not_hide_send():
    requests = [{'method': 'eth_call', 'params': [TX, 'latest']},
                {'method': 'eth_sendTransaction', 'params': [{**TX, 'value': '2'}]}]
    intents = tx_intents(requests)
    assert len(intents) == 1
    assert intents[0]['payload']['value'] == '2'
