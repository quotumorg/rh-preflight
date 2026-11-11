"""Answer-first surfaces and coverage from offline source outcomes."""
import argparse
from dataclasses import replace
import json
import re
from pathlib import Path
from demo.captures import capture_path
import time

import pytest

from rh_preflight import __version__, cli, commands, tools
from rh_preflight.card import Card, agent_card
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.network import Network, MODULES
from rh_preflight.policy import Decision, evaluate
from rh_preflight.presentation import PROJECTS, coverage, present
from rh_preflight.settings import Settings
from rh_preflight.sources.common import Reading
from rh_preflight.sources.sherwood import Client as Sherwood
from demo.fake_hey import ACTIVE, QUIET, records

ROOT = Path(__file__).resolve().parents[1]


def checked_coverage(card):
    line = coverage(card)
    parts = line.split(' · ')
    total = int(parts[0].removesuffix(' sources'))
    assert total == 11
    answered = 0
    named = []
    for part in parts[1:]:
        if part.endswith(' answered'):
            answered = int(part.removesuffix(' answered'))
            assert answered > 0
        elif ' not for this target: ' in part:
            count, names = part.split(' not for this target: ')
            names = names.split(', ')
            assert len(names) == int(count) > 0
            named.extend(names)
        else:
            bucket, names = part.split(': ')
            assert bucket in {'silent', 'off'}
            names = names.split(', ')
            assert names
            if bucket == 'silent':
                assert all(re.fullmatch(
                    r'HEY \([0-9]+\.[0-9] s\)|.+ \((?:payment required|rate limited|refused|unreachable); [0-9]+\.[0-9] s\)',
                    name) for name in names)
                names = [name.rsplit(' (', 1)[0] for name in names]
                if 'HEY' in names:
                    assert names[0] == 'HEY'
            named.extend(names)
    assert len(named) == len(set(named))
    assert set(named) <= set(PROJECTS)
    assert answered + len(named) == total
    assert 'asked' not in line
    return line


def network_card(readings=(), **data):
    card = Card(ACTIVE, 'network', {'base': Card(ACTIVE, 'token', records()[ACTIVE]),
                                  'sources': list(readings), **data})
    checked_coverage(card)
    return card


@pytest.mark.parametrize('outcome,label', [('pass', 'go'), ('ask', 'ask'), ('block', 'stop'), ('note', 'note')])
@pytest.mark.parametrize('rule_count', [0, 1, 2])
def test_first_answer_on_every_surface(tmp_path, capsys, outcome, label, rule_count):
    # A read card is attended in production; the block presentation is injected
    # here to exercise serialization of every policy outcome on every surface.
    rules = ('your rule: no ship in 30 days', 'your rule: activity not measured')[:rule_count] if outcome != 'pass' else ()
    card = present(network_card(), Decision(outcome, rules), read_only=True)
    class ReadService:
        def card(self, *args, **kwargs):
            return card
    app = ReadService()
    public = json.loads(tools.handler(app, {'address': ACTIVE}))
    slash = commands.card_command(app, ACTIVE)
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    assert cli.run(app, parser.parse_args(['card', ACTIVE])) == 0
    terminal = capsys.readouterr().out.strip()
    ledger = Ledger(tmp_path)
    ledger.add('', 'swap', [card], rules, 'ask_when', outcome)
    stored = ledger.rows()[0]['cards'][0]
    for rendered in (public['card'], slash, terminal, stored['card']):
        assert rendered.splitlines()[0].startswith('if Hermes swapped this now: ' + label + ' · ')
        assert rendered.splitlines()[1] == checked_coverage(card)
        if len(rules) > 1 or outcome == 'block' and rules:
            assert rendered.splitlines()[2:2 + len(rules)] == list(rules)
        else:
            assert not any(line.startswith('your rule:') for line in rendered.splitlines()[2:])
            if rules and outcome != 'block':
                assert rendered.count(rules[0]) == 1
    assert public['answer'] == stored['answer'] == slash.splitlines()[0]
    assert public['coverage'] == stored['coverage'] == slash.splitlines()[1]
    if outcome in {'ask', 'note'} and len(rules) > 1:
        assert public['answer'].endswith('your rule: no ship in 30 days +1 more')
    if outcome == 'block':
        assert public['answer'].endswith('stop · human approval required, unattended')


