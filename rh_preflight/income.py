"""Offline Hookr income attribution from a documented, caller-supplied sidecar.

Hookr income rows do not identify an economic payer. Only reconciled payer paths
may add attribution. This module performs no reads, trace discovery or signing.
See docs/HOOKR_INCOME.md for the input contract and its evidence limitations.
"""
import argparse
from collections import Counter, defaultdict
import json
from fractions import Fraction
import re
import sys

SCHEMA = 'rh-preflight/hookr-income/v1'
MAX_INCOME_LINES = 5000  # Bounded 30-day records, including merged daily pages.
EXTERNAL_RULE = ('external payer (approximate): no direct transfer from the creator, deployer, door or token wallets '
                 'to this payer in 7 days, all of their outgoing transfers read')
PAYMENT_REASON = 'exact payment provenance unavailable'


class PublicParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'invalid command arguments\n')
from .payer_attribution import (ADDRESS, HASH, CREATOR, LOOP, EXTERNAL, UNKNOWN,
                                address, number, identities, relationship, swap_eoa, project_origins, HUB_RULES, KNOWN_HUBS)


def line_key(row):
    if not isinstance(row, dict) or row.get('source') not in ('door', 'direct-transfer', 'bonus'):
        return None
    token = address(row.get('token'))
    currency = address(row.get('currency'), zero=True)
    tx = row.get('txHash')
    block = number(row.get('blockNumber'), maximum=2 ** 64 - 1)
    stamp = row.get('time')
    amount = number(row.get('amount'))
    if (not token or not currency or not isinstance(tx, str) or not HASH.fullmatch(tx)
            or not block or type(stamp) is not int or stamp < 0 or not amount):
        return None
    return (row['source'], token, tx.lower(), block, stamp, currency, amount)


def context(token, data):
    """No valid token stream and dated income lines means no result at all."""
    if not isinstance(data, dict) or data.get('schema') != SCHEMA:
        return None
    identity, income = data.get('identity'), data.get('income')
    if not isinstance(identity, dict) or not isinstance(income, dict):
        return None
    token = address(token)
    subject, window = income.get('subject'), income.get('window')
    doors = identity.get('doors')
    if (not token or address(identity.get('token')) != token
            or type(identity.get('chainId')) is not int or identity['chainId'] != 4663
            or not address(identity.get('tokenizer'))
            or identity.get('generation') not in ('2026-10-29', 'pre-2026-10-29')
            or not isinstance(doors, list) or not doors or len(doors) > 200
            or any(not address(door) for door in doors)
            or not isinstance(subject, dict) or subject.get('kind') != 'token'
            or address(subject.get('token')) != token
            or income.get('status') not in ('ready', 'partial')
            or not isinstance(window, dict)
            or not isinstance(income.get('lines'), list)
            or not income['lines'] and not data.get('funding')
            or len(income['lines']) > MAX_INCOME_LINES):
        return None
    block = number(window.get('throughBlock'), maximum=2 ** 64 - 1)
    if not block or number(identity.get('observedBlock')) != block:
        return None
    if (type(window.get('days')) is not int or not 1 <= window['days'] <= 30
            or any(type(window.get(k)) is not int or window[k] < 0
                   for k in ('from', 'through', 'recordStarts'))
            or window['from'] > window['through']
            or window['from'] < window['recordStarts']
            or window['through'] - window['from'] > window['days'] * 86400
            or type(window.get('cut')) is not bool):
        return None
    creators = identities(identity)
    relatives = identity.get('relatedWallets')
    relations_read = (isinstance(relatives, list) and len(relatives) <= 200
                      and all(address(a) for a in relatives))
    related = creators | {token} | {address(a) for a in doors}
    complete = (identity.get('relationshipsComplete') is True and identity.get('hubRules') == HUB_RULES and relations_read
                and all(address(identity.get(k)) for k in ('creator', 'deployer')))
    return token, identity, income, window, creators, related, complete


def approximate_trace(allocation, key, identity, creators, related):
    """Accept only the explicit model contract; never upgrade it to exact."""
    amount, weight, total = (number(allocation.get(k)) for k in ('amount', 'weight', 'totalWeight'))
    available = number(allocation.get('attributableAmount'))
    payer = address(allocation.get('sender'))
    swaps, evidence = allocation.get('swaps'), allocation.get('senderEvidence')
    if isinstance(evidence, dict) and not isinstance(evidence.get('fundingLeads', []), list):
        return UNKNOWN, None
    omitted = identity.get('excludedPayers', [])
    if not isinstance(omitted, list) or any(not address(a) for a in omitted):
        return UNKNOWN, None
    excluded = {address(a) for a in omitted} | {
        address(identity['token']), *map(address, identity['doors']), address(identity.get('dropBox')),
        '0x8366a39cc670b4001a1121b8f6a443a643e40951',
        '0x8876789976decbfcbbbe364623c63652db8c0904',
        '0xc2bc9a3fe6c4ce8c2f7eb4655b0869b01d8c93c9'}
    if (allocation.get('approximate') is not True or allocation.get('complete') is not False
            or allocation.get('scanComplete') is not True or allocation.get('senderResolved') is not True
            or not payer or payer in excluded or not amount or not weight or not total
            or not available or not weight <= total or not available <= key[6]
            or amount != available * weight // total
            or not isinstance(swaps, list) or not 1 <= len(swaps) <= 20000
            or not isinstance(evidence, dict) or address(evidence.get('sender')) != payer):
        return UNKNOWN, None
    seen, sum_weight, before = set(), 0, key[3]
    for swap in swaps:
        if not isinstance(swap, dict):
            return UNKNOWN, None
        tx, pool = swap.get('txHash'), swap.get('poolId')
        block, index, fee = (number(swap.get(k)) for k in ('blockNumber', 'logIndex', 'feeWeight'))
        if (not isinstance(tx, str) or not HASH.fullmatch(tx) or not isinstance(pool, str) or not HASH.fullmatch(pool)
                or block is None or not 0 < block <= key[3] or index is None or fee is None
                or (tx.lower(), index) in seen):
            return UNKNOWN, None
        seen.add((tx.lower(), index))
        sum_weight += fee
        before = min(before, block)
    if sum_weight != weight:
        return UNKNOWN, None
    if (allocation.get('reconciliationOk') is not True
            or identity.get('generation') == 'pre-2026-10-29' and not identity.get('v1StreamsVerified')):
        return UNKNOWN, payer
    return pool_payer(allocation, swaps, evidence, payer, before, identity)


