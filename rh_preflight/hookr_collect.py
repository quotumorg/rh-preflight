"""Run-scoped log scans and explicitly approximate Hookr fee attribution.

All remote reads go through Reader's guarded, budgeted transport. Missing
recordings and incomplete ranges never become empty, complete histories.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction
import re

from .income import SCHEMA, CREATOR, LOOP, EXTERNAL, UNKNOWN, address, number, line_key, classify
from .hookr_live import (POOL_MANAGER, TOKENIZER, TOKENIZER_V1, SWAP, INITIALIZE, DROP, BASIS, ROUTERS,
                         ZERO, ReadStopped, integer, log_index, hash32, words, word_address,
                         income_projection)
from .hookr_recapture import actions, recapture_scan, exact_parts, RECAPTURE
from .payer_attribution import HUB_RULES, BOUNDED_RULE, hub_status, relationship, identities, fee_weights


def log_scan(reader, topic, start, end, **filters):
    """Split saturated ranges; page saturated single blocks without gaps."""
    key = ('logs', topic, start, end, tuple(sorted(filters.items())))
    if key in reader.cache:
        return reader.cache[key]
    seen, pending, complete = {}, [(start, end, 1)], True
    while pending:
        lo, hi, page = pending.pop()
        try:
            batch = reader.es(module='logs', action='getLogs', address=POOL_MANAGER,
                              fromBlock=lo, toBlock=hi, topic0=topic, page=page, offset=1000,
                              **filters)
            if len(batch) > 1000:
                raise ReadStopped('log page unavailable')
            progress = 0
            for row in batch:
                if not isinstance(row, dict):
                    raise ReadStopped('log row unavailable')
                tx, index, height = hash32(row.get('transactionHash')), log_index(row.get('logIndex')), integer(row.get('blockNumber'))
                topics = row.get('topics')
                if (not tx or index is None or height is None or not lo <= height <= hi
                        or address(row.get('address')) != POOL_MANAGER or row.get('removed') is True
                        or not isinstance(topics, list) or not topics or topics[0].lower() != topic
                        or any(not hash32(t) for t in topics)
                        or any(len(topics) <= int(k[-1]) or topics[int(k[-1])].lower() != v
                               for k, v in filters.items() if k in ('topic1', 'topic2', 'topic3'))):
                    raise ReadStopped('log row unavailable')
                event = (tx, index)
                if event in seen and seen[event] != row:
                    raise ReadStopped('conflicting log event')
                if event not in seen:
                    progress += 1
                    seen[event] = row
                if len(seen) >= 20000:
                    reader.hit('swap log row', 20000, fromBlock=start, throughBlock=end)
                    raise ReadStopped('swap log row cap reached')
            if len(batch) == 1000:
                if lo < hi:
                    mid = (lo + hi) // 2
                    pending.extend([(mid + 1, hi, 1), (lo, mid, 1)])
                elif page > 1 and not progress:
                    raise ReadStopped('log paging did not advance')
                elif page >= 20:
                    reader.hit('single block log page', 20, block=lo)
                    raise ReadStopped('single block log page cap reached')
                else:
                    pending.append((lo, hi, page + 1))
        except ReadStopped as error:
            reader.note(str(error))
            complete = False
            break
    result = {'logs': sorted(seen.values(), key=lambda r: (integer(r['blockNumber']), log_index(r['logIndex']))),
              'complete': complete, 'fromBlock': start, 'throughBlock': end}
    reader.cache[key] = result
    return result


def pool_currencies(reader, pool, end):
    # A single metadata call is allowed; hookr_trades is never a swap source.
    # This avoids scanning Initialize from genesis for every hot pool. v4
    # currencies are ordered by address, not the token/quote display order.
    key = ('currencies', pool)
    if key not in reader.cache:
        pair = None
        try:
            meta = reader.mcp('hookr_trades', {'poolId': pool})
            observations = reader.cache.setdefault(('identity-hints',), [])
            for row in meta.get('trades', []) if isinstance(meta.get('trades'), list) else []:
                if isinstance(row, dict):
                    for field in ('sender', 'payer'):
                        if ev := identity_pass(row.get(field), getattr(reader, 'identity', {})):
                            observations.append(ev)
            a = address((meta.get('token') or {}).get('address'), zero=True)
            b = address((meta.get('quote') or {}).get('address'), zero=True)
            if hash32(meta.get('poolId')) == pool and a and b and a != b:
                pair = sorted((a, b))
        except ReadStopped as error:
            reader.note(str(error))
        if pair is None:
            scan = log_scan(reader, INITIALIZE, 0, end, topic1=pool, topic0_1_opr='and')
            if scan['complete'] and len(scan['logs']) == 1:
                topics = scan['logs'][0]['topics']
                if len(topics) == 4:
                    a, b = (word_address(t[2:]) for t in topics[2:])
                    if a and b and int(a, 16) < int(b, 16):
                        pair = [a, b]
        reader.cache[key] = pair
    return reader.cache[key]


def signed(word, bits):
    n = int(word, 16)
    value = n - (1 << 256) if n >> 255 else n
    return value if -(1 << (bits - 1)) <= value < 1 << (bits - 1) else None


def decode_swap(row, pool, currencies):
    data, topics = words(row.get('data')), row.get('topics', [])
    if len(data) != 6 or len(topics) != 3 or topics[1].lower() != pool:
        return None
    a, b = signed(data[0], 128), signed(data[1], 128)
    fee = int(data[5], 16)
    if (a is None or b is None or not ((a < 0 <= b) or (b < 0 <= a))
            or not 0 <= fee <= 1000000 or int(data[2], 16) >= 1 << 160
            or int(data[3], 16) >= 1 << 128 or signed(data[4], 24) is None):
        return None
    # PoolManager's negative delta is the input owed by the swap caller.
    index, units = (0, -a) if a < 0 else (1, -b)
    return {'poolId': pool, 'txHash': hash32(row['transactionHash']),
            'blockNumber': str(integer(row['blockNumber'])), 'logIndex': log_index(row['logIndex']),
            'time': integer(row.get('timeStamp')), 'currency': currencies[index],
            'amount': str(units), 'feePips': fee, 'feeWeight': str(units * fee),
            'feeAmount': str(units * fee // 1000000), 'sqrtPriceX96': str(int(data[2], 16)),
            'eventSender': word_address(topics[2][2:]), 'sender': None, 'rawLog': deepcopy(row)}


def swap_scan(reader, pool, start, end):
    pair = pool_currencies(reader, pool, end)
    if pair is None:
        reader.note('pool currencies unavailable')
        return {'poolId': pool, 'trades': [], 'complete': False, 'fromBlock': start, 'throughBlock': end}
    scan = log_scan(reader, SWAP, start, end, topic1=pool, topic0_1_opr='and')
    trades, complete = [], scan['complete']
    for row in scan['logs']:
        trade = decode_swap(row, pool, pair)
        if trade is None:
            complete = False
            reader.note('swap event unavailable')
        else:
            trades.append(trade)
    return {'poolId': pool, 'trades': trades, 'complete': complete,
            'fromBlock': start, 'throughBlock': end, 'currencies': pair}


def value_prefix(items, amount, share_bps):
    """Exact integer threshold, deterministic ties, independent per currency."""
    ordered = sorted(items, key=lambda item: -amount(item))
    total, covered, selected = sum(amount(item) for item in ordered), 0, []
    for item in ordered:
        if covered * 10000 >= total * share_bps:
            break
        selected.append(item)
        covered += amount(item)
    return selected


def select_income(rows, share_bps):
    groups = defaultdict(list)
    for row in rows:
        groups[row['currency']].append(row)
    return [row for currency in sorted(groups)
            for row in value_prefix(groups[currency], lambda r: int(r['amount']), share_bps)]


def identity_pass(sender, identity):
    """Identity observations need no history and never verify a payment."""
    sender = address(sender)
    if not sender:
        return None
    if sender in identities(identity):
        return {'sender': sender, 'classification': CREATOR, 'relationship': 'literal identity'}
    if sender in {identity.get('token'), identity.get('dropBox'), *identity.get('doors', [])}:
        return {'sender': sender, 'classification': UNKNOWN, 'relationship': 'project contract identity'}
    return None


def selection_coverage(pages, totals, selected, capped, share_bps, valued=True):
    """Pure per-pool diagnostics; the target combines pools within a currency."""
    available_totals = Counter()
    covered = Counter()
    for page in pages.values():
        for t in actions(page):
            value = int(t.get('traceValue', 0)) if valued else int(t['feeWeight'])
            available_totals[t['currency']] += value
            if t['txHash'] in selected:
                covered[t['currency']] += value
    coverage = []
    for pool, page in sorted(pages.items()):
        pool_txs = {t['txHash'] for t in actions(page)}
        pool_available, pool_selected, pool_capped = Counter(), Counter(), Counter()
        for t in actions(page):
            c = t['currency']
            value = int(t.get('traceValue', 0)) if valued else int(t['feeWeight'])
            pool_available[c] += value
            if t['txHash'] in selected:
                pool_selected[c] += value
            elif t['txHash'] in capped:
                pool_capped[c] += value
        currencies = []
        for c in sorted(set(page.get('currencies', pool_available)) & set(totals)):
            total, available, chosen = totals[c], pool_available[c], pool_selected[c]
            reasons = []
            if covered[c] * 10000 < total * share_bps:
                if valued and available_totals[c] * 10000 < total * share_bps:
                    reasons.append('income outside observed swap fee basis')
                if pool_capped[c]:
                    reasons.append('swap sender transaction cap reached')
                if not reasons:
                    reasons.append('below trace threshold')
            currencies.append({'currency': c, 'totalWeight': str(total),
                               'availableWeight': str(available), 'selectedWeight': str(chosen),
                               'shortfallReasons': reasons})
        coverage.append({'poolId': pool, 'distinctTransactions': len(pool_txs),
                         'selectedTransactions': len(pool_txs & set(selected)),
                         'targetPercent': share_bps / 100,
                         'selectionBasis': 'income-value' if valued else 'fee-weight',
                         'currencies': currencies})
    return coverage


def resolve_senders(reader, pages, excluded):
    """Select highest income value first, separately for every currency."""
    weights, totals = defaultdict(Counter), Counter()
    valued = any('traceTotals' in p for p in pages.values())
    for page in pages.values():
        for t in actions(page):
            weight = int(t.get('traceValue', 0)) if valued else int(t['feeWeight'])
            weights[t['txHash']][t['currency']] += weight
            if not valued:
                totals[t['currency']] += weight
        if valued:
            totals.update({c: 0 for c in page['traceTotals']})
            for c, value in page['traceTotals'].items():
                totals[c] = max(totals[c], value)
    pool_transactions = {pool: {t['txHash'] for t in actions(p)} for pool, p in pages.items()}
    tx_pools = {tx: {pool for pool, transactions in pool_transactions.items() if tx in transactions}
                for tx in weights}
    # Merge per-currency rankings by relative share, never by adding asset units.
    ranking = sorted(weights, key=lambda tx: (-max((Fraction(w, totals[c]) for c, w in weights[tx].items()
                                                   if totals[c]), default=Fraction()), tx))
    selected, covered, capped = [], Counter(), set()
    for tx in ranking:
        if all(covered[c] * 10000 >= total * reader.trace_share_bps for c, total in totals.items()):
            break
        if not any(w > 0 and covered[c] * 10000 < totals[c] * reader.trace_share_bps
                   for c, w in weights[tx].items()):
            continue
        if any(len(set(selected) & pool_transactions[pool]) >= reader.max_swap_txs for pool in tx_pools[tx]):
            capped.add(tx)
            for pool in sorted(tx_pools[tx]):
                reader.hit('swap sender transaction', reader.max_swap_txs, poolId=pool,
                           distinctTransactions=len(pool_transactions[pool]),
                           selectedTransactions=reader.max_swap_txs)
            continue
        selected.append(tx)
        covered.update(weights[tx])
    resolved = dict(zip(selected, reader.parallel(reader.get_tx, selected)))
    reader.hubs.update(address(tx.get('to')) for tx in resolved.values()
                       if isinstance(tx, dict) and address(tx.get('to')))
    coverage = selection_coverage(pages, totals, set(selected), capped, reader.trace_share_bps, valued)
    for page in pages.values():
        for trade in actions(page):
            tx = resolved.get(trade['txHash'])
            sender = (address(tx.get('from')) if isinstance(tx, dict)
                      and hash32(tx.get('hash')) == trade['txHash']
                      and integer(tx.get('blockNumber')) == int(trade['blockNumber']) else None)
            trade['sender'] = sender if sender not in excluded else None
            if trade.get('basis') == RECAPTURE:
                trade['transaction'] = deepcopy(tx)
                trade['scanComplete'] = page.get('donateComplete') is True and page['complete'] is True
            trade['traceSelected'] = trade['txHash'] in resolved
            if not trade['traceSelected']:
                trade['traceSkipReason'] = ('swap sender transaction cap reached' if trade['txHash'] in capped
                                             else 'below trace threshold')
            if trade['sender'] is None and trade['traceSelected']:
                reader.note('swap sender unresolved')
    return coverage


def trace_values(rows, selected, details, deliveries, pages, window, start):
    """Value proxy uses the same integer allocation as the shared classifier.

    Preserve every swap's denominator. Only selected income windows contribute
    to the ranked numerator; selecting a tail row cannot inflate its peers.
    """
    totals = Counter()
    for row in rows:
        totals[row['currency']] += int(row['amount'])
    aliases = {tx: '0x' + f'{i + 1:040x}' for i, tx in enumerate(sorted(
        {t['txHash'] for page in pages.values() for t in actions(page)}))}
    for page in pages.values():
        page['traceTotals'] = dict(totals)
        for t in actions(page):
            if t.get('basis') == RECAPTURE:
                t['scanComplete'] = page.get('donateComplete') is True and page['complete'] is True
            t['sender'], t['traceValue'] = aliases[t['txHash']], 0
    by_event = {(t['txHash'], t['logIndex'], t['currency']): t for p in pages.values() for t in actions(p)}
    basis = defaultdict(lambda: defaultdict(Fraction))
    selected_keys = {line_key(r) for r in selected}
    for row in rows:
        detail = details.get(row['txHash'])
        if not detail:
            continue
        attr = allocate(row, detail, deliveries, pages, window, start, {})
        for part in attr['allocations']:
            if part.get('basis') == RECAPTURE:
                if line_key(row) in selected_keys:
                    d = part['donate']
                    by_event[(d['txHash'], d['logIndex'], d['currency'])]['traceValue'] += int(part['amount'])
                continue
            if part.get('basis') != BASIS:
                continue
            for pool in {swap['poolId'] for swap in part['swaps']}:
                pool_weight = sum(int(t['feeWeight']) for t in part['swaps'] if t['poolId'] == pool)
                basis[pool][row['currency']] += Fraction(int(part['amount']) * pool_weight, int(part['weight']))
            if line_key(row) not in selected_keys:
                continue
            # Allocate by provisional transaction sender, then distribute its integer
            # value conservatively back to its events for sender resolution.
            total = int(part['weight'])
            for swap in part['swaps']:
                by_event[(swap['txHash'], swap['logIndex'], row['currency'])]['traceValue'] += (
                    int(part['amount']) * int(swap['feeWeight']) // total)
    for pool, page in pages.items():
        page['allocationBasis'] = dict(basis[pool])
        for t in actions(page):
            t['sender'] = None


def history(reader, wallet, start, end, offset=50):
    key = ('history', wallet, start, end, offset)
    if key in reader.cache:
        return reader.cache[key]
    superset = reader.cache.get(('history', wallet, 0, end, offset)) if start else None
    if superset is not None:
        return {'rows': [row for row in superset['rows'] if int(row['blockNumber']) >= start],
                'complete': superset['complete']}
    result, complete = [], True
    native_observations, native_conflicts = {}, set()
    for action in ('txlist', 'txlistinternal', 'tokentx'):
        fingerprints, previous_oldest = set(), None
        for page in range(1, 4):
            batch = reader.optional_es(module='account', action=action, address=wallet,
                                      startblock=start, endblock=end, page=page, offset=offset, sort='desc')
            if batch is None:
                complete = False
                break
            signature = repr(batch)
            if len(batch) > offset or (batch and signature in fingerprints):
                reader.note('funding history paging did not advance')
                complete = False
                break
            fingerprints.add(signature)
            heights = [number(row.get('blockNumber')) if isinstance(row, dict) else None for row in batch]
            # Descending pages may cross the lower bound. Older rows establish
            # completeness, but cannot become funding edges in this window.
            valid_order = all(h is not None and h <= end for h in heights)
            if valid_order and len(batch) == offset:
                valid_order = heights == sorted(heights, reverse=True)
            if valid_order and heights and previous_oldest is not None:
                valid_order = heights[0] <= previous_oldest
            if not valid_order:
                complete = False
            if valid_order and heights:
                previous_oldest = min(heights)
            for row in batch:
                if not isinstance(row, dict):
                    complete = False
                    continue
                tx = hash32(row.get('hash'))
                if action == 'txlist' and tx:
                    observation = (address(row.get('from'), zero=True), address(row.get('to'), zero=True),
                                   number(row.get('value')), number(row.get('blockNumber')),
                                   number(row.get('timeStamp')), row.get('input', ''),
                                   row.get('isError', '0'), row.get('txreceipt_status', '1'))
                    if tx in native_observations and native_observations[tx] != observation:
                        native_conflicts.add(tx)
                        complete = False
                    native_observations.setdefault(tx, observation)
                height = number(row.get('blockNumber'))
                if (height is None or height > end or not hash32(row.get('hash'))
                        or number(row.get('timeStamp')) is None
                        or action == 'tokentx' and not address(row.get('contractAddress'))
                        or not address(row.get('from'), zero=True)
                        or not (address(row.get('to'), zero=True) or row.get('to') == '')
                        or number(row.get('value')) is None):
                    complete = False
                    continue
                if height < start:
                    continue
                if row.get('isError', '0') != '0' or row.get('txreceipt_status', '1') != '1':
                    continue
                result.append({**row, '_kind': action})
            if len(batch) < offset or valid_order and heights and min(heights) < start:
                break
            if page == 3:
                reader.hit('funding history page', 3, wallet=wallet, action=action, offset=offset,
                           fromBlock=start, throughBlock=end)
                complete = False
    if native_conflicts:
        reader.note('funding event identity unavailable')
        for row in result:
            if row['_kind'] == 'txlist' and hash32(row['hash']) in native_conflicts:
                row['_historyConflict'] = True
    answer = {'rows': result, 'complete': complete}
    reader.cache[key] = answer
    return answer


def edge(row):
    currency = address(row.get('contractAddress')) if row['_kind'] == 'tokentx' else ZERO
    if not currency or not number(row.get('value')) or not address(row.get('from')) or not address(row.get('to')):
        return None
    return {'from': address(row['from']), 'to': address(row['to']), 'currency': currency,
            'amount': str(number(row['value'])), 'txHash': hash32(row['hash']),
            'blockNumber': str(number(row['blockNumber'])), 'time': number(row.get('timeStamp')),
            'kind': row['_kind'], **({'historyConflict': True} if row.get('_historyConflict') else {})}


def account_observation(reader, wallet, through, start=None, swap_blocks=()):
    if start is None or not 0 < start <= through:
        return {'code': None, 'hub': None, 'historyComplete': False}
    heights = sorted({through, *swap_blocks})
    key = ('account_observation-r7', wallet, start, through, tuple(heights), tuple(sorted(reader.hubs)))
    if key not in reader.cache:
        checks = {str(h): reader.code(wallet, h) for h in sorted({b for h in heights for b in (h - 1, h)})}
        code, opening = checks[str(through)], checks[str(through - 1)]
        all_eoa = all(c == '0x' for c in checks.values())
        data = history(reader, wallet, start, through) if wallet not in reader.hubs and all_eoa else {'rows': [], 'complete': False}
        reader.cache[key] = {'code': code, 'openingCode': opening,
            'codeChecks': checks,
            'hub': hub_status(wallet, code if all_eoa else next((c for c in checks.values() if c != '0x'), None), data['rows'],
                history_complete=data['complete'], max_counterparties=reader.hub_counterparties,
                known_hubs=reader.hubs), 'historyComplete': data['complete'],
            'fromBlock': start, 'throughBlock': through}
    return reader.cache[key]


def native_funding(reader, e, recipient_eoa=True):
    """A successful native EOA action, not an arbitrary token's Transfer log."""
    if e.get('kind') != 'txlist' or e['currency'] != ZERO or e.get('historyConflict'):
        return False
    tx = reader.get_tx(e['txHash'])
    receipt = reader.optional_es(module='proxy', action='eth_getTransactionReceipt', txhash=e['txHash'])
    block = int(e['blockNumber'])
    if (not isinstance(tx, dict) or not isinstance(receipt, dict)
            or hash32(tx.get('hash')) != e['txHash'] or hash32(receipt.get('transactionHash')) != e['txHash']
            or integer(tx.get('blockNumber')) != block or integer(receipt.get('blockNumber')) != block
            or integer(receipt.get('status')) != 1 or integer(tx.get('value')) != int(e['amount'])
            or any(address(obj.get(k)) != e[k] for obj in (tx, receipt) for k in ('from', 'to'))):
        return False
    return all(reader.code(w, height) == '0x' for w in ((e['from'], e['to']) if recipient_eoa else (e['from'],))
               for height in (max(0, block - 1), block))


