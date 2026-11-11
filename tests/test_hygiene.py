import ast
import json
import os
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
import re
import subprocess
import sys

import pytest

from rh_preflight.card import WORDING, _trnch_attribution
from demo.captures import CAPTURE_ROOT, capture_path
from demo.r6_replay import cards


ROOT = Path(__file__).resolve().parents[1]


def public_files():
    for directory, folders, names in os.walk(ROOT):
        folders[:] = [name for name in folders if name not in {'.git', '__pycache__', '.pytest_cache'}]
        for name in names:
            if name != '.gitignore':
                yield Path(directory) / name


def public_text(path):
    # The banner is an image; every other public file must still decode strictly.
    raw = path.read_bytes()
    if path.suffix == '.png':
        assert raw.startswith(b'\x89PNG\r\n\x1a\n'), path
        return ''
    return raw.decode('utf-8')


def test_hygiene_accepts_png_but_keeps_text_strict(tmp_path):
    banner = ROOT / 'docs' / 'banner.png'
    assert public_text(banner) == ''
    disguised = tmp_path / 'source.png'
    disguised.write_text('this is text, not a PNG')
    with pytest.raises(AssertionError):
        public_text(disguised)
    source = tmp_path / 'source.py'
    source.write_bytes(b'\xff')
    with pytest.raises(UnicodeDecodeError):
        public_text(source)


def test_all_authored_user_facing_strings_follow_wording_rule():
    # The regex definition names the prohibited terms so it can omit source text.
    # Every other Python string, JSON fixture, JS string and README is scanned.
    for path in list((ROOT / "rh_preflight").rglob("*.py")) + list((ROOT / "demo").glob("*.py")):
        tree = ast.parse(path.read_text())
        exempt = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {"WORDING", "_TRNCH_WORDING"} for t in node.targets):
                exempt.update(id(n) for n in ast.walk(node))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in exempt:
                # Hookr's numeric API field is not authored card wording.
                if path.name == 'hookr.py' and node.value == 'score':
                    continue
                if path.name == 'wormhole.py' and node.value in {
                    'verdict', ' has not observed this token; that is not a verdict · '}:
                    continue
                if path.name == 'trnch.py' and node.value in {
                    'passed the rug screen, live in the Heat',
                    'no rug, no dump on record · first seen ',
                    'rugged', 'dumped', 'blocked · '}:
                    continue
                if path.name == 'card.py' and node.value == (
                    r'Agent Wormhole has not observed this token; that is not a verdict'
                    r' · https://dashboard\.agentwormhole\.com/t/4663/0x[0-9a-fA-F]{40}'):
                    continue
                if path.name == 'policy.py' and node.value == (
                    'your setting: human approval required; unattended actions are blocked'):
                    continue
                assert not WORDING.search(node.value), (path.name, node.lineno)
    # The existing Wormhole absence phrase is already allowed above; the
    # regenerated PRIORS example must retain that exact bundled source line.
    wormhole_absence = {r.line for r in cards()[0].data["sources"]
                        if r.source == "Agent Wormhole" and r.state == "missing"}
    local_gate_copy = {
        '| `stop` | The detected call is blocked because a person cannot answer, or the local check failed. |',
        'The plugin detects literal `cast call` commands as well as writes. Its card should begin '
        '`ask · your rule: ask before every detected action`; Hermes presents the human approval choices. '
        'Choose **deny**. The command is blocked before execution, so this walkthrough does not require '
        'a wallet or an installed `cast`. If the agent only explains the command, request a terminal '
        'tool call. A slash-card read cannot demonstrate this prompt.',
    }
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
    for path in [ROOT / "README.md", ROOT / "desktop" / "plugin.js", *ROOT.glob("tests/fixtures/*.json")]:
        # JSON keys name API fields, not authored source statements.
        lines = strings(json.loads(path.read_text())) if path.suffix == '.json' else path.read_text().splitlines()
        assert not any(WORDING.search(line) and not (path.name == "README.md" and
                       (_trnch_attribution(line) or line in wormhole_absence or line in local_gate_copy))
                       for line in lines), path.name


