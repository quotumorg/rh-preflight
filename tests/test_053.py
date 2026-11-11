import json

import pytest

from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger


LABEL = "rh-preflight public network data (external source text):\n"
MESSAGE = "go · no rule of yours fired\nHEY Research Lab: example record"


@pytest.fixture
def app(tmp_path):
    service = Service(ledger=Ledger(tmp_path))
    service.pending[service._key("terminal", {}, {})] = MESSAGE
    return service


@pytest.mark.parametrize("exit_code", [0, 1, 124, None])
def test_json_object_preserves_every_value_and_key_order(app, exit_code):
    original = {"output": "command output\nλ", "exit_code": exit_code, "error": None,
                "stdout": "out", "stderr": "diagnostic", "lines": ["line"],
                "extra": {"nested": [False, 0, None, {"x": "y"}]}}
    transformed = app.transform_tool_result("terminal", {}, json.dumps(original))
    parsed = json.loads(transformed)
    assert list(parsed) == [*original, "rh_preflight"]
    assert {key: parsed[key] for key in original} == original
    assert parsed["rh_preflight"] == LABEL + MESSAGE
    assert LABEL.splitlines()[0] in transformed and MESSAGE in parsed["rh_preflight"]
    assert app.transform_tool_result("terminal", {}, transformed) is None


@pytest.mark.parametrize("result", [
    "plain command output", '["output", 0]', '"scalar"', "42", "true", "null",
    '{"output":', "", b'{"output":"bytes"}', None,
])
def test_non_object_results_keep_text_append(app, result):
    assert app.transform_tool_result("terminal", {}, result) == str(result) + "\n\n" + LABEL + MESSAGE


def test_empty_json_object_gains_card(app):
    assert json.loads(app.transform_tool_result("terminal", {}, "{}")) == {"rh_preflight": LABEL + MESSAGE}


def test_collisions_preserve_tool_data_and_append_one_unused_key(app):
    original = {"rh_preflight": {"own": True}, "output": "done", "rh_preflight_2": None,
                "rh_preflight_3": "another tool field"}
    parsed = json.loads(app.transform_tool_result("terminal", {}, json.dumps(original)))
    assert list(parsed) == [*original, "rh_preflight_4"]
    assert {key: parsed[key] for key in original} == original
    assert parsed["rh_preflight_4"] == LABEL + MESSAGE


@pytest.mark.parametrize("original", [
    ' \r\n{ \t}\n ',
    '{"precise":0.123456789012345678901,"large":1e400,"tiny":1e-400,"zero":-0}',
    '{"large":' + '9' * 5000 + '}',
    r'{"output":"\ud800\udfff\udc00","exit_code":0}',
    r'{"output":"\u03bb \/ \" \\ \n \t","emoji":"\ud83d\ude80"}',
    '{"output":"first","output":"second","exit_code":0}',
    '{"output":"' + 'λ' * 300000 + '","exit_code":0}',
    '{"nested":' + '[{"item":' * 200 + '0' + '}]' * 200 + '}',
    r'{"\u0072h_preflight":"tool data","rh_preflight_2":false}',
], ids=["empty-whitespace", "exact-numbers", "huge-integer", "lone-surrogates",
        "escapes", "duplicate-keys", "large-output", "nested", "escaped-key-collision"])
def test_json_object_keeps_original_representation_and_utf8(app, original):
    transformed = app.transform_tool_result("terminal", {}, original)
    closing = original.rindex("}")
    # Compare the original text as well as decoded values: a float round trip
    # can erase precision, overflow to Infinity or underflow to zero.
    assert transformed[:closing] == original[:closing]
    assert transformed.endswith(original[closing:])
    transformed.encode("utf-8")
    before = json.loads(original, parse_int=str, parse_float=str)
    after = json.loads(transformed, parse_int=str, parse_float=str)
    assert list(after)[:-1] == list(before)
    assert {key: after[key] for key in before} == before
    assert len(after) == len(before) + 1
    assert list(after.values())[-1] == LABEL + MESSAGE
    assert app.transform_tool_result("terminal", {}, transformed) is None


@pytest.mark.parametrize("operation", ["loads", "dumps"])
def test_json_errors_fall_back_to_existing_append(app, monkeypatch, operation):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic JSON failure")

    # An explicit call id keeps correlation independent of JSON serialization.
    app.pending[app._key("terminal", {}, {"tool_call_id": "example"})] = MESSAGE
    monkeypatch.setattr("rh_preflight.hooks.json." + operation, fail)
    result = '{"output":"done","exit_code":0}'
    assert app.transform_tool_result("terminal", {}, result, tool_call_id="example") == result + "\n\n" + LABEL + MESSAGE


def test_correlation_error_does_not_escape(app, monkeypatch):
    def fail(*args):
        raise RuntimeError("synthetic correlation failure")

    monkeypatch.setattr(app, "_key", fail)
    assert app.transform_tool_result("terminal", {}, "done") is None


def test_unprintable_result_keeps_card_without_raising(app):
    class Unprintable:
        def __str__(self):
            raise RuntimeError("synthetic string conversion failure")

    assert app.transform_tool_result("terminal", {}, Unprintable()) == LABEL + MESSAGE
