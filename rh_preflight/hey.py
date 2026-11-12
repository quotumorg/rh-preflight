"""Keyless, bounded HTTP reads with a small in-memory cache."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import re
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import __version__
from .card import Card, contract_card, token_card, agent_card
from .extract import ADDRESS, normalize_repo


class Unavailable(Exception):
    """The read did not establish a record or an absence."""


class RateLimited(Unavailable):
    """The source answered with a rate limit."""

    def __init__(self, data=None):
        self.data = data


class Refused(Unavailable):
    """A source returned an HTTP refusal."""

    def __init__(self, status, data=None):
        self.status, self.data = status, data


class ResponseAnswer(Unavailable):
    """A JSON HTTP error answer, distinct from a transport failure."""

    def __init__(self, status, data):
        self.status, self.data = status, data


class TemporaryUnavailable(Unavailable):
    """An upstream service error or transport timeout permits a fallback read."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, base_url="https://heyresearch.xyz", timeout=4.0, ttl=600, clock=time.monotonic):
        parsed = urlsplit(base_url)
        if base_url != "https://heyresearch.xyz" and not (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and not parsed.username and not parsed.password and parsed.path in {"", "/"}
        ):
            raise ValueError("The API origin must be HEY or a loopback demo server")
        self.base_url = base_url.rstrip("/")
        self.timeout, self.ttl, self.clock = timeout, ttl, clock
        self.cache = OrderedDict()
        self.lock = threading.RLock()
        self.stripes = [threading.Lock() for _ in range(32)]
        self.slots = threading.BoundedSemaphore(3)
        self.cooldown = 0.0
        self.hits = self.misses = self.requests = 0
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def stats(self):
        with self.lock:
            return {"entries": len(self.cache), "hits": self.hits, "misses": self.misses,
                    "requests": self.requests, "retry_after_seconds": round(max(0, self.cooldown - self.clock()), 3)}

    def _body(self, response, deadline):
        response = response.fp if isinstance(response, HTTPError) else response
        chunks, size = [], 0
        while True:
            if response.isclosed():
                return b"".join(chunks)
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TemporaryUnavailable()
            response.fp.raw._sock.settimeout(remaining)
            chunk = response.read1(min(65536, 2_000_001 - size))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > 2_000_000:
                raise Unavailable()

    def request_headers(self, path):
        return {}

    def _read(self, path, deadline, payload=None):
        # Reserve time for one retry of transport/5xx failures. A rate limit,
        # refusal or malformed response never causes a second request.
        first_end = min(deadline, self.clock() + 2.0)
        try:
            return self._read_once(path, first_end, payload)
        except TemporaryUnavailable:
            if deadline - self.clock() <= 0.01:
                raise
            return self._read_once(path, deadline, payload)

    def _read_once(self, path, deadline, payload=None):
        remaining = deadline - self.clock()
        if remaining <= 0 or not self.slots.acquire(timeout=max(0, remaining)):
            raise Unavailable()
        try:
            with self.lock:
                if self.clock() < self.cooldown:
                    raise RateLimited()
                self.requests += 1
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise Unavailable()
            request = Request(self.base_url + path, headers={
                "User-Agent": f"rh-preflight/{__version__} (+https://github.com/quotumorg/rh-preflight)",
                "Accept": getattr(self, "accept", "application/json")
            })
            if urlsplit(request.full_url).hostname == "heyresearch.xyz":
                request.add_header("x-hey-integration", f"quotum-hermes/{__version__}")
            for name, value in self.request_headers(path).items():
                request.add_header(name, value)
            if payload is not None:
                request.data = json.dumps(payload).encode()
                request.add_header("Content-Type", "application/json")
            try:
                with self.opener.open(request, timeout=remaining) as response:
                    raw = self._body(response, deadline)
                    if len(raw) > 2_000_000:
                        raise Unavailable()
                    data = self._decode(raw)
                    if not isinstance(data, dict) and not (getattr(self, "allow_list", False) and isinstance(data, list)):
                        raise Unavailable()
                    return 200, data
            except HTTPError as error:
                if error.code == 402:
                    error.close()
                    raise Refused(402) from error
                if error.code == 429:
                    value = error.headers.get("Retry-After", "60")
                    try:
                        delay = float(value)
                    except ValueError:
                        try:
                            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                        except (TypeError, ValueError, OverflowError):
                            delay = 60
                    if not math.isfinite(delay):
                        delay = 60
                    with self.lock:
                        self.cooldown = max(self.cooldown, self.clock() + max(0, delay))
                    data = self._error_body(error, deadline)
                    raise RateLimited(data) from error
                if error.code >= 500:
                    error.close()
                    raise TemporaryUnavailable() from error
                if error.code == 403:
                    data = self._error_body(error, deadline)
                    raise Refused(error.code, data) from error
                if not 400 <= error.code < 500:
                    error.close()
                    raise Unavailable() from error
                try:
                    data = json.loads(self._body(error, deadline))
                    if not isinstance(data, dict) and not (getattr(self, "allow_list", False) and isinstance(data, list)):
                        raise Unavailable()
                    return error.code, data
                except (ValueError, OSError) as decode_error:
                    raise Unavailable() from decode_error
                finally:
                    error.close()
            except (URLError, OSError, socket.timeout) as error:
                raise TemporaryUnavailable() from error
            except ValueError as error:
                raise Unavailable() from error
        finally:
            self.slots.release()

    def _decode(self, raw):
        return json.loads(raw)

    def _error_body(self, error, deadline):
        try:
            data = json.loads(self._body(error, deadline))
            return data if isinstance(data, dict) else None
        except (ValueError, OSError, Unavailable):
            return None
        finally:
            error.close()

    @staticmethod
    def _error_card(target, status, data, label=None):
        if isinstance(data, dict) and isinstance(data.get("answer"), dict):
            return agent_card(target, data)
        label = label or f"HEY answered (HTTP {status})"
        detail = ": " + json.dumps(data, ensure_ascii=False) if data else ""
        return Card(target, "unreachable", {"read_error": label + detail})

    def lookup(self, target, project=None, required=None, deadline=None):
        target = target.strip()
        if ADDRESS.fullmatch(target):
            target = target.lower()
        else:
            repo = normalize_repo(target)
            if repo:
                target = repo
            elif not target or len(target) > 200 or "://" in target:
                raise ValueError("Enter an address, project name or GitHub repository URL")
        required = tuple(required) if required is not None else ("verified_builder", "activity_measured", "meaningful_ships_30d")
        key = (target, project, required)
        # Stripes bound lock memory and collapse concurrent reads of the same target.
        deadline = min(deadline, self.clock() + self.timeout) if deadline is not None else self.clock() + self.timeout
        stripe = self.stripes[hash(target) % len(self.stripes)]
        if not stripe.acquire(timeout=max(0, deadline - self.clock())):
            return Card(target, "unreachable")
        try:
            with self.lock:
                cached = self.cache.get(key) if self.ttl > 0 else None
                if cached and self.clock() - cached[0] < self.ttl:
                    self.hits += 1
                    self.cache.move_to_end(key)
                    return deepcopy(cached[1])
                self.misses += 1
            deadline = deadline if deadline is not None else self.clock() + self.timeout
            try:
                card = self._agent(target, deadline, project, required)
            except TemporaryUnavailable:
                return Card(target, "unreachable")
            except ResponseAnswer as error:
                return self._error_card(target, error.status, error.data)
            except Refused as error:
                return self._error_card(target, error.status, error.data, f"HEY refused the request (HTTP {error.status})")
            except RateLimited as error:
                return self._error_card(target, 429, error.data, "HEY answered with a rate limit (HTTP 429)")
            except (Unavailable, TypeError, ValueError, AttributeError, KeyError):
                return Card(target, "unreachable", {"read_error": "HEY's response did not establish a builder record"})
            if card.kind == "unreachable":
                return card
            with self.lock:
                if self.ttl == 0:
                    return deepcopy(card)
                self.cache[key] = (self.clock(), card)
                self.cache.move_to_end(key)
                while len(self.cache) > 512:
                    self.cache.popitem(last=False)
            return deepcopy(card)

        finally:
            stripe.release()

    def attach_locks(self, card, deadline):
        """Retain only token-lock fields from a resolved project's snapshot."""
        subject = ((card.data.get('payload') or {}).get('subject') or {}).get('project') or {}
        slug = subject.get('slug')
        if card.kind != 'agent' or not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9-]{1,120}', slug):
            return card
        key = ('snapshot-locks', slug)
        with self.lock:
            cached = self.cache.get(key)
        if cached and self.clock() - cached[0] < 60:
            locks = cached[1]
        else:
            try:
                status, snapshot = self._read('/api/projects/' + slug + '/snapshot', deadline)
                if status != 200 or not isinstance(snapshot, dict):
                    return card
                locks = snapshot.get('locks')
                with self.lock:
                    self.cache[key] = (self.clock(), locks)
                    while len(self.cache) > 512:
                        self.cache.popitem(last=False)
            except (Unavailable, ValueError, TypeError):
                return card
        return Card(card.target, card.kind, {**card.data, 'locks': locks})

    def many(self, targets):
        if not targets:
            return []
        with ThreadPoolExecutor(max_workers=3) as pool:
            return list(pool.map(self.lookup, targets))

    def _agent(self, target, deadline, project=None, required=()):
        repo = normalize_repo(target)
        if repo:
            # The public catalogue accepts a repository name; ownership still needs
            # an exact normalized URL match in the registered dossier.
            return self._repo(target, deadline)
        endpoint = "builder_status" if ADDRESS.fullmatch(target) else "research_project"
        if project:
            query = {"project": project}
        elif ADDRESS.fullmatch(target):
            query = {"address": target}
        else:
            query = {"q": target}
        try:
            status, data = self._read("/api/agent/" + endpoint + "?" + urlencode(query), deadline)
        except TemporaryUnavailable:
            return self._address(target, deadline) if ADDRESS.fullmatch(target) else Card(target, "unreachable")
        resolution_note = None
        if project and (status == 404 or data.get("status") == "not_found"):
            # One name lookup can resolve a display name mistaken for a slug.
            try:
                resolved_status, resolved = self._read(
                    "/api/agent/" + endpoint + "?" + urlencode({"q": project}), deadline)
                subject = (resolved.get("subject") or {}).get("project") or {}
                if resolved_status == 200 and resolved.get("status") == "ok" and subject.get("slug"):
                    status, data = resolved_status, resolved
                    resolution_note = f'slug {project} not found; HEY resolved "{project}" to {subject["slug"]}'
            except Unavailable:
                pass
        if 400 <= status < 500 and not isinstance(data.get("answer"), dict):
            raise ResponseAnswer(status, data)
        if not isinstance(data, dict) or not isinstance(data.get("answer"), dict):
            raise Unavailable()
        card = agent_card(target, data)
        if 400 <= status < 500 or data.get("status") == "not_found":
            return card
        if status == 200 and data.get("status") == "ok":
            subject = (data.get("subject") or {}).get("project") or {}
            token = subject.get("token") or {}
            address = target if ADDRESS.fullmatch(target) else token.get("address") if token.get("chainId") == 4663 else None
            needs = any(card.data.get(k) is None for k in required) or card.data.get("verified_builder") is None
            if needs and isinstance(address, str) and ADDRESS.fullmatch(address):
                try:
                    fallback = self._address(address.lower(), deadline)
                    merged = dict(card.data)
                    for key in ("verified_builder", "activity_measured", "meaningful_ships_30d"):
                        if merged.get(key) is None:
                            merged[key] = fallback.data.get(key)
                    ship = dict(merged["latest_meaningful_ship"])
                    for key, value in (fallback.data.get("latest_meaningful_ship") or {}).items():
                        if ship.get(key) is None:
                            ship[key] = value
                    merged["latest_meaningful_ship"] = ship
                    card = Card(target, "agent", merged)
                except Unavailable:
                    pass
            if card.data.get("verified_builder") is None and not address:
                try:
                    slug = subject.get("slug")
                    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", slug):
                        return Card(target, card.kind, {**card.data, "resolution_note": resolution_note})
                    code, record = self._read("/api/projects/" + quote(slug, safe=""), deadline)
                    badge = self._project_badge(record) if code == 200 else None
                    card = Card(target, "agent", {**card.data, "verified_builder": badge})
                except Unavailable:
                    pass
        if resolution_note:
            card = Card(target, card.kind, {**card.data, "resolution_note": resolution_note})
        return card

    @staticmethod
    def _project_badge(record):
        levels = [record.get(k) for k in ("researchLevel", "catalogStatus") if record.get(k)]
        return "VERIFIED_BUILDER" in levels if levels else None

    def _address(self, target, deadline):
        status, data = self._read("/api/v1/builder?" + urlencode({"chain": 4663, "token": target}), deadline)
        if status == 200:
            if data.get("chain_id") != 4663 or data.get("contract_address", "").lower() != target:
                raise Unavailable()
            return token_card(target, data)
        if 400 <= status < 500 and status != 404 and "not a token identity" not in str(data.get("message", "")).lower():
            raise ResponseAnswer(status, data)
        status, data = self._read(f"/api/contracts/4663/{target}", deadline)
        if status == 200:
            if data.get("chainId") != 4663 or data.get("address", "").lower() != target:
                raise Unavailable()
            return contract_card(target, data)
        if status == 404:
            return Card(target, "missing")
        if 400 <= status < 500:
            raise ResponseAnswer(status, data)
        raise Unavailable()

    def _repo(self, target, deadline):
        owner, basename = target.rsplit("/", 2)[-2:]
        preferred = [owner + "-" + basename, owner, basename]
        checked = set()
        total_checked = 0
        last_search = None
        mismatch = None
        truncated = False

        def incomplete(kind="unreachable"):
            query, total, count = last_search
            answer = f'HEY lists {total} projects for "{query}"; none of the first {count} registers this repository'
            if mismatch:
                name, registered = mismatch
                answer += f'; HEY registers {registered} for {name}, not this repository'
            return Card(target, kind, {"read_error": answer + " · " +
                        self.base_url + "/api/projects?" + urlencode({"q": query})})

        for query in dict.fromkeys((owner, basename)):
            offset = 0
            for _ in range(20):
                try:
                    status, page = self._read("/api/projects?" + urlencode({
                        "q": query, "limit": 48, "offset": offset}), deadline)
                    if 400 <= status < 500:
                        raise ResponseAnswer(status, page)
                    if status != 200 or not isinstance(page.get("items"), list):
                        raise Unavailable()
                    total = page.get("total", len(page["items"]))
                    count = last_search[2] if last_search and last_search[0] == query else 0
                    last_search = (query, total, count)
                    candidates = sorted(page["items"], key=lambda item:
                        preferred.index(item.get("slug")) if item.get("slug") in preferred else len(preferred))
                    owner_limit = query == owner and owner != basename and total_checked >= 4
                    if owner_limit:
                        truncated = True
                        break
                    for item in candidates:
                        if query == owner and owner != basename and total_checked >= 4:
                            truncated = True
                            break
                        slug = item.get("slug", "")
                        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", slug):
                            raise Unavailable()
                        if slug in checked:
                            continue
                        if total_checked >= 8:
                            return incomplete()
                        status, detail = self._read("/api/projects/" + quote(slug, safe=""), deadline)
                        if 400 <= status < 500:
                            raise ResponseAnswer(status, detail)
                        if status != 200:
                            raise Unavailable()
                        checked.add(slug)
                        total_checked += 1
                        count += 1
                        last_search = (query, total, count)
                        registered = [detail.get("repo_url")]
                        registered.extend(source.get("url") for source in detail.get("sources", [])
                                          if source.get("sourceType") == "GITHUB_REPO" and source.get("contextOnly") is False)
                        urls = {normalize_repo(url) for url in registered} - {None}
                        if target in urls:
                            return self._registered_repo(target, slug, detail, deadline)
                        if slug in preferred and urls:
                            mismatch = (detail.get("name") or slug, sorted(urls)[0])
                    if truncated and query == owner:
                        break
                    next_offset = page.get("nextOffset")
                    if next_offset is None:
                        break
                    if type(next_offset) is not int or next_offset <= offset:
                        raise Unavailable()
                    offset = next_offset
                except ResponseAnswer:
                    raise
                except Unavailable:
                    if last_search:
                        return incomplete()
                    raise
            else:
                return incomplete()
        if truncated:
            return incomplete()
        if mismatch:
            return incomplete("missing")
        return Card(target, "missing")

    def _registered_repo(self, target, slug, detail, deadline):
        token = detail.get("token") or {}
        address = token.get("contractAddress", "")
        if token.get("chainId") == 4663 and ADDRESS.fullmatch(address):
            try:
                card = self._address(address.lower(), deadline)
                if card.kind not in {"missing", "unreachable"}:
                    return Card(target, card.kind, {**card.data, "repository_match": target})
            except Unavailable:
                pass
        # A project without a token still has a factual dossier. The dossier's
        # short list of ships is not a measured 30-day count.
        try:
            status, snapshot = self._read(f"/api/projects/{slug}/snapshot", deadline)
        except Unavailable:
            status, snapshot = 200, {}
        if 400 <= status < 500:
            raise ResponseAnswer(status, snapshot)
        if status != 200:
            raise Unavailable()
        build = snapshot.get("build") or {}
        ships = detail.get("ships") or []
        # The dossier may include events the status does not count as building.
        # Select only the meaningful-ship date the snapshot itself names.
        last_shipped = build.get("lastShippedAt")
        newest = next((ship for ship in ships if last_shipped
                       and ship.get("publishedAt") == last_shipped), {})
        summary = snapshot.get("summary") or {}
        latest_line = next((line for line in summary.get("lines", [])
                            if line.get("dimension") == "latestChange"), {})
        change = latest_line.get("change") or {}
        timing = change.get("time") or {}
        return Card(target, "repo", {
            "repository_match": target,
            "hey_project_name": detail.get("name"), "project_link": detail.get("url"),
            "hey_status_label": detail.get("activityStatus"),
            "verified_builder": self._project_badge(detail),
            "activity_measured": build.get("activityMeasured"),
            "latest_meaningful_ship": ({"title": newest.get("displayTitle") or newest.get("title"), "timestamp": last_shipped,
                                       "url": newest.get("sourceUrl"), "evidence_id": newest.get("evidenceId"),
                                       "evidence_link": newest.get("url")} if last_shipped else None),
            "latest_change": ({"summary": change.get("what"), "occurred_at": timing.get("occurredAt"),
                               "detected_at": timing.get("detectedAt"), "evidence_id": change.get("evidenceId"),
                               "url": change.get("explainUrl")} if change else None),
            "meaningful_ships_30d": None, "as_of": snapshot.get("asOf"), "repo_url": target, "locks": snapshot.get("locks"),
        })

    def probe(self):
        start = self.clock()
        try:
            status, _ = self._read("/api/projects?limit=1", start + self.timeout)
            reached = status == 200
        except Unavailable:
            reached = False
        return {"reachable": reached, "latency_ms": round((self.clock() - start) * 1000, 2)}