def relationships(reader, wallet, before, through, roots, before_time=None, through_time=None, swap_blocks=()):
    """Only direct inbound funding in [first action - lookback, last action]."""
    stamp = number(before_time)
    end_time = number(through_time) if through_time is not None else stamp
    start_time = max(0, stamp - reader.funding_lookback_seconds) if stamp is not None else None
    start = (number(reader.optional_es(module='block', action='getblocknobytime',
             timestamp=start_time, closest='after')) if start_time is not None else None)
    result = {'wallet': wallet, 'depth': reader.depth, 'beforeBlock': before,
              'beforeTime': stamp, 'lookbackSeconds': reader.funding_lookback_seconds,
              'hubRules': HUB_RULES, 'accounts': {}, 'paths': [], 'relationshipsComplete': False}
    if start is None or not 0 < start <= before <= through or end_time is None:
        reader.note('funding window block unavailable')
        return result
    account = account_observation(reader, wallet, through, start, (before, *swap_blocks))
    result['accounts'][wallet] = account
    data = history(reader, wallet, start, through) if account['hub'] is False else {'rows': [], 'complete': False}
    direct = False
    complete = data['complete'] and reader.depth > 0
    seen = set()
    for row in data['rows']:
        e = edge(row)
        if not e or e['to'] != wallet or e['from'] == wallet:
            continue
        if e['time'] is None:
            complete = False
            continue
        if not start_time <= e['time'] <= end_time:
            continue
        marker = (e['txHash'], e['from'], e['currency'], e['amount'])
        if marker in seen: continue
        seen.add(marker)
        # Every direct project-origin observation excludes externality, even
        # when it cannot establish authenticated creator funding.
        if e['from'] in roots:
            direct = True
            e['nativeFundingVerified'] = native_funding(reader, e)
            if e['nativeFundingVerified']:
                result['accounts'][e['from']] = {'code': '0x', 'hub': False, 'historyComplete': False,
                                                'authenticatedIdentityOrigin': True}
        result['paths'].append({'path': [e], 'edgesRead': True, 'controlProven': False})
    scope = {'rule': BOUNDED_RULE, 'fromBlock': start, 'throughBlock': through,
             'fromTime': start_time, 'throughTime': end_time,
             'candidateHistoryComplete': bool(complete),
             'projectOutgoingComplete': False, 'directProjectFunding': direct}
    result.update(fundingScope=scope, relationshipsComplete=bool(complete))
    return result


