"""Offline regressions for Desktop source visuals and terminal-only color."""
import argparse
from dataclasses import replace
import io
from itertools import product
import json
import re

import pytest

from rh_preflight import cli, commands, tools
from rh_preflight.card import Card, agent_card
from demo.fake_network import agent_payload
from rh_preflight.ledger import Ledger
from rh_preflight.policy import Decision
from rh_preflight.presentation import PROJECTS, SILENT, coverage, present
from rh_preflight.sources.common import Reading
from test_r8 import ROOT, all_readings, desktop_eval, desktop_tree, find_element, network_card


def section_for(card, **extra):
    row = {'id': 'visual', 'cards': [card], 'tool_name': 'swap', 'mode': 'ask_when',
           'time': '2026-11-11', 'disposition': 'pass', **extra}
    return find_element(desktop_tree(row), 'section')


def assert_source_parity(card):
    public = card.public()
    parsed = desktop_eval('buckets(row)', public['coverage'])
    assert public['coverage'] == coverage(card) == public['card'].splitlines()[1]
    assert public['sources'] == [
        {'name': 'HEY Research Lab' if item['name'] == 'HEY' else item['name'],
         'bucket': 'not_for_target' if item['kind'] == 'other' else item['kind'],
         'reason': item.get('reason'), 'link': source['link']}
        for item, source in zip(parsed, public['sources'])]
    assert len(public['sources']) == len(PROJECTS)
    assert all(source['link'] is None for source in public['sources']
               if source['bucket'] != 'answered')


@pytest.mark.parametrize('label,color', [
    ('go', '#15803d'), ('ask', '#b45309'), ('stop', '#b91c1c'), ('note', '#4b5563'),
])
@pytest.mark.parametrize('prefix', ['', 'if Hermes swapped this now: '])
def test_desktop_answer_color(label, color, prefix):
    card = {'target': 'agent:31337', 'answer': prefix + label + ' · your rule',
            'coverage': '11 sources · 11 answered', 'card': ''}
    heading = section_for(card)['children'][0]
    assert heading['children'] == card['answer']
    assert heading['style']['background'] == color
    assert heading['style']['color'] == '#fff'


@pytest.mark.parametrize('answer', [None, '', 'unknown · text', 'toString · text',
                                        '<script>go</script> · text', 'Go · text'])
def test_desktop_unknown_answer_has_no_color(answer):
    assert desktop_eval('answerStyle(row)', answer) is None


@pytest.mark.parametrize('other,silent,off,hey_silent', list(product([False, True], repeat=4)))
def test_desktop_buckets_for_every_partition(other, silent, off, hey_silent):
    readings = all_readings()
    if other:
        readings = [replace(r, asked=False) if r.source == 'Hookr' else r for r in readings]
    if silent:
        readings = [replace(r, state='refused', elapsed=2.5) if r.source == 'Borro' else r for r in readings]
    card = network_card(readings, source_settings={'trnch': 'off'} if off else {},
                        **({'base': Card('example', 'unreachable'), 'hey_elapsed': 1.5} if hey_silent else {}))
    expected = [{'name': name, 'kind': 'answered'} for name in PROJECTS]
    for bucket in expected:
        if bucket['name'] == 'Hookr' and other:
            bucket['kind'] = 'other'
        elif bucket['name'] == 'Trnch' and off:
            bucket['kind'] = 'off'
        elif bucket['name'] == 'Borro' and silent:
            bucket.update(kind='silent', reason='refused; 2.5 s')
        elif bucket['name'] == 'HEY' and hey_silent:
            bucket.update(kind='silent', reason='1.5 s')
    assert desktop_eval('buckets(row)', coverage(card)) == expected
    assert_source_parity(card)


