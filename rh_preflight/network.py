"""Compose source facts in a fixed order with independent source deadlines."""
from concurrent.futures import ThreadPoolExecutor
import re
import time
from dataclasses import replace
from .card import Card, plain, text, link
from .extract import ADDRESS, walk
from .sources import priors, sherwood, hookr, chain, wormhole, toll, dexscreener, geckoterminal, offrouter, sentinelhood, trnch, borro, hoodlock
from .sources.common import field, Reading

MODULES = {"priors": priors, "sherwood": sherwood, "hookr": hookr, "chain": chain, "wormhole": wormhole,
           "toll": toll, "dexscreener": dexscreener, "geckoterminal": geckoterminal, "offrouter": offrouter, "sentinelhood": sentinelhood, "hoodlock": hoodlock,
           "trnch": trnch, "borro": borro}
ORDER = ['Priors', 'Sentinelhood', 'HoodLock', 'Trnch', 'Agent Wormhole', 'Hookr', 'Toll', 'Borro', 'Sherwood', 'OffRouter', 'Robinhood Chain', 'DexScreener', 'GeckoTerminal']
POOL = re.compile(r"(?<![\w])0x[0-9a-fA-F]{64}(?![\w])")


def identifiers(args):
    payees, agents = [], []
    def visit(item):
        if isinstance(item, dict):
            for key, val in item.items():
                normalized = re.sub('[^a-z0-9]', '', str(key).lower())
                if normalized in {"payto", "paytoaddress", "payee", "recipient"}:
                    payees.extend(m.group().lower() for s in walk(val) for m in ADDRESS.finditer(s))
                if normalized in {"agentid", "erc8004agentid", "erc8004agent"} and str(val).isdigit():
                    agents.append(str(val))
                visit(val)
        elif isinstance(item, list):
            for val in item:
                visit(val)
    visit(args)
    pools = [m.group().lower() for s in walk(args) for m in POOL.finditer(s)]
    return list(dict.fromkeys(payees))[:5], list(dict.fromkeys(agents))[:5], list(dict.fromkeys(pools))[:5]


