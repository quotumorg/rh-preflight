"""Resolve documented install identifiers with stock Hermes, without installing."""

import os
from pathlib import Path
import re
import shlex
import sys


def forbid_install_io(event, args):
    if event in {"socket.__new__", "subprocess.Popen", "os.system"}:
        raise AssertionError("README resolver check forbids sockets and subprocesses")


sys.addaudithook(forbid_install_io)
sys.path.insert(0, os.environ["RH_PREFLIGHT_HERMES_SOURCE"])

# This is the stock install path's identifier parser, before any Git clone.
from hermes_cli.plugins_cmd import _resolve_git_url


root = Path(__file__).resolve().parents[1]
readme = (root / "README.md").read_text()
commands = re.findall(r'(?:^|`)(hermes\s+plugins\s+install\s+[^`\n]+)', readme, re.MULTILINE)
assert commands, "README must print an install command"
for command in commands:
    argv = shlex.split(command)
    assert argv[:3] == ["hermes", "plugins", "install"], command
    # Expand only the documented cwd substitution, without invoking a shell.
    identifier = argv[3].replace("$(pwd)", str(root))
    assert "$" not in identifier, command
    git_url, subdir = _resolve_git_url(identifier)
    assert git_url and subdir is None, command

try:
    _resolve_git_url(".")
except ValueError:
    pass
else:
    raise AssertionError("Stock Hermes must reject the original bare-dot identifier")

print(f"{len(commands)} README install identifiers accepted; no sockets or subprocesses")