@pytest.mark.parametrize('state,reason', SILENT.items())
def test_desktop_silent_reasons_and_no_answered_bucket(state, reason):
    card = network_card([Reading('Borro', state, '', elapsed=4.0)],
                        base=Card('example', 'unreachable'), hey_elapsed=1.5)
    assert_source_parity(card)
    buckets = desktop_eval('buckets(row)', coverage(card))
    assert buckets[0] == {'name': 'HEY', 'kind': 'silent', 'reason': '1.5 s'}
    assert buckets[8] == {'name': 'Borro', 'kind': 'silent', 'reason': reason + '; 4.0 s'}
    assert all(b['kind'] == 'other' for b in buckets[1:8] + buckets[9:])
    all_other = network_card(base=Card('agent:31337', 'identifier'))
    assert {b['kind'] for b in desktop_eval('buckets(row)', coverage(all_other))} == {'other'}
    assert_source_parity(all_other)
    all_off = network_card(source_settings={name.lower().replace('agent ', ''): 'off' for name in PROJECTS[1:]})
    assert [b['kind'] for b in desktop_eval('buckets(row)', coverage(all_off))] == ['answered'] + ['off'] * 10
    assert_source_parity(all_off)


@pytest.mark.parametrize('value', [None, '', 'garbage', '<script>alert(1)</script>',
                                  ' 11 sources', '11 source', '11 sources external',
                                  'prefix 11 sources', 'HEY not queried for agent or pool ids'])
def test_desktop_unrecognized_coverage_has_no_buckets_bar_or_list(value):
    assert desktop_eval('buckets(row)', value) is None
    card = {'target': '<img src=x>', 'answer': 'external', 'coverage': value,
            'card': 'external\n' + (value or '') + '\n<script>alert(1)</script>'}
    section = section_for(card)
    assert 'style' not in section['children'][1]
    assert section['children'][4] is None
    assert 'dangerouslySetInnerHTML' not in json.dumps(section)


def test_desktop_empty_and_malformed_named_buckets_do_not_throw():
    for line in ('11 sources', '11 sources · silent: ', '11 sources · off: ',
                 '11 sources · silent: Hookrx (1.0 s), ', '11 sources · arbitrary text'):
        assert desktop_eval('buckets(row)', line) == [
            {'name': name, 'kind': 'answered'} for name in PROJECTS]


@pytest.mark.parametrize('legacy', [False, True])
def test_desktop_source_list_bar_and_legacy_fallback(legacy):
    readings = [replace(r, state='payment_required', elapsed=4.0) if r.source == 'Borro' else
                replace(r, asked=False) if r.source == 'Hookr' else r for r in all_readings()]
    card = present(network_card(readings, base=Card('example', 'unreachable'), hey_elapsed=1.5,
                                source_settings={'trnch': 'off'}), Decision('pass', ())).public()
    card['card'] += '\nHEY Research Lab: record · https://example.org/hey'
    if legacy:
        del card['sources']
        del card['coverage']
        del card['answer']
    section = section_for(card)
    gradient = section['children'][1]['style']['backgroundImage']
    assert gradient.startswith('linear-gradient(90deg, ')
    assert gradient.count('transparent ') == 22
    items = section['children'][4]['children']
    assert len(items) == 11
    names = ['HEY Research Lab', *PROJECTS[1:]]
    assert [item['children'][1]['children'] for item in items] == names
    for name, item in zip(PROJECTS, items):
        label = item['children'][2]['children']
        expected = {'HEY': 'silent · 1.5 s', 'Trnch': 'off', 'Hookr': 'not for this target',
                    'Borro': 'silent · payment required; 4.0 s'}.get(name, 'answered')
        assert label == expected
        anchor = item['children'][3]
        assert anchor is None if legacy or expected != 'answered' else anchor['href'] == 'https://example.org'
    modern = {**card, 'answer': card['card'].splitlines()[0], 'coverage': card['card'].splitlines()[1]}
    assert section_for(modern)['children'][4] == section['children'][4]


@pytest.mark.parametrize('name', PROJECTS[1:])
def test_sources_links_only_use_own_reading(name):
    base = agent_card('https://argument.invalid', agent_payload())
    base.data['payload']['answer'] = {'text': name + ': decoy · https://hey.invalid'}
    readings = [Reading('RPC', 'present', name + ': decoy · https://rpc.invalid'),
                Reading(name, 'present', 'arbitrary prefix · https://first.invalid · '
                        'https://own.invalid/page?utm_source=external&format=json&keep=1')]
    card = present(network_card(readings, base=base), Decision('pass', ())).public()
    source = card['sources'][PROJECTS.index(name)]
    assert source == {'name': name, 'bucket': 'answered', 'reason': None,
                      'link': 'https://own.invalid/page?keep=1'}
    item = section_for(card, arguments={'project': name + ' · https://tool.invalid'})['children'][4]['children'][PROJECTS.index(name)]
    assert item['children'][3]['href'] == source['link']