class Network:
    def __init__(self, timeout=4, origins=None):
        self.timeout = timeout
        self.clients = {name: module.Client(base_url=(origins or {}).get(name), timeout=timeout)
                        for name, module in MODULES.items()}

    def enrich(self, cards, tool_name, args, settings, all_sources=False, deadline=None):
        deadline = deadline if deadline is not None else time.monotonic() + self.timeout
        payees, agents, pools = identifiers(args)
        pay = bool(re.search('pay|x402', tool_name, re.I))
        swap = bool(re.search('swap|buy|sell|trade|liquidity|lp', tool_name, re.I))
        def lookup(name, target, **kwargs):
            start = time.monotonic()
            reading = self.clients[name].lookup(target, deadline=deadline, **kwargs)
            return replace(reading, elapsed=max(0.0, time.monotonic() - start))

        reads = []
        target_map = {}
        for card in cards:
            address = card.target if ADDRESS.fullmatch(card.target) else card.data.get("contract_address")
            if card.kind == "agent":
                token = (((card.data.get("payload") or {}).get("subject") or {}).get("project") or {}).get("token") or {}
                if not address and token.get("chainId") == 4663:
                    address = token.get("address")
            if isinstance(address, str) and ADDRESS.fullmatch(address):
                target_map[card.target] = address.lower()
        targets = list(dict.fromkeys(target_map.values()))
        enabled = lambda name: settings.sources.get(name, "on") == "on"
        if enabled('priors') and (pay or payees or agents or all_sources):
            reads.extend(('priors', target, {}) for target in (payees or (targets if pay or all_sources else [])))
            reads.extend(('priors', agent, {'agent': True}) for agent in agents)
        if enabled('sherwood'):
            reads.extend(('sherwood', target, {}) for target in targets)
        if enabled('hookr') and (swap or all_sources):
            reads.extend(('hookr', target, {}) for target in dict.fromkeys(pools + targets))
        with ThreadPoolExecutor(max_workers=max(8, len(reads) + len(targets) * 9 + 4)) as pool:
            # RPC starts first. Independent registries run while RPC establishes
            # contract presence; only its dependent reads await that result.
            chains = {target: pool.submit(lookup, 'chain', target)
                      for target in targets if enabled('chain')}
            primes = {name: pool.submit(self.clients[name].prefetch, deadline)
                      for name in ('hookr', 'sherwood') if enabled(name)}
            def source_read(name, target, kw):
                start = time.monotonic()
                if name == 'hookr':
                    kw = {**kw, 'index_result': primes['hookr'].result()}
                reading = lookup(name, target, **kw)
                return replace(reading, elapsed=max(0.0, time.monotonic() - start))

            futures = [(name, target, pool.submit(source_read, name, target, kw))
                       for name, target, kw in reads]

            def address_read(name, target):
                known = chains[target].result().state if target in chains else None
                options = {'has_code': True if known == 'code' else False if known == 'no_code' else None}
                return lookup(name, target, **options)

            for target in targets:
                prerequisites = {}
                for name in ('sentinelhood', 'toll', 'hoodlock', 'trnch'):
                    if enabled(name):
                        future = pool.submit(lookup, name, target)
                        prerequisites[name] = future
                        futures.append((name, target, future))
                if enabled('borro'):
                    def stock_read(target, membership):
                        ticker = membership.result().stock_ticker if membership else None
                        if ticker:
                            return lookup('borro', target, stock_ticker=ticker)
                        return Reading('Borro', 'unlisted', '', asked=False)
                    futures.append(('borro', target, pool.submit(stock_read, target, prerequisites.get('toll'))))
                for name in ('wormhole', 'dexscreener', 'geckoterminal'):
                    if enabled(name):
                        future = pool.submit(address_read, name, target) if name != 'geckoterminal' else pool.submit(lookup, name, target)
                        futures.append((name, target, future))
            for base in cards:
                if enabled('offrouter') and self.is_offrouter(base):
                    futures.append(('offrouter', base.target, pool.submit(lookup, 'offrouter', base.target)))
            # Schedule each dependent read now, waiting only for its own
            # prerequisite. An unrelated timeout must not consume Toll's time.
            extra = []
            if enabled('toll'):
                def agent_read(target, prior_future=None, agent=None):
                    ids = prior_future.result().agent_ids if prior_future else (agent,)
                    return [('toll', target, lookup('toll', ident, kind='agent'))
                            for ident in ids]

                for name, target, future in futures:
                    if name == 'priors':
                        extra.append(pool.submit(agent_read, target, future))
                for agent in agents:
                    extra.append(pool.submit(agent_read, agent, agent=agent))

                if enabled('hookr'):
                    def hook_read(target):
                        snapshot = primes['hookr'].result()
                        if isinstance(snapshot, Reading):
                            return []
                        hooks = self.clients['hookr']
                        pools_index, hooks_index = snapshot if snapshot is not None else (hooks.pool_index, hooks.hook_index)
                        row = pools_index.get(target)
                        hook = (row.get('hook') or {}).get('address') if row else target
                        if hook and (row or hook.lower() in hooks_index or any(
                                (r.get('hook') or {}).get('address', '').lower() == hook.lower() for r in pools_index.values())):
                            return [('toll', target, lookup('toll', hook, kind='hook'))]
                        return []
                    extra.extend(pool.submit(hook_read, target) for target in dict.fromkeys(pools + targets))
            results = [('chain', target, future.result()) for target, future in chains.items()]
            results += [(name, target, future.result()) for name, target, future in futures]
            for future in primes.values():
                future.result()
            for future in extra:
                results.extend(future.result())
        if not cards:
            return []
        composed = []
        for index, base in enumerate(cards):
            relevant = [r for name, target, r in results if target == target_map.get(base.target) or target == base.target or index == 0 and target not in targets]
            relevant = list(dict.fromkeys(relevant))
            by_source = {}
            for reading in relevant:
                previous = by_source.get(reading.source)
                if previous is None or (bool(reading.line), reading.state == 'present') > (bool(previous.line), previous.state == 'present'):
                    by_source[reading.source] = reading
            relevant = list(by_source.values())
            citation = text(plain(base.data['payload'].get('citation'))) if base.kind == 'agent' and base.data['payload'].get('citation') else None
            composed.append(Card(base.target, 'network', {'base': base, 'sources': self.annotate(base, relevant), 'citation': citation,
                'source_settings': settings.sources, 'offrouter': self.is_offrouter(base),
                'is_payment': pay or bool(payees)}))
        return composed

    @staticmethod
    def is_offrouter(base):
        from urllib.parse import urlsplit
        if base.kind == 'agent':
            project = ((base.data.get('payload') or {}).get('subject') or {}).get('project') or {}
            return project.get('slug') == 'offrouter'
        url = base.data.get('project_link') or base.data.get('hey_project_url') or ''
        parsed = urlsplit(url)
        return parsed.hostname == 'heyresearch.xyz' and parsed.path == '/project/offrouter'

    @staticmethod
    def annotate(base, readings):
        from dataclasses import replace
        from .card import plain
        name = base.data.get('hey_project_name')
        if base.kind == 'agent':
            name = plain(((((base.data.get('payload') or {}).get('subject') or {}).get('project') or {}).get('name')))
        sentinel_ok = any(r.source == 'Sentinelhood' and r.state == 'present' for r in readings)
        readings = [r for r in readings if not (sentinel_ok and r.source == 'Robinhood Chain')]
        readings = sorted(readings, key=lambda r: ORDER.index(r.source) if r.source in ORDER else len(ORDER))
        out = []
        for reading in readings:
            if name and reading.source == 'Robinhood Chain' and reading.state == 'code':
                onchain = reading.line.split(', name ', 1)[1].split(', symbol ', 1)[0]
                if field(name) != onchain:
                    reading = replace(reading, line=reading.line.replace(', read at block ', f', HEY project name {field(name)}, read at block '))
            out.append(reading)
        locks = base.data.get('locks') or (base.data.get('payload') or {}).get('locks') or ((base.data.get('payload') or {}).get('data') or {}).get('locks')
        if locks and isinstance(locks, dict):
            lock = locks.get('tokenLock') or {}
            if not isinstance(lock, dict):
                return out
            own = next((r for r in readings if r.source == 'HoodLock' and r.state == 'present'), None)
            pct = lock.get('supplyPct')
            if own and type(pct) in (int, float):
                prefix = f'HoodLock: {pct:.2f}%'
                until = str(lock.get('nextUnlockAt') or lock.get('until') or '')[:10]
                same = own.line.startswith(prefix) and (not until or until in own.line)
                same = same or pct == 0 and own.token_lock is False
                if not same:
                    page = (base.data.get('payload') or {}).get('subject', {}).get('project', {}).get('url') or base.data.get('project_link') or 'https://heyresearch.xyz'
                    out.insert(0, Reading('HEY locks', 'present', f'HEY snapshot: locks.tokenLock supplyPct {field(pct)}, until {field(lock.get("until"))}, scope {field(lock.get("pairLockScope"))} · {link(page)}'))
        return out

    def doctor(self, settings):
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {name: pool.submit(client.probe) for name, client in self.clients.items()
                       if settings.sources.get(name, 'on') == 'on'}
            return {name: {"enabled": True, **futures[name].result(), "cache": client.stats()}
                    if name in futures else {"enabled": False, "reachable": None, "latency_ms": None,
                                             "cache": client.stats()}
                    for name, client in self.clients.items()}
