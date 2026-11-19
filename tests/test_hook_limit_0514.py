"""A Toll WAIT inside pre_tool_call ends within Hermes's hook budget."""
import time

import pytest

from rh_preflight.card import Card
from rh_preflight.hooks import Service, HOOK_LIMIT
from rh_preflight.ledger import Ledger
from rh_preflight.network import MODULES
from rh_preflight.settings import Settings
from test_toll_0511 import A, B, synthetic
from test_toll_u3 import Clock, client, wait

ARGS = {'chainId': 4663, 'tokenIn': B, 'tokenOut': A, 'amount': '1'}


def dispatched(tmp_path, settings, hook):
    clock, waits = Clock(), []
    class Executor:
        def submit(self, fn, *args, **kwargs):
            return {'tx': fn.__name__ == '_tx_cards', 'args': args}
    def wait_for(future, deadline, fallback):
        waits.append(deadline)
        if future['tx']:
            future['args'][-1].set()
            return fallback
        return [Card(A, 'identifier')]
    service = Service(settings, ledger=Ledger(tmp_path), presence=lambda: False,
                      clock=clock, executor=Executor(), wait=wait_for)
    if hook:
        service.pre_tool_call('swap', ARGS)
    else:
        service.check('swap', ARGS, settings)
    return waits


@pytest.mark.parametrize('ceiling,last', [(0, 104), (12, 116), (21, 125), (30, 125), (120, 125)])
def test_hook_wait_never_passes_the_hook_limit(tmp_path, ceiling, last):
    waits = dispatched(tmp_path, Settings(timeout=4, toll_wait_max_seconds=ceiling), hook=True)
    assert waits[-1] == pytest.approx(last) and max(waits) <= 100 + HOOK_LIMIT


@pytest.mark.parametrize('timeout', [4, 15, 25])
def test_hook_limit_holds_for_every_builder_timeout(tmp_path, timeout):
    waits = dispatched(tmp_path, Settings(timeout=timeout, toll_wait_max_seconds=120), hook=True)
    assert max(waits) <= 100 + HOOK_LIMIT


def test_direct_check_keeps_the_full_wait_outside_hermes(tmp_path):
    waits = dispatched(tmp_path, Settings(timeout=4, toll_wait_max_seconds=30), hook=False)
    assert waits[-1] == pytest.approx(134)


def run_hook(tmp_path, monkeypatch, controlled_budget, answers, durations=()):
    toll, clock, calls = client(answers, durations)
    settings = Settings(timeout=4, toll_wait_max_seconds=30,
                        sources={name: 'on' if name == 'toll' else 'off' for name in MODULES})
    service = Service(settings, ledger=Ledger(tmp_path), presence=lambda: False, clock=clock)
    service.network.clients['toll'] = toll
    service.client.lookup = lambda *args, **kwargs: Card(A, 'identifier')
    controlled_budget(lambda *args: False, service)
    monkeypatch.setattr(time, 'monotonic', clock)
    toll.clock = clock
    service.pre_tool_call('swap', ARGS)
    return service, clock, calls


def test_wait_longer_than_the_hook_budget_is_not_retried(tmp_path, monkeypatch, controlled_budget):
    service, clock, calls = run_hook(tmp_path, monkeypatch, controlled_budget, [wait(22)])
    assert clock.sleeps == [] and len(calls) == 1
    assert clock.now - 100 <= HOOK_LIMIT


def test_wait_inside_the_hook_budget_retries_under_it(tmp_path, monkeypatch, controlled_budget):
    service, clock, calls = run_hook(tmp_path, monkeypatch, controlled_budget, [wait(20), synthetic('BLOCK')])
    output = service.ledger.rows()[0]['cards'][0]['card']
    assert 'Toll before buy: BLOCK' in output and clock.sleeps == [20]
    assert all(row[0] <= 100 + HOOK_LIMIT for row in calls)


def test_slow_retry_after_wait_stays_silent_at_the_hook_limit(tmp_path, monkeypatch, controlled_budget):
    service, clock, calls = run_hook(tmp_path, monkeypatch, controlled_budget,
                                     [wait(20), synthetic('BLOCK')], durations=(0, 10))
    output = service.ledger.rows()[0]['cards'][0]['card']
    assert 'Toll before buy: BLOCK' not in output
    assert all(row[0] <= 100 + HOOK_LIMIT for row in calls)
