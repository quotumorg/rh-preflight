"""Adversarial regressions for the 0.5.6 Stock Token threshold review."""
from dataclasses import replace

import pytest

from rh_preflight.card import Card
from rh_preflight.policy import evaluate
from rh_preflight.settings import Settings
from rh_preflight.sources.toll import Client as Toll
from rh_preflight.stock_swap import RULE, annotate
from test_stock_swap_above_usd import A, B, AS_OF, stock


@pytest.mark.parametrize('rule', [{}, {RULE: None}, {RULE: 250}])
@pytest.mark.parametrize('missing', ['prices', 'chain', 'usd', 'as_of', 'reference', 'updated_at'])
def test_incomplete_toll_prices_preserve_existing_ask(rule, missing):
    data = stock()
    container = data if missing == 'prices' else data['prices'] if missing in {'chain', 'reference'} else (
        data['prices']['reference'] if missing == 'updated_at' else data['prices']['chain'])
    del container[missing]
    client = Toll()
    client._read = lambda *a, **kw: (200, data)
    reading = client.lookup(A)
    card = Card(A, 'network', {'base': Card(A, 'missing'), 'sources': [reading]})
    decision = evaluate([card], Settings(ask_when=rule))
    assert decision.action == 'ask'
    assert 'your rule: no HEY record' in decision.rules
    assert reading.state == 'unreachable'
    assert reading.line == client.unavailable(A).line
