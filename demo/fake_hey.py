"""Synthetic HEY-shaped records on a loopback HTTP server."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs, urlsplit


ACTIVE = "0x" + "11" * 20
QUIET = "0x" + "22" * 20
UNKNOWN = "0x" + "33" * 20
CONTRACT = "0x" + "44" * 20
MISSING = "0x" + "55" * 20
LIMITED = "0x" + "66" * 20
ERROR = "0x" + "77" * 20
SLOW = "0x" + "88" * 20
RESEARCHED = "0x" + "99" * 20
REPO = "https://github.com/example-lab/harbor-kit"


def fixture(name):
    return json.loads((Path(__file__).resolve().parents[1] / "tests" / "fixtures" / name).read_text()
                      .replace("https://heyresearch.xyz", "https://example.invalid"))


def records():
    active = fixture("active.json")
    now = datetime.now(timezone.utc)
    active["as_of"] = now.isoformat()
    active["latest_meaningful_ship"]["timestamp"] = (now - timedelta(days=1)).isoformat()
    quiet = deepcopy(active)
    quiet.update(contract_address=QUIET, hey_status="QUIET", hey_status_label="Quiet", meaningful_ships_30d=0)
    quiet["latest_meaningful_ship"]["timestamp"] = (now - timedelta(days=45)).isoformat()
    unknown = deepcopy(active)
    unknown.update(contract_address=UNKNOWN, hey_status="UNKNOWN", hey_status_label="Not measured: activity not measurable",
                   activity_measured=False, meaningful_ships_30d=None, repo_url=None)
    researched = deepcopy(unknown)
    researched.update(contract_address=RESEARCHED, verified_builder=False, research_level="RESEARCHED",
                      latest_meaningful_ship=None)
    researched["latest_change"] = {"id": "abi:example-change", "evidence_id": "abi:example-change",
                                   "summary": "Contract source verification recorded", "occurred_at": None,
                                   "detected_at": now.isoformat(), "url": "https://example.invalid/api/evidence/abi%3Aexample-change"}
    return {ACTIVE: active, QUIET: quiet, UNKNOWN: unknown, RESEARCHED: researched}


@contextmanager
def fake_server():
    state = {"cards": records(), "requests": [], "down": False, "slow_seconds": 0.2,
             "retry_after": "1", "search": None, "details": {}, "snapshots": {}, "overrides": {}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["requests"].append((self.path, dict(self.headers)))
            time.sleep(state.get("delays", {}).get(self.path.split("?", 1)[0], 0))
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            code, body, headers = 200, {}, {}
            if state["down"]:
                code, body = 500, {"error": "internal_error", "retryable": True}
            elif url.path.startswith("/api/agent/"):
                if state.get("agent_payload"):
                    body = state["agent_payload"]
                else:
                    code, body = 503, {"error": "temporarily_unavailable"}
            elif url.path == "/api/v1/builder":
                address = query.get("token", [""])[0]
                if address == LIMITED:
                    code, body, headers = 429, {"error": "rate_limited"}, {"Retry-After": state["retry_after"]}
                elif address == ERROR:
                    code, body = 500, {"error": "internal_error"}
                elif address == SLOW:
                    time.sleep(state["slow_seconds"])
                    body = state["cards"][ACTIVE].copy()
                    body["contract_address"] = SLOW
                elif address in state["cards"]:
                    body = state["cards"][address]
                elif address == CONTRACT:
                    code, body = 400, {"error": "bad_request", "message": "That address is well-formed but is not a token identity on this chain.", "retryable": False}
                else:
                    code, body = 404, {"error": "not_found", "contract_address": address, "chain_id": 4663,
                                       "message": "HEY has not published a record", "scan_url": "https://example.invalid/scan"}
            elif url.path.startswith("/api/contracts/4663/"):
                if url.path.endswith(CONTRACT):
                    body = fixture("contract.json")
                else:
                    code, body = 404, {"error": "not_found"}
            elif url.path == "/api/projects":
                body = state["search"] or {"items": [{"slug": "harbor-kit", "name": "Harbor Kit"}], "total": 1, "nextOffset": None}
            elif url.path.endswith("/snapshot"):
                body = state["snapshots"].get(url.path.split("/")[-2], {"asOf": "2026-11-08T00:00:00Z", "build": {"activityMeasured": True}})
            elif url.path.startswith("/api/projects/"):
                slug = url.path.rsplit("/", 1)[-1]
                body = state["details"].get(slug, {
                    "slug": slug, "name": "Harbor Kit", "researchLevel": "VERIFIED_BUILDER", "activityStatus": "SHIPPING",
                    "url": "https://example.invalid/project/harbor-kit", "ships": [],
                    "sources": [{"sourceType": "GITHUB_REPO", "url": REPO, "contextOnly": False}],
                    "token": {"chainId": 4663, "contractAddress": ACTIVE},
                })
            else:
                code, body = 404, {"error": "not_found"}
            if self.path in state["overrides"]:
                code, body, headers = state["overrides"][self.path]
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                chunk_delay = state.get("chunk_delays", {}).get(url.path)
                if chunk_delay:
                    for offset in range(0, len(raw), 8):
                        self.wfile.write(raw[offset:offset + 8])
                        self.wfile.flush()
                        time.sleep(chunk_delay)
                else:
                    self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
