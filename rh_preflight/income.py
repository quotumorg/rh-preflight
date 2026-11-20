"""Offline Hookr income attribution from a documented, caller-supplied sidecar.

Hookr income rows do not identify an economic payer. Only reconciled payer paths
may add attribution. This module performs no reads, trace discovery or signing.
See docs/HOOKR_INCOME.md for the input contract and its evidence limitations.
"""
import argparse
from collections import Counter, defaultdict
import json
import re
import sys

SCHEMA = 'rh-preflight/hookr-income/v1'
ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}')
HASH = re.compile(r'0x[0-9a-fA-F]{64}')
CREATOR = 'creator or deployer wallet'
LOOP = 'self-funded loop'
EXTERNAL = 'external payer'
UNKNOWN = 'unknown'


def address(value, *, zero=False):
    if isinstance(value, str) and ADDRESS.fullmatch(value) and (zero or int(value, 16)):
        return value.lower()
    return None


def number(value, *, maximum=2 ** 256 - 1):
    if type(value) is int:
        return value if 0 <= value <= maximum else None
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,78}', value):
        value = int(value)
        return value if value <= maximum else None
    return None


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
            or len(income['lines']) > 200):
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
    creators = {a for k in ('creator', 'deployer') if (a := address(identity.get(k)))}
    relatives = identity.get('relatedWallets')
    relations_read = (isinstance(relatives, list) and len(relatives) <= 200
                      and all(address(a) for a in relatives))
    related = {address(a) for a in relatives} if relations_read else set()
    related.update(creators | {token} | {address(a) for a in doors})
    complete = (identity.get('relationshipsComplete') is True and relations_read
                and all(address(identity.get(k)) for k in ('creator', 'deployer')))
    return token, identity, income, window, creators, related, complete


def trace(allocation, key, identity, creators, related, relationships_complete):
    """Check a same-currency, ordered path ending in a registered stream door."""
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
                or not block or not previous_block <= block <= key[3]
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
    if previous_to not in destinations:
        return UNKNOWN, None
    payer = wallets[0]
    if payer in creators and len(wallets) == 1:
        return CREATOR, payer
    if payer in related:
        return LOOP, payer
    # A project wallet can collect customer funds before forwarding them.
    # Its presence mid-path does not establish a subsidy or independence.
    if any(wallet in related for wallet in wallets[1:]):
        return UNKNOWN, payer
    return (EXTERNAL if relationships_complete else UNKNOWN), payer


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
        symbol = address(row.get('currency'), zero=True)
    return units + ' ' + symbol.lower()


def classify(token, data):
    """Return JSON-serializable source facts and card lines, or None when not answered.

    Uppercase severity is machine data; every emitted card line is lowercase.
    INFO never asserts a clean bill of health or changes a user's trading rules.
    """
    ctx = context(token, data)
    if ctx is None:
        return None
    token, identity, income, window, creators, related, relationships_complete = ctx
    attributions = defaultdict(list)
    supplied = data.get('attributions', [])
    if isinstance(supplied, list) and len(supplied) <= 200:
        for item in supplied:
            key = line_key(item.get('line')) if isinstance(item, dict) else None
            if key:
                attributions[key].append(item)
    rows = [(row, 'door') for row in income['lines']]
    funding = data.get('funding', [])
    funding_read = isinstance(funding, list) and len(funding) <= 200
    if funding_read:
        rows.extend((row, 'funding') for row in funding)
    keys = [line_key(row) for row, _ in rows]
    counts = Counter(key for key in keys if key)
    seen, sources, lines = set(), [], []
    invalid = 0 if funding_read else 1

    def emit(row, key, amount, classification, payer=None):
        warning = classification in (CREATOR, LOOP)
        label = ('hookr income' if key[0] == 'door' else
                 'hookr funding (' + key[0].replace('-', ' ') + ', excluded from door income)')
        line = ('warn: ' if warning else '') + label + ' · ' + classification
        line += ' · ' + quantity(amount, row)
        if payer:
            line += ' · payer ' + payer
        line += ' · tx ' + key[2]
        lines.append(line)
        sources.append({'classification': classification, 'severity': 'WARN' if warning else 'INFO',
                        'amount': str(amount), 'currency': key[5], 'payer': payer,
                        'source': key[0], 'token': token, 'txHash': key[2],
                        'blockNumber': str(key[3]), 'time': key[4]})

    for (row, origin), key in zip(rows, keys):
        if (key is None or key[1] != token or key[3] > number(window['throughBlock'])
                or not window['from'] <= key[4] <= window['through']
                or identity['generation'] == '2026-10-29' and key[5] == token
                or origin == 'door' and key[0] != 'door'
                or origin == 'funding' and (key[0] == 'door' or address(row.get('to')) != token)):
            invalid += 1
            continue
        if key in seen:
            invalid += 1
            continue
        seen.add(key)
        matches = attributions[key]
        allocations = matches[0].get('allocations') if len(matches) == 1 and counts[key] == 1 else None
        if (not isinstance(allocations, list) or not 1 <= len(allocations) <= 200
                or any(not isinstance(part, dict) or not number(part.get('amount')) for part in allocations)
                or sum(number(part['amount']) for part in allocations) > key[6]):
            emit(row, key, key[6], UNKNOWN)
            continue
        used = 0
        for part in allocations:
            amount = number(part['amount'])
            category, payer = trace(part, key, identity, creators, related, relationships_complete)
            emit(row, key, amount, category, payer)
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
    return {'severity': 'WARN' if any(s['severity'] == 'WARN' for s in sources) else 'INFO',
            'synthetic': synthetic,
            'token': token, 'tokenizer': address(identity['tokenizer']), 'generation': identity['generation'],
            'sources': sources, 'coverage': coverage, 'lines': lines}


def main():
    parser = argparse.ArgumentParser(description='Print Hookr income-source lines from offline JSON on stdin.')
    parser.add_argument('--token', required=True)
    parser.add_argument('--json', action='store_true', help='print structured facts instead of card lines')
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError()
        data = json.loads(raw)
    except (ValueError, RecursionError):
        parser.exit(2, 'invalid offline income json\n')
    result = classify(args.token, data)
    if result is not None:
        print(json.dumps(result, sort_keys=True) if args.json else '\n'.join(result['lines']))


if __name__ == '__main__':
    main()