def project_outgoing(reader, roots, relations):
    """Etherscan has no destination filter. Bound reads, then retain candidates only."""
    scopes = [r['fundingScope'] for r in relations if 'fundingScope' in r]
    if not scopes:
        return []
    start, through = min(s['fromBlock'] for s in scopes), max(s['throughBlock'] for s in scopes)
    rows, complete, read_roots = [], True, []
    candidates = {r['wallet'] for r in relations}
    for root in sorted(roots):
        data = history(reader, root, start, through)
        complete &= data['complete']
        if data['complete']:
            read_roots.append(root)
        rows.extend(e for row in data['rows'] if (e := edge(row))
                    and e['from'] == root and e['to'] in candidates)
    for rel in relations:
        scope = rel.get('fundingScope')
        if not scope: continue
        scope['projectOutgoingComplete'] = bool(complete)
        scope['projectOutgoingRoots'] = sorted(roots)
        scope['projectOutgoingReadRoots'] = sorted(read_roots)
        for e in rows:
            if e['to'] != rel['wallet'] or not scope['fromBlock'] <= int(e['blockNumber']) <= scope['throughBlock']:
                continue
            if e['time'] is None:
                scope['projectOutgoingComplete'] = False
            elif scope['fromTime'] <= e['time'] <= scope['throughTime']:
                scope['directProjectFunding'] = True
                lead = deepcopy(e)
                lead['nativeFundingVerified'] = native_funding(reader, lead)
                if lead['nativeFundingVerified']:
                    rel['accounts'][lead['from']] = {'code': '0x', 'hub': False,
                        'historyComplete': False, 'authenticatedIdentityOrigin': True}
                rel['paths'].append({'path': [lead], 'edgesRead': True, 'controlProven': False})
    return rows


