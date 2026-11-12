from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import time

import pytest

from rh_preflight.card import Card, WORDING, token_card
from rh_preflight.hey import Client
from demo.fake_hey import ACTIVE, QUIET, UNKNOWN, RESEARCHED, CONTRACT, MISSING, LIMITED, ERROR, SLOW, REPO


@pytest.mark.parametrize("target,kind", [(ACTIVE, "token"), (QUIET, "token"), (UNKNOWN, "token"),
                                        (RESEARCHED, "token"), (CONTRACT, "contract"), (MISSING, "missing")])
def test_cards(server, target, kind):
    client = Client(server[0])
    card = client.lookup(target)
    assert card.kind == kind
    rendered = card.render()
    assert 4 <= len(rendered.splitlines()) <= 7
    assert ("HEY does not know" in rendered) == (target in {UNKNOWN, RESEARCHED})
    assert not WORDING.search(rendered)
    assert "\u2014" not in rendered


def test_unknown_count_and_latest_change_not_a_ship(server):
    card = Client(server[0]).lookup(RESEARCHED)
    view = card.render()
    assert "Ships in 30 days: not measured" in view
    assert "Latest meaningful ship: not in HEY's answer" in view
    assert "Contract source verification recorded" in view
    assert "Ships in 30 days: 0" not in view
    assert card.evidence_ids == ["abi:example-change"]


def test_contract_creation_and_source_are_separate(server):
    card = Client(server[0]).lookup(CONTRACT)
    assert "Creation:" in card.render() and "Source published: yes (HEY)" in card.render()
    assert "Verified Builder: not in HEY's answer" in card.render()
    paths = [p for p, _ in server[1]["requests"]]
    assert paths[-1] == f"/api/contracts/4663/{CONTRACT}"


def test_no_rpc_or_eoa_inference(server):
    card = Client(server[0]).lookup(MISSING)
    assert "HEY has no record of this address" in card.render()
    assert "EOA" not in card.render()
    assert len(server[1]["requests"]) == 4


def test_activity_belongs_to_project_when_token_mismatch(server):
    server[1]["cards"][ACTIVE]["activity_applies_to_token"] = False
    assert "activity does not apply to this token" in Client(server[0]).lookup(ACTIVE).render()


def test_source_text_is_bounded_and_wording_filtered(server):
    payload = deepcopy(server[1]["cards"][ACTIVE])
    payload["hey_project_name"] = "A\n\x1bB"
    payload["latest_meaningful_ship"]["title"] = WORDING.pattern.split("(?:", 1)[1].split("|", 1)[0]
    view = token_card(ACTIVE, payload).render()
    assert not WORDING.search(view)
    assert "\x1b" not in view
    assert len(view.splitlines()) == 7


def test_cache_ttl_and_header(server):
    ticks = [0.0]
    client = Client(server[0], clock=lambda: ticks[0])
    first = client.lookup(ACTIVE)
    first.data["hey_project_name"] = "mutated locally"
    assert client.lookup(ACTIVE).data["hey_project_name"] == "Harbor Kit"
    assert len(server[1]["requests"]) == 3
    assert not any(k.lower() == "x-hey-integration" for k in server[1]["requests"][0][1])
    ticks[0] = 600
    client.lookup(ACTIVE)
    assert len(server[1]["requests"]) == 6
    assert client.stats()["hits"] == 1


def test_concurrent_same_target_only_fetches_once(server):
    client = Client(server[0])
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(c.kind == "token" for c in pool.map(client.lookup, [ACTIVE] * 12))
    assert len(server[1]["requests"]) == 3


@pytest.mark.parametrize("target", [ERROR, SLOW])
def test_failures_are_unreachable_and_not_cached(server, target, expire_transport):
    client = Client(server[0], timeout=0.04)
    calls = expire_transport(client) if target == SLOW else None
    assert client.lookup(target).kind == "unreachable"
    assert "could not be reached" in client.lookup(target).render()
    assert client.stats()["entries"] == 0
    if target == SLOW:
        assert len(calls) == 8
        assert server[1]['requests'] == []


@pytest.mark.parametrize("http_date", [False, True])
def test_retry_after_cooldown(server, http_date):
    if http_date:
        server[1]["retry_after"] = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    ticks = [0.0]
    client = Client(server[0], clock=lambda: ticks[0])
    assert client.lookup(LIMITED).kind == "unreachable"
    assert client.lookup(ACTIVE).kind == "unreachable"
    assert len(server[1]["requests"]) == 3
    ticks[0] = 40
    assert client.lookup(ACTIVE).kind == "token"