@pytest.mark.parametrize('mode,target,away,label', [
    ('ask_when', ACTIVE, False, 'go'), ('ask_when', QUIET, False, 'ask'),
    ('ask_when', ACTIVE, True, 'stop'), ('note', QUIET, False, 'note'),
])
def test_real_hook_decision_ledger_and_transform(tmp_path, mode, target, away, label):
    class Hey:
        def lookup(self, target, **kwargs):
            return Card(target, 'token', records()[target])
    settings = Settings(mode=mode, sources={n: 'off' for n in MODULES})
    app = Service(settings=settings, client=Hey(), ledger=Ledger(tmp_path), presence=lambda: away)
    args = {'chain': 4663, 'token': target}
    directive = app.pre_tool_call('swap', args)
    stored = app.ledger.rows()[0]['cards'][0]
    assert stored['card'].splitlines()[0] == stored['answer']
    assert stored['coverage'].startswith('11 sources · 1 answered · off: ')
    assert stored['answer'].startswith(label + ' · ')
    assert not stored['answer'].startswith('if Hermes')
    transformed = app.transform_tool_result('swap', args, 'result')
    assert stored['card'] in transformed
    if directive:
        assert directive['message'].splitlines()[0] == stored['answer']
        assert directive['message'].splitlines()[1] == stored['coverage']


def test_read_is_attended_swap_and_respects_current_settings(tmp_path):
    class Hey:
        def lookup(self, target, **kwargs):
            return Card(target, 'missing')
    settings = Settings(sources={n: 'off' for n in MODULES})
    app = Service(settings=settings, client=Hey(), ledger=Ledger(tmp_path), presence=lambda: True)
    assert app.card(ACTIVE).render().startswith('if Hermes swapped this now: ask · your rule: no HEY record\n')
    app.settings = replace(settings, mode='note')
    assert app.card(ACTIVE).render().startswith('if Hermes swapped this now: note · ')
    app.settings = replace(settings, ask_no_record=False)
    assert app.card(ACTIVE).render().startswith('if Hermes swapped this now: go · no rule of yours fired\n')
    assert not app.ledger.rows()


def all_readings():
    return [Reading(name, 'present', name + ': record · https://example.org') for name in PROJECTS[1:]]


def test_all_answered_and_plumbing_excluded():
    readings = all_readings() + [Reading('RPC', 'unreachable', ''), Reading('DexScreener', 'unreachable', ''), Reading('GeckoTerminal', 'rate_limited', '')]
    assert checked_coverage(network_card(readings)) == '11 sources · 11 answered'


@pytest.mark.parametrize('state,reason', [
    ('timeout', 'unreachable'), ('unreachable', 'unreachable'),
    ('rate_limited', 'rate limited'), ('refused', 'refused'),
    ('payment_required', 'payment required'),
])
def test_silent_and_measured_elapsed(tmp_path, state, reason):
    readings = [replace(r, state=state, elapsed=4.0) if r.source == 'Borro' else r for r in all_readings()]
    card = present(network_card(readings), Decision('pass', ()))
    expected = f'11 sources · 10 answered · silent: Borro ({reason}; 4.0 s)'
    assert checked_coverage(card) == expected
    assert 'Borro: record' in card.render()
    ledger = Ledger(tmp_path)
    ledger.add('', 'swap', [card], (), 'ask_when', 'pass')
    row = ledger.rows()[0]
    stored = row['cards'][0]
    assert stored['coverage'] == stored['card'].splitlines()[1] == card.public()['coverage'] == expected
    section = find_element(desktop_tree(row), 'section')
    assert section['children'][1]['children'] == expected
    card = Card(card.target, 'network', {**card.data, 'base': Card(ACTIVE, 'unreachable'), 'hey_elapsed': 1.5})
    assert f'silent: HEY (1.5 s), Borro ({reason}; 4.0 s)' in checked_coverage(card)


@pytest.mark.parametrize('state', ['timeout', 'unreachable', 'rate_limited', 'refused', 'payment_required'])
def test_silent_partition_precedence_and_named_order(state):
    # Reverse input order; every named bucket still follows PROJECTS.
    rows = [replace(row, state=state, elapsed=0.1,
                    asked=row.source != 'Hookr') for row in reversed(all_readings())]
    rows = [replace(row, state='not_applicable') if row.source == 'Toll' else row for row in rows]
    card = network_card(rows, base=Card(ACTIVE, 'unreachable'), hey_elapsed=4.0,
                        source_settings={'sentinelhood': 'off', 'borro': 'off'})
    reason = {'timeout': 'unreachable', 'rate_limited': 'rate limited',
              'payment_required': 'payment required'}.get(state, state)
    assert checked_coverage(card) == (
        '11 sources · 2 not for this target: Hookr, Toll · silent: HEY (4.0 s), ' +
        ', '.join(f'{name} ({reason}; 0.1 s)' for name in
                  ('Priors', 'HoodLock', 'Trnch', 'Agent Wormhole', 'Sherwood', 'OffRouter')) +
        ' · off: Sentinelhood, Borro')


