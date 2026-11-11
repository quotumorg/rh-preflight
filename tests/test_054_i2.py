"""Offline stable-code, metadata, scanner-link and independent-budget checks."""
from copy import deepcopy
import json
from pathlib import Path
import threading
import time

import pytest

from rh_preflight.card import Card, WORDING
from rh_preflight.extract import tx_intent
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.network import Network, MODULES
from rh_preflight.policy import evaluate
from rh_preflight.presentation import present
from rh_preflight.settings import Settings
from rh_preflight.sources import hookr, toll
from rh_preflight.transactions import local_card, project, TOLL_CODES

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/toll-codes'
A, B, C = ('0x' + c * 40 for c in 'abc')
REQUEST = {'to': A, 'data': '0x12345678', 'chainId': 4663, 'from': B}
CASES = json.loads((FIXTURES / 'synthetic.json').read_text())['cases']


def card_for(response):
    intent = tx_intent(REQUEST)[0]
    return project(local_card(intent), intent, response)


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['code'])
def test_all_stable_codes_minimal_synthetic(case):
    card = card_for(case['response'])
    decision = evaluate([card], Settings())
    output = present(card, decision).render() + '\n'
    assert decision.action == case['action']
    assert output == (FIXTURES / (case['code'] + '.txt')).read_text()
    assert not WORDING.search(output)
    assert 'remote message' not in output
    if case.get('line'):
        assert case['line'] in output
    else:
        baseline = deepcopy(case['response'])
        baseline['warnings'] = []
        assert card.data == card_for(baseline).data


def test_allowlist_matches_provider_list():
    assert set(TOLL_CODES) == {case['code'] for case in CASES}
    assert len(TOLL_CODES) == 14


@pytest.mark.parametrize('code', ['new-code', 'swap-fcfs-note', 'coin-read-stale', 'sim-reverted-new'])
def test_unknown_codes_have_no_line_or_rule(code):
    baseline = {'x-toll': {'simulation': {'status': 'success', 'block': 10}}}
    changed = {**baseline, 'action': 'BLOCK', 'warnings': [
        {'code': code, 'severity': 'critical', 'message': 'remote message',
         'x-toll-evidence': {'token': C, 'spender': B, 'delegate': A}}]}
    assert card_for(changed).data == card_for(baseline).data
    assert evaluate([card_for(changed)], Settings()).action == 'pass'


@pytest.mark.parametrize('code', ['unbounded-approval-to-eoa', 'approval-for-all-to-eoa'])
@pytest.mark.parametrize('allow', [False, True])
def test_eoa_code_stops_even_without_addresses_or_severity(code, allow):
    card = card_for({'warnings': [{'code': code}], 'x-toll': {'simulation': {'status': 'success'}}})
    assert evaluate([card], Settings(mode='note', allow_unattended=allow), unattended=True).action == 'block'


@pytest.mark.parametrize('code', ['typed-data-unread', 'coin-partly-read'])
@pytest.mark.parametrize('setting,expected', [('ask', 'ask'), ('stop', 'block'), ('note', 'pass')])
def test_partial_and_signature_follow_unchecked(code, setting, expected):
    case = next(case for case in CASES if case['code'] == code)
    assert evaluate([card_for(case['response'])], Settings(on_unchecked=setting)).action == expected


@pytest.mark.parametrize('placement', ['coins', 'decoded', 'response', 'delta', 'nested-token'])
def test_amounts_use_only_address_bound_structured_metadata(placement):
    row = {'token': C, 'symbol': 'USDG', 'decimals': 6}
    delta = {'holder': B, 'token': C, 'delta': '12345678901234567890123456789'}
    response = {'x-toll': {'simulation': {'status': 'success', 'block': 10, 'deltas': [delta]}},
                'warnings': [{'code': 'coin-careful', 'message': 'remote message 18 OTHER'}]}
    x = response['x-toll']
    if placement == 'response':
        response['coins'] = [row]
    elif placement == 'delta':
        delta.update(row)
    elif placement == 'nested-token':
        x['decoded'] = [{'kind': 'swap', 'bought': [{'address': C, 'symbol': 'USDG', 'decimals': 6}]}]
    else:
        x[placement] = [row]
    output = present(card_for(response), evaluate([card_for(response)], Settings())).render()
    assert '+12345678901234567890123.456789 USDG' in output
    assert 'OTHER' not in output


@pytest.mark.parametrize('decimals', [None, -1, 256, True, '18'])
def test_missing_or_invalid_decimals_keep_base_units_and_short_address(decimals):
    response = {'x-toll': {'simulation': {'status': 'success', 'deltas': [
        {'holder': B, 'token': C, 'delta': '-123', 'decimals': decimals}]}}}
    assert 'simulated sender balance change: -123 0xcccc…cccc base units' in card_for(response).data['lines']


def test_symbol_and_decimals_join_without_reading_message():
    response = {'x-toll': {'coins': [{'token': C, 'sym': '$CLEAR'}],
        'decoded': [{'token': C, 'decimals': 0}],
        'simulation': {'status': 'success', 'deltas': [{'token': C, 'holder': B, 'delta': '17'}]}}}
    assert 'simulated sender balance change: +17 $CLEAR' in card_for(response).data['lines']


@pytest.mark.parametrize('value', [0, -1, 25.01, 26, float('inf'), float('nan'), True, '15', None])
def test_tx_timeout_validation(value):
    with pytest.raises(ValueError):
        Settings(tx_timeout=value)


def test_tx_timeout_defaults_and_maximum():
    assert Settings().tx_timeout == 15
    assert Settings(tx_timeout=25).tx_timeout == 25


