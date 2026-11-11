"""Offline release-review regressions for executable argument shapes."""
import json
import sqlite3

import pytest

from rh_preflight.card import Card, WORDING
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.network import MODULES
from rh_preflight.settings import Settings
from rh_preflight.sources.common import Reading
from rh_preflight.transactions import MAX, tx_intents

A, B, C = ('0x' + c * 40 for c in 'abc')
SHAPES = [
    {'tokenIn': A, 'tokenOut': B, 'amount': '10', 'recipient': C, 'from': C},
    {'tokenIn': A, 'tokenOut': B, 'amountIn': '10', 'wallet': C},
    {'token': A, 'spender': B, 'amount': '10', 'from': C},
    {'recipient': B, 'amount': '10', 'from': C},
    {'payTo': B, 'amount': '10', 'wallet': C},
    {'to': B, 'amount': '10', 'from': C},
]


def app(tmp_path, **options):
    service = Service(settings=Settings(sources={n: 'off' for n in MODULES}, **options),
                      ledger=Ledger(tmp_path), presence=lambda: False)
    service._read_cards = lambda targets, *a, **kw: [Card(t, 'identifier') for t in targets]
    service.network.clients['toll'].check_tx = lambda *a: pytest.fail('no unsigned payload to simulate')
    return service


@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('unchecked,expected', [('ask', 'approve'), ('stop', 'block'), ('note', None)])
def test_unencoded_actions_do_not_claim_a_completed_check(tmp_path, shape, unchecked, expected):
    service = app(tmp_path, on_unchecked=unchecked, ask_payout_other=False)
    args = {'chainId': 4663, **shape}
    directive = service.pre_tool_call('wallet_action', args)
    assert (directive['action'] if directive else None) == expected
    row = service.ledger.rows()[0]
    assert row['cards'][0]['kind'] == 'transaction'
    assert 'without calldata' in row['cards'][0]['card']
    assert 'your rule: transaction could not be checked' in row['rules'] or unchecked == 'note'
    assert not WORDING.search(json.dumps(row))
    result = service.transform_tool_result('wallet_action', args, '{}')
    assert 'without calldata' in result


@pytest.mark.parametrize('shape', SHAPES)
def test_unencoded_actions_keep_explicit_simulation_off(tmp_path, shape):
    service = app(tmp_path, tx_check='off', ask_payout_other=False)
    args = {'chainId': 4663, **shape}
    assert service.pre_tool_call('wallet_action', args) is None
    assert service.ledger.rows()[0]['disposition'] == 'pass'


def test_unencoded_unlimited_approval_still_stops_for_no_code(tmp_path):
    service = app(tmp_path)
    service.settings = Settings(sources={n: 'on' if n == 'chain' else 'off' for n in MODULES})
    service.network.clients['chain'].lookup = lambda *a, **k: Reading('Robinhood Chain', 'no_code', '')
    directive = service.pre_tool_call('approve', {'chainId': 4663, 'token': A, 'spender': B, 'amount': str(MAX), 'from': C})
    assert directive['action'] == 'block'
    assert 'unlimited approval to a spender with no code' in directive['message']


@pytest.mark.parametrize('shape', SHAPES)
def test_action_shapes_respect_chain_boundary(shape):
    assert not tx_intents({'chainId': 1, **shape}, 4663)
    assert not tx_intents(shape)


def test_upgrade_053_config_and_ledger_remain_usable(tmp_path):
    # This is the unchanged 0.5.3 schema and public card payload.
    with sqlite3.connect(tmp_path / 'checks.db') as conn:
        conn.execute('CREATE TABLE checks (id TEXT PRIMARY KEY, time TEXT NOT NULL, session TEXT NOT NULL, tool_name TEXT NOT NULL, targets TEXT NOT NULL, cards TEXT NOT NULL, rules TEXT NOT NULL, decision TEXT, mode TEXT NOT NULL, disposition TEXT NOT NULL)')
        conn.execute('INSERT INTO checks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (
            'legacy', '2026-11-11T00:00:00+00:00', 'session', 'swap', json.dumps([A]),
            json.dumps([{'target': A, 'kind': 'token', 'card': 'legacy source facts', 'digest': 'old', 'evidence_ids': []}]),
            '[]', None, 'ask_when', 'ask'))
    ledger = Ledger(tmp_path)
    legacy = ledger.rows()[0]
    ledger.decide('legacy', 'deny')
    ledger.add('session', 'swap', [Card(A, 'identifier')], (), 'ask_when', 'pass')
    assert len(ledger.rows()) == 2
    assert next(row for row in ledger.rows() if row['id'] == 'legacy')['cards'] == legacy['cards']
    assert next(row for row in ledger.rows() if row['id'] == 'legacy')['decision'] == 'deny'
    class Context:
        def get_config(self, name, default):
            return {'sources': {'retired': 'invalid', 'toll': 'off'}, 'timeout': 4.0,
                    'unknown_future_key': 'ignored'}.get(name, default)
    settings = Settings.from_context(Context())
    assert settings.tx_check == 'on' and settings.toll_coin_findings == 'silent'
    assert settings.sources == {'toll': 'off'}


@pytest.mark.parametrize('envelope', [False, True])
@pytest.mark.parametrize('primary', ['SignIn', 'TransferWithAuthorization'])
def test_missing_signer_uses_unchecked_even_with_simulation_success(tmp_path, envelope, primary):
    service = app(tmp_path)
    service.settings = Settings(sources={n: 'on' if n == 'toll' else 'off' for n in MODULES})
    service.network.clients['toll'].check_tx = lambda *a: {'x-toll': {'simulation': {'status': 'success', 'block': 1}}}
    typed = {'domain': {'chainId': 4663, 'verifyingContract': A}, 'types': {primary: []},
             'primaryType': primary, 'message': {'from': C, 'to': B, 'value': '10'}}
    args = {'chainId': 4663, 'rpc': {'method': 'eth_signTypedData_v4', 'params': [None, typed]}} if envelope else typed
    directive = service.pre_tool_call('wallet_sign', args)
    assert directive['action'] == 'approve'
    assert 'signing wallet not recorded' in directive['message']
    assert 'transaction could not be checked' in directive['message']