def test_payment_required_coverage_has_no_detail_or_followup(tmp_path):
    from rh_preflight.hey import Refused
    from rh_preflight.sources.toll import Client as Toll
    client = Toll()
    calls = []
    def paid(path, deadline, payload=None):
        calls.append(path)
        raise Refused(402)
    client._read = paid
    reading = replace(client.lookup('31337', kind='agent'), elapsed=0.1)
    assert len(calls) == 1
    assert reading.state == 'payment_required' and reading.line == ''
    card = present(network_card([reading]), Decision('pass', ()))
    expected = checked_coverage(card)
    assert 'silent: Toll (payment required; 0.1 s)' in expected
    assert not any('Toll' in line for line in card.render().splitlines()[2:])
    ledger = Ledger(tmp_path)
    ledger.add('', 'swap', [card], (), 'ask_when', 'pass')
    row = ledger.rows()[0]
    assert row['cards'][0]['coverage'] == row['cards'][0]['card'].splitlines()[1] == expected
    section = find_element(desktop_tree(row), 'section')['children']
    assert section[1]['children'] == expected
    assert 'Toll' not in section[3]['children'][1]['children']


def test_settings_off_not_applicable_no_record_and_offrouter():
    rows = [replace(r, state='missing') if r.source == 'Priors' else
            replace(r, state='unlisted', line='') if r.source == 'Sherwood' else r for r in all_readings()]
    card = network_card(rows, source_settings={'borro': 'off'})
    line = checked_coverage(card)
    assert line == '11 sources · 9 answered · 1 not for this target: Sherwood · off: Borro'
    assert 'off: Borro' in line and 'not for this target: Sherwood' in line
    own = network_card(all_readings() + [Reading('OffRouter', 'present', '')], offrouter=True)
    assert checked_coverage(own) == '11 sources · 11 answered'
    assert 'OffRouter' in checked_coverage(network_card([]))
    rows = [replace(r, asked=False, state='unlisted') if r.source == 'Borro' else r for r in all_readings()]
    assert checked_coverage(network_card(rows)) == '11 sources · 10 answered · 1 not for this target: Borro'


def test_source_timer_is_monotonic_and_original_deadline(monkeypatch):
    net = Network()
    ends = []
    class Clock:
        tick = 100.0
        def __call__(self):
            return self.tick
    clock = Clock()
    monkeypatch.setattr('rh_preflight.network.time.monotonic', clock)
    def timeout(target, deadline=None):
        ends.append(deadline)
        clock.tick += 4
        return Reading('Borro', 'unreachable', 'Borro could not be reached · https://borro.markets')
    # Use an independent source here to avoid a fabricated stock prerequisite.
    net.clients['priors'].lookup = lambda target, **kw: replace(timeout(target, **kw), source='Priors')
    settings = Settings(sources={n: 'on' if n == 'priors' else 'off' for n in MODULES})
    card = net.enrich([Card(ACTIVE, 'identifier')], 'read', {}, settings, all_sources=True, deadline=104)[0]
    assert ends == [104]
    assert 'silent: Priors (unreachable; 4.0 s)' in checked_coverage(card)


def test_unresolved_hey_one_line_resolved_unchanged():
    payload = json.loads(capture_path('hey-builder_status-unknown.json').read_text())
    result = agent_card(ACTIVE, payload).render()
    assert len(result.splitlines()) == 1
    assert result.startswith("HEY Research Lab: no published project for this contract (HEY's index, not a statement about the token)")
    assert payload['asOf'] in result and result.endswith(' · https://heyresearch.xyz')
    resolved = json.loads(capture_path('hey-builder_status-addr.json').read_text())
    result = agent_card(ACTIVE, resolved).render()
    assert 'Verified Builder:' in result and 'Latest meaningful ship:' in result


def test_sherwood_saved_bundle_plural():
    data = json.loads(capture_path('sherwood-vault-bundle.json').read_text())
    result = Sherwood().parse({}, data).line
    assert '1 agent · ' in result and 'agent 1' not in result
    data['agents'] *= 2
    assert '2 agents · ' in Sherwood().parse({}, data).line