def pool_payer(allocation, swaps, evidence, payer, before, identity):
    """Same bounded relationship rule for exact recapture and fee estimates."""
    if payer in identities(identity):
        return CREATOR, payer
    stamps = [number(s.get('time')) for s in swaps]
    complete_times = all(t is not None for t in stamps)
    funding = evidence.get('fundingPath')
    if funding and (not complete_times or not isinstance(funding, list) or not isinstance(funding[0], dict)
                    or number(funding[0].get('time')) is None or number(funding[0]['time']) >= min(stamps)):
        return UNKNOWN, payer
    # Older sidecars' relatedWallets and bare funding paths cannot promote a
    # sender. Revalidate the new explicit evidence contract in the pure core.
    rel = {'depth': evidence.get('relationshipDepth'), 'hubRules': evidence.get('hubRules'),
           'beforeBlock': evidence.get('beforeBlock', before),
           'beforeTime': evidence.get('beforeTime', min(stamps) if complete_times else None),
           'fundingScope': evidence.get('fundingScope', {}),
           'lookbackSeconds': evidence.get('lookbackSeconds'), 'accounts': evidence.get('accounts', {}),
           'relationshipsComplete': evidence.get('relationshipsComplete'),
           'paths': ([{'path': evidence['fundingPath']}] if evidence.get('fundingPath') else [])
                    + evidence.get('fundingLeads', [])}
    scope = evidence.get('fundingScope')
    if isinstance(scope, dict) and scope:
        if (not complete_times or number(evidence.get('beforeTime')) is None
                or number(evidence['beforeTime']) > min(stamps)
                or number(evidence.get('beforeBlock')) is None or number(evidence['beforeBlock']) > before
                or number(scope.get('throughBlock')) is None
                or number(scope['throughBlock']) < max(number(s['blockNumber']) for s in swaps)
                or number(scope.get('throughTime')) is None or number(scope['throughTime']) < max(stamps)):
            return UNKNOWN, payer
    if evidence.get('note') == 'common funder lead':
        return UNKNOWN, payer
    result = relationship(payer, identity, rel, identity.get('projectRelationshipsComplete') is True)
    if result['classification'] == EXTERNAL and not swap_eoa(
            rel['accounts'].get(payer, {}), [number(s['blockNumber']) for s in swaps]):
        return UNKNOWN, payer
    if result['classification'] == EXTERNAL and (number(rel.get('lookbackSeconds')) or 0) < 7 * 86400:
        return UNKNOWN, payer
    return result['classification'], payer


def recapture_trace(allocation, key, identity):
    try:
        return checked_recapture(allocation, key, identity)
    except (TypeError, ValueError, KeyError, IndexError, AttributeError, RecursionError):
        return UNKNOWN, None