@pytest.mark.parametrize('name', PROJECTS[1:])
def test_sources_use_existing_wording_projection(name):
    card = network_card([Reading(name, 'present', name + ': safe · https://own.invalid')])
    assert card.public()['sources'][PROJECTS.index(name)]['link'] is None


@pytest.mark.parametrize('kind', ['token', 'contract', 'repo'])
@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('project', ['Example', 'Priors: companion'])
def test_desktop_hey_project_header_links_belong_to_hey(kind, legacy, project):
    base = Card('0x' + 'ab' * 20, kind, {
        'hey_project_name': project, 'project_link': 'https://hey.example/project',
        'activity_applies_to_token': False,
    })
    original = replace(network_card(all_readings(), base=base), target=base.target)
    card = present(original, Decision('ask', ('your rule: no HEY record',
                                             'your rule: no ship in 30 days'))).public()
    if legacy:
        del card['sources']
        del card['coverage']
        del card['answer']
    items = section_for(card)['children'][4]['children']
    assert items[0]['children'][3] is None if legacy else items[0]['children'][3]['href'] == 'https://hey.example/project'
    assert items[1]['children'][3] is None if legacy else items[1]['children'][3]['href'] == 'https://example.org'


@pytest.mark.parametrize('project_link', [None, 'http://hey.example/project'])
def test_desktop_hey_header_does_not_link_repository_target(project_link):
    target = 'https://github.com/example/project'
    base = Card(target, 'token', {'hey_project_name': 'Priors: companion',
                                 'project_link': project_link})
    original = replace(network_card(all_readings(), base=base), target=target)
    card = present(original, Decision('pass', ())).public()
    items = section_for(card)['children'][4]['children']
    assert items[0]['children'][3] is None
    assert items[1]['children'][3]['href'] == 'https://example.org'


@pytest.mark.parametrize('name', PROJECTS[1:])
@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('own_line', [True, False])
def test_sources_hey_answer_cannot_supply_another_sources_link(name, legacy, own_line):
    payload = agent_payload()
    payload['answer'] = {'text': name + ': builder research · https://hey.example/evidence'}
    readings = [Reading(name, 'present', name + ': record · https://source.example/page'
                        if own_line else '')]
    original = network_card(readings, base=agent_card('example', payload))
    card = present(original, Decision('pass', ())).public()
    if legacy:
        del card['sources']
        del card['answer']
        del card['coverage']
    items = section_for(card)['children'][4]['children']
    anchor = items[PROJECTS.index(name)]['children'][3]
    assert anchor['href'] == 'https://source.example/page' if own_line and not legacy else anchor is None
    assert items[0]['children'][3] is None if legacy else items[0]['children'][3]['href'] == 'https://example.invalid/project/harbor-kit'


@pytest.mark.parametrize('url', ['', 'http://example.org', 'javascript:alert(1)',
                                'data:text/html,test', '//example.org',
                                'https://', 'https://user:password@example.org',
                                'https://example.org/unsafe'])
def test_sources_links_require_projected_https(url):
    base = Card('https://argument.invalid', 'token', {'project_link': url})
    card = network_card([Reading('Hookr', 'present', 'Hookr: record · ' + url)], base=base).public()
    assert all(source['link'] is None for source in card['sources'])
    assert all(item['children'][3] is None for item in section_for(card)['children'][4]['children'])


@pytest.mark.parametrize('kind', ['agent', 'token', 'contract', 'repo'])
@pytest.mark.parametrize('url,expected', [
    ('https://hey.example/project?utm_source=test&keep=1', 'https://hey.example/project?keep=1'),
    ('HTTPS://hey.example/project', 'https://hey.example/project'),
    ('https://hey.example/unsafe', None),
])
def test_sources_hey_links_match_header_projection(kind, url, expected):
    if kind == 'agent':
        payload = agent_payload()
        payload['subject']['project']['url'] = url
        base = agent_card('example', payload)
    else:
        base = Card('https://argument.invalid', kind, {'hey_project_url': url})
    card = network_card([Reading('HEY', 'present', 'HEY: decoy · https://decoy.invalid')], base=base)
    assert card.public()['sources'][0]['link'] == expected
    if expected:
        assert base.render().splitlines()[0].endswith(' · ' + expected)


