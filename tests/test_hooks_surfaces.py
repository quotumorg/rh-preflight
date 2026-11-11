import argparse
from dataclasses import replace
import json
from pathlib import Path
import re

import pytest

from rh_preflight import cli, commands, tools
from rh_preflight import __version__
from rh_preflight.hey import Client
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.plugin import register
from rh_preflight.settings import Settings
from demo.fake_hey import ACTIVE, QUIET, UNKNOWN, REPO


def service(server, tmp_path, **settings):
    return Service(settings=Settings(**settings), client=Client(server[0]), ledger=Ledger(tmp_path), presence=lambda: False)


def test_real_plugin_entry_requests_human_with_card(server, tmp_path):
    app = service(server, tmp_path)
    directive = app.pre_tool_call("mcp__dex__swap", {"chainId": 4663, "token": QUIET}, session_id="example-session", tool_call_id="call-a")
    assert directive["action"] == "approve"
    assert "Harbor Kit" in directive["message"]
    assert "your rule: no ship in 30 days" in directive["message"]
    assert directive["rule_key"].startswith("rh-preflight:")
    row = app.ledger.rows()[0]
    assert row["disposition"] == "ask" and row["decision"] is None
    assert row["cards"][0]["evidence_ids"]


def test_active_passes_and_ledger_omits_arguments(server, tmp_path):
    app = service(server, tmp_path)
    args = {"chainId": 4663, "token": ACTIVE, "private": "synthetic secret marker"}
    assert app.pre_tool_call("swap", args, session_id="example-session") is None
    rows = app.ledger.rows()
    assert len(rows) == 1 and rows[0]["disposition"] == "pass"
    assert "synthetic secret marker" not in json.dumps(rows)
    assert rows[0]["targets"] == [ACTIVE]
    assert app.ledger.today() == {"checked": 1, "asked": 0}


def test_hey_down_asks_instead_of_claiming_no_record(server, tmp_path):
    server[1]["down"] = True
    app = service(server, tmp_path)
    directive = app.pre_tool_call("swap", {"chainId": 4663, "token": ACTIVE})
    assert directive["action"] == "approve" and "could not be reached" in directive["message"]
    assert "has no record" not in directive["message"]
    assert "HEY record not read for this check" in directive["message"]
    assert "HEY does not know" not in directive["message"]


def test_note_never_blocks_and_appends_card(server, tmp_path):
    app = service(server, tmp_path, mode="note")
    args = {"chainId": 4663, "token": QUIET}
    assert app.pre_tool_call("swap", args, session_id="example-session", tool_call_id="call-a") is None
    transformed = app.transform_tool_result("swap", args, '{"result":"example"}', session_id="example-session", tool_call_id="call-a")
    assert json.loads(transformed)["result"] == "example" and "Harbor Kit" in transformed
    assert app.transform_tool_result("swap", args, "unchanged", session_id="example-session", tool_call_id="call-a") is None


def test_parallel_results_keep_their_cards(server, tmp_path):
    app = service(server, tmp_path, mode="note")
    a, b = {"chain": 4663, "token": ACTIVE}, {"chain": 4663, "token": UNKNOWN}
    app.pre_tool_call("swap", a, tool_call_id="a")
    app.pre_tool_call("swap", b, tool_call_id="b")
    assert "activity not measured" in app.transform_tool_result("swap", b, "result", tool_call_id="b")
    assert "Ships in 30 days: 4" in app.transform_tool_result("swap", a, "result", tool_call_id="a")


def test_approval_observer_correlates_only_its_row(server, tmp_path):
    app = service(server, tmp_path)
    args = {"chain": 4663, "token": QUIET}
    first = app.pre_tool_call("swap", args, tool_call_id="a")
    second = app.pre_tool_call("swap", args, tool_call_id="b")
    assert first["rule_key"] != second["rule_key"]
    app.post_approval_response(pattern_key="unrelated", choice="once")
    app.post_approval_response(pattern_key="plugin_rule:" + first["rule_key"], choice="deny")
    rows = app.ledger.rows()
    assert rows[0]["decision"] is None and rows[1]["decision"] == "deny"
    assert app.ledger.today() == {"checked": 2, "asked": 2}


@pytest.mark.parametrize("choice", ["once", "session", "always", "deny", "timeout", "cancelled"])
def test_all_human_response_choices(server, tmp_path, choice):
    app = service(server, tmp_path)
    directive = app.pre_tool_call("swap", {"chain": 4663, "token": QUIET})
    app.post_approval_response(pattern_keys=["plugin_rule:" + directive["rule_key"]], choice=choice)
    assert app.ledger.rows()[0]["decision"] == choice


def test_block_unattended_precedes_host_bypass(server, tmp_path):
    app = service(server, tmp_path)
    app.presence = lambda: True
    directive = app.pre_tool_call("swap", {"chain": 4663, "token": ACTIVE})
    assert directive["action"] == "block" and "unattended actions are blocked" in directive["message"]


def test_allow_unattended_returns_no_approval_directive(server, tmp_path):
    app = service(server, tmp_path, allow_unattended=True)
    app.presence = lambda: True
    assert app.pre_tool_call("swap", {"chain": 4663, "token": QUIET}) is None
    assert app.ledger.rows()[0]["decision"] is None