def checked_recapture(allocation, key, identity):
    from .hookr_recapture import decode_donate
    from .hookr_collect import decode_swap
    from .hookr_live import integer, SWAP, POOL_MANAGER
    payer = address(allocation.get('sender'))
    d, tx, ev = allocation.get('donate'), allocation.get('transaction'), allocation.get('senderEvidence')
    if (allocation.get('complete') is not True or allocation.get('approximate') is not False
            or allocation.get('scanComplete') is not True or allocation.get('senderResolved') is not True
            or allocation.get('reconciliationOk') is not True or not payer
            or not isinstance(d, dict) or not isinstance(tx, dict) or not isinstance(ev, dict)
            or address(ev.get('sender')) != payer or address(tx.get('from')) != payer
            or not isinstance(ev.get('fundingLeads', []), list)
            or payer in set(identity.get('excludedPayers', []))
            or payer in {identity.get('token'), identity.get('dropBox'), *identity['doors']}
            or identity.get('generation') == 'pre-2026-10-29' and not identity.get('v1StreamsVerified')):
        return UNKNOWN, payer
    pool, block = d.get('poolId'), number(d.get('blockNumber'))
    currencies, raw = d.get('currencies'), d.get('rawLog')
    if (not isinstance(pool, str) or not HASH.fullmatch(pool) or not block or block > key[3]
            or not isinstance(currencies, list) or len(currencies) != 2
            or any(address(c, zero=True) is None for c in currencies)
            or currencies != sorted(set(currencies)) or not isinstance(raw, dict)
            or tx.get('hash') != d.get('txHash') or integer(tx.get('blockNumber')) != block
            or d.get('samePoolSwap') is not True or d.get('reason')):
        return UNKNOWN, payer
    decoded = decode_donate(raw, pool, currencies)
    match = [r for r in decoded or [] if r['currency'] == key[5]]
    if (len(match) != 1 or any(match[0].get(k) != d.get(k) for k in
            ('txHash', 'blockNumber', 'logIndex', 'currency', 'amount', 'time', 'donor'))
            or number(d['amount']) != number(allocation.get('amount'))):
        return UNKNOWN, payer
    swaps = allocation.get('swaps')
    if not isinstance(swaps, list) or not swaps or swaps != d.get('swapEvents'):
        return UNKNOWN, payer
    seen = set()
    for swap in swaps:
        if not isinstance(swap, dict) or not isinstance(swap.get('rawLog'), dict):
            return UNKNOWN, payer
        raw_swap = swap['rawLog']
        if (address(raw_swap.get('address')) != POOL_MANAGER or raw_swap.get('removed') is True
                or not raw_swap.get('topics') or raw_swap['topics'][0] != SWAP):
            return UNKNOWN, payer
        parsed = decode_swap(raw_swap, pool, currencies)
        if (not parsed or parsed['txHash'] != d['txHash'] or parsed['blockNumber'] != d['blockNumber']
                or any(parsed.get(k) != swap.get(k) for k in ('txHash','blockNumber','poolId','logIndex','time','eventSender'))
                or parsed['logIndex'] in seen):
            return UNKNOWN, payer
        seen.add(parsed['logIndex'])
    reconciled = allocation.get('recaptureReconciliation', {})
    lanes = (reconciled.get('yieldEvidence') or {}).get('lanes', {})
    expected = lanes.get('recapture', {}).get(key[5])
    if (reconciled.get('poolId') != pool or reconciled.get('currency') != key[5]
            or reconciled.get('status') != 'matched' or reconciled.get('recaptureStatus') != 'matched'
            or expected is None or number(reconciled.get('donateAmount')) != expected
            or number(d['amount']) > expected):
        return UNKNOWN, payer
    return pool_payer(allocation, swaps, ev, payer, block, identity)


def trace(allocation, key, identity, creators, related, relationships_complete):
    """Check a same-currency, ordered path ending in a registered stream door."""
    if allocation.get('basis') == 'pool-donate-recapture-exact':
        return recapture_trace(allocation, key, identity)
    if allocation.get('basis') == 'pool-fee-weight-estimate':
        return approximate_trace(allocation, key, identity, creators, related)
    amount = number(allocation.get('amount'))
    path = allocation.get('path')
    if (allocation.get('basis') != 'payer-trace' or allocation.get('complete') is not True
            or not amount or not isinstance(path, list) or not 1 <= len(path) <= 16):
        return UNKNOWN, None
    previous_to, previous_block = None, 0
    wallets, events = [], set()
    for edge in path:
        if not isinstance(edge, dict):
            return UNKNOWN, None
        sender, destination = address(edge.get('from')), address(edge.get('to'))
        block = number(edge.get('blockNumber'), maximum=2 ** 64 - 1)
        units = number(edge.get('amount'))
        tx = edge.get('txHash')
        if (not sender or not destination or sender == destination
                or previous_to is not None and sender != previous_to
                or address(edge.get('currency'), zero=True) != key[5]
                or not block or not previous_block < block <= key[3]
                or units is None or units < amount
                or not isinstance(tx, str) or not HASH.fullmatch(tx)):
            return UNKNOWN, None
        event = (tx.lower(), sender, destination, key[5], units)
        if event in events:
            return UNKNOWN, None
        events.add(event)
        wallets.append(sender)
        previous_to, previous_block = destination, block
    destinations = ({address(a) for a in identity['doors']} if key[0] == 'door'
                    else {address(identity['token'])})
    if allocation.get('directFunding') is True and key[0] in ('direct-transfer', 'bonus'):
        destination = address(allocation.get('destination'))
        allowed = {address(identity['token']), *map(address, identity['doors'])}
        if key[0] == 'bonus':
            allowed.add(address(identity.get('dropBox')))
        if destination in allowed:
            destinations = {destination}
    if previous_to not in destinations:
        return UNKNOWN, None
    payer = wallets[0]
    # This is a same-payment contract, not a historical balance allocation.
    # Only one authenticated top-level native value can bind a supplemental
    # line, in full. No origin label or old completeness flag can replace it.
    terminal = path[-1]
    if (key[0] == 'door' or len(path) != 1
            or key[5] != '0x' + '00' * 20
            or allocation.get('directFunding') is not True
            or address(allocation.get('destination')) != previous_to
            or terminal.get('nativeFundingVerified') is not True
            or terminal.get('kind') != 'txlist' or terminal.get('historyConflict')
            or terminal['txHash'].lower() != key[2]
            or number(terminal['blockNumber']) != key[3]
            or number(terminal.get('time')) != key[4]
            or number(terminal['amount']) != amount or amount != key[6]):
        return UNKNOWN, payer
    roots = identities(identity)
    origins = {address(identity['token']), *map(address, identity['doors']), address(identity.get('dropBox'))} - {None}
    hubs = KNOWN_HUBS | set(identity.get('hubs', []))
    if any(w in hubs for w in wallets):
        return UNKNOWN, payer
    if payer in roots and len(wallets) == 1:
        return CREATOR, payer
    accounts = allocation.get('accounts', identity.get('accounts', {}))
    # Only the first project endpoint can be a contract. All intermediary
    # links must have complete hub checks, including fan-out history.
    if allocation.get('hubRules', identity.get('hubRules')) != HUB_RULES:
        return UNKNOWN, payer
    for wallet in wallets[(1 if payer in origins else 0):]:
        observation = accounts.get(wallet, {})
        if observation.get('hub') is not False or observation.get('code') != '0x':
            return UNKNOWN, payer
    if payer in origins and len(wallets) >= 2:
        # Historical graph connectivity cannot prove unspent, non-reused funds.
        return UNKNOWN, payer
    if any(wallet in related for wallet in wallets[1:]) or payer in origins or payer in roots:
        return UNKNOWN, payer
    evidence = allocation.get('senderEvidence')
    if isinstance(evidence, dict):
        if not isinstance(evidence.get('fundingLeads', []), list):
            return UNKNOWN, payer
        rel = {'depth': evidence.get('relationshipDepth'), 'hubRules': evidence.get('hubRules'),
               'beforeBlock': number(path[0].get('blockNumber')), 'beforeTime': number(path[0].get('time', key[4])),
               'fundingScope': evidence.get('fundingScope', {}),
               'lookbackSeconds': evidence.get('lookbackSeconds'), 'accounts': evidence.get('accounts', {}),
               'relationshipsComplete': evidence.get('relationshipsComplete'),
               'paths': ([{'path': evidence['fundingPath']}] if evidence.get('fundingPath') else [])
                        + evidence.get('fundingLeads', [])}
        if evidence.get('note') == 'common funder lead':
            return UNKNOWN, payer
        result = relationship(payer, identity, rel, identity.get('projectRelationshipsComplete') is True)
        return result['classification'], payer
    # Legacy completeness booleans do not describe the bounded funding scope.
    return UNKNOWN, payer


