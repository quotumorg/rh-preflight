"""Final 0.5.7 review regressions, using recorded bodies and synthetic dispatch."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from demo.fake_hey import fake_server
from rh_preflight.card import Card
from rh_preflight.hey import Client as Hey
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.network import MODULES
from rh_preflight.policy import evaluate
from rh_preflight.settings import Settings
from rh_preflight.sources.toll import Client as Toll
from rh_preflight.transactions import local_card, project, tx_intents


ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / 'tests' / 'fixtures' / '057'
USDG = '0x5fc5360d0400a0fd4f2af552add042d716f1d168'
SLUG = 'rajkaria-hunch-vpm-rh'


@pytest.mark.parametrize('fixed_dispatch', [False, True])
def test_deferred_plugin_action_bridge_gate(tmp_path, fixed_dispatch):
    # Model only the host boundary documented by PR #155527:
    # model_tools.py:945 at v0.23.1 forwards skip flags to the inner dispatch;
    # canary 20261115 lines 959-966 let the real tool run pre_tool_call.
    # This is a dispatch simulation, not execution of either host version.
    service = Service(settings=Settings(mode='ask', sources={s: 'off' for s in MODULES}),
                      ledger=Ledger(tmp_path), presence=lambda: False)
    reads = []
    def lookup(target, **kwargs):
        reads.append(target)
        return Card(target, 'missing')
    service.client.lookup = lookup
    real_name = 'plugin_wallet_swap'
    real_args = {'chainId': 4663, 'token': USDG}
    bridge_args = {'tool_name': real_name, 'arguments': real_args}
    ids = {'session_id': 'bridge-session', 'tool_call_id': 'bridge-call'}
    # The outer bridge name has no action evidence of its own.
    assert service.pre_tool_call('tool_call', bridge_args, **ids) is None
    assert not service.ledger.rows() and not reads
    directive = service.pre_tool_call(real_name, real_args, **ids) if fixed_dispatch else None
    executions = []
    if directive is None:
        executions.append((real_name, deepcopy(real_args)))  # record only, never execute
    if fixed_dispatch:
        assert directive['action'] == 'approve'
        assert 'ask before every detected action' in directive['message']
        assert reads == [USDG] and not executions
        row, = service.ledger.rows()
        assert row['tool_name'] == real_name and row['targets'] == [USDG]
        assert row['disposition'] == 'ask' and row['decision'] is None
    else:
        assert executions == [(real_name, real_args)]
        assert not service.ledger.rows() and not reads


@pytest.mark.parametrize('name', ['permit-unlimited', 'eip3009-5usdg'])
def test_recorded_pull_block_through_real_http_transport(name):
    # Exercise the actual HTTP reader and precision-preserving decoder, not a
    # mocked project() result. Only the source is replaced by a loopback server.
    raw = (CAPTURES / 'toll-live' / ('res-' + name + '.json')).read_bytes()
    request = json.loads((CAPTURES / 'toll-live' / ('req-' + name + '.json')).read_bytes())
    intent = tx_intents(request)[0]
    with fake_server() as (origin, state):
        state['overrides']['/recorded-take'] = (200, raw, {})
        client = Toll(base_url=origin)
        _, response = client._read('/recorded-take', client.clock() + 4)
        card = project(local_card(intent), intent, response)
        row, = card.data['take']['takes']
        assert row['simulated']['block'] == row['block'] + 1
        assert f"Toll: pull simulated at block {row['simulated']['block']}" in card.render()
        assert f"Toll: pull simulated at block {row['block']}" not in card.render()
        assert not card.data['unchecked']
        assert evaluate([card], Settings()).action == ('block' if name == 'permit-unlimited' else 'ask')
        assert [path for path, headers in state['requests']] == ['/recorded-take']


@pytest.mark.parametrize('missing', ['absent', 'null', 'mixed'])
def test_pull_without_complete_simulation_blocks_labels_take_read(missing):
    request = json.loads((CAPTURES / 'toll-live' / 'req-eip3009-5usdg.json').read_bytes())
    response = json.loads((CAPTURES / 'toll-live' / 'res-eip3009-5usdg.json').read_bytes())
    rows = response['x-toll']['take']['takes']
    if missing == 'mixed':
        rows.append(deepcopy(rows[0]))
    if missing == 'null':
        rows[0]['simulated']['block'] = None
    else:
        rows[0]['simulated'].pop('block')
    intent = tx_intents(request)[0]
    card = project(local_card(intent), intent, response)
    assert f"Toll: pull simulated; take read at block {rows[0]['block']}" in card.render()
    assert 'Toll: pull simulated at block' not in card.render()
    assert not card.data['unchecked']


def test_context_only_capture_through_real_http_transport():
    with fake_server() as (origin, state):
        paths = ['/api/agent/builder_status?address=' + USDG, '/api/projects/' + SLUG]
        captures = ['bs-usdg.json', 'project-rajkaria-hunch-vpm-rh.json']
        for path, name in zip(paths, captures):
            state['overrides'][path] = (200, (CAPTURES / 'hey-live' / name).read_bytes(), {})
        card = Hey(origin).lookup(USDG)
        assert card.kind == 'context'
        assert card.data == {'context_only': True, 'context_reason': 'third_party_contract'}
        assert card.render() == "HEY: shared contract, not one project's own"
        assert [path for path, headers in state['requests']] == paths