@pytest.mark.parametrize('delay,limit,simulated', [(14, 15, True), (26, 25, False)])
def test_real_slow_toll_stub_and_parallel_builder_budget(tmp_path, delay, limit, simulated):
    service = Service(settings=Settings(tx_timeout=limit), ledger=Ledger(tmp_path), presence=lambda: False)
    started = threading.Event()
    finished = threading.Event()
    def builders(targets, *args, **kwargs):
        assert started.wait(1)
        time.sleep(4)
        finished.set()
        return [Card(target, 'identifier') for target in targets]
    def simulation(*args):
        started.set()
        time.sleep(delay)
        return {'x-toll': {'simulation': {'status': 'success', 'block': 10}}}
    service._read_cards = builders
    service.network.clients['toll'].check_tx = simulation
    start = time.monotonic()
    directive = service.pre_tool_call('swap', REQUEST)
    elapsed = time.monotonic() - start
    assert finished.is_set()
    assert elapsed < limit + .5
    output = service.ledger.rows()[0]['cards'][0]['card']
    assert ('Toll: simulated at block 10' in output) == simulated
    assert ('Toll: not simulated' in output) != simulated
    if not simulated:
        assert directive['action'] == 'approve'
        assert 'transaction could not be checked' in output


@pytest.mark.parametrize('unchecked,action', [('ask', 'approve'), ('stop', 'block'), ('note', None)])
def test_short_tx_timeout_policy_and_slow_builder_do_not_add_budgets(tmp_path, unchecked, action):
    settings = Settings(timeout=.12, tx_timeout=.04, on_unchecked=unchecked)
    service = Service(settings=settings, ledger=Ledger(tmp_path), presence=lambda: False)
    def builders(targets, *args, **kwargs):
        time.sleep(.09)
        return [Card(target, 'identifier') for target in targets]
    def simulation(*args):
        time.sleep(.07)  # After the tx budget, before builder completion.
        return {'x-toll': {'simulation': {'status': 'success', 'block': 10}}}
    service._read_cards = builders
    service.network.clients['toll'].check_tx = simulation
    start = time.monotonic()
    directive = service.pre_tool_call('swap', REQUEST)
    assert time.monotonic() - start < .15
    assert (directive['action'] if directive else None) == action
    assert 'Toll: not simulated' in service.ledger.rows()[0]['cards'][0]['card']


def test_healthy_tx_transport_gets_full_first_attempt():
    client = toll.Client(clock=lambda: 10)
    seen = []
    client._read_once = lambda path, deadline, payload: (seen.append(deadline), (200, {'x-toll': {}}))[1]
    assert client.check_tx({}, 25) == {'x-toll': {}}
    assert seen == [25]


def test_hookr_unknown_hook_links_but_unknown_token_and_zero_hook_do_not():
    client = hookr.Client()
    pools = {'0x' + 'd' * 64: {'hook': {'address': A}}}
    line = 'Hookr: not in the catalog · scan https://hookr.fun/scan?hook=' + A
    for target in (A, next(iter(pools))):
        row = client.parse(target, pools, {})
        assert row.state == 'unlisted' and row.line == line
        assert not WORDING.search(row.line)
    assert client.parse(A, {}, {}, is_hook=True).line == line
    assert client.parse(B, pools, {}).line == ''
    assert client.parse('0x' + '0' * 40, {}, {}, is_hook=True).line == ''
    assert client.parse('0x' + 'f' * 64, {}, {}, is_hook=True).line == ''


def test_network_explicit_hook_is_not_a_catalog_spender():
    network = Network()
    answers = {'/api/hooks/catalog': {'root': {}}, '/api/hooks/integrations': {'integrations': []},
               '/api/discover/pools': {'pools': []}}
    network.clients['hookr']._read = lambda path, *args: (200, answers[path])
    settings = Settings(sources={name: 'on' if name == 'hookr' else 'off' for name in MODULES})
    card = network.enrich([Card(A, 'identifier')], 'swap', {'hook': A}, settings)[0]
    assert 'Hookr: not in the catalog · scan https://hookr.fun/scan?hook=' + A in card.render()
    assert not any(row.source == 'Hookr' and row.state == 'present' for row in card.data['sources'])


def test_wording_filter_on_every_projected_transaction_fixture():
    for path in (ROOT / 'tests/fixtures/toll-tx').glob('*/request.json'):
        intent = tx_intent(json.loads(path.read_text()))[0]
        card = project(local_card(intent), intent, json.loads(path.with_name('response.json').read_text()))
        assert not WORDING.search(present(card, evaluate([card], Settings())).render())


def test_readme_outputs_are_all_generated_from_code():
    import re
    from demo.tx_replay import readme_outputs
    actual = re.findall(r'```text\n(.*?)```', (ROOT / 'README.md').read_text(), flags=re.S)
    assert actual == list(readme_outputs())


def test_builder_budget_stays_short_while_toll_finishes(tmp_path):
    service = Service(settings=Settings(timeout=.04, tx_timeout=.12),
                      ledger=Ledger(tmp_path), presence=lambda: False)
    def builders(targets, *args, **kwargs):
        time.sleep(.07)
        return [Card(target, 'identifier') for target in targets]
    def simulation(*args):
        time.sleep(.09)
        return {'x-toll': {'simulation': {'status': 'success', 'block': 10}}}
    service._read_cards = builders
    service.network.clients['toll'].check_tx = simulation
    directive = service.pre_tool_call('swap', REQUEST)
    assert directive['action'] == 'approve'
    assert 'Toll: simulated at block 10' in directive['message']
    assert 'ask when HEY could not be reached' in directive['message']
