"""Hookr catalog reads and read-only MCP hook screening and wallet quotes."""
from .common import Source, Reading, value, field, Unavailable
from collections import OrderedDict, deque
from copy import deepcopy
import json
import re
import time
from ..budget import Pool, result as budget_result
from ..card import link
from ..hookr_swap import decode, ZERO, ROUTER, units
from ..extract import ADDRESS
from ..hey import Refused, RateLimited, TemporaryUnavailable

LABEL = "Hookr"


class Client(Source):
    origin = "https://hookr.fun"
    label = LABEL
    page = origin + "/hooks"
    probe_path = "/api/hooks/catalog"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mcp_cache = OrderedDict()
        self.scan_times = deque()
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

    def fetch(self, target, deadline, index_result=None, is_hook=False):
        if is_hook and ADDRESS.fullmatch(target) and int(target, 16):
            return self.screen(target.lower(), deadline)
        if index_result is not None:
            return index_result if isinstance(index_result, Reading) else self.parse(target, *index_result, is_hook=is_hook)
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
        return self.parse(target, *snapshot, is_hook=is_hook) if target else snapshot

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

    def parse(self, target, pools, hooks, is_hook=False):
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
            discovered = any((row.get('hook') or {}).get('address', '').lower() == address
                             for row in pools.values())
            if (pool or discovered or is_hook) and ADDRESS.fullmatch(address) and int(address, 16):
                # Absence from the catalog does not establish a known spender.
                return Reading(LABEL, "unlisted", "Hookr: not in the catalog · scan "
                               + "https://hookr.fun/scan?hook=" + address)
            return Reading(LABEL, "unlisted", "")
        return Reading(LABEL, "present", line + " · " + self.page)

    def mcp(self, name, arguments, deadline, cached=False):
        """Read-only tools/call; no initialization, session state or paid follow-up."""
        key = ('mcp', name, json.dumps(arguments, sort_keys=True))
        stripe = self.stripes[hash(key) % len(self.stripes)]
        if not stripe.acquire(timeout=max(0, deadline - self.clock())):
            raise Unavailable()
        try:
            with self.lock:
                previous = self.mcp_cache.get(key)
                if cached and previous and self.clock() - previous[0] < 600:
                    self.hits += 1
                    return deepcopy(previous[1])
                if name == 'hookr_scan':
                    now = self.clock()
                    while self.scan_times and now - self.scan_times[0] >= 60:
                        self.scan_times.popleft()
                    if len(self.scan_times) >= 6:
                        raise RateLimited()
                    self.scan_times.append(now)
                self.misses += 1
            # A scan can exceed four seconds. Give it the remaining budget,
            # without a retry that would consume a second fresh scan slot.
            status, envelope = self._read_once('/api/mcp', deadline, {
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': name, 'arguments': arguments}})
            result = envelope.get('result') or {}
            if status != 200 or envelope.get('error') or result.get('isError'):
                raise Unavailable()
            data = result.get('structuredContent')
            if not isinstance(data, dict):
                data = next((json.loads(row['text']) for row in result.get('content', [])
                             if row.get('type') == 'text'), None)
            if not isinstance(data, dict) or self.clock() > deadline:
                raise Unavailable()
            if name in {'hookr_hook', 'hookr_scan'} and data.get('address') is not None:
                if str(data['address']).lower() != arguments['address'].lower():
                    raise Unavailable()
            if cached:
                with self.lock:
                    self.mcp_cache[key] = (self.clock(), deepcopy(data))
                    self.mcp_cache.move_to_end(key)
                    while len(self.mcp_cache) > 512:
                        self.mcp_cache.popitem(last=False)
            return data
        finally:
            stripe.release()

    @staticmethod
    def scan_link(address):
        return 'https://hookr.fun/scan?hook=' + address

    @staticmethod
    def scan_line(data, address):
        score_data = data.get('score')
        if score_data is not None and not isinstance(score_data, dict):
            raise ValueError('invalid numeric screen field')
        score = (score_data or {}).get('score')
        rating = f'{score}/100' if type(score) in (int, float) and 0 <= score <= 100 else 'numeric result not read'
        findings = [row for row in data.get('findings', []) if isinstance(row, dict)]
        extra = data.get('moreFindings', 0)
        extra = extra if type(extra) is int and extra >= 0 else 0
        # Stable finding ids project to our fixed facts; upstream titles can
        # contain classifications just as headlines and bands do.
        labels = {'inline-assembly': 'Inline assembly needs manual review',
                  'unchecked': 'Unchecked arithmetic block',
                  'delegatecall-pinned': 'Delegated execution to code fixed at construction',
                  'time-dependence': 'Block time or height influences behavior',
                  'assembly-call-discarded': "Assembly discards a call's success flag"}
        titles = [labels[row['id']] for row in findings[:3] if row.get('id') in labels]
        remaining = len(findings) - len(titles) + extra
        if remaining:
            titles.append('+' + str(remaining) + ' more')
        page = data.get('page')
        if not isinstance(page, str) or not page.startswith('https://hookr.fun/scan?'):
            page = Client.scan_link(address)
        return (f'Hookr scan: {rating}, automated screening · {len(findings) + extra} findings'
                + (': ' + ', '.join(titles) if titles else '') + ' · ' + link(page))

    def screen(self, address, deadline):
        fallback = Reading(LABEL, 'unlisted', 'Hookr scan: ' + self.scan_link(address))
        # Kind and scan are independent. A slow kind read must not eat the scan budget.
        def call(name):
            try:
                return self.mcp(name, {'address': address}, deadline, cached=True)
            except Exception:
                return None
        wait_deadline = time.monotonic() + max(0, deadline - self.clock())
        with Pool(max_workers=2) as pool:
            kind_future = pool.submit(call, 'hookr_hook')
            scan_future = pool.submit(call, 'hookr_scan')
            scan = budget_result(scan_future, wait_deadline, None)
            kind = budget_result(kind_future, wait_deadline, None)
        if scan is None:
            return fallback
        known = {'shared-root': 'shared root', 'own-hook': 'own hook from Hookr factory',
                 'own': 'own hook from Hookr factory', 'factory': 'own hook from Hookr factory'}
        try:
            label = known.get((kind or {}).get('kind'), 'unknown')
            return Reading(LABEL, 'present' if label != 'unknown' else 'unlisted',
                           'Hookr: hook kind ' + label + '\n' + self.scan_line(scan, address))
        except (TypeError, ValueError, AttributeError):
            return fallback

    def check_trade(self, intent, deadline, response=None):
        """Quote locally decoded input alongside Toll; fallback uses its bought token."""
        try:
            spec = decode(intent)
            tx = intent.get('payload') or {}
            if spec and spec['raw'] is None:
                return None
            if str(tx.get('to', '')).lower() != ROUTER or not intent.get('sender'):
                return None
            if spec and spec['raw'] is not None and spec['sold'] == ZERO:
                arguments = {'poolId': spec['poolId'], 'side': 'buy',
                             'amount': units(spec['raw'], 18), 'wallet': intent['sender']}
            else:
                swaps = ((response or {}).get('x-toll') or {}).get('decoded') or []
                swap = next((row for row in swaps if row.get('kind') == 'swap'), {})
                bought = spec['bought'] if spec else next(iter(swap.get('bought') or []), None)
                bought = bought.get('address') if isinstance(bought, dict) else bought
                if not isinstance(bought, str) or not ADDRESS.fullmatch(bought):
                    return None
                pools = self.mcp('hookr_pools', {'token': bought}, deadline).get('pools') or []
                pool = next((p for p in pools if spec and p.get('poolId', '').lower() == spec['poolId']), None) if spec else next(iter(pools), None)
                if not pool:
                    return None
                if spec:
                    paying = next((pool.get(k) or {} for k in ('token', 'quote')
                                   if str((pool.get(k) or {}).get('address', '')).lower() == spec['sold']), {})
                    raw, decimals = spec['raw'], paying.get('decimals')
                    side = 'sell' if paying == pool.get('token') else 'buy'
                else:
                    # Without a known ABI only a native-funded buy has a known
                    # paying amount. Never guess token decimals or exact output.
                    from ..transactions import integer
                    raw, decimals, side = integer(tx.get('value')), 18, 'buy'
                    sold = next(iter(swap.get('sold') or []), None)
                    sold = sold.get('address') if isinstance(sold, dict) else sold
                    if (sold != ZERO or (pool.get('quote') or {}).get('address', '').lower() != ZERO
                            or str((pool.get('token') or {}).get('address', '')).lower() != bought.lower()):
                        return None
                if raw is None or raw <= 0 or type(decimals) is not int or not 0 <= decimals <= 36:
                    return None
                pool_id = pool.get('poolId', '')
                if not re.fullmatch(r'0x[0-9a-fA-F]{64}', pool_id):
                    return None
                arguments = {'poolId': pool_id, 'side': side, 'amount': units(raw, decimals),
                             'wallet': intent['sender']}
            return self.mcp('hookr_check_trade', arguments, deadline)
        except Exception:
            # Hookr cannot suppress a Toll card, even for a malformed MCP reply.
            return None
