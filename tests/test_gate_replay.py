"""The capture-backed host simulation cannot dispatch an action or open a socket."""
from pathlib import Path
import socket
import subprocess
import sys
import time
from unittest.mock import Mock, patch

import pytest

from demo import gate_replay as replay
from demo.r6_replay import DELTA, answer
from rh_preflight import hooks, ledger, network
from rh_preflight.sources import chain


ROOT = Path(__file__).resolve().parents[1]


def test_denial_observer_correlates_only_first_request_and_persists(tmp_path, capsys):
    execute = Mock()
    with replay.replay_clock() as clock:
        app = replay.replay_service(tmp_path, clock)
        with patch.object(app, 'post_approval_response', wraps=app.post_approval_response) as observer:
            first, retry, unattended, rows = replay.walkthrough(app, execute)
        observer.assert_called_once_with(
            command='<swap> (plugin approval rule)', description=first['message'],
            pattern_key='plugin_rule:' + first['rule_key'],
            pattern_keys=['plugin_rule:' + first['rule_key']],
            session_key='simulated-replay', surface='cli', choice='deny',
            session_id='simulated-replay', tool_call_id='first', turn_id='',
            telemetry_schema_version='hermes.observer.v1')
    execute.assert_not_called()
    assert first['action'] == retry['action'] == 'approve'
    assert first['rule_key'] != retry['rule_key']
    by_key = {'rh-preflight:' + row['id']: row for row in ledger.Ledger(tmp_path).rows()}
    assert len(by_key) == len(rows) == 3
    assert by_key[first['rule_key']]['decision'] == 'deny'
    assert by_key[retry['rule_key']]['decision'] is None
    assert unattended['action'] == 'block'
    stopped = next(row for row in rows if row['disposition'] == 'block')
    assert stopped['decision'] is None
    assert stopped['cards'][0]['card'].startswith('stop · human approval required, unattended')
    assert all(row['time'] == replay.INSTANT.isoformat() for row in rows)
    assert all(row['targets'] == [replay.TARGET] for row in rows)
    output = capsys.readouterr().out
    assert all(line.startswith('simulated · ') for line in output.splitlines())
    assert output.endswith('simulated · action executions: 0\n')
    assert 'human response: denied' in output
    assert 'retry: fresh approval required' in output


def test_hook_uses_only_exact_matching_captures(tmp_path):
    with replay.replay_clock() as clock:
        app = replay.replay_service(tmp_path, clock)
        hey, sentinel = app.client._read, app.network.clients['sentinelhood']._read
        with patch.object(app.client, '_read', wraps=hey) as hey_read, \
                patch.object(app.network.clients['sentinelhood'], '_read', wraps=sentinel) as sentinel_read:
            directive = app.pre_tool_call('swap', {'chainId': 4663, 'token': replay.TARGET})
        assert directive['message'].startswith('ask · your rule: no HEY record\n')
        assert 'HEY Research Lab: no published project' in directive['message']
        assert 'Sentinelhood: no contract at this address, block 110264826' in directive['message']
        assert '11 sources · 2 answered · off:' in directive['message']
        hey_read.assert_called_once()
        sentinel_read.assert_called_once()
        with pytest.raises(AssertionError, match='uncaptured reads'):
            hey('/api/agent/builder_status?address=' + DELTA, clock())
        with pytest.raises(AssertionError, match='uncaptured reads'):
            sentinel('/api/service?action=token&value=' + DELTA, clock())
        with pytest.raises(AssertionError, match='uncaptured reads'):
            app.network.clients['toll']._read('/uncaptured', clock())


def test_replay_freezes_hoodlock_wall_clock_and_restores_production_bindings(tmp_path):
    original = hooks.time, network.time, ledger.datetime
    # A host clock after the captured unlock must not expire the replayed lock.
    with patch.object(time, 'time', return_value=1900000000), \
            patch.object(chain, 'VERIFIED', set(chain.VERIFIED)):
        with replay.replay_clock() as clock:
            app = replay.replay_service(tmp_path, clock)
            client = app.network.clients['hoodlock']
            client._read = lambda path, deadline, payload=None: answer('hoodlock', path, payload)
            assert client.clock() == client.wall_clock() == replay.INSTANT.timestamp()
            assert hooks.time.monotonic() == network.time.monotonic() == clock()
            assert ledger.datetime.now(replay.INSTANT.tzinfo) == replay.INSTANT
            reading = client.lookup(DELTA)
            assert reading.token_lock is True
            assert '10.00% of supply locked until 2027-01-31' in reading.line
    assert (hooks.time, network.time, ledger.datetime) == original


def test_socket_creation_forbidden_and_restored():
    original = socket.socket
    with replay.replay_clock():
        with pytest.raises(AssertionError, match='socket creation'):
            socket.socket()
    assert socket.socket is original


def test_main_uses_only_temporary_ledger_and_restores_clocks(tmp_path, monkeypatch, capsys):
    original = hooks.time, network.time, ledger.datetime, socket.socket
    temporary_directory = replay.TemporaryDirectory
    monkeypatch.setattr(replay, 'TemporaryDirectory', lambda: temporary_directory(dir=tmp_path))
    monkeypatch.setattr(sys, 'argv', ['demo/gate_replay.py'])
    monkeypatch.setattr(ledger, 'data_dir', replay.forbidden)
    monkeypatch.setattr(hooks, 'unattended', replay.forbidden)
    monkeypatch.setattr(replay.Settings, 'from_context', replay.forbidden)
    connect = ledger.Ledger._connect
    directories = []
    def temporary_connect(self):
        assert self.directory is not None
        assert self.directory.resolve().is_relative_to(tmp_path.resolve())
        directories.append(self.directory)
        return connect(self)
    monkeypatch.setattr(ledger.Ledger, '_connect', temporary_connect)
    replay.main()
    assert directories and len(set(directories)) == 1
    assert not directories[0].exists()
    assert (hooks.time, network.time, ledger.datetime, socket.socket) == original
    assert capsys.readouterr().out.endswith('simulated · action executions: 0\n')
    with pytest.raises(RuntimeError, match='interrupted replay'):
        with replay.replay_clock():
            raise RuntimeError('interrupted replay')
    assert (hooks.time, network.time, ledger.datetime, socket.socket) == original


def test_command_is_byte_stable_and_readme_output_is_exact():
    command = [sys.executable, 'demo/gate_replay.py']
    first = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=10)
    second = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=10)
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert first.stdout == second.stdout
    section = (ROOT / 'README.md').read_text().split('## See the gate without a model\n', 1)[1]
    documented = section.split('```text\n', 1)[1].split('```', 1)[0]
    assert first.stdout.decode() == documented


def test_explicit_scenario_with_socket_audit_guard():
    # The audit guard also rejects sockets independently of the demo's own guard.
    script = """
import runpy, sys
def guard(event, args):
    if event == 'socket.__new__':
        raise AssertionError('socket creation forbidden by test')
sys.addaudithook(guard)
sys.argv = ['demo/gate_replay.py', '--scenario', 'deny-retry-unattended']
runpy.run_path('demo/gate_replay.py', run_name='__main__')
"""
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith('simulated · action executions: 0\n')
