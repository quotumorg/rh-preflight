from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from rh_preflight.card import Card, token_card
from rh_preflight.policy import evaluate
from rh_preflight.settings import Settings
from demo.fake_hey import ACTIVE, QUIET, UNKNOWN, RESEARCHED, records


@pytest.mark.parametrize("mode,target,action", [
    ("ask", ACTIVE, "ask"), ("ask", QUIET, "ask"), ("ask_when", ACTIVE, "pass"),
    ("ask_when", QUIET, "ask"), ("ask_when", UNKNOWN, "ask"),
    ("note", ACTIVE, "note"), ("note", QUIET, "note"), ("note", UNKNOWN, "note"),
])
def test_modes(mode, target, action):
    card = token_card(target, records()[target])
    assert evaluate([card], Settings(mode=mode)).action == action


@pytest.mark.parametrize("mode", ["ask", "ask_when", "note"])
@pytest.mark.parametrize("target", [ACTIVE, QUIET, UNKNOWN])
@pytest.mark.parametrize("allow", [False, True])
def test_unattended(mode, target, allow):
    result = evaluate([token_card(target, records()[target])], Settings(mode=mode, allow_unattended=allow), unattended=True)
    assert result.action == ("note" if mode == "note" else "pass" if allow else "block")


def test_each_rule_is_optional():
    cards = [token_card(QUIET, records()[QUIET]), token_card(RESEARCHED, records()[RESEARCHED]), Card("example", "missing")]
    off = Settings(ask_quiet=False, ask_unmeasured=False, ask_no_record=False)
    assert evaluate(cards, off).action == "pass"
    assert evaluate(cards, replace(off, ask_not_verified=True)).rules == ("your rule: not a Verified Builder on HEY's record",)


def test_api_failure_asks_even_when_optional_rules_are_off():
    assert evaluate([Card("example", "unreachable")], Settings(ask_quiet=False, ask_unmeasured=False, ask_no_record=False)).action == "ask"


def test_unknown_is_not_quiet_zero():
    card = token_card(UNKNOWN, records()[UNKNOWN])
    result = evaluate([card], Settings(ask_unmeasured=False))
    assert result.action == "pass"


@pytest.mark.parametrize("days,expected", [(7, True), (30, True), (60, False)])
def test_configurable_quiet_days(days, expected):
    card = token_card(QUIET, records()[QUIET])
    assert bool(evaluate([card], Settings(quiet_days=days)).rules) is expected


def test_unknown_date_does_not_establish_sixty_day_absence():
    data = records()[QUIET]
    data["latest_meaningful_ship"] = None
    assert evaluate([token_card(QUIET, data)], Settings(quiet_days=60)).action == "pass"


def test_rules_deduplicated():
    card = token_card(QUIET, records()[QUIET])
    assert evaluate([card, card], Settings()).rules == ("your rule: no ship in 30 days",)


@pytest.mark.parametrize('mode', ['ask_when', 'ask', 'note'])
@pytest.mark.parametrize('unattended,allow', [(False, False), (True, False), (True, True)])
def test_target_explanations_separate_call_settings_and_preserve_decision(mode, unattended, allow):
    from rh_preflight.explanations import target_rules
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    cards = [Card('public-a', 'missing'), token_card(UNKNOWN, records()[UNKNOWN])]
    settings = Settings(mode=mode, allow_unattended=allow)
    before = evaluate(cards, settings, unattended=unattended, now=now)
    assert target_rules(cards, settings, now) == [
        [{'target': 'public-a', 'rule': 'your rule: no HEY record'}],
        [{'target': UNKNOWN, 'rule': 'your rule: activity not measured'}]]
    assert evaluate(cards, settings, unattended=unattended, now=now) == before


@pytest.mark.parametrize('offset,expected', [(-1, []), (0, ['your rule: no ship in 30 days']),
                                            (1, ['your rule: no ship in 30 days'])])
def test_target_explanations_share_exact_quiet_boundary(offset, expected):
    from rh_preflight.explanations import target_rules
    shipped = datetime(2026, 10, 10, tzinfo=timezone.utc)
    data = {**records()[QUIET], 'latest_meaningful_ship': {'timestamp': shipped.isoformat()}}
    cards = [token_card(QUIET, data), Card('public-b', 'missing')]
    now = shipped + timedelta(days=30, microseconds=offset)
    result = target_rules(cards, Settings(), now)
    assert [item['rule'] for item in result[0]] == expected
    assert evaluate(cards, Settings(), now=now).rules == tuple(expected + ['your rule: no HEY record'])


def test_target_explanations_keep_multiple_rules_on_their_own_card():
    from rh_preflight.explanations import target_rules
    data = {**records()[UNKNOWN], 'verified_builder': False}
    cards = [token_card(UNKNOWN, data), Card('public-b', 'missing')]
    settings = Settings(ask_not_verified=True)
    result = target_rules(cards, settings, datetime(2026, 11, 9, tzinfo=timezone.utc))
    assert result == [[{'target': UNKNOWN, 'rule': rule} for rule in (
        'your rule: activity not measured', "your rule: not a Verified Builder on HEY's record")],
        [{'target': 'public-b', 'rule': 'your rule: no HEY record'}]]
