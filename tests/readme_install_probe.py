"""Exercise the public install command, aborting before any subprocess starts."""

import os
from pathlib import Path
import re
import shlex
import sys
import subprocess
from unittest.mock import patch


class CloneIntercept(BaseException):
    pass


expected_url = None


def forbid_install_io(event, args):
    if event in {"socket.__new__", "subprocess.Popen", "os.system"}:
        raise AssertionError("README install check forbids sockets and subprocesses")


def intercept_git(argv, **kwargs):
    # Newer hosts inspect Git's safe.directory settings before cloning.
    if argv[1:] in (["config", "--system", "-z", "--get-all", "safe.directory"],
                    ["config", "--global", "-z", "--get-all", "safe.directory"]):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
    assert expected_url and "clone" in argv and expected_url in argv, argv
    raise CloneIntercept()


sys.addaudithook(forbid_install_io)
sys.path.insert(0, os.environ["RH_PREFLIGHT_HERMES_SOURCE"])

from hermes_cli.plugins_cmd import cmd_install


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
    expected_url = (identifier if identifier.startswith("file://") else
                    "https://github.com/quotumorg/rh-preflight.git")
    try:
        # Skip catalog refresh; this probe checks custom-source syntax only.
        with patch("subprocess.run", side_effect=intercept_git):
            cmd_install(identifier, enable=False, no_deps=True, allow_removed=True)
    except CloneIntercept:
        pass
    else:
        raise AssertionError(f"Install did not reach clone: {command}")

try:
    expected_url = None
    cmd_install(".", enable=False, no_deps=True, allow_removed=True)
except SystemExit as exc:
    assert exc.code != 0
else:
    raise AssertionError("Stock Hermes must reject the original bare-dot identifier")

print(f"{len(commands)} README install identifiers accepted; no sockets or subprocesses")