def sender_evidence(sender, identity, rel, project_complete):
    return relationship(sender, identity, rel, project_complete)


def allocate(row, detail, deliveries, pages, window, start_block, evidence):
    attr = {'line': deepcopy(row), 'allocations': []}
    currency, units, block = row['currency'], int(row['amount']), int(row['blockNumber'])
    collections = [p for p in detail['pools'] if p['collect'] and p['sender']]
    fee_doors = set(detail.get('feeDoors', detail['doors']))
    if not collections:
        return attr
    forwards = [e for e in detail['forwarding'] if e['currency'] == currency]
    total_forward = sum(int(e['amount']) for e in forwards)
    fee_forward = sum(int(e['amount']) for e in forwards if e.get('from') in fee_doors)
    # A non-pool donation leg and an unmatched delivery stay unknown. A sync
    # tip can make the reported net delivery smaller than gross forwarding.
    if not total_forward or not fee_forward:
        return attr
    attributable = min(units, total_forward) * fee_forward // total_forward
    candidates, donations, bounds = {}, {}, []
    for p in collections:
        pool, position_manager = p['poolId'], p['sender']
        page = pages.get(pool)
        if not page or not page['complete']:
            return attr
        previous = [(int(d['blockNumber']), int(q['logIndex'])) for d in deliveries for q in d['pools']
                    if q['collect'] and q['poolId'] == pool and q['sender'] == position_manager and d.get('feeDoors') == detail.get('feeDoors')
                    and (int(d['blockNumber']), int(q['logIndex'])) < (block, int(p['logIndex']))]
        lower = max(previous, default=(start_block, -1))
        upper = (block, int(p['logIndex']))
        bounds.append({'poolId': pool, 'positionManager': position_manager, 'after': list(lower), 'before': list(upper)})
        for d in page.get('recaptures', []):
            if d['currency'] == currency and lower < (int(d['blockNumber']), d['logIndex']) < upper:
                donations[(d['txHash'], d['logIndex'], currency)] = d
        for t in page['trades']:
            if t['currency'] == currency and lower < (int(t['blockNumber']), t['logIndex']) < upper:
                candidates[(t['txHash'], t['logIndex'])] = t
    attr['accrualWindow'] = {'from': window['from'], 'through': row['time'], 'bounds': bounds, 'exact': False,
                             'basis': 'previous observed collection or reporting boundary; co-observed pool and fee-door mapping approximate'}
    recapture_amount = sum(int(d['amount']) for d in donations.values())
    if recapture_amount > attributable:
        attr['reason'] = 'recapture exceeds delivered income'
        return attr
    attr['allocations'].extend(exact_parts(donations.values(), currency, evidence))
    attributable -= recapture_amount
    weights = fee_weights(candidates.values()).get(currency, {})
    total = sum(weights.values())
    if not total:
        return attr
    # Do not inflate a small fee basis into donations or pre-window accrual.
    attributable = min(attributable, total // 1000000)
    groups = defaultdict(list)
    for t in candidates.values():
        if t['sender']:
            groups[t['sender']].append(t)
    tail_weight = sum(int(t['feeWeight']) for t in candidates.values() if t.get('traceSkipReason') == 'below trace threshold')
    tail_amount = attributable * tail_weight // total
    if tail_amount:
        attr['allocations'].append({'basis': 'below-trace-threshold', 'amount': str(tail_amount)})
    for sender, swaps in sorted(groups.items()):
        weight = weights[sender]
        amount = attributable * weight // total
        if not amount:
            continue
        ev = evidence.get(sender, {'classification': UNKNOWN, 'sender': sender})
        attr['allocations'].append({'basis': BASIS, 'amount': str(amount), 'complete': False,
                                    'approximate': True, 'reconciliationOk': False, 'sender': sender, 'senderResolved': True,
                                    'scanComplete': True, 'weight': str(weight), 'totalWeight': str(total),
                                    'attributableAmount': str(attributable), 'senderEvidence': ev,
                                    'swaps': [{'txHash': t['txHash'], 'poolId': t['poolId'],
                                               'blockNumber': t['blockNumber'], 'logIndex': t['logIndex'], 'time': t.get('time'),
                                               'feeWeight': t['feeWeight']} for t in swaps]})
    return attr


def direct_funding(reader, sidecar, start, excluded):
    identity, window = sidecar['identity'], sidecar['income']['window']
    token, through = identity['token'], int(window['throughBlock'])
    box = identity.get('dropBox')
    reader.loop_origins = {token, *identity['doors'], box} - {None}
    destinations = ({token, *identity['doors']} | ({box} if box else set())) - set(identity.get('agentDoors', []))
    found, drops, native_observations, conflicts = {}, {}, {}, set()
    for destination in sorted(destinations):
        data = history(reader, destination, start, through)
        if not data['complete']:
            reader.note('direct funding history incomplete')
        for row in data['rows']:
            # A top-level transaction has exactly one identity. Observe even
            # rows outside the destination filter so conflicting copies cannot
            # authenticate whichever version happened to survive the filter.
            tx = hash32(row.get('hash'))
            if row['_kind'] == 'txlist' and tx:
                observation = (edge(row), number(row.get('timeStamp')), row.get('input', ''),
                               row.get('isError', '0'), row.get('txreceipt_status', '1'))
                if tx in native_observations and native_observations[tx] != observation:
                    conflicts.add(('txlist', tx))
                native_observations.setdefault(tx, observation)
            sender = address(row.get('from'))
            stamp = number(row.get('timeStamp'))
            if (not sender or sender in excluded or address(row.get('to')) != destination
                    or stamp is None or not window['from'] <= stamp <= window['through']):
                continue
            calldata = row.get('input', '')
            if destination == box and row['_kind'] == 'txlist' and isinstance(calldata, str) and calldata[:10] == DROP:
                args = words('0x' + calldata[10:])
                asset = word_address(args[0]) if len(args) == 2 else None
                if asset and int(args[1], 16):
                    drops[(hash32(row['hash']), sender, asset)] = (int(args[1], 16), stamp, row)
            e = edge(row)
            if e:
                kind, marker = row['_kind'], None
                key = (kind, e['txHash'])
                if kind == 'tokentx':
                    marker = integer(row.get('logIndex'))
                    valid = marker is not None and 0 <= marker < 2 ** 64
                    key += (marker,)
                elif kind == 'txlistinternal':
                    marker = row.get('traceId')
                    valid = isinstance(marker, str) and re.fullmatch(r'[0-9]+(?:_[0-9]+)*', marker)
                    marker = marker if valid else None
                    key += (marker,)
                else:
                    valid = kind == 'txlist'
                if not valid or key in found and found[key][:2] != (e, stamp):
                    conflicts.add(key)
                found.setdefault(key, (e, stamp, kind, marker))
    for key in conflicts:
        if key in found:
            found[key][0]['historyConflict'] = True
        reader.note('funding event identity unavailable')
    normal = {(e['txHash'], e['from'], e['to'], e['amount'])
              for e, _, kind, _ in found.values() if kind == 'txlist'}
    events = []
    for e, stamp, kind, marker in found.values():
        if (kind == 'txlistinternal' and marker in ('root', '0')
                and (e['txHash'], e['from'], e['to'], e['amount']) in normal):
            continue
        events.append((e, stamp))
    groups = defaultdict(list)
    for e, stamp in events:
        source = 'direct-transfer'
        if e['to'] == box:
            drop = drops.get((e['txHash'], e['from'], e['currency']))
            if not drop or int(e['amount']) != drop[0]:
                continue
            source = 'bonus'
        groups[(source, e['txHash'], e['currency'], stamp, e['blockNumber'], e['to'])].append(e)
    totals, covered = Counter(), Counter()
    for key, edges in groups.items():
        totals[key[2]] += sum(int(e['amount']) for e in edges)
    ordered = sorted(groups.items(), key=lambda item: (item[0][2], -sum(int(e['amount']) for e in item[1]), item[0]))
    for (source, tx, currency, stamp, block, destination), edges in ordered:
        if len(sidecar['funding']) >= 200:
            reader.hit('direct funding row', 200)
            break
        row = {'source': source, 'token': token, 'to': destination, 'currency': currency,
               'amount': str(sum(int(e['amount']) for e in edges)), 'txHash': tx, 'time': stamp, 'blockNumber': block}
        asset_meta = next((r for r in sidecar['income']['lines'] if r.get('currency') == currency), {})
        row.update({k: asset_meta[k] for k in ('symbol', 'decimals') if k in asset_meta})
        if currency == ZERO:
            row.update(symbol='eth', decimals=18)
        if covered[currency] * 10000 >= totals[currency] * reader.trace_share_bps:
            sidecar['funding'].append(row)
            sidecar['attributions'].append({'line': deepcopy(row), 'allocations': [], 'reason': 'below trace threshold'})
            continue
        covered[currency] += int(row['amount'])
        # Supplemental flows are never allocated to an LP delivery or summed
        # with it. Show exact transfers even when origin classification is unknown.
        parts = []
        for e in edges:
            e['nativeFundingVerified'] = native_funding(reader, e, recipient_eoa=False)
            path = [e]
            project = {token, *identity['doors'], box}
            rel = ({'accounts': {}, 'paths': []} if not e['nativeFundingVerified'] or identity_pass(e['from'], identity) is not None else
                   relationships(reader, e['from'], int(e['blockNumber']), int(e['blockNumber']), project - {None}, stamp))
            parts.append({'basis': 'payer-trace', 'complete': e['nativeFundingVerified'], 'amount': e['amount'],
                          'path': path, 'directFunding': True, 'destination': destination,
                          'hubRules': HUB_RULES, 'accounts': rel['accounts'], 'fundingLeads': rel['paths'],
                          'senderEvidence': sender_evidence(e['from'], identity, rel, identity.get('projectRelationshipsComplete', False))})
        sidecar['funding'].append(row)
        sidecar['attributions'].append({'line': deepcopy(row), 'allocations': parts})


def collect(reader, token, days):
    token = address(token)
    if not token or type(days) is not int or not 1 <= days <= 30:
        raise ValueError('invalid token or days')
    if reader.used:
        raise ValueError('use one reader per run')
    reader.used = True
    reader.started = reader.clock()
    reader.deadline = reader.started + reader.run_seconds
    identity = {'chainId': 4663, 'token': token, 'relationshipsComplete': False, 'relatedWallets': []}
    live = {'status': 'partial', 'sources': [], 'deliveries': [], 'swaps': [], 'funding': [],
            'creation': {}, 'linesRead': 0, 'linesTraced': 0, 'linesUnknown': 0, 'approximations': True}
    sidecar = {'schema': SCHEMA, 'identity': identity, 'income': {}, 'attributions': [], 'funding': [], 'live': live}
    try:
        meta = reader.mcp('hookr_revenue_token', {'token': token})
        from .hookr_sources import stream_metadata
        streams = stream_metadata(reader, token, meta)
        if (meta.get('status') != 'ready' or meta.get('chainId', 4663) != 4663
                or address((meta.get('token') or {}).get('address')) != token
                or (meta.get('tokenizer'), address(meta.get('tokenizerAddress'))) not in (('v2', TOKENIZER), ('v1', TOKENIZER_V1))
                or not isinstance(streams, dict) or streams.get('status') != 'ready'
                or not isinstance(streams.get('streams'), list) or not 1 <= len(streams['streams']) <= 200
                or not number(meta.get('blockNumber'))):
            raise ReadStopped('token identity unavailable')
        doors = sorted({a for s in streams['streams'] if isinstance(s, dict) and (a := address(s.get('door')))})
        if not doors:
            raise ReadStopped('token streams unavailable')
        reader.generation = meta['tokenizer']
        identity.update(tokenizer=address(meta['tokenizerAddress']),
                        generation='pre-2026-10-29' if reader.generation == 'v1' else '2026-10-29', doors=doors,
                        creator=address(meta.get('creator')), deployer=None,
                        streamManager=address((meta.get('addingStreams') or {}).get('manager')),
                        dropBox=address((meta.get('drops') or {}).get('box')),
                        metadataBlock=str(number(meta['blockNumber'])))
        identity['v1StreamsVerified'] = reader.generation != 'v1' or all(s.get('v1VerifiedView') is True for s in streams['streams'])
        identity['feeDoors'] = sorted({address(s.get('door')) for s in streams['streams']
                                       if isinstance(s, dict) and s.get('kind') in ('family', 'market', 'positions')
                                       and address(s.get('door')) and identity['v1StreamsVerified']})
        identity['agentDoors'] = sorted({address(s.get('door')) for s in streams['streams']
            if isinstance(s, dict) and isinstance(s.get('agentIncomeDoor'), dict) and address(s.get('door'))})
        identity['streams'] = deepcopy(streams['streams'])
        reader.loop_origins = {token, *doors, identity.get('dropBox')} - {None}
        # Shared contracts from metadata are additional hub terminals.
        for field in ('legCollector', 'legCollectorAddress', 'factory', 'doorFactory'):
            if a := address(meta.get(field)):
                reader.hubs.add(a)
        identity['hubRules'] = HUB_RULES
        live['sources'].append('hookr revenue token')
        from .hookr_window import income_window
        raw = income_window(reader, token, days)
        live['incomePages'] = reader.income_pages
        sidecar['income'] = income_projection(raw)
        window, rows = sidecar['income']['window'], sidecar['income']['lines']
        identity['observedBlock'] = window.get('throughBlock')
        if window.get('days') != days or not classify(token, sidecar):
            sidecar['income'] = {}
            live['incomeStatus'] = 'unknown'
            raise ReadStopped('income window or lines unavailable')
        live['sources'].append('hookr income')
        keys = Counter(line_key(r) for r in rows)
        valid = [r for r in rows if line_key(r) and keys[line_key(r)] == 1 and line_key(r)[1] == token
                 and r['source'] == 'door' and int(r['blockNumber']) <= window['throughBlock']
                 and window['from'] <= r['time'] <= window['through']]
        if len(valid) != len(rows):
            reader.note('invalid or duplicate income lines')
        if raw['status'] != 'ready' or window['cut'] or raw.get('linesOmitted') != 0 or raw.get('notCounted') != []:
            reader.note('income coverage incomplete')
        live['creation'] = reader.creation(token, window['throughBlock'])
        identity['deployer'] = live['creation'].get('deployer')
        identity['deploymentSender'] = live['creation'].get('deploymentSender')
        reader.identity = identity
        selected = select_income(valid, reader.trace_share_bps)
        selected_keys = {line_key(r) for r in selected}
        live['traceShareBps'] = reader.trace_share_bps
        live['selection'] = [{'line': deepcopy(r), 'reason': 'below trace threshold'}
                             for r in valid if line_key(r) not in selected_keys]
        live['identityPass'] = [{'line': deepcopy(r), 'observation': ev}
                                for r in valid for field in ('payer', 'sender')
                                if (ev := identity_pass(r.get(field), identity)) is not None]
        details = {}
        transaction_rows = list({r['txHash']: r for r in [*selected, *valid]}.values())
        decoded = reader.parallel(lambda r: reader.get_delivery(token, r, doors), transaction_rows)
        details.update((r['txHash'], d) for r, d in zip(transaction_rows, decoded))
        for row in valid:
            tx = row['txHash']
            if tx not in details:
                details[tx] = reader.get_delivery(token, row, doors)
            if details[tx]:
                live['linesRead'] += 1
        live['deliveries'] = [d for d in details.values() if d]
        for detail in live['deliveries']:
            detail['feeDoors'] = sorted(set(detail['doors']) & set(identity['feeDoors']))
        if live['deliveries']:
            live['sources'].append('etherscan delivery transactions')
        keepers = {d['keeper'] for d in live['deliveries']}
        excluded = {*reader.hubs, token, *doors, *keepers}
        if identity['dropBox']:
            excluded.add(identity['dropBox'])
        identity['excludedPayers'] = sorted(excluded)
        block = reader.optional_es(module='block', action='getblocknobytime', timestamp=window['from'], closest='after')
        start = number(block)
        pages = {}
        if start is not None and start <= window['throughBlock']:
            pools = sorted({p['poolId'] for d in live['deliveries'] for p in d['pools']
                            if p['collect'] and d['feeDoors']})
            for pool in pools:
                # Cache the union once, then select each door's accrual interval
                # by (block, logIndex). No per-delivery repeated log requests.
                end = window['throughBlock']
                pages[pool] = swap_scan(reader, pool, start, end)
                if pages[pool].get('currencies'):
                    recapture_scan(reader, pages[pool])
            from .hookr_sources import prepare_yields
            prepare_yields(reader, pages, window)
            trace_values(valid, selected, details, live['deliveries'], pages, window, start)
            live['identityPass'].extend(reader.cache.get(('identity-hints',), []))
            live['senderResolution'] = resolve_senders(reader, pages, excluded)
            live['swaps'] = [t for p in pages.values() for t in p['trades']]
            live['recaptures'] = [t for p in pages.values() for t in p.get('recaptures', [])]
            live['pools'] = [{k: v for k, v in p.items() if k not in ('trades', 'recaptures', 'allocationBasis')}
                             for p in pages.values()]
            if live['recaptures']:
                live['sources'].append('etherscan pool donate logs')
            if pages:
                live['sources'].append('etherscan pool swap logs')
        else:
            reader.note('window block unavailable')
        roots = identities(identity)
        funding_roots = roots | ({identity['deploymentSender']} if identity.get('deploymentSender') else set())
        project = funding_roots | reader.loop_origins
        identity['hubs'] = sorted(reader.hubs)
        project_complete = bool(identity['creator'] and live['creation'].get('deploymentSender') and
                                (identity['streamManager'] or reader.generation == 'v1' and meta['token'].get('sealed') is True))
        identity['relatedWallets'] = []
        identity['commonFunders'] = []
        identity['projectRelationshipsComplete'] = bool(project_complete)
        evidence, relations = {}, []
        payer_actions = [*live['swaps'], *live.get('recaptures', [])]
        senders = {t['sender'] for t in payer_actions if t['sender']}
        groups = defaultdict(list)
        for t in payer_actions:
            if t['sender']:
                groups[t['sender']].append(t)
        totals = Counter()
        for row in valid:
            totals[row['currency']] += int(row['amount'])
        def relative_value(key):
            values = Counter()
            for t in groups[key]:
                values[t['currency']] += int(t.get('traceValue', 0))
            return max((Fraction(v, totals[c]) for c, v in values.items() if totals[c]), default=Fraction())
        ranked = sorted(groups, key=lambda key: (-relative_value(key), key))
        pending = []
        for key in ranked:
            ev = identity_pass(key, identity)
            if ev is not None:
                evidence[key] = ev
            else:
                pending.append(key)
        def read_relation(key):
            sender = key
            swaps = groups[key]
            before = min(int(t['blockNumber']) for t in swaps)
            through = max(int(t['blockNumber']) for t in swaps)
            times = [t['time'] for t in swaps if t['time'] is not None]
            stamp = min(times) if len(times) == len(swaps) else None
            end_time = max(times) if len(times) == len(swaps) else None
            return relationships(reader, sender, before, through, project, stamp, end_time,
                                 {int(t['blockNumber']) for t in swaps})
        relations = reader.parallel(read_relation, pending)
        live['projectOutgoing'] = project_outgoing(reader, project, relations)
        live['funding'].extend(relations)
        for key, rel in zip(pending, relations):
            evidence[key] = sender_evidence(key, identity, rel, project_complete)
        if senders:
            live['sources'].append('etherscan swap transactions')
        if live['funding']:
            live['sources'].append('etherscan funding history')
        estimated = (3 * len(details) + len({t['txHash'] for t in live['swaps'] if t.get('traceSelected')})
                     + 6 * len(pending) + 3 * len(funding_roots) + 20)
        live['budgetEstimate'] = {'etherscanMinimum': estimated, 'secondsAtConfiguredRate': round(estimated / reader.etherscan_rps, 1),
                                  'exceedsCallBudget': estimated > reader.caps['etherscan']}
        for row in valid:
            if line_key(row) not in selected_keys:
                sidecar['attributions'].append({'line': deepcopy(row), 'allocations': [],
                                                'reason': 'below trace threshold'})
                continue
            detail = details.get(row['txHash'])
            sidecar['attributions'].append(allocate(row, detail, live['deliveries'], pages, window, start, evidence)
                                           if detail and start is not None else {'line': deepcopy(row), 'allocations': []})
        if start is not None:
            from .hookr_sources import agent_payments, reconcile_pools
            agent_payments(reader, sidecar, start)
            reconcile_pools(reader, sidecar, pages, start)
            # A token/door return is allowed as evidence of a loop, while
            # PoolManager and inter-door forwarding never become direct payers.
            direct_funding(reader, sidecar, start, {*reader.hubs, *doors, *keepers})
        reader.note('swap fee shares approximate; position history not read')
        reader.note('accrual boundaries and historical stream ownership unproven')
    except (ReadStopped, TypeError, ValueError, KeyError, AttributeError, RecursionError) as error:
        reader.note(str(error) if isinstance(error, ReadStopped) else 'source shape unavailable')
        if not sidecar['income'].get('window'):
            live['incomeStatus'] = 'unknown'
    live.update(calls=dict(reader.counts), caps=dict(reader.caps), reasons=list(reader.reasons),
                failures=deepcopy(reader.failures), fundingLookbackDays=reader.funding_lookback_seconds // 86400,
                etherscanRps=reader.etherscan_rps, workers=reader.workers, traceShareBps=reader.trace_share_bps, limits=deepcopy(reader.limit_hits), elapsedSeconds=round(reader.clock() - reader.started, 3),
                runSeconds=reader.run_seconds, status='partial' if reader.reasons else 'read')
    facts = classify(token, sidecar)
    if facts and facts.get('live'):
        live.update(linesTraced=facts['live']['linesTraced'], linesUnknown=facts['live']['linesUnknown'],
                    payerClasses=facts['live']['payerClasses'])
    return sidecar