@pytest.mark.parametrize('name', PROJECTS[1:])
@pytest.mark.parametrize('state,line,asked', [
    ('present', 'record · https://own.invalid', True),
    ('missing', 'record · https://own.invalid', True),
    ('unlisted', '', True), ('unlisted', 'record · https://own.invalid', True),
    ('not_applicable', 'record · https://own.invalid', True),
    ('unreachable', 'record · https://own.invalid', False),
])
def test_sources_parity_for_remaining_coverage_shapes(name, state, line, asked):
    rows = [Reading('RPC', 'unreachable', ''), *reversed(all_readings()),
            Reading(name, state, line, asked=asked)]
    card = network_card(rows, source_settings={'hey': 'off'})
    assert_source_parity(card)


@pytest.mark.parametrize('state', SILENT)
def test_sources_silent_precedence_matches_coverage(state):
    rows = [replace(row, state=state, elapsed=0.1, asked=row.source != 'Hookr')
            for row in reversed(all_readings())]
    rows = [replace(row, state='not_applicable') if row.source == 'Toll' else row for row in rows]
    card = network_card(rows, base=Card('example', 'unreachable'), hey_elapsed=4.0,
                        source_settings={'sentinelhood': 'off', 'borro': 'off'})
    assert_source_parity(card)


@pytest.mark.parametrize('base,extra', [
    (Card('example', 'unreachable'), {'hey_asked': False}),
    (Card('example', 'identifier'), {'hey_asked': True}),
    (Card('example', 'missing'), {}),
])
def test_sources_hey_asked_override_and_absence(base, extra):
    card = network_card(base=base, **extra)
    assert_source_parity(card)
    assert card.public()['sources'][0]['link'] is None


@pytest.mark.parametrize('invalid', [None, False, 42, 'sources', {}, [None], [[]], [{}]])
def test_desktop_invalid_sources_container_drops_list(invalid):
    card = network_card(all_readings()).public()
    card['sources'] = invalid
    section = section_for(card)
    assert section['children'][4] is None
    assert section['children'][1]['style']['backgroundImage'].startswith('linear-gradient(')


@pytest.mark.parametrize('field,value', [
    ('name', None), ('name', 1), ('name', 'HEY'), ('name', 'Unknown'),
    ('name', '<script>Hookr</script>'), ('bucket', None), ('bucket', 'other'),
    ('bucket', 'toString'), ('bucket', {}), ('reason', False), ('reason', []),
    ('reason', {}), ('reason', 4), ('link', False), ('link', []), ('link', {}),
    ('link', 4), ('link', ''), ('link', 'http://example.org'),
    ('link', 'HTTPS://example.org'), ('link', 'javascript:alert(1)'),
    ('link', 'data:text/html,test'), ('link', '//example.org'),
])
def test_desktop_invalid_source_field_drops_entire_list(field, value):
    card = network_card(all_readings()).public()
    card['sources'][-1][field] = value
    assert section_for(card)['children'][4] is None


@pytest.mark.parametrize('field', ['name', 'bucket', 'reason', 'link'])
def test_desktop_missing_source_field_drops_list(field):
    card = network_card(all_readings()).public()
    del card['sources'][-1][field]
    assert section_for(card)['children'][4] is None


def test_desktop_structured_sources_work_without_recognized_coverage():
    card = network_card(all_readings()).public()
    card['coverage'] = 'external text'
    card['card'] = 'no source lines'
    section = section_for(card)
    assert 'style' not in section['children'][1]
    assert len(section['children'][4]['children']) == 11
    assert section['children'][4]['children'][1]['children'][3]['href'] == 'https://example.org'


def test_desktop_external_strings_stay_string_children():
    card = network_card(all_readings()).public()
    external = '<script>alert(1)</script><img src=x>'
    card.update(target=external, answer=external, card='answer\ncoverage\n' + external)
    card['sources'][1].update(bucket='silent', reason=external,
                              link='https://example.org/<img>')
    section = section_for(card)
    assert section['children'][0]['children'] == external
    assert section['children'][3]['children'][1]['children'] == external
    item = section['children'][4]['children'][1]
    assert item['children'][2]['children'] == 'silent · ' + external
    assert item['children'][3] is None
    card['sources'][1]['bucket'] = 'answered'
    anchor = section_for(card)['children'][4]['children'][1]['children'][3]
    assert anchor['href'] == 'https://example.org/<img>'
    assert anchor['children'] == 'source'
    assert 'dangerouslySetInnerHTML' not in json.dumps(section)