def test_read_only_tool_and_slash(server, tmp_path):
    app = service(server, tmp_path)
    assert json.loads(tools.handler(app, {"address": ACTIVE}))["kind"] == "network"
    assert json.loads(tools.handler(app, {"repo_url": REPO}))["target"] == REPO
    assert "error" in json.loads(tools.handler(app, {"address": ACTIVE, "repo_url": REPO}))
    assert "Harbor Kit" in commands.card_command(app, ACTIVE)
    assert "Usage:" in commands.card_command(app, "")
    assert not app.ledger.rows()


def test_cli_all_commands_and_doctor(server, tmp_path, capsys):
    app = service(server, tmp_path)
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    assert cli.run(app, parser.parse_args(["card", ACTIVE])) == 0
    assert "Harbor Kit" in capsys.readouterr().out
    cli.run(app, parser.parse_args(["log"]))
    assert json.loads(capsys.readouterr().out) == []
    cli.run(app, parser.parse_args(["doctor"]))
    info = json.loads(capsys.readouterr().out)
    assert info["reachable"] and info["mode"] == "ask_when"
    assert "latency_ms" in info and "cache" in info and info["last_checks"] == []


def test_missing_cli_target_is_actionable_without_reading(capsys):
    class NoReads:
        def card(self, *args, **kwargs):
            pytest.fail("missing target must not read sources")
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    assert cli.run(NoReads(), parser.parse_args(["card"])) == 1
    assert "card <address|GitHub repository URL|project> or card --project <slug>" in capsys.readouterr().out


def test_documented_terminal_walkthrough_requests_approval(server, tmp_path):
    readme = (Path(__file__).resolve().parents[1] / 'README.md').read_text()
    command = re.search(r'^cast call .+$', readme, re.M).group()
    app = service(server, tmp_path, mode='ask')
    directive = app.pre_tool_call('terminal', {'command': command}, tool_call_id='walkthrough')
    assert directive['action'] == 'approve'
    assert directive['message'].startswith('ask · your rule: ask before every detected action')
    app.post_approval_response(pattern_key='plugin_rule:' + directive['rule_key'], choice='deny')
    assert app.ledger.rows()[0]['decision'] == 'deny'
    assert app.ledger.rows()[0]['targets'] == ['0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec']


def test_manifest_compatibility_and_version():
    root = Path(__file__).resolve().parents[1]
    manifest = (root / 'plugin.yaml').read_text()
    assert re.search(r'^version: (.+)$', manifest, re.M).group(1) == __version__
    assert 'requires_hermes: ">=0.23.0"' in manifest
    assert 'requires_hermes: ">=0.23.0"' in (root / 'README.md').read_text()


def test_registration_declares_only_present_surfaces():
    class Context:
        def __init__(self):
            self.hooks, self.tool, self.command, self.cli = {}, None, None, None

        def get_config(self, key, default=None):
            return default

        def register_hook(self, name, callback):
            self.hooks[name] = callback

        def register_tool(self, **kwargs):
            self.tool = kwargs

        def register_command(self, name, **kwargs):
            self.command = name
            self.command_help = kwargs

        def register_cli_command(self, **kwargs):
            self.cli = kwargs["name"]

    ctx = Context()
    register(ctx)
    assert set(ctx.hooks) == {"pre_tool_call", "transform_tool_result", "post_approval_response"}
    assert ctx.tool["name"] == "builder_card" and ctx.command == ctx.cli == "rh-preflight"
    assert ctx.command_help['args_hint'] == '<address|repository-url|project>'
    assert 'network card' in ctx.command_help['description']


def test_storage_failure_explicitly_blocks_ask_modes(server, tmp_path):
    app = service(server, tmp_path)
    directory_file = tmp_path / "not-a-directory"
    directory_file.write_text("example")
    app.ledger = Ledger(directory_file)
    result = app.pre_tool_call("swap", {"chain": 4663, "token": ACTIVE})
    assert result["action"] == "block" and "rh-preflight could not complete this check" in result["message"]
    assert "HEY" not in result["message"]


