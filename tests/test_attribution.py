"""Attribution and disclosure regressions, using only bundled or invented records."""
import json
from pathlib import Path

import pytest

from demo.capture_replay import card_readings
from demo.captures import capture_path
from demo.fake_hey import fixture, records
from demo.fake_network import agent_payload
from demo.r6_replay import cards
from rh_preflight.card import Card, agent_card, contract_card, link, stamp, text, token_card
from rh_preflight.policy import evaluate
from rh_preflight.settings import Settings
from rh_preflight.sources.common import Reading


@pytest.mark.parametrize('target', ['agent #7622', '0x' + 'ab' * 32])
@pytest.mark.parametrize('wrapped', [False, True])
def test_identifier_does_not_invent_a_hey_measurement(target, wrapped):
    card = Card(target, 'identifier')
    assert card.render().splitlines()[-1] == 'HEY not queried for agent or pool ids'
    if wrapped:
        card = Card(target, 'network', {'base': card, 'sources': []})
    decision = evaluate([card], Settings(ask_not_verified=True))
    assert decision.action == 'pass' and decision.rules == ()
    assert evaluate([card], Settings(mode='ask')).action == 'ask'
    assert evaluate([card], Settings(), unattended=True).action == 'block'


def test_identifier_retains_applicable_payment_rule():
    card = Card('agent #7622', 'network', {
        'base': Card('agent #7622', 'identifier'), 'is_payment': True,
        'sources': [Reading('Priors', 'missing', 'Priors has no record')],
    })
    result = evaluate([card], Settings())
    assert result.action == 'ask'
    assert result.rules == ('your rule: payee has no Priors record',)


@pytest.mark.parametrize('error', [None, 'reply was not readable'])
def test_unread_record_does_not_claim_hey_answered(error):
    view = Card('example', 'unreachable', {'read_error': error}).render()
    assert view.count('HEY record not read for this check') == 2
    assert 'HEY does not know' not in view


def test_missing_badge_is_absent_from_answer():
    payload = agent_payload()
    payload['claims'] = []
    view = agent_card('example', payload).render()
    assert "Verified Builder: not in HEY's answer" in view
    assert view.startswith('HEY Research Lab:')


@pytest.mark.parametrize('dash', [chr(0x2013), chr(0x2014)])
def test_citation_keeps_hey_text_except_required_punctuation(dash):
    payload = agent_payload()
    citation = 'Source: HEY Research Lab, builder research for Robinhood Chain'
    payload['citation']['text'] = citation + ' ' + dash + ' https://example.invalid/project'
    assert agent_card('example', payload).render().splitlines()[-1] == citation + ', https://example.invalid/project'
    assert text('a' + dash + 'b') == 'a, b'


def test_saved_citation_keeps_upstream_name_and_uses_comma():
    import json
    payload = json.loads(capture_path('hey-builder_status-addr.json').read_text())
    citation = payload['citation']['text']
    assert 'Source: HEY Research Lab,' in citation
    assert 'Robinhood Chain, https://' in citation
    assert agent_card('example', payload).render().splitlines()[-1] == citation


def test_collection_has_source_lines_without_policy_or_coverage():
    collection = card_readings()
    assert collection.render().splitlines() == [r.line for r in collection.sources if r.line]
    assert '11 sources' not in collection.render()
    assert not any(line.startswith(('go ·', 'ask ·', 'stop ·')) for line in collection.render().splitlines())


def test_synthetic_records_link_to_example_domain():
    assert 'https://heyresearch.xyz' not in str(records())
    assert 'https://heyresearch.xyz' not in str(fixture('contract.json'))
    assert 'https://heyresearch.xyz' not in str(agent_payload())


def test_readme_offline_card_matches_bundled_replay():
    readme = (Path(__file__).resolve().parents[1] / 'README.md').read_text()
    nvda = cards()[1]
    assert nvda.render() in readme
    assert nvda.render().startswith('if Hermes swapped this now: ask · your rule: HEY record could not be read')
    assert 'HEY (0.0 s)' not in nvda.render()


@pytest.mark.parametrize('value', [None, ''])
def test_absent_field_date_and_link_do_not_claim_hey_ignorance(value):
    assert text(value) == "not in HEY's answer"
    assert stamp(value) == "date not in HEY's answer"
    assert link(value) == "link not in HEY's answer"


@pytest.mark.parametrize('kind', ['token', 'contract', 'missing', 'agent'])
def test_sparse_answer_does_not_invent_unknown_states(kind):
    card = agent_card('example', {}) if kind == 'agent' else Card('example', kind)
    view = card.render()
    assert 'HEY does not know' not in view
    assert "not in HEY's answer" in view


@pytest.mark.parametrize('measured', [True, False, None])
def test_missing_ship_is_not_a_claim_that_no_ship_exists(measured):
    view = token_card('example', {'activity_measured': measured}).render()
    assert "Latest meaningful ship: not in HEY's answer" in view
    assert 'HEY has no ship on record' not in view
    assert 'HEY does not know' not in view


def test_activity_unknown_applies_only_to_activity_not_other_missing_fields():
    view = token_card('example', {'hey_status': 'UNKNOWN', 'activity_measured': False}).render()
    assert view.count('HEY does not know') == 1
    assert 'HEY does not know current building activity; activity not measured' in view
    assert "Latest meaningful ship: not in HEY's answer" in view
    assert "Latest change: not in HEY's answer" in view


@pytest.mark.parametrize('state', ['UNKNOWN', 'NOT_READ', 'MEASURED', None])
def test_verified_source_unknown_requires_its_own_state(state):
    view = contract_card('example', {'verifiedSource': {'state': state}}).render()
    expected = 'HEY does not know' if state == 'UNKNOWN' else "not in HEY's answer"
    assert f'Source published: {expected} (HEY)' in view
    assert view.count('HEY does not know') == int(state == 'UNKNOWN')


@pytest.mark.parametrize('marker', ['status', 'state', 'tag', None])
def test_unknown_statement_requires_explicit_unknown_marker(marker):
    item = {'statement': {'text': 'No measurement is published.'}}
    if marker:
        item[marker] = 'UNKNOWN'
    payload = agent_payload()
    payload['unknowns'] = [item]
    view = agent_card('example', payload).render()
    label = 'HEY does not know: ' if marker else 'HEY reports: '
    assert label + item['statement']['text'] in view
    assert view.count('HEY does not know') == int(marker is not None)


@pytest.mark.parametrize('name', ['hey-builder_status-addr.json', 'hey-research_project-name.json'])
def test_bundled_answers_preserve_citation_without_invented_unknowns(name):
    payload = json.loads(capture_path(name).read_text())
    view = agent_card('example', payload).render()
    assert 'HEY does not know' not in view
    assert view.splitlines()[-1] == payload['citation']['text']
    if name == 'hey-builder_status-addr.json':
        assert "Latest meaningful ship: title not in HEY's answer · 2026-11-09T09:53:07.000Z" in view
    else:
        statement = payload['unknowns'][0]['statement']['text']
        assert 'HEY reports: ' + statement in view


def test_saved_priors_keeps_answer_coverage_and_citation():
    view = cards()[0].render().splitlines()
    assert view[:2] == [
        'if Hermes swapped this now: go · no rule of yours fired',
        '11 sources · 8 answered · 1 not for this target: Borro · off: Sherwood, OffRouter',
    ]
    payload = json.loads(capture_path('hey-builder_status-addr.json').read_text())
    assert view[-1] == payload['citation']['text']
    assert not any('HEY does not know' in line for line in view)
