"""Optional check against an installed Hermes runtime, using only synthetic calls."""

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, os.environ["RH_PREFLIGHT_HERMES_SOURCE"])

from rh_preflight.hey import Client
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.runtime import unattended
from rh_preflight.settings import Settings
from demo.fake_hey import ACTIVE, QUIET, fake_server
from hermes_cli.plugins import PluginContext, get_plugin_manager, resolve_pre_tool_block
from hermes_cli.plugins_manifest import PluginManifest
from tools.terminal_tool import set_approval_callback


scenario = sys.argv[1]
manager = get_plugin_manager()
manager.discover_and_load()
ctx = PluginContext(PluginManifest(name="rh-preflight", source="user"), manager)
with fake_server() as (origin, state):
    app = Service(settings=Settings(mode="ask" if scenario == "quickstart" else "ask_when"),
                  client=Client(origin), ledger=Ledger(),
                  presence=lambda: unattended(ctx))
    ctx.register_hook("pre_tool_call", app.pre_tool_call)
    if scenario == "transform":
        from model_tools import _CallIds, _apply_transform_tool_result_hook
        from tools.terminal_tool import _error_json

        ctx.register_hook("transform_tool_result", app.transform_tool_result)
        ids = _CallIds(session_id="example-session", tool_call_id="example-call")
        args = {"command": f"cast call {ACTIVE} 'symbol()(string)' --rpc-url https://rpc.mainnet.chain.robinhood.com"}
        label = "rh-preflight public network data (external source text):\n"
        originals = [json.dumps({"output": "synthetic command output\nλ", "exit_code": 0,
                                 "error": None, "extra": {"retained": True}}, ensure_ascii=False),
                     _error_json("synthetic timeout", exit_code=124),
                     json.dumps({"output": "done", "exit_code": None, "rh_preflight": "tool data"}),
                     r'{"output":"\ud800","exit_code":0}',
                     '{"output":"done","large":1e400,"tiny":1e-400,"precise":0.123456789012345678901}']
        for original in originals:
            assert resolve_pre_tool_block("terminal", args, **ids.hook_kwargs()) is None
            pending = app.pending[app._key("terminal", args, ids.hook_kwargs())]
            transformed = _apply_transform_tool_result_hook("terminal", args, original, 0, ids)
            closing = original.rindex("}")
            assert transformed[:closing] == original[:closing]
            transformed.encode("utf-8")
            before = json.loads(original, parse_int=str, parse_float=str)
            after = json.loads(transformed, parse_int=str, parse_float=str)
            assert list(after)[:-1] == list(before)
            assert {key: after[key] for key in before} == before
            assert len(after) == len(before) + 1
            assert list(after.values())[-1] == label + pending
            assert "go · no rule of yours fired" in list(after.values())[-1]
            assert _apply_transform_tool_result_hook("terminal", args, transformed, 0, ids) == transformed
        assert len(app.ledger.rows()) == len(originals)
        print("Hermes transform dispatch confirmed; no actions executed")
        sys.exit(0)
    events = []
    def observe_response(**event):
        events.append(event)
        app.post_approval_response(**event)
    ctx.register_hook("post_approval_response", observe_response)
    prompts = []

    def simulated_human(command, description, **kwargs):
        prompts.append(description)
        return "deny"

    set_approval_callback(simulated_human)
    tool, args = "swap", {"chainId": 4663, "token": QUIET}
    if scenario == "quickstart":
        command = "cast call 0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec 'symbol()(string)' --rpc-url https://rpc.mainnet.chain.robinhood.com"
        assert command in (Path(__file__).resolve().parents[1] / "README.md").read_text()
        tool, args = "terminal", {"command": command}
    result = resolve_pre_tool_block(tool, args,
                                    session_id="example-session", tool_call_id="example-call")
    assert result is not None
    if scenario in {"interactive", "quickstart"}:
        from demo.gate_replay import simulated_approval_event
        assert len(events) == 1
        event = events[0]
        directive = {'rule_key': 'rh-preflight:' + app.ledger.rows()[0]['id'],
                     'message': app.ledger.rows()[0]['cards'][0]['card']}
        expected = simulated_approval_event(tool, directive, event['session_key'],
                                            session_id='example-session', tool_call_id='example-call')
        assert event.keys() == expected.keys(), (event.keys(), expected.keys())
        for key in expected:
            assert event[key] == expected[key], (key, event[key], expected[key])
    if scenario == "quickstart":
        assert len(prompts) == 1
        assert "ask · your rule: ask before every detected action" in prompts[0]
        assert app.ledger.rows()[0]["decision"] == "deny"
        assert app.ledger.rows()[0]["tool_name"] == "terminal"
    elif scenario == "interactive":
        assert len(prompts) == 1 and "Harbor Kit" in prompts[0]
        assert "your rule: no ship in 30 days" in prompts[0]
        assert app.ledger.rows()[0]["decision"] == "deny"
        assert resolve_pre_tool_block("swap", {"chainId": 4663, "token": ACTIVE},
                                      session_id="example-session", tool_call_id="active-call") is None
        assert len(prompts) == 1
        state["down"] = True
        assert resolve_pre_tool_block("swap", {"chainId": 4663, "token": "0x" + "ab" * 20},
                                      session_id="example-session", tool_call_id="down-call") is not None
        assert len(prompts) == 2 and "could not be reached" in prompts[-1]
    else:
        assert prompts == []
        assert "unattended actions are blocked" in result
        assert app.ledger.rows()[0]["disposition"] == "block"
    # Load the real router adapter as the dashboard does, in the temporary profile.
    from importlib.util import module_from_spec, spec_from_file_location
    api_spec = spec_from_file_location("example_rh-preflight_api", Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py")
    api = module_from_spec(api_spec)
    api_spec.loader.exec_module(api)
    response = api.checks()
    assert response["today"]["checked"] == (3 if scenario == "interactive" else 1)
    assert response["checks"][0]["cards"][0]["card"]
    print(f"Hermes {scenario} dispatch confirmed; no actions executed")