@pytest.mark.parametrize('reverse', [False, True])
def test_captured_batch_names_only_missing_target_and_correlates_response(tmp_path, reverse):
    from demo import gate_replay as replay
    from demo.r6_replay import PRIORS
    from rh_preflight.policy import evaluate
    with replay.replay_clock() as clock:
        app = replay.replay_service(tmp_path, clock, multi_target=True)
        targets = [PRIORS.lower(), replay.TARGET][::(-1 if reverse else 1)]
        args = {'chainId': 4663, 'tokens': targets, 'private': 'synthetic secret marker'}
        before = evaluate(app._read_cards(targets, 'swap', args, app.settings), app.settings, now=replay.INSTANT)
        first = app.pre_tool_call('swap', args, tool_call_id='first')
        retry = app.pre_tool_call('swap', args, tool_call_id='retry')
        assert first['action'] == retry['action'] == 'approve'
        assert first['rule_key'] != retry['rule_key']
        app.post_approval_response(**replay.simulated_approval_event(
            'swap', first, 'simulated-batch', session_id='simulated-batch', tool_call_id='first'))
        rows = app.ledger.rows()
        assert len(rows) == 2
        for row in rows:
            assert row['rules'] == list(before.rules) == ['your rule: no HEY record']
            assert row['disposition'] == before.action == 'ask'
            for card in row['cards']:
                assert card['answer'] == 'ask · your rule: no HEY record'
                assert card['rules'] == row['rules']
                expected = ([{'target': replay.TARGET, 'rule': row['rules'][0]}]
                            if card['target'] == replay.TARGET else [])
                assert card['rule_targets'] == expected
                assert ('rule target:' in card['card']) == bool(expected)
            assert row['decision'] == ('deny' if 'rh-preflight:' + row['id'] == first['rule_key'] else None)
        assert 'synthetic secret marker' not in json.dumps(rows)
        assert 'rule target: ' + replay.TARGET + ' · your rule: no HEY record' in first['message']
        # Existing Desktop consumes only the stored string, including legacy rows.
        from test_r8 import desktop_tree, find_element
        tree = desktop_tree(rows[0])
        sections = find_element(tree, 'li')['children'][:2]
        details = [find_element(section, 'details')['children'][1]['children'] for section in sections]
        assert sum('rule target: ' + replay.TARGET in content for content in details) == 1
        legacy = json.loads(json.dumps(rows[0]))
        for card in legacy['cards']:
            card.pop('rule_targets')
        assert desktop_tree(legacy) == desktop_tree(rows[0])


@pytest.mark.parametrize('shared', [False, True])
@pytest.mark.parametrize('mode,unattended,allow', [
    ('ask_when', False, False), ('ask', False, False), ('note', False, False),
    ('ask_when', True, False), ('ask', True, True), ('note', True, False)])
def test_batch_explains_own_rules_in_every_mode(tmp_path, monkeypatch, shared, mode, unattended, allow):
    from datetime import datetime, timezone
    from rh_preflight import explanations, hooks
    from rh_preflight.card import Card
    from rh_preflight.policy import evaluate
    a, b = ACTIVE, UNKNOWN
    cards = [Card(a, 'missing'), Card(b, 'missing' if shared else 'unreachable')]
    settings = Settings(mode=mode, allow_unattended=allow)
    app = Service(settings=settings, ledger=Ledger(tmp_path), presence=lambda: unattended)
    monkeypatch.setattr(app, '_read_cards', lambda *args, **kw: cards)
    calls = []
    original = hooks.evaluate
    def evaluate_once(cards, settings, **kwargs):
        calls.append(kwargs['now'])
        return original(cards, settings, **kwargs)
    monkeypatch.setattr(hooks, 'evaluate', evaluate_once)
    explanatory_calls = []
    per_card = explanations.evaluate
    def explain(cards, settings, **kwargs):
        explanatory_calls.append((settings.mode, kwargs['unattended'], kwargs['now']))
        return per_card(cards, settings, **kwargs)
    monkeypatch.setattr(explanations, 'evaluate', explain)
    directive = app.pre_tool_call('swap', {'chain': 4663, 'tokens': [a, b]})
    row = app.ledger.rows()[0]
    aggregate = evaluate(cards, settings, unattended=unattended, now=calls[0])
    assert len(calls) == 1 and isinstance(calls[0], datetime) and calls[0].tzinfo == timezone.utc
    assert explanatory_calls == [('ask_when', False, calls[0])] * 2
    assert row['rules'] == list(aggregate.rules) and row['disposition'] == aggregate.action
    assert (directive['action'] if directive else None) == {
        'ask': 'approve', 'block': 'block', 'pass': None, 'note': None}[aggregate.action]
    own = ['your rule: no HEY record', 'your rule: no HEY record' if shared else
           'your rule: ask when HEY could not be reached']
    for card, rule in zip(row['cards'], own):
        assert card['rule_targets'] == [{'target': card['target'], 'rule': rule}]
        assert card['card'].splitlines()[2] == f"rule target: {card['target']} · {rule}"
        for setting in (rule for rule in aggregate.rules if rule.startswith('your setting:')):
            assert setting in card['card']
        assert 'rule target: ' + (b if card['target'] == a else a) not in card['card']


def test_optional_two_target_walkthrough_is_labelled_and_socket_free(tmp_path, capsys):
    from demo import gate_replay as replay
    with replay.replay_clock() as clock:
        app = replay.replay_service(tmp_path, clock, multi_target=True)
        first, retry, stopped, rows = replay.walkthrough(app, multi_target=True)
    output = capsys.readouterr().out
    assert replay.MULTI_SCENARIO in output
    assert all(line.startswith('simulated · ') for line in output.splitlines())
    assert output.count('rule target: ' + replay.TARGET + ' · your rule: no HEY record') == 1
    assert first['action'] == retry['action'] == 'approve' and stopped['action'] == 'block'
    assert first['rule_key'] != retry['rule_key']
    assert len(rows) == 3 and all(len(row['cards']) == 2 for row in rows)
    assert output.endswith('simulated · action executions: 0\n')
