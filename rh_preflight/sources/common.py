"""Bounded reads, projected source lines and independent caches."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from copy import deepcopy
from urllib.parse import urlsplit

from ..hey import Client, Unavailable, Refused, RateLimited
from ..card import text


@dataclass(frozen=True)
class Reading:
    source: str
    state: str
    line: str
    inactive: bool = False
    agent_ids: tuple = ()
    deployer: str | None = None
    stock_ticker: str | None = None
    token_lock: bool | None = None
    elapsed: float = 0.0
    asked: bool = True


def value(item):
    return field(item)


def boolean(item):
    return "yes" if item is True else "no" if item is False else "not recorded"


def amount(raw, decimals):
    try:
        if raw is None or type(decimals) is not int or not 0 <= decimals <= 36:
            return "not recorded"
        number = Decimal(str(raw)) / (Decimal(10) ** decimals)
        return format(number, "f") if number.is_finite() else "not recorded"
    except (InvalidOperation, ValueError):
        return "not recorded"


class Source(Client):
    allow_list = True
    origin = ""
    label = ""
    page = ""
    probe_path = "/"

    def __init__(self, base_url=None, **kwargs):
        origin = base_url or self.origin
        if base_url and base_url.rstrip("/") != self.origin:
            p = urlsplit(base_url)
            if p.scheme != "http" or p.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("Use the source origin or a loopback server")
        super().__init__(base_url=base_url, **kwargs) if base_url and base_url != self.origin else super().__init__(**kwargs)
        self.base_url = origin.rstrip("/")

    def read(self, path, deadline, ttl=600):
        stripe = self.stripes[hash(path) % len(self.stripes)]
        if not stripe.acquire(timeout=max(0, deadline - self.clock())):
            raise Unavailable()
        try:
            with self.lock:
                cached = self.cache.get(path)
                if cached and self.clock() - cached[0] < ttl:
                    self.hits += 1
                    return deepcopy(cached[1])
                self.misses += 1
            status, data = self._read(path, deadline)
            if status != 200:
                raise Unavailable()
            with self.lock:
                self.cache[path] = (self.clock(), data)
                self.cache.move_to_end(path)
                while len(self.cache) > 512:
                    self.cache.popitem(last=False)
            return data

        finally:
            stripe.release()

    def prefetch(self, deadline=None):
        try:
            self.read(self.probe_path, deadline if deadline is not None else self.clock() + self.timeout, ttl=60)
        except (Unavailable, KeyError, TypeError, ValueError, AttributeError):
            pass

    def target_page(self, target=None, **kwargs):
        return self.page

    def unavailable(self, target=None, **kwargs):
        return Reading(self.label, "unreachable", f"{self.label} could not be reached · {self.target_page(target, **kwargs)}")

    def rate_limited(self, target=None, **kwargs):
        from dataclasses import replace
        return replace(self.unavailable(target, **kwargs), state="rate_limited")

    def lookup(self, target, deadline=None, **kwargs):
        try:
            end = self.clock() + self.timeout
            return self.fetch(target, min(deadline, end) if deadline is not None else end, **kwargs)
        except RateLimited:
            return self.rate_limited(target, **kwargs)
        except Refused as error:
            if error.status == 402:
                return Reading(self.label, "payment_required", "")
            return Reading(self.label, "refused", f"{self.label} refused the request (HTTP {error.status}) · {self.target_page(target, **kwargs)}")
        except (Unavailable, KeyError, TypeError, ValueError, AttributeError):
            return self.unavailable(target, **kwargs)

    def probe(self):
        start = self.clock()
        try:
            self.read(self.probe_path, start + self.timeout, ttl=0)
            reached = True
        except (Unavailable, ValueError, TypeError, Absent):
            reached = False
        return {"reachable": reached, "latency_ms": round((self.clock()-start)*1000, 2)}


class Absent(Exception):
    def __init__(self, data):
        self.data = data


class Keyless(Source):
    """Read-only GET/JSON-RPC, sixty-second cache, no paid follow-ups."""
    def read(self, path, deadline, ttl=60, payload=None):
        import json
        key = (path, json.dumps(payload, sort_keys=True))
        stripe = self.stripes[hash(key) % len(self.stripes)]
        if not stripe.acquire(timeout=max(0, deadline - self.clock())):
            raise Unavailable()
        try:
            with self.lock:
                cached = self.cache.get(key)
                if cached and self.clock() - cached[0] < ttl:
                    self.hits += 1
                    status, data = deepcopy(cached[1])
                    if status == 404:
                        raise Absent(data)
                    return data
                self.misses += 1
            status, data = self._read(path, deadline, payload)
            if status not in (200, 404):
                raise Unavailable()
            with self.lock:
                self.cache[key] = (self.clock(), (status, data))
                self.cache.move_to_end(key)
                while len(self.cache) > 512:
                    self.cache.popitem(last=False)
            if status == 404:
                raise Absent(data)
            return data
        finally:
            stripe.release()

    def lookup(self, target, deadline=None, **kwargs):
        try:
            end = self.clock() + self.timeout
            return self.fetch(target, min(deadline, end) if deadline is not None else end, **kwargs)
        except Absent:
            return Reading(self.label, "unlisted", "")
        except RateLimited:
            return self.rate_limited(target, **kwargs)
        except Refused as error:
            return Reading(self.label, "payment_required", "") if error.status == 402 else self.unavailable(target, **kwargs)
        except (Unavailable, KeyError, TypeError, ValueError, AttributeError, IndexError, OverflowError):
            return self.unavailable(target, **kwargs)


def field(item, fallback="not recorded"):
    import unicodedata
    if item is None or item == "":
        return fallback
    cleaned = "".join(c for c in str(item) if not unicodedata.category(c).startswith("C"))
    cleaned = text(" ".join(cleaned.split()))
    if len(cleaned) <= 80:
        return cleaned
    boundary = cleaned.rfind(" ", 0, 81)
    return cleaned[:boundary if boundary > 0 else 80] + "…"


def plural(count, noun):
    """One shared count inflection for numeric fields, including string counts."""
    return noun if str(count) == "1" else noun + ("es" if noun == "launch" else "s")


def money(item):
    try:
        number = Decimal(str(item))
        if not number.is_finite() or number < 0:
            raise ValueError()
        return format(number, ",.2f")
    except (InvalidOperation, ValueError):
        return "not recorded"
