"""Every transaction rule remains readable without repeating its heading."""
import pytest

from demo.hookr_replay import cards
from rh_preflight.policy import evaluate
from rh_preflight.presentation import present
from rh_preflight.settings import Settings
from test_054 import fixture_card


@pytest.mark.parametrize('name', ['clean-swap', 'payout-other-wallet', 'replayed-swap', 'unlimited-approval-eoa'])
def test_capture_rules_are_named_once(name):
    card = fixture_card(name)
    decision = evaluate([card], Settings())
    output = present(card, decision).render()
    for rule in decision.rules:
        assert output.count(rule) == 1


def test_hookr_capture_heading_is_not_repeated():
    for name, output in cards():
        if name == 'payout-other-wallet':
            assert output.count('your rule: Hookr pool rules refuse this wallet') == 1
            assert 'your rule: transaction could not be checked' in output
