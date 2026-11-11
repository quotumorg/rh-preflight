from dataclasses import replace

import pytest

from rh_preflight.extract import addresses, chain_context, extract, normalize_repo
from rh_preflight.settings import Settings
from demo.fake_hey import ACTIVE, QUIET, REPO


def test_nested_addresses_dedup_and_boundaries():
    value = {"route": [{"token": ACTIVE.upper().replace("0X", "0x")}, QUIET], "again": ACTIVE,
             "too_long": ACTIVE + "0", "word": "prefix" + ACTIVE}
    assert addresses(value) == [ACTIVE, QUIET]


def test_five_addresses_cap():
    assert len(addresses(["0x" + f"{i:040x}" for i in range(100, 120)])) == 5


@pytest.mark.parametrize("args,assume,expected", [
    ({"chainId": 4663}, None, True), ({"chain": "4663"}, None, True),
    ({"nested": {"chain_id": "0x1237"}}, None, True),
    ({"chainId": 1}, 4663, False), ({"chain": "other"}, 4663, False),
    ({"chain": True}, 4663, False), ({}, 4663, True), ({}, None, False),
    ({"rpc": "https://rpc.mainnet.chain.robinhood.com"}, None, True),
    ({"rpc": "https://rpc.nodeflare.app/robinhood/public"}, None, True),
    ({"rpc": "https://rpc.mainnet.chain.robinhood.com.example.org"}, None, False),
    ({"rpc": "https://rpc.testnet.chain.robinhood.com"}, None, False),
    ({"a": {"chainId": 4663}, "b": {"chain": 1}}, None, False),
])
def test_chain(args, assume, expected):
    assert chain_context(args, assume) is expected


@pytest.mark.parametrize("name", ["swap", "buy_token", "mcp__dex__swap", "mcp__wallet__approve", "deposit", "deploy"])
def test_default_tool_classes(name):
    assert extract(name, {"chainId": 4663, "nested": {"address": ACTIVE}}, Settings()).addresses == (ACTIVE,)


def test_configurable_regex():
    config = Settings(tool_patterns=["^custom_action$"])
    assert not extract("swap", {"chain": 4663, "token": ACTIVE}, config).addresses
    assert extract("custom_action", {"chain": 4663, "token": ACTIVE}, config).addresses


@pytest.mark.parametrize("command", [
    f"cast send {ACTIVE} 'poke()' --chain 4663",
    f"cast call {ACTIVE} 'state()' --rpc-url https://rpc.mainnet.chain.robinhood.com",
])
def test_cast(command):
    assert extract("terminal", {"command": command}, Settings()).addresses == (ACTIVE,)


def test_cast_explicit_other_chain_beats_assumption():
    assert not extract("terminal", {"command": f"cast send {ACTIVE} --chain=1"}, Settings(assume_chain=4663)).addresses


@pytest.mark.parametrize("command", [f"hermes plugins install {REPO}", f"hermes mcp add example {REPO}"])
def test_installs(command):
    assert extract("terminal", {"command": command}, Settings()).repos == (REPO,)


@pytest.mark.parametrize("value,expected", [
    (REPO + ".git/", REPO), (REPO.upper().replace("HTTPS://", "https://"), REPO),
    (REPO + "/tree/main", None), (REPO + "?token=example", None),
    (REPO + "#anchor", None), ("https://github.com.example.org/a/b", None),
    ("https://user:password@github.com/a/b", None), ("https://github.com/a/..", None),
    ("https://github.com:bad/a/b", None),
])
def test_repo_normalization(value, expected):
    assert normalize_repo(value) == expected


def test_unrelated_tools_and_other_chains():
    assert not extract("read_file", {"chain": 4663, "text": ACTIVE}, Settings()).addresses
    assert not extract("swap", {"chain": 1, "text": ACTIVE}, Settings()).addresses
    assert not extract("swap", {"text": ACTIVE}, Settings()).addresses


@pytest.mark.parametrize("changes", [
    {"mode": "anything"}, {"quiet_days": 0}, {"quiet_days": True}, {"timeout": 0},
    {"timeout": float("inf")}, {"allow_unattended": "false"},
    {"assume_chain": 1}, {"tool_patterns": ["["]},
])
def test_settings_reject_invalid(changes):
    with pytest.raises((ValueError, __import__("re").error)):
        replace(Settings(), **changes)