def test_desktop_uses_answer_first_then_coverage_then_human():
    bundle = (ROOT / 'desktop/plugin.js').read_text()
    row = bundle[bundle.index('children: rows.map'):]
    assert row.index('card.answer') < row.index('card.coverage') < row.index('human response:')
    assert "className: 'text-sm font-semibold', children: card.answer" in row
    assert "slice(2).join('\\n')" in row
    assert 'No checks recorded yet.' in bundle and "role: 'alert'" in bundle


def test_version_and_public_source_notes():
    assert __version__ == '0.5.2'
    readme = (ROOT / 'README.md').read_text()
    assert readme.startswith('# rh-preflight\n\nrh-preflight by [quotum](https://quotum.org) with [HEY Research Lab](https://heyresearch.xyz).\n\n![rh-preflight](docs/banner.png)')
    assert '../CATALOG' not in readme and 'rh-preflight/0.4' not in readme
    assert readme.index('## Quickstart') < readme.index('## Settings') < readme.index('## Sources')
    sources = (ROOT / 'docs/SOURCES.md').read_text()
    assert '4015' in sources and 'HoodLock' in sources and '1358' in sources
    assert not re.search("/" + "(?:Users|home)/", sources)


def desktop_tree(row):
    return desktop_eval('Checks({})', row)