def quantity(amount, row):
    decimals = row.get('decimals')
    if type(decimals) is int and 0 <= decimals <= 36:
        raw = str(amount).zfill(decimals + 1)
        units = (raw[:-decimals] + '.' + raw[-decimals:]).rstrip('0').rstrip('.') if decimals else raw
    else:
        units = str(amount) + ' base units'
    symbol = row.get('symbol')
    if (not isinstance(symbol, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,23}', symbol)
            or 'rece' + 'ipt' in symbol.lower()):
        symbol = row.get('_publicCurrency', 'currency')
    return units + ' ' + symbol.lower()


def native_weighted(shares, live, window):
    zero = '0x' + '00' * 20
    prices = {zero: Fraction(1)}
    raw_pools, raw_swaps = live.get('pools'), live.get('swaps')
    pools = {p['poolId']: p for p in raw_pools if isinstance(p, dict)
             and isinstance(p.get('poolId'), str) and HASH.fullmatch(p['poolId'])} if isinstance(raw_pools, list) else {}
    swaps = [s for s in raw_swaps if isinstance(s, dict) and isinstance(s.get('poolId'), str)] if isinstance(raw_swaps, list) else []
    for swap in sorted(swaps, key=lambda s: (number(s.get('blockNumber')) or 0,
                                           number(s.get('logIndex')) or 0)):
        pair = pools.get(swap.get('poolId'), {}).get('currencies')
        sqrt = number(swap.get('sqrtPriceX96'), maximum=2**160 - 1)
        if (not isinstance(pair, list) or len(pair) != 2 or pair[0] != zero
                or not address(pair[1]) or not sqrt or not HASH.fullmatch(str(swap.get('txHash')))
                or not (b := number(swap.get('blockNumber'))) or b > window['throughBlock']
                or (stamp := number(swap.get('time'))) is None
                or not window['from'] <= stamp <= window['through']):
            continue
        prices[pair[1]] = Fraction(2**192, sqrt * sqrt)
    if not shares or any(s['currency'] not in prices for s in shares):
        return None
    total = sum((int(s['incomeAmount']) * prices[s['currency']] for s in shares), Fraction())
    traced = sum((int(s['tracedAmount']) * prices[s['currency']] for s in shares), Fraction())
    return {'incomeNumerator': str(total.numerator), 'incomeDenominator': str(total.denominator),
            'tracedNumerator': str(traced.numerator), 'tracedDenominator': str(traced.denominator),
            'tracedShareBps': int(traced * 10000 // total), 'approximate': True} if total else None


def unknown_window_text(data):
    if isinstance(data, dict) and isinstance(data.get('private'), dict):
        data = data['private'].get('evidence', data)
    live = data.get('live') if isinstance(data, dict) else None
    if isinstance(live, dict) and (live.get('incomeStatus') == 'unknown' or any(
            r in ('income window unavailable', 'income window or lines unavailable')
            for r in (live.get('reasons') if isinstance(live.get('reasons'), list) else []))):
        return 'hookr income: unknown · income window unavailable'
    return None


def classify(token, data):
    """Return JSON-serializable source facts and card lines, or None when not answered.

    Uppercase severity is machine data; every emitted card line is lowercase.
    INFO never asserts a clean bill of health or changes a user's trading rules.
    """
    if isinstance(data, dict) and isinstance(data.get('private'), dict) and 'evidence' in data['private']:
        data = data['private']['evidence']
    ctx = context(token, data)
    if ctx is None:
        return None
    token, identity, income, window, creators, related, relationships_complete = ctx
    attributions = defaultdict(list)
    supplied = data.get('attributions', [])
    if isinstance(supplied, list) and len(supplied) <= MAX_INCOME_LINES + 200:
        for item in supplied:
            key = line_key(item.get('line')) if isinstance(item, dict) else None
            if key:
                attributions[key].append(item)
    donate_uses = Counter((p.get('donate', {}).get('txHash'), p.get('donate', {}).get('logIndex'),
                            p.get('donate', {}).get('currency'))
                           for records in attributions.values() for a in records if isinstance(a.get('allocations'), list)
                           for p in a['allocations'] if isinstance(p, dict)
                           and p.get('basis') == 'pool-donate-recapture-exact' and isinstance(p.get('donate'), dict)
                           and isinstance(p['donate'].get('txHash'), str) and type(p['donate'].get('logIndex')) is int
                           and isinstance(p['donate'].get('currency'), str)) if isinstance(supplied, list) else Counter()
    rows = [(row, 'door') for row in income['lines']]
    funding = data.get('funding', [])
    funding_read = isinstance(funding, list) and len(funding) <= 200
    if funding_read:
        rows.extend((row, 'funding') for row in funding)
    keys = [line_key(row) for row, _ in rows]
    counts = Counter(key for key in keys if key)
    payments = Counter(key[2] for key in keys if key and key[0] != 'door'
                       and key[5] == '0x' + '00' * 20)
    seen, sources, lines = set(), [], []
    invalid = 0 if funding_read else 1

    currency_ids = {c: f'currency {i + 1}' for i, c in enumerate(sorted({k[5] for k in keys if k}))}

    def emit(row, key, amount, classification, payer=None, basis=None, reason=None):
        approximate = basis == 'pool-fee-weight-estimate'
        # A transaction sender is an observation, not the economic pool payer.
        warning = classification in (CREATOR, LOOP) and basis == 'payer-trace'
        label = ('hookr income' if key[0] == 'door' else
                 'hookr funding (' + key[0].replace('-', ' ') + ', excluded from door income)')
        if row.get('origin') == 'agent-door payment':
            label += ' (agent door payment)'
        line = ('warn: ' if warning else '') + label + ' · ' + (classification + ' (approximate)' if classification == EXTERNAL else classification)
        line += ' · ' + quantity(amount, {**row, '_publicCurrency': currency_ids[key[5]]})
        line += ' · tx ' + key[2]
        if approximate:
            line += ' · approximate pool fee share'
        if basis == 'pool-donate-recapture-exact':
            line += ' · exact swap recapture amount'
        reason = (PAYMENT_REASON if classification == UNKNOWN and basis == 'payer-trace' else
                  'below trace threshold' if classification == UNKNOWN and
                  (basis == 'below-trace-threshold' or reason == 'below trace threshold') else reason if reason in ('recapture yield mismatch', 'donate without same-pool swap',
                       'recapture payer unresolved', 'recapture exceeds delivered income') else None)
        if reason:
            line += ' · ' + reason
        line += f' · {amount * 10000 // key[6] / 100:.2f}% of line'
        if payer:
            line += ' · sender count 1'
        lines.append(line)
        sources.append({'classification': classification, 'severity': 'WARN' if warning else 'INFO',
                        'amount': str(amount), 'currency': key[5], 'payer': payer,
                        'source': key[0], 'token': token, 'txHash': key[2],
                        'blockNumber': str(key[3]), 'time': key[4], 'lineAmount': str(key[6]),
                        'shareBps': amount * 10000 // key[6],
                        **({'reason': reason} if reason else {}),
                        **({'allocationBasis': basis, 'approximate': True} if approximate else
                           {'allocationBasis': basis, 'exactAmount': True, 'approximatePayerRule': True}
                           if basis == 'pool-donate-recapture-exact' else {})})

    for (row, origin), key in zip(rows, keys):
        if (key is None or key[1] != token or key[3] > number(window['throughBlock'])
                or not window['from'] <= key[4] <= window['through']
                or identity['generation'] == '2026-10-29' and key[5] == token
                or origin == 'door' and key[0] != 'door'
                or origin == 'funding' and (key[0] == 'door' or address(row.get('to')) not in
                    {token, *map(address, identity['doors']), address(identity.get('dropBox'))})):
            invalid += 1
            continue
        if key in seen:
            invalid += 1
            continue
        seen.add(key)
        matches = attributions[key]
        allocations = matches[0].get('allocations') if len(matches) == 1 and counts[key] == 1 else None
        recapture_count = sum(isinstance(p, dict) and p.get('basis') == 'pool-donate-recapture-exact'
                              for p in allocations) if isinstance(allocations, list) else 0
        if (not isinstance(allocations, list) or not 1 <= len(allocations) <= 400 + recapture_count
                or recapture_count > 20000
                or any(not isinstance(part, dict) or not number(part.get('amount')) for part in allocations)
                or sum(number(part['amount']) for part in allocations) > key[6]):
            reason = matches[0].get('reason') if len(matches) == 1 else None
            emit(row, key, key[6], UNKNOWN, reason=reason)
            continue
        used = 0
        for part in allocations:
            amount = number(part['amount'])
            category, payer = trace(part, key, identity, creators, related, relationships_complete)
            if part.get('basis') == 'pool-donate-recapture-exact':
                d = part.get('donate') if isinstance(part.get('donate'), dict) else {}
                event = (d.get('txHash'), d.get('logIndex'), d.get('currency'))
                if not all(isinstance(v, (str, int, type(None))) for v in event) or donate_uses[event] != 1:
                    category = UNKNOWN
            if part.get('basis') == 'payer-trace' and (payments[key[2]] > 1
                    or address(row.get('to')) != address(part.get('destination'))):
                category = UNKNOWN
            emit(row, key, amount, category, payer, part.get('basis'), part.get('reason'))
            used += amount
        if used < key[6]:
            emit(row, key, key[6] - used, UNKNOWN)
    if not sources:
        return None
    omitted = number(income.get('linesOmitted'), maximum=2 ** 64 - 1)
    not_counted = income.get('notCounted')
    excluded = len(not_counted) if isinstance(not_counted, list) else None
    complete = (income['status'] == 'ready' and not window['cut'] and omitted == 0
                and excluded == 0 and invalid == 0
                and all(row['classification'] != UNKNOWN for row in sources))
    coverage = {**window, 'status': income['status'], 'complete': complete,
                'linesOmitted': omitted, 'notCounted': excluded, 'invalidLines': invalid}
    timing = (f'hookr income coverage: {"complete for supplied lines" if complete else "partial"}'
              f' · chain 4663 · block {window["throughBlock"]}'
              f' · window {window["from"]} to {window["through"]}'
              f' · record starts {window["recordStarts"]}')
    if window['cut']:
        timing += ' · window cut'
    if omitted is None:
        timing += ' · omitted payments unknown'
    elif omitted:
        timing += f' · {omitted} payments omitted'
    if excluded is None:
        timing += ' · exclusions unknown'
    elif excluded:
        timing += f' · {excluded} parts not counted'
    if invalid:
        timing += f' · {invalid} invalid or duplicate lines'
    lines.extend([timing, 'hookr income: payer attribution only; customer revenue and profit are not established'])
    synthetic = data.get('synthetic') is True
    if synthetic:
        lines.append('hookr income: synthetic fixture, not live evidence')
    live = data.get('live')
    live_facts = None
    if isinstance(live, dict) and live.get('status') in ('read', 'partial'):
        read = number(live.get('linesRead'), maximum=len(income['lines']))
        if read is not None:
            by_line = defaultdict(list)
            for source in sources:
                if source['source'] == 'door':
                    by_line[(source['txHash'], source['currency'])].append(source)
            traced = sum(any(s['classification'] != UNKNOWN for s in group) for group in by_line.values())
            line_shares = []
            for (tx, currency), group in by_line.items():
                total = sum(int(s['amount']) for s in group)
                traced_amount = sum(int(s['amount']) for s in group if s['classification'] != UNKNOWN)
                share = {'txHash': tx, 'currency': currency, 'amount': str(total),
                         'tracedAmount': str(traced_amount), 'unknownAmount': str(total - traced_amount),
                         'tracedShareBps': traced_amount * 10000 // total,
                         'senderCount': len({s['payer'] for s in group if s['payer']}),
                         'tracedSenderCount': len({s['payer'] for s in group if s['payer'] and s['classification'] != UNKNOWN}),
                         'approximate': any(s.get('approximate') for s in group)}
                line_shares.append(share)
                asset_meta = next((row for row in income['lines'] if row.get('currency') == currency), {})
                label = quantity(0, {**asset_meta, 'currency': currency, 'decimals': 0, '_publicCurrency': currency_ids[currency]})[2:]
                lines.append(f'hookr line share: {label} · tx {tx} · traced {traced_amount}/{total} base units'
                             f' · {share["tracedShareBps"] / 100:.2f}% · unknown {total - traced_amount}'
                             f' · sender count {share["senderCount"]} · traced sender count {share["tracedSenderCount"]}'
                             + (' · approximate shares' if share['approximate'] else ''))
            classes = {name: sum(s['classification'] == name for s in sources)
                       for name in (EXTERNAL, CREATOR, LOOP, UNKNOWN)}
            calls = live.get('calls', {})
            mcp = number(calls.get('mcp'), maximum=40) if isinstance(calls, dict) else None
            es = number(calls.get('etherscan'), maximum=4000) if isinstance(calls, dict) else None
            live_facts = {'status': live['status'], 'deliveryLinesRead': read, 'linesTraced': traced,
                          'linesUnknown': len(by_line) - traced, 'payerClasses': classes,
                          'calls': {'mcp': mcp, 'etherscan': es}, 'approximate': True, 'lineShares': line_shares}
            shares = []
            for currency in sorted({s['currency'] for s in sources if s['source'] == 'door'}):
                group = [s for s in sources if s['source'] == 'door' and s['currency'] == currency]
                total = sum(int(s['amount']) for s in group)
                value = sum(int(s['amount']) for s in group if s['classification'] != UNKNOWN)
                external_value = sum(int(s['amount']) for s in group if s['classification'] == EXTERNAL)
                # Creator in a pool estimate is an observed identity, not a
                # verified direct payment. Keep that count separate.
                counts = {'external': sum(s['classification'] == EXTERNAL for s in group),
                          'creatorVerified': sum(s['classification'] == CREATOR and s['severity'] == 'WARN' for s in group),
                          'creatorIdentity': sum(s['classification'] == CREATOR and s['severity'] != 'WARN' for s in group),
                          'loop': sum(s['classification'] == LOOP for s in group),
                          'unknown': sum(s['classification'] == UNKNOWN for s in group)}
                share = {'currency': currency, 'incomeAmount': str(total), 'tracedAmount': str(value),
                         'unknownAmount': str(total - value), 'tracedShareBps': value * 10000 // total,
                         'counts': counts, 'externalAmount': str(external_value),
                         'externalShareBps': external_value * 10000 // total}
                shares.append(share)
                asset = next((r for r in income['lines'] if r.get('currency') == currency), {})
                label = quantity(0, {**asset, 'currency': currency, 'decimals': 0,
                                     '_publicCurrency': currency_ids[currency]})[2:]
                lines.append(f'hookr value: {label} · traced {share["tracedShareBps"] / 100:.2f}% of income by value'
                             f' · {value}/{total} base units · external (approximate) {counts["external"]}'
                             f' · external (approximate) rule covers {share["externalShareBps"] / 100:.2f}% of income by value'
                             f' · creator (verified) {counts["creatorVerified"]}'
                             f' · creator identity (approximate) {counts["creatorIdentity"]}'
                             f' · loop {counts["loop"]} · unknown {counts["unknown"]} · allocation rows')
            live_facts['currencyShares'] = shares
            threshold = number(live.get('traceShareBps'), maximum=10000)
            if threshold:
                live_facts['traceShareBps'] = threshold
                lines.append(f'hookr trace threshold: {threshold / 100:.2f}% per currency by value'
                             ' · tail unknown: below trace threshold · selected value is not resolved value')
            weighted = native_weighted(shares, live, window)
            if weighted is not None:
                live_facts['nativeWeighted'] = weighted
                lines.append(f'hookr native weighted: traced {weighted["tracedShareBps"] / 100:.2f}% of income by value'
                             ' · approximate same-run pool spot prices')
            lines.append(f'hookr live: {live["status"]} · delivery lines read {read}'
                         f' · payer lines traced {traced} · unknown {len(by_line) - traced}'
                         f' · mcp calls {mcp} · etherscan calls {es}')
            allowed_sources = ('hookr revenue token', 'hookr income', 'hookr pool trades',
                               'etherscan delivery transactions', 'etherscan swap transactions',
                               'etherscan funding history', 'etherscan pool swap logs', 'etherscan pool donate logs')
            read_sources = live.get('sources', [])
            if isinstance(read_sources, list):
                lines.append('hookr live reads: ' + ', '.join(s for s in allowed_sources if s in read_sources))
            payments = live.get('agentPayments')
            if isinstance(payments, list) and payments:
                proved = sum(isinstance(p, dict) and isinstance(p.get('payment'), dict) for p in payments)
                lines.append(f'hookr agent payments: {proved} complete paths · {len(payments) - proved} unresolved'
                             ' · supplemental payments excluded from door income')
            lookback = number(live.get('fundingLookbackDays'), maximum=30)
            if lookback:
                lines.append('hookr ' + EXTERNAL_RULE)
                scopes = [r.get('fundingScope', {}) for r in live.get('funding', []) if isinstance(r, dict)]
                required = project_origins(identity)
                read_roots = set(required)
                if not scopes:
                    read_roots.clear()
                for scope in scopes:
                    covered_roots = set(scope.get('projectOutgoingReadRoots', []))
                    if scope.get('projectOutgoingComplete') is True:
                        covered_roots.update(scope.get('projectOutgoingRoots', []))
                    read_roots &= covered_roots
                live_facts['projectOutgoingRead'] = {'read': len(read_roots), 'required': len(required)}
                lines.append(f'hookr project outgoing read {len(read_roots)}/{len(required)}')
            estimate = live.get('budgetEstimate', {})
            expected = number(estimate.get('etherscanMinimum')) if isinstance(estimate, dict) else None
            if expected is not None:
                lines.append(f'hookr read budget: estimated minimum {expected} etherscan calls'
                             + (' · exceeds configured cap' if estimate.get('exceedsCallBudget') is True else ''))
            resolutions = live.get('senderResolution', [])
            if isinstance(resolutions, list):
                for i, resolution in enumerate(resolutions):
                    if not isinstance(resolution, dict): continue
                    for j, item in enumerate(resolution.get('currencies', [])):
                        if not isinstance(item, dict): continue
                        total, selected = number(item.get('totalWeight')), number(item.get('selectedWeight'))
                        if total and selected is not None and selected <= total:
                            basis = 'income value' if resolution.get('selectionBasis') == 'income-value' else 'fee weight'
                            lines.append(f'hookr sender selection: pool {i + 1} currency {j + 1}'
                                         f' · {selected * 10000 // total / 100:.2f}% {basis} selected')
                            reasons = item.get('shortfallReasons', [])
                            allowed = ('income outside observed swap fee basis', 'swap sender transaction cap reached',
                                       'below trace threshold', 'integer allocation rounding')
                            if isinstance(reasons, list) and any(r in allowed for r in reasons):
                                lines.append(f'hookr sender selection limit: pool {i + 1} currency {j + 1} · '
                                             + '; '.join(r for r in allowed if r in reasons))
            lines.append('hookr live: pool fee shares approximate; swap senders and funding links do not prove income payers')
            lines.append('hookr payer classes: ' + ' · '.join(f'{name + " (approximate)" if name == EXTERNAL else name} {count}' for name, count in classes.items())
                         + ' · allocation rows')
            metadata_block = number(identity.get('metadataBlock'))
            if metadata_block:
                lines.append(f'hookr live: stream metadata block {metadata_block}'
                             ' · historical stream ownership and accrual boundaries unproven')
            if live['status'] == 'partial':
                coverage['complete'] = False
            reasons = live.get('reasons', [])
            visible = ('mcp call cap reached', 'etherscan call cap reached', 'run deadline reached',
                       'etherscan key unavailable', 'mcp read unavailable', 'etherscan read unavailable',
                       'trade paging unavailable', 'trade paging did not advance',
                       'trade block boundary incomplete', 'trade history incomplete',
                       'funding history page cap reached', 'swap sender unresolved',
                       'delivery transaction not resolved', 'income coverage incomplete',
                       'window block unavailable', 'pool currencies unavailable', 'swap event unavailable',
                       'log page unavailable', 'log row unavailable', 'conflicting log event',
                       'log paging did not advance', 'swap log row cap reached', 'single block log page cap reached',
                       'swap sender transaction cap reached', 'funding wallet cap reached', 'funding path cap reached',
                       'related wallet cap reached', 'direct funding row cap reached', 'direct funding history incomplete',
                       'funding history paging did not advance', 'etherscan rate limit retries exhausted',
                       'mcp rate limit retries exhausted', 'contract read unavailable',
                       'verified abi unavailable', 'v1 streams unavailable', 'v1 pulled abi unavailable',
                       'v1 pulled event unverified', 'agent payment unresolved', 'agent balance interval unresolved',
                       'donate event unavailable', 'donate without same-pool swap', 'recapture yield mismatch',
                       'pool yield unavailable', 'pool yield window unavailable', 'pool yield mismatch',
                       'funding window block unavailable', 'income record capped at 200',
                       'income daily coverage incomplete', 'income line cap reached', 'income window unavailable',
                       'fee nft upstream payer unavailable', 'etherscan history unavailable', 'source shape unavailable',
                       'funding event identity unavailable')
            if isinstance(reasons, list):
                selected = [reason for reason in visible if reason in reasons]
                if selected:
                    lines.append('hookr live limits: ' + '; '.join(selected))
            limits = live.get('limits', [])
            if isinstance(limits, list):
                grouped = defaultdict(list)
                for limit in limits:
                    if isinstance(limit, dict) and limit.get('kind', '') + ' cap reached' in visible:
                        value = number(limit.get('limit'))
                        if value is not None:
                            grouped[limit['kind']].append(value)
                for kind, values in grouped.items():
                    lines.append(f'hookr live cap: {kind} · limit {max(values)} · count {len(values)}')
            reconciliation = live.get('reconciliation', [])
            if isinstance(reconciliation, list):
                public_reconciliation = []
                for i, item in enumerate(reconciliation):
                    if not isinstance(item, dict):
                        continue
                    status = item.get('status')
                    if status not in ('matched', 'mismatch', 'unavailable'):
                        continue
                    numerator, denominator = number(item.get('allocatedSwapFeesAndRecapture', item.get('allocatedSwapFees'))), number(item.get('swapFeesAndRecapture', item.get('swapFees')))
                    ratio = 'unavailable'
                    if numerator is not None and denominator is not None:
                        ratio = (f'{numerator * 10000 // denominator / 10000:.4f}' if denominator else
                                 '1.0000' if numerator == 0 else 'undefined (zero reference)')
                    label = 'allocated/(swap fees + recapture)' if item.get('yieldEvidence') else 'allocated/swap fees'
                    lines.append(f'hookr live reconciliation: pool/currency {i + 1} · {label} {ratio} · {status}')
                    public_reconciliation.append({'status': status, 'ratio': ratio})
                if live_facts is not None:
                    live_facts['reconciliation'] = public_reconciliation
    return {'severity': 'WARN' if any(s['severity'] == 'WARN' for s in sources) else 'INFO',
            'synthetic': synthetic,
            'token': token, 'tokenizer': address(identity['tokenizer']), 'generation': identity['generation'],
            'sources': sources, 'coverage': coverage, 'lines': lines,
            **({'live': live_facts} if live_facts else {})}


def private_facts(facts):
    """Only explicit private JSON fields contain attributed wallet identities."""
    if facts is None:
        return None
    public = {k: v for k, v in facts.items() if k != 'sources'}
    public['sources'] = [{k: v for k, v in row.items() if k != 'payer'} for row in facts['sources']]
    public['private'] = {'payers': [row.get('payer') for row in facts['sources']]}
    return public


def private_sidecar(data):
    if isinstance(data.get('private'), dict) and 'evidence' in data['private']:
        return data
    token = data.get('identity', {}).get('token')
    facts = private_facts(classify(token, data))
    return {'schema': SCHEMA, 'token': token, 'summary': facts,
            'live': {k: data['live'].get(k) for k in ('status', 'calls', 'caps', 'elapsedSeconds', 'runSeconds', 'reasons')},
            'private': {'evidence': data}}


def public_text(text):
    return re.sub(r'0x[0-9a-fA-F]{40}(?![0-9a-fA-F])', '[address omitted]', text)


def main():
    parser = PublicParser(description='Print Hookr income-source lines from offline JSON on stdin.')
    parser.add_argument('--token', required=True)
    parser.add_argument('--public', action='store_true', default=True, help='omit wallet addresses from text')
    parser.add_argument('--json', action='store_true', help='private structured facts; takes precedence over --public')
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(32_000_001)
        if len(raw) > 32_000_000:
            raise ValueError()
        data = json.loads(raw)
    except (ValueError, RecursionError):
        parser.exit(2, 'invalid offline income json\n')
    result = classify(args.token, data)
    if result is not None:
        print(json.dumps(private_facts(result), sort_keys=True) if args.json else '\n'.join(result['lines']))
    elif unknown_window_text(data):
        print(json.dumps({'status': 'unknown', 'reason': 'income window unavailable'}) if args.json
              else unknown_window_text(data))


if __name__ == '__main__':
    main()
