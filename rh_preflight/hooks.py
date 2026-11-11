"""Hermes hook handlers and their local service state."""

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .extract import extract
from .hey import Client
from .ledger import Ledger
from .policy import evaluate
from .explanations import target_rules
from .presentation import present
from .runtime import unattended
from .settings import Settings
from .network import Network, identifiers, POOL, MODULES
from .extract import chain_context, addresses
from .card import Card, text, plain
from .transactions import tx_intents, local_card, project
from .budget import Pool, result as budget_result


# A bounded dispatcher also contains a custom source that ignores its deadline.
CHECKS = ThreadPoolExecutor(max_workers=8, thread_name_prefix="rh-preflight")


class Service:
    def __init__(self, settings=None, client=None, ledger=None, context=None, presence=None, network=None,
                 clock=None, executor=None, wait=None):
        self.settings = settings or Settings()
        self.client = client or Client(timeout=self.settings.timeout)
        origin = getattr(self.client, 'base_url', '')
        origins = {name: origin for name in MODULES} if origin.startswith('http://') else None
        self.network = network or Network(timeout=self.settings.timeout, origins=origins)
        self.ledger = ledger or Ledger()
        self.context = context
        self.presence = presence if presence is not None else lambda: unattended(context)
        self.pending = {}
        self.lock = threading.Lock()
        self.clock = clock or time.monotonic
        self.executor = executor or CHECKS
        self.wait = wait or budget_result

    def _settings(self):
        return Settings.from_context(self.context) if self.context is not None else self.settings

    @staticmethod
    def _key(tool_name, args, kw):
        # The fallback is ephemeral and never enters the ledger.
        call_id = kw.get("tool_call_id") or hashlib.sha256(
            json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()
        return (kw.get("session_id", ""), kw.get("task_id", ""), call_id, tool_name)

    def pre_tool_call(self, tool_name, args, **kwargs):
        try:
            settings = self._settings()
            # Leave a margin below Hermes's fail-closed 30-second hook limit.
            started = self.clock()
            deadline = started + min(settings.timeout, 25.0)
            intents = tx_intents(args, settings.assume_chain)
            local = [local_card(intent) for intent in intents]
            targets = extract(tool_name, args, settings)
            wanted = list(targets.addresses + targets.repos)
            payees, agents, pools = identifiers(args)
            if chain_context(args, settings.assume_chain):
                wanted = list(dict.fromkeys(wanted + payees))[:5]
                if not wanted and (agents or pools):
                    wanted = ["agent #" + agents[0] if agents else pools[0]]
            if not wanted and not intents and chain_context(args, settings.assume_chain) and settings.sources.get("sherwood", "on") == "on":
                # The registry is a public list. Only a known vault causes a bundle or core lookup.
                candidates = addresses(args)
                def vaults():
                    return [target for target in candidates
                            if self.network.clients["sherwood"].lookup(target, deadline=deadline).state == "present"]
                discovery = self.executor.submit(vaults)
                wanted = self.wait(discovery, deadline, candidates)
            if not wanted and not intents:
                return None
            required = []
            if settings.ask_unmeasured or settings.ask_quiet:
                required.append("activity_measured")
            if settings.ask_quiet:
                required.append("meaningful_ships_30d")
            if settings.ask_not_verified:
                required.append("verified_builder")
            # Local signature decoding above always precedes any Toll request.
            margin = min(.02, settings.timeout * .1, settings.tx_timeout * .1)
            read_deadline = deadline - margin
            tx_enabled = (settings.tx_check == 'on' and settings.sources.get('toll', 'on') == 'on'
                          and any(intent['payload'] is not None for intent in intents))
            tx_deadline = started + settings.tx_timeout - margin if tx_enabled else read_deadline
            hook_deadline = max(deadline, tx_deadline + margin)
            reads = self.executor.submit(self._read_cards, wanted, tool_name, args, settings, required=required, deadline=read_deadline) if wanted else None
            txs = self.executor.submit(self._tx_cards, intents, local, settings, tx_deadline, read_deadline) if intents else None
            # Both start above; collecting builder reads first preserves their
            # shorter budget even when a custom adapter ignores its deadline.
            cards = self.wait(reads, deadline, [Card(target, 'unreachable') for target in wanted]) if reads else []
            tx_cards = self.wait(txs, hook_deadline,
                [project(card, intent) if settings.tx_check == 'on' else card
                 for card, intent in zip(local, intents)]) if txs else []
            # Only a positive per-address registry answer establishes a known
            # spender. HEY builder presence alone does not establish that role.
            known = {card.target.lower() for card in cards if card.kind == 'network'
                     and any(r.source in {'Priors', 'Hookr'} and r.state == 'present'
                             for r in card.data.get('sources', []))}
            for tx_card in tx_cards:
                for approval in tx_card.data['approvals']:
                    approval['known'] = approval.get('known', False) or approval['spender'] in known
            cards = tx_cards + cards
            checked_at = datetime.now(timezone.utc)
            decision = evaluate(cards, settings, unattended=self.presence(), now=checked_at)
            associations = target_rules(cards, settings, checked_at) if len(cards) > 1 else [None]
            cards = [present(card, decision, rule_targets=rules)
                     for card, rules in zip(cards, associations)]
            message = "\n\n".join(c.render() for c in cards)
            check_id = self.ledger.add(kwargs.get("session_id") or kwargs.get("task_id", ""),
                                       tool_name, cards, decision.rules, settings.mode, decision.action)
            with self.lock:
                self.pending[self._key(tool_name, args, kwargs)] = message
                while len(self.pending) > 512:
                    self.pending.pop(next(iter(self.pending)))
            if decision.action == "ask":
                # Hermes calls this directive 'approve'; it REQUESTS a human decision.
                # A unique key prevents a previous session/always answer bypassing a new check.
                return {"action": "approve", "message": message, "rule_key": f"rh-preflight:{check_id}"}
            if decision.action == "block":
                return {"action": "block", "message": message}
            return None
        except Exception:
            # Hermes skips hooks that raise. Keep failures explicit at our boundary.
            message = ("stop · rh-preflight could not complete this check\n"
                       "coverage unavailable for this check\n"
                       "rh-preflight could not complete this check")
            return {"action": "block", "message": message}

    def _tx_cards(self, intents, local, settings, deadline, builder_deadline=None):
        cards = []
        builder_deadline = builder_deadline if builder_deadline is not None else deadline
        with Pool(max_workers=16) as pool:
            # Simulation and code reads start together under separate budgets.
            def simulate(intent):
                response = self.network.clients['toll'].check_tx(intent['payload'], deadline, settings.toll_key_header)
                return response if time.monotonic() <= deadline else None
            simulations = [pool.submit(simulate, intent)
                           if settings.tx_check == 'on' and settings.sources.get('toll', 'on') == 'on'
                           and intent['payload'] is not None else None for intent in intents]
            codes = {approval['spender']: pool.submit(self.network.clients['chain'].lookup,
                     approval['spender'], deadline=builder_deadline)
                     for card in local for approval in card.data['approvals']
                     if settings.sources.get('chain', 'on') == 'on'}
            for intent, card, simulation in zip(intents, local, simulations):
                kinds = {}
                for approval in card.data['approvals']:
                    spender = approval['spender']
                    reading = budget_result(codes[spender], builder_deadline - .005, None) if spender in codes else None
                    kinds[spender] = {'code': 'contract with code', 'no_code': 'no code'}.get(reading.state if reading else None, 'not read')
                response = budget_result(simulation, deadline - .005, None) if simulation else None
                composed = project(card, intent, response, kinds)
                # Simulation can reveal approvals in calldata that local selector
                # decoding did not cover. Establish those spenders' code too.
                extra_codes = {approval['spender']: pool.submit(self.network.clients['chain'].lookup,
                               approval['spender'], deadline=builder_deadline)
                               for approval in composed.data['approvals']
                               if approval['spender'] not in kinds and settings.sources.get('chain', 'on') == 'on'}
                for spender, future in extra_codes.items():
                    reading = budget_result(future, builder_deadline - .005, None)
                    kinds[spender] = {'code': 'contract with code', 'no_code': 'no code'}.get(reading.state if reading else None, 'not read')
                if extra_codes:
                    composed = project(card, intent, response, kinds)
                for approval in composed.data['approvals']:
                    if approval['unlimited'] and approval['kind'] == 'contract with code':
                        registries = [pool.submit(self.network.clients[name].lookup, approval['spender'], deadline=builder_deadline)
                                      for name in ('priors', 'hookr') if settings.sources.get(name, 'on') == 'on']
                        readings = [budget_result(future, builder_deadline - .005, None) for future in registries]
                        approval['known'] = any(reading is not None and reading.state == 'present' for reading in readings)
                if settings.tx_check == 'off':
                    composed = Card(composed.target, composed.kind, {**composed.data,
                        'unchecked': False, 'lines': [line for line in composed.data['lines'] if not line.startswith('Toll')] + ['transaction simulation switched off']})
                cards.append(composed)
        return cards

    def transform_tool_result(self, tool_name, args, result, **kwargs):
        try:
            with self.lock:
                message = self.pending.pop(self._key(tool_name, args, kwargs), None)
        except Exception:
            return None
        if message is None:
            return None
        labelled = "rh-preflight public network data (external source text):\n" + message
        try:
            if isinstance(result, str):
                payload = json.loads(result, parse_int=str, parse_float=str)
                if isinstance(payload, dict):
                    key, suffix = "rh_preflight", 2
                    while key in payload:
                        key = f"rh_preflight_{suffix}"
                        suffix += 1
                    # Serialize only the new field. Keep numeric precision,
                    # duplicate keys and escaped Unicode in the tool's text.
                    field = json.dumps({key: labelled}, ensure_ascii=False)[1:-1]
                    closing = result.rindex("}")
                    separator = ", " if payload else ""
                    return result[:closing] + separator + field + result[closing:]
        except Exception:
            # Keep the card even if parsing or serialization fails.
            pass
        try:
            return str(result) + "\n\n" + labelled
        except Exception:
            return labelled

    def post_approval_response(self, pattern_key="", pattern_keys=None, choice="", **kwargs):
        for key in set([pattern_key] + list(pattern_keys or [])):
            prefix = "plugin_rule:rh-preflight:"
            if isinstance(key, str) and key.startswith(prefix):
                check_id = key[len(prefix):]
                if len(check_id) == 32 and all(c in "0123456789abcdef" for c in check_id):
                    self.ledger.decide(check_id, choice)

    def _read_cards(self, targets, tool_name, args, settings, project=None, required=None, deadline=None, all_sources=False):
        if not targets:
            return []
        deadline = deadline if deadline is not None else time.monotonic() + settings.timeout
        placeholders = [Card(target, "identifier") for target in targets]
        def core(target):
            start = time.monotonic()
            if target.startswith("agent #") or POOL.fullmatch(target):
                return Card(target, "identifier"), 0.0, False
            options = {"project": project} if project else {}
            if required is not None:
                options["required"] = required
            if isinstance(self.client, Client):
                options["deadline"] = deadline
            card = self.client.lookup(target, **options)
            if isinstance(self.client, Client) and settings.sources.get("hoodlock", "on") == "on":
                card = self.client.attach_locks(card, deadline)
            return card, max(0.0, time.monotonic() - start), True
        with Pool(max_workers=len(targets) + 1) as pool:
            cores = [pool.submit(core, target) for target in targets]
            neighbours = pool.submit(self.network.enrich, placeholders, tool_name, args, settings,
                                     all_sources=all_sources, deadline=deadline)
            cards = [budget_result(future, deadline, (Card(target, 'unreachable'), 0.0, True))
                     for target, future in zip(targets, cores)]
            early = budget_result(neighbours, deadline, [Card(target, 'network', {'base': Card(target, 'unreachable'), 'sources': []}) for target in targets])
        result = []
        for (base, elapsed, asked), prepared in zip(cards, early):
            if not addresses(base.target) and base.kind == "agent":
                enriched = self.network.enrich([base], tool_name, args, settings,
                                               all_sources=all_sources, deadline=deadline)[0]
                result.append(Card(base.target, "network", {**enriched.data,
                    "hey_elapsed": elapsed, "hey_asked": asked}))
            else:
                if self.network.is_offrouter(base) and settings.sources.get('offrouter', 'on') == 'on':
                    from dataclasses import replace
                    start = time.monotonic()
                    own = self.network.clients['offrouter'].lookup(base.target, deadline=deadline)
                    own = replace(own, elapsed=max(0.0, time.monotonic() - start))
                    prepared = Card(base.target, 'network', {**prepared.data, 'sources': prepared.data['sources'] + [own]})
                citation = text(plain(base.data['payload'].get('citation'))) if base.kind == 'agent' and base.data['payload'].get('citation') else None
                result.append(Card(base.target, "network", {**prepared.data, "base": base, "sources": self.network.annotate(base, prepared.data['sources']),
                    "citation": citation, "hey_elapsed": elapsed, "hey_asked": asked,
                    "offrouter": self.network.is_offrouter(base)}))
        return result

    def card(self, target, project=None):
        settings = self._settings()
        card = self._read_cards([project or target], "read", {}, settings, project=project, all_sources=True)[0]
        return present(card, evaluate([card], settings, unattended=False), read_only=True)

    def doctor(self):
        core = self.client.probe()
        return {**core, "sources": {"hey": {**core, "cache": self.client.stats()},
                                    **self.network.doctor(self._settings())},
                "cache": self.client.stats(), "mode": self._settings().mode,
                "last_checks": self.ledger.rows(5)}
