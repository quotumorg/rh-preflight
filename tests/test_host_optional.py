import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("scenario", ["interactive", "quickstart", "cron", "yolo", "off", "single-query", "transform"])
def test_host_dispatch_if_runtime_is_available(tmp_path, scenario):
    python = os.environ.get("RH_PREFLIGHT_HERMES_PYTHON")
    source = os.environ.get("RH_PREFLIGHT_HERMES_SOURCE")
    if not python or not source:
        pytest.skip("Set RH_PREFLIGHT_HERMES_SOURCE and RH_PREFLIGHT_HERMES_PYTHON for host checks")
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
    env.update(HERMES_HOME=str(tmp_path), HERMES_INTERACTIVE="1", PYTHONDONTWRITEBYTECODE="1")
    if scenario == "cron":
        env["HERMES_CRON_SESSION"] = "1"
    elif scenario == "single-query":
        env["HERMES_SINGLE_QUERY_SESSION"] = "1"
    elif scenario == "yolo":
        env["HERMES_YOLO_MODE"] = "1"
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  enabled: []\n  disabled: []\n"
        "approvals:\n  mode: " + ('"off"' if scenario == "off" else "manual") +
        "\n  cron_mode: approve\n  single_query_mode: approve\n")
    result = subprocess.run([python, "tests/host_probe.py", scenario], cwd=Path(__file__).resolve().parents[1],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr[-6000:] + result.stdout[-2000:]
    assert f"Hermes {scenario} dispatch confirmed" in result.stdout


def test_readme_install_commands_if_runtime_is_available(tmp_path):
    python = os.environ.get("RH_PREFLIGHT_HERMES_PYTHON")
    source = os.environ.get("RH_PREFLIGHT_HERMES_SOURCE")
    if not python or not source:
        pytest.skip("Set RH_PREFLIGHT_HERMES_SOURCE and RH_PREFLIGHT_HERMES_PYTHON for host checks")
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
    env.update(HERMES_HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([python, "tests/readme_install_probe.py"],
                            cwd=Path(__file__).resolve().parents[1], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr[-6000:] + result.stdout[-2000:]
    assert "README install identifiers accepted; no sockets or subprocesses" in result.stdout