def test_exact_repo_match(server):
    card = Client(server[0]).lookup(REPO + ".git")
    assert card.kind == "token" and card.target == REPO
    assert server[1]["requests"][0][0] == "/api/projects?q=example-lab&limit=48&offset=0"


def test_namesake_or_context_only_repo_never_matches(server):
    server[1]["details"]["harbor-kit"] = {"name": "Harbor Kit", "sources": [
        {"sourceType": "GITHUB_REPO", "contextOnly": True, "url": REPO},
        {"sourceType": "GITHUB_REPO", "contextOnly": False, "url": REPO + "-other"}]}
    card = Client(server[0]).lookup(REPO)
    assert card.kind == "missing"
    assert "HEY has no project record for this repository" in card.render()


def test_repo_search_no_hits(server):
    server[1]["search"] = {"items": [], "total": 0}
    assert Client(server[0]).lookup(REPO).kind == "missing"


def test_repo_search_cannot_complete_is_not_no_record(server):
    server[1]["search"] = {"items": [], "nextOffset": 0}
    assert Client(server[0]).lookup(REPO).kind == "unreachable"


def test_repository_without_token_uses_dossier(server):
    server[1]["details"]["harbor-kit"] = {"name": "Harbor Kit", "researchLevel": "RESEARCHED",
        "activityStatus": "ACTIVE", "sources": [{"sourceType": "GITHUB_REPO", "contextOnly": False, "url": REPO}],
        "url": "https://heyresearch.xyz/project/harbor-kit", "ships": None}
    card = Client(server[0]).lookup(REPO)
    assert card.kind == "repo"
    assert "Ships in 30 days: not measured" in card.render()


def test_wrong_token_identity_response_is_unavailable(server):
    server[1]["cards"][ACTIVE]["contract_address"] = QUIET
    assert Client(server[0]).lookup(ACTIVE).kind == "unreachable"


@pytest.mark.parametrize("origin", ["https://example.org", "http://heyresearch.xyz", "https://heyresearch.xyz.example.org"])
def test_only_disclosed_origin_or_loopback_allowed(origin):
    with pytest.raises(ValueError):
        Client(origin)


@pytest.mark.parametrize("body", [b"invalid-json", b"[]", b"null"])
def test_malformed_response_is_unavailable(server, body):
    server[1]["overrides"][f"/api/v1/builder?chain=4663&token={ACTIVE}"] = (200, body, {})
    assert Client(server[0]).lookup(ACTIVE).kind == "unreachable"


def test_redirect_is_not_followed(server):
    server[1]["overrides"][f"/api/v1/builder?chain=4663&token={ACTIVE}"] = (
        302, {}, {"Location": "https://example.org/not-requested"})
    assert Client(server[0]).lookup(ACTIVE).kind == "unreachable"
    assert len(server[1]["requests"]) == 3


def test_other_bad_request_does_not_become_missing(server):
    server[1]["overrides"][f"/api/v1/builder?chain=4663&token={ACTIVE}"] = (
        400, {"error": "bad_request", "message": "Invalid parameter"}, {})
    assert Client(server[0]).lookup(ACTIVE).kind == "unreachable"
    assert len(server[1]["requests"]) == 3


def test_repo_project_uses_snapshot_meaningful_ship_and_change(server):
    when = "2026-11-01T00:00:00Z"
    server[1]["details"]["harbor-kit"] = {"name": "Harbor Kit", "researchLevel": "RESEARCHED",
        "activityStatus": "ACTIVE", "sources": [{"sourceType": "GITHUB_REPO", "contextOnly": False, "url": REPO}],
        "url": "https://heyresearch.xyz/project/harbor-kit", "ships": [
            {"title": "An announcement", "publishedAt": "2026-11-08T00:00:00Z"},
            {"title": "A counted release", "publishedAt": when, "evidenceId": "ship:example-release"}]}
    server[1]["snapshots"]["harbor-kit"] = {"asOf": when,
        "build": {"activityMeasured": True, "lastShippedAt": when},
        "summary": {"lines": [{"dimension": "latestChange", "change": {
            "what": "Official links changed", "evidenceId": "sourcechange:example-links",
            "time": {"occurredAt": None, "detectedAt": when}}}]}}
    card = Client(server[0]).lookup(REPO)
    assert "A counted release" in card.render() and "An announcement" not in card.render()
    assert "Official links changed" in card.render()
    assert card.evidence_ids == ["ship:example-release", "sourcechange:example-links"]