def test_sources_are_additive_ledger_json(tmp_path):
    card = network_card(all_readings())
    ledger = Ledger(tmp_path)
    ledger.add('', 'swap', [card], (), 'ask_when', 'pass')
    stored = ledger.rows()[0]['cards'][0]
    assert stored['sources'] == card.public()['sources']
    assert 'sources' not in card.data['base'].public()
    legacy = {key: value for key, value in stored.items() if key != 'sources'}
    assert all(item['children'][3] is None
               for item in section_for(legacy)['children'][4]['children'])


def test_desktop_has_no_new_remote_asset_or_html_path():
    bundle = (ROOT / 'desktop/plugin.js').read_text()
    assert 'dangerouslySetInnerHTML' not in bundle
    assert not re.search(r'\bfetch\s*\(', bundle)
    for token in ("jsx('img'", "jsx('iframe'", 'url('):
        assert token not in bundle
    assert bundle.count("ctx.rest('/checks')") == 1


class Terminal(io.StringIO):
    def __init__(self, tty=True):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


@pytest.mark.parametrize('outcome,code', [('go', '32'), ('ask', '33'), ('stop', '31')])
@pytest.mark.parametrize('prefix', ['', 'if Hermes swapped this now: '])
def test_cli_color_only_leading_outcome_on_tty(monkeypatch, outcome, code, prefix):
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setattr(cli.sys, 'stdout', Terminal())
    tail = ' · your rule\nask · source line\nstop · last line'
    assert cli.colored(prefix + outcome + tail) == prefix + '\033[1;' + code + 'm' + outcome + '\033[0m' + tail


@pytest.mark.parametrize('tty,no_color', [(False, None), (True, ''), (True, '1'), (True, '0')])
def test_cli_no_color_when_set_or_redirected(monkeypatch, tty, no_color):
    monkeypatch.delenv('NO_COLOR', raising=False)
    if no_color is not None:
        monkeypatch.setenv('NO_COLOR', no_color)
    monkeypatch.setattr(cli.sys, 'stdout', Terminal(tty))
    for label in ('go', 'ask', 'stop'):
        line = label + ' · your rule'
        assert cli.colored(line) == line


@pytest.mark.parametrize('text', ['note · rule', 'Go · rule', 'go', 'go ·', 'goose · rule',
                                 'prefix go · rule', ' go · rule', 'go - rule',
                                 '\nask · rule', 'if Hermes swapped this now: now: stop · rule'])
def test_cli_regex_leaves_other_text_unchanged(monkeypatch, text):
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setattr(cli.sys, 'stdout', Terminal())
    assert cli.colored(text) == text


def test_cli_capture_and_other_surfaces_never_get_ansi(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv('NO_COLOR', raising=False)
    card = present(network_card(), Decision('ask', ('your rule: no HEY record',)))
    class Service:
        def card(self, *args, **kwargs):
            return card
    app = Service()
    app.ledger = Ledger(tmp_path)
    app.ledger.add('', 'swap', [card], (), 'ask_when', 'ask')
    app.doctor = lambda: {'answer': 'go · example'}
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    for command in (['card', card.target], ['log'], ['doctor']):
        assert cli.run(app, parser.parse_args(command)) == 0
        assert '\033' not in capsys.readouterr().out
    terminal = Terminal()
    with monkeypatch.context() as patch:
        patch.setattr(cli.sys, 'stdout', terminal)
        assert cli.run(app, parser.parse_args(['card', card.target])) == 0
        assert '\033[1;33mask\033[0m' in terminal.getvalue()
        rendered = [commands.card_command(app, card.target), tools.handler(app, {'address': card.target}),
                    json.dumps(app.ledger.rows())]
        for command in (['log'], ['doctor']):
            terminal.seek(0)
            terminal.truncate(0)
            assert cli.run(app, parser.parse_args(command)) == 0
            rendered.append(terminal.getvalue())
    assert all('\033' not in output and '\\u001b' not in output for output in rendered)
