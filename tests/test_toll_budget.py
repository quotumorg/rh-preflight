"""Toll's tx route is told how long the hook will wait (budgetMs), so a slow coin read cannot stall it."""
import pytest

from rh_preflight.extract import tx_intent
from rh_preflight.hey import TemporaryUnavailable
from rh_preflight.sources.toll import Client as Toll, BUDGET_MAX_MS, BUDGET_MIN_MS, BUDGET_ROUND_TRIP
from rh_preflight.transactions import local_card, project, short_address

A = '0x' + 'a' * 40
COIN = '0xff9e86e6b21fd688027981bee4b64c8b6b804ed3'


def recording(client, answer=None):
    seen = []
    def read(path, deadline, payload=None):
        seen.append((path, deadline, payload))
        return answer or (200, {'x-toll': {}})
    client._read_once = read
    return seen


def test_budget_is_time_left_less_round_trip_and_keeps_the_payload():
    client = Toll(clock=lambda: 10)
    seen = recording(client)
    payload = {'chainId': 4663, 'to': A, 'data': '0x', 'value': '1'}
    assert client.check_tx(payload, 14) == {'x-toll': {}}
    assert seen == [('/api/tx/check', 14, {**payload, 'budgetMs': 3600})]
    # The caller's own payload is not changed.
    assert 'budgetMs' not in payload


def test_retry_states_the_smaller_budget_left():
    ticks = [10.0]
    client = Toll(clock=lambda: ticks[0])
    budgets = []
    def read(path, deadline, payload=None):
        budgets.append(payload['budgetMs'])
        if len(budgets) == 1:
            ticks[0] = 12.0
            raise TemporaryUnavailable()
        return 200, {'x-toll': {}}
    client._read_once = read
    assert client.check_tx({}, 14) == {'x-toll': {}}
    assert budgets == [3600, 1600]


@pytest.mark.parametrize('deadline, expected', [
    (1000, BUDGET_MAX_MS),
    (float('inf'), BUDGET_MAX_MS),
    (10 + BUDGET_ROUND_TRIP + BUDGET_MIN_MS / 1000 + .5, BUDGET_MIN_MS + 500),
    (10.5, None),
    (10, None),
    (9, None),
    (float('nan'), None),
])
def test_budget_bounds(deadline, expected):
    client = Toll(clock=lambda: 10)
    sent = client.budgeted({'to': A}, deadline)
    assert sent.get('budgetMs') == expected
    assert sent == ({'to': A} if expected is None else {'to': A, 'budgetMs': expected})
    assert expected is None or (type(sent['budgetMs']) is int and BUDGET_MIN_MS <= expected <= BUDGET_MAX_MS)


def test_only_the_tx_route_gets_a_budget():
    client = Toll(clock=lambda: 10)
    seen = recording(client, (200, {'jsonrpc': '2.0', 'id': 1, 'result': {}}))
    client._read('/mcp', 14, {'jsonrpc': '2.0', 'id': 1})
    client._read('/api/markets/stock/NVDA', 14)
    assert [payload for _, _, payload in seen] == [{'jsonrpc': '2.0', 'id': 1}, None]


def test_coin_not_read_inside_the_budget_is_shown_as_not_read_beside_the_simulation():
    # What Toll answers when the coin read did not finish inside budgetMs:
    # the simulation is complete and the coin carries the stable coin-not-read code.
    response = {'action': 'WARN',
                'warnings': [{'code': 'coin-not-read', 'severity': 'medium',
                              'x-toll-evidence': {'token': COIN, 'retry_after_s': 9}}],
                'x-toll': {'simulation': {'status': 'success', 'block': 110000000}}}
    client = Toll(clock=lambda: 10)
    recording(client, (200, response))
    intent = tx_intent({'chainId': 4663, 'to': A, 'data': '0x', 'value': '1'})[0]
    card = project(local_card(intent), intent, client.check_tx(intent['payload'], 12))
    lines = card.data['lines']
    assert f'Toll has not read {short_address(COIN)} yet' in lines
    assert any(line.startswith('Toll: simulated at block ') for line in lines)
    assert 'Toll: not simulated' not in lines


def test_no_answer_inside_the_deadline_stays_not_simulated():
    client = Toll(clock=lambda: 10)
    def late(path, deadline, payload=None):
        raise TemporaryUnavailable()
    client._read_once = late
    intent = tx_intent({'chainId': 4663, 'to': A, 'data': '0x', 'value': '1'})[0]
    card = project(local_card(intent), intent, client.check_tx(intent['payload'], 12))
    assert 'Toll: not simulated' in card.data['lines']
    assert card.data['unchecked'] is True
