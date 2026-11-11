"""Hookr catalog descriptions and discovered pools, indexed per list window."""
from .common import Source, Reading, value, Unavailable
from ..hey import Refused, RateLimited, TemporaryUnavailable

LABEL = "Hookr"


class Client(Source):
    origin = "https://hookr.fun"
    label = LABEL
    page = origin + "/hooks"
    probe_path = "/api/hooks/catalog"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.index_stamp = None
        self.pool_index = {}
        self.hook_index = {}
        self.index_lock = __import__('threading').Lock()

    def _decode(self, raw):
        # The bounded transport can also read the public HTML page. Its Next.js
        # content is presentation, not the deployed-hook/pool registry schema.
        if raw.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            return {"html_page": True}
        return super()._decode(raw)

    def prefetch(self, deadline=None):
        # One immutable index outcome for all targets in a card. Failures must
        # travel with the snapshot rather than become an empty/stale index.
        return self.lookup('', deadline=deadline)

    def load_index(self, deadline):
        if not self.index_lock.acquire(timeout=max(0, deadline - self.clock())):
            raise Unavailable()
        try:
            catalog = self.read(self.probe_path, deadline, ttl=60)
            if not isinstance(catalog.get("root"), dict):
                raise Unavailable()
            integrations = self.read("/api/hooks/integrations", deadline, ttl=60)
            pools = self.read("/api/discover/pools", deadline, ttl=60)
            if not isinstance(integrations.get("integrations"), list) or not isinstance(pools.get("pools"), list):
                raise Unavailable()
            # Publish rows and their cache stamp together, while the complete
            # load is serialized. No competing load can stamp older rows newer.
            stamp = tuple(self.cache[path][0] for path in (self.probe_path, "/api/hooks/integrations", "/api/discover/pools"))
            if stamp != self.index_stamp:
                self.pool_index, self.hook_index = self.index(catalog, integrations, pools)
                self.index_stamp = stamp
            return self.pool_index, self.hook_index
        finally:
            self.index_lock.release()

    def fetch(self, target, deadline, index_result=None):
        if index_result is not None:
            return index_result if isinstance(index_result, Reading) else self.parse(target, *index_result)
        try:
            snapshot = self.load_index(deadline)
        except (Refused, RateLimited, TemporaryUnavailable):
            raise
        except (Unavailable, KeyError, TypeError, ValueError, AttributeError):
            # A reachable website without registry data is a source answer.
            # A failed HTTP read still keeps the transport/refusal wording.
            page = self.read("/hooks", deadline, ttl=60)
            if page.get("html_page"):
                return Reading(LABEL, "unreadable",
                               "Hookr's page no longer lists hooks in a readable form · " + self.page)
            raise
        return self.parse(target, *snapshot) if target else snapshot

    @staticmethod
    def index(catalog, integrations, pools):
        hooks = {}
        root = catalog.get("root") or {}
        if root.get("address"):
            hooks[root['address'].lower()] = (root.get("name"), None, True)
        for item in integrations.get("integrations", []):
            for deployment in item.get("deployments", []):
                if deployment.get("chainId") == 4663 and deployment.get("address"):
                    hooks[deployment['address'].lower()] = (item.get("name"), item.get("summary"))
        return {p['poolId'].lower(): p for p in pools['pools'] if p.get("chainId") == 4663}, hooks

    def parse(self, target, pools, hooks):
        pool = pools.get(target.lower())
        address = (pool.get("hook") or {}).get("address", "").lower() if pool else target.lower()
        description = hooks.get(address)
        if description:
            name, summary = description[:2]
            prefix = f"pool {target[:12]} uses hook" if pool else "hook"
            if len(description) == 3 and description[2]:
                line = (f"{LABEL}: this is the Hookr shared root hook, listed in Hookr's hook catalog"
                        if not pool else f"{LABEL}: {prefix} Hookr shared root, listed in Hookr's hook catalog")
            else:
                line = f"{LABEL}: {prefix} {value(name)} listed in Hookr's integrations ({value(summary)})"
        else:
            return Reading(LABEL, "unlisted", "")
        return Reading(LABEL, "present", line + " · " + self.page)
