"""Public cron context and host-owned approval directives, without provider reads."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from rh_preflight.card import Card
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.runtime import unattended
from rh_preflight.settings import Settings


class RaisingContext:
    def current_cron_execution(self):
        raise RuntimeError('context unavailable')


CONTEXTS = [None, SimpleNamespace(), RaisingContext(),
            SimpleNamespace(current_cron_execution=lambda: None),
            SimpleNamespace(current_cron_execution=lambda: {}),
            SimpleNamespace(current_cron_execution=lambda: {'job_id': 'example'})]


@pytest.mark.parametrize('ctx,expected', list(zip(CONTEXTS, [False, False, False, False, True, True])))
def test_public_cron_identity(ctx, expected):
    assert unattended(ctx) is expected


def app_for(ctx, tmp_path, monkeypatch, card, **options):
    settings = Settings(**options)
    app = Service(settings=settings, ledger=Ledger(tmp_path), context=ctx)
    monkeypatch.setattr(app, '_settings', lambda: settings)
    monkeypatch.setattr(app, '_read_cards', lambda *args, **kwargs: [card])
    return app


@pytest.mark.parametrize('ctx', CONTEXTS[:4])
@pytest.mark.parametrize('allow', [False, True])
@pytest.mark.parametrize('bypass', ['', 'yolo'])
def test_non_cron_asks_always_delegate_to_hermes(ctx, allow, bypass, tmp_path, monkeypatch):
    if bypass == 'yolo':
        monkeypatch.setenv('HERMES_YOLO_MODE', '1')
    app = app_for(ctx, tmp_path, monkeypatch, Card('example', 'missing'), allow_unattended=allow)
    directive = app.pre_tool_call('swap', {'chainId': 4663, 'token': '0x' + 'ab' * 20})
    assert directive['action'] == 'approve'
    assert 'no HEY record' in directive['message']
    assert directive['rule_key'].startswith('rh-preflight:')
    assert app.ledger.rows()[0]['disposition'] == 'ask'


@pytest.mark.parametrize('allow,expected', [(False, 'block'), (True, None)])
def test_cron_only_exception(allow, expected, tmp_path, monkeypatch):
    app = app_for(CONTEXTS[-1], tmp_path, monkeypatch, Card('example', 'missing'),
                  allow_unattended=allow)
    directive = app.pre_tool_call('swap', {'chainId': 4663, 'token': '0x' + 'ab' * 20})
    assert (directive['action'] if directive else None) == expected


@pytest.mark.parametrize('ctx', CONTEXTS)
@pytest.mark.parametrize('allow', [False, True])
def test_codeless_unlimited_approval_still_blocks(ctx, allow, tmp_path, monkeypatch):
    card = Card('example', 'transaction', {'lines': [], 'approvals': [], 'approval_eoa': True})
    app = app_for(ctx, tmp_path, monkeypatch, card, allow_unattended=allow)
    monkeypatch.setenv('HERMES_YOLO_MODE', '1')
    directive = app.pre_tool_call('swap', {'chainId': 4663, 'token': '0x' + 'ab' * 20})
    assert directive['action'] == 'block'
    assert 'unlimited approval to a spender with no code' in directive['message']


def test_repository_has_no_private_host_imports():
    root = Path(__file__).resolve().parents[1]
    for path in root.rglob('*.py'):
        if '.git' in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            names = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                     [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            assert all(name.split('.')[0] != 'tools' for name in names), (path, node.lineno)

            if isinstance(node, ast.ImportFrom) and (node.module or '').split('.')[0] in {
                    'hermes_cli', 'model_tools', 'plugins'}:
                assert all(not alias.name.startswith('_') for alias in node.names), (path, node.lineno)
            assert all(not name.startswith(('hermes_cli._', 'plugins._')) for name in names), (path, node.lineno)