def desktop_eval(expression, value):
    import shutil
    import subprocess
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node unavailable; bundle order is also checked as text')
    bundle = (ROOT / 'desktop/plugin.js').read_text()
    bundle = '\n'.join(line for line in bundle.splitlines() if not line.startswith('import '))
    bundle = bundle.replace('export default', 'const plugin =')
    harness = """
const row = JSON.parse(process.argv[1]);
const host = {state: {profile: {}, gateway: {}}};
const useValue = () => 'example';
const gatewayActivationEpoch = () => 1;
const useQuery = () => ({data: {checks: [row]}});
const jsx = (type, props) => ({type, ...props});
const jsxs = jsx;
""" + bundle + f"\nprocess.stdout.write(JSON.stringify({expression}) ?? 'null');"
    result = subprocess.run([node, '--input-type=module', '-e', harness, json.dumps(value)],
                            text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def find_element(value, tag):
    if isinstance(value, dict):
        if value.get('type') == tag:
            return value
        for item in value.values():
            found = find_element(item, tag)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = find_element(item, tag)
            if found:
                return found


@pytest.mark.parametrize('outcome', ['pass', 'ask', 'block', 'note'])
@pytest.mark.parametrize('rule_count', [0, 1, 2])
def test_desktop_rendered_row_for_every_outcome(outcome, rule_count):
    rules = ('your rule: no ship in 30 days', 'your rule: activity not measured')[:rule_count]
    original = present(network_card(), Decision(outcome, rules))
    card = original.public()
    assert card['coverage'] == checked_coverage(original)
    row = {'id': 'example', 'cards': [card], 'tool_name': 'swap', 'mode': 'ask_when',
           'time': '2026-11-10', 'disposition': outcome, 'decision': None}
    tree = desktop_tree(row)
    credit = tree['children'][1]['children']
    assert credit[0] == 'rh-preflight by quotum with HEY Research Lab'
    assert credit[2]['href'] == 'https://heyresearch.xyz'
    li = find_element(tree, 'li')
    section = li['children'][0]['children']
    assert section[0]['type'] == 'h2' and section[0]['children'] == card['answer']
    assert section[1]['type'] == 'p' and section[1]['children'] == card['coverage']
    assert section[2]['type'] == 'p' and section[2]['children'] == 'target: ' + card['target']
    details = section[3]
    assert details['type'] == 'details' and not details.get('open')
    summary, content = details['children']
    assert summary['type'] == 'summary' and summary['children'] == 'Source details'
    assert summary['aria-label'] == 'Source details for ' + card['target']
    assert 'focus-visible:outline' in summary['className']
    assert content['children'] == '\n'.join(card['card'].splitlines()[2:])
    if rule_count < 2 and not (outcome == 'block' and rules):
        assert 'your rule:' not in content['children']
    else:
        assert content['children'].splitlines()[:len(rules)] == list(rules)
    expected = 'not recorded' if outcome == 'ask' else 'not requested'
    assert li['children'][-1]['children'] == 'human response: ' + expected


@pytest.mark.parametrize('choice,label', [
    ('once', 'approved once; execution not recorded'),
    ('session', 'approved for session; execution not recorded'),
    ('always', 'approved always; execution not recorded'),
    ('deny', 'denied'), ('timeout', 'timed out'), ('cancelled', 'cancelled'),
    (None, 'not recorded'), ('unexpected', 'not recorded'), ('toString', 'not recorded'),
])
@pytest.mark.parametrize('legacy', [False, True])
def test_desktop_recorded_responses_and_legacy_cards(choice, label, legacy):
    card = present(network_card(), Decision('ask', ('your rule: no HEY record',))).public()
    if legacy:
        card = {key: value for key, value in card.items() if key not in {'answer', 'coverage'}}
    row = {'id': 'example', 'cards': [card], 'tool_name': 'swap', 'mode': 'ask_when',
           'time': '2026-11-10', 'decision': choice}
    if not legacy:
        row['disposition'] = 'ask'
    li = find_element(desktop_tree(row), 'li')
    section = li['children'][0]['children']
    assert section[0]['children'] == card['card'].splitlines()[0]
    assert section[1]['children'] == card['card'].splitlines()[1]
    assert section[2]['children'] == 'target: ' + card['target']
    content = section[3]['children'][1]['children']
    assert content == (card['card'] if legacy else '\n'.join(card['card'].splitlines()[2:]))
    assert li['children'][-1]['children'] == 'human response: ' + label


def visible_strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [] if value.get('type') == 'details' else visible_strings(value.get('children'))
    if isinstance(value, list):
        return [text for child in value for text in visible_strings(child)]
    return []


@pytest.mark.parametrize('legacy', [False, True])
def test_desktop_two_identical_answers_keep_full_targets_visible(legacy):
    cards = [present(replace(network_card(base=Card(target, 'missing')), target=target),
                     Decision('ask', ('your rule: no HEY record',))).public()
             for target in (ACTIVE, '0x000000000000000000000000000000000000dead')]
    if legacy:
        cards = [{key: value for key, value in card.items() if key not in {'answer', 'coverage'}} for card in cards]
    row = {'id': 'two', 'cards': cards, 'tool_name': 'swap', 'mode': 'ask_when',
           'time': '2026-11-10', 'disposition': 'ask', 'decision': 'deny'}
    li = find_element(desktop_tree(row), 'li')
    visible = visible_strings(li)
    assert visible.count(cards[0]['card'].splitlines()[0]) == 2
    assert visible.count(cards[0]['card'].splitlines()[1]) == 2
    for section, card in zip(li['children'][:2], cards):
        assert 'target: ' + card['target'] in visible_strings(section)
        assert not find_element(section, 'details').get('open')
        assert section['style']['overflowWrap'] == 'anywhere'
    assert li['children'][-2]['children'] == 'swap · 2 checked targets · ask_when · 2026-11-10'
    assert visible[-1] == 'human response: denied'


@pytest.mark.parametrize('target', [
    'https://github.com/example/a-repository-with-a-long-complete-name',
    'agent:31337', '0x' + 'ab' * 32, '<img src=x onerror=alert(1)>',
])
def test_desktop_identifiers_and_external_text_are_string_children(target):
    external = '<script>alert("source")</script>'
    card = {'target': target, 'answer': external, 'coverage': external,
            'card': '\n'.join([external] * 3)}
    row = {'id': 'external', 'cards': [card], 'tool_name': external,
           'mode': external, 'time': external, 'disposition': 'pass'}
    li = find_element(desktop_tree(row), 'li')
    section = li['children'][0]['children']
    assert section[0]['children'] == section[1]['children'] == external
    assert section[2]['children'] == 'target: ' + target
    assert section[3]['children'][1]['children'] == external
    assert 'target: ' + target in visible_strings(li)
    assert li['children'][-2]['children'] == f'{external} · {external} · {external}'
    assert 'dangerouslySetInnerHTML' not in json.dumps(li)


@pytest.mark.parametrize('outcome', ['pass', 'note', 'block'])
def test_desktop_does_not_infer_requested_approval_from_other_dispositions(outcome):
    row = {'id': 'example', 'cards': [], 'tool_name': 'swap', 'mode': 'ask_when',
           'time': '2026-11-10', 'disposition': outcome, 'decision': 'once'}
    li = find_element(desktop_tree(row), 'li')
    assert li['children'][-1]['children'] == 'human response: not requested'