def test_public_files_contain_no_local_paths_or_long_dashes():
    for path in public_files():
        content = public_text(path)
        assert not any(chr(code) in content for code in (0x2013, 0x2014)), path.name
        # Build the local-home prefixes so the guard itself stays portable.
        assert not re.search("/" + "(?:Users|home)/[A-Za-z]", content), path.name


def test_plugin_has_no_external_fixture_or_absolute_filesystem_paths():
    # Public HTTP routes beginning with a slash are endpoint paths.
    parent_lookup = re.compile(r'parents\s*\[\s*' + str(2) + r'\s*\]')
    sibling = '..' + '/inputs'
    absolute = re.compile(
        r'(?<![\w:])/(?:Users|home|tmp|private|etc|var|opt|Volumes|root|workspace|srv|mnt|usr|bin)/'
        r'|\b[A-Za-z]:[\\/]')
    for path in public_files():
        assert path.resolve().is_relative_to(ROOT), path
        body = public_text(path)
        assert not parent_lookup.search(body), path
        assert sibling not in body, path
        assert not absolute.search(body), path
        if path.suffix != '.py':
            continue
        for node in ast.walk(ast.parse(body)):
            values = []
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
                'Path', 'PurePosixPath', 'PureWindowsPath'}:
                values = node.args
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                values = [node.right]
            for value in values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    assert not PurePosixPath(value.value).is_absolute(), (path, node.lineno)
                    assert not PureWindowsPath(value.value).is_absolute(), (path, node.lineno)


def test_every_fixture_reference_exists_inside_plugin():
    fixtures = list((ROOT / 'tests' / 'fixtures').rglob('*'))
    references = set()
    for path in public_files():
        if path.suffix != '.py' or path.relative_to(ROOT).parts[0] not in {'tests', 'demo'}:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            name = node.value
            if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:json|html)', name):
                continue
            matches = [fixture for fixture in fixtures if fixture.is_file()
                       and fixture.relative_to(ROOT / 'tests' / 'fixtures').as_posix().endswith(name)]
            assert matches, (path, node.lineno, name)
            for fixture in matches:
                assert fixture.resolve().is_relative_to(ROOT), fixture
                references.add(fixture)
    captures = {path for path in CAPTURE_ROOT.rglob('*') if path.suffix in {'.json', '.html'}}
    assert captures <= references, 'remove unused captures or spell dynamic filenames in full'


def test_capture_resolver_rejects_escape_and_missing_files(tmp_path):
    with pytest.raises(ValueError):
        capture_path(tmp_path / ('outside' + '.json'))
    with pytest.raises(ValueError):
        capture_path('..', folder='..')
    with pytest.raises(FileNotFoundError):
        capture_path('missing' + '.json')
    assert capture_path('rpc-chainid.json', 'r5').is_file()


def test_offline_demo_runs_under_thirty_seconds():
    result = subprocess.run([sys.executable, "demo/offline.py"], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("rh-preflight by quotum with HEY Research Lab · https://heyresearch.xyz\ninvented records from a local fake HEY server, not HEY data\n")
    assert "Saved source lines from 2026-11-09" in result.stdout
    assert "Saved captures from 2026-11-09 for PRIORS and NVDA" in result.stdout
    assert result.stdout.rstrip().endswith("4 synthetic calls checked; 1 human approval requests; no actions executed")


def test_joint_credit_on_public_surfaces():
    readme = (ROOT / 'README.md').read_text()
    assert readme.splitlines()[2] == 'rh-preflight by [quotum](https://quotum.org) with [HEY Research Lab](https://heyresearch.xyz).'
    manifest = (ROOT / 'plugin.yaml').read_text()
    assert 'rh-preflight by quotum with HEY Research Lab. Reads builder records and ten Robinhood Chain projects; rh-preflight applies your approval rules before Hermes acts on chain 4663.' in manifest
    assert 'author: quotum, HEY Research Lab' in manifest
    desktop = (ROOT / 'desktop/plugin.js').read_text()
    assert desktop.count('CREDIT = "rh-preflight by quotum with HEY Research Lab"') == 1
    assert "children: [CREDIT," in desktop
    assert 'Copyright (c) 2026 quotum and HEY Research Lab' in (ROOT / 'LICENSE').read_text()
    assert (ROOT / 'docs/SOURCES.md').read_text().startswith('HEY Research Lab is co-author and the first source.')
