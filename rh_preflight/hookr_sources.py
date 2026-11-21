"""Defensive version/door adapters and currency-preserving reconciliation.

ABI-dependent v1 fields are decoded only from the deployed emitter's verified
ABI. Unsupported layouts remain unavailable. No invented Pulled topic, stream
tuple or MCP yield schema is used as evidence.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction
import re

from .hookr_live import (TOKENIZER_V1, ZERO, TRANSFER, ReadStopped, integer,
                         hash32, words, word_address, keccak256)
from .payer_attribution import address, number, HUB_RULES


def abi(reader, target):
    return reader.optional_es(module='contract', action='getabi', address=target) or []


def canonical(field):
    kind = field.get('type', '')
    if kind.startswith('tuple'):
        return '(' + ','.join(canonical(f) for f in field.get('components', [])) + ')' + kind[5:]
    return kind


def signature(entry):
    return entry['name'] + '(' + ','.join(canonical(f) for f in entry['inputs']) + ')'


def width(field):
    kind = field.get('type', '')
    if kind == 'tuple':
        return sum(width(f) for f in field['components'])
    if kind in ('address', 'bool', 'bytes32') or re.fullmatch(r'uint(?:8|16|24|32|64|128|160|256)', kind):
        return 1
    raise ValueError('unsupported abi layout')


def static_value(field, data, offset):
    kind = field['type']
    size = width(field)
    if offset + size > len(data):
        raise ValueError('short abi data')
    if kind == 'tuple':
        result = {}
        for f in field['components']:
            if not f.get('name') or f['name'] in result:
                raise ValueError('unnamed abi field')
            result[f['name']] = static_value(f, data, offset)
            offset += width(f)
        return result
    value = int(data[offset], 16)
    if kind == 'address':
        result = word_address(data[offset])
        if result is None:
            raise ValueError('invalid abi address')
        return result
    if kind == 'bool':
        if value > 1:
            raise ValueError('invalid abi bool')
        return bool(value)
    if kind == 'bytes32':
        return '0x' + data[offset]
    if value >= 1 << int(kind[4:]):
        raise ValueError('invalid abi integer')
    return value


def decode_outputs(fields, raw):
    """Restricted ABI decoder: static values and one array of static tuples."""
    data = words(raw)
    if len(fields) == 1 and fields[0].get('type', '').endswith('[]'):
        if len(data) < 2 or int(data[0], 16) != 32:
            raise ValueError('invalid array offset')
        count = int(data[1], 16)
        item = {**fields[0], 'type': fields[0]['type'][:-2]}
        size = width(item)
        if not 0 <= count <= 200 or len(data) != 2 + count * size:
            raise ValueError('invalid array length')
        return [static_value(item, data, 2 + i * size) for i in range(count)]
    if len(data) != sum(width(f) for f in fields):
        raise ValueError('invalid output length')
    if len(fields) == 1:
        return static_value(fields[0], data, 0)
    result, offset = {}, 0
    for field in fields:
        if not field.get('name') or field['name'] in result:
            raise ValueError('unnamed output')
        result[field['name']] = static_value(field, data, offset)
        offset += width(field)
    return result


def verified_call(reader, target, name, input_types, args):
    entries = [f for f in abi(reader, target) if isinstance(f, dict) and f.get('type') == 'function'
               and f.get('name') == name and f.get('stateMutability') in ('view', 'pure')
               and [i.get('type') for i in f.get('inputs', [])] == input_types]
    if len(entries) != 1:
        return None
    try:
        fn = entries[0]
        selector = '0x' + keccak256(signature(fn).encode())[:8]
        data = selector + ''.join(f'{int(v, 16) if isinstance(v, str) else v:064x}' for v in args)
        raw = reader.optional_es(module='proxy', action='eth_call', to=target, data=data, tag='latest')
        return decode_outputs(fn['outputs'], raw)
    except (KeyError, TypeError, ValueError, RecursionError):
        return None


def stream_metadata(reader, token, meta):
    streams = meta.get('streams', {})
    if meta.get('tokenizer') != 'v1' or address(meta.get('tokenizerAddress')) != TOKENIZER_V1:
        return streams
    # StreamRecord.target and Door.target are confirmed by the recorded ABIs.
    # A failed view retains descriptive facts, but cannot enable attribution.
    fallback = {**streams, 'streams': [
        {**s, 'door': address(s.get('door')) or address(s.get('target')), 'v1VerifiedView': False}
        for s in streams.get('streams', []) if isinstance(s, dict)]} if isinstance(streams, dict) else {}
    values = verified_call(reader, TOKENIZER_V1, 'streamsOf', ['address'], [token])
    count = verified_call(reader, token, 'doorCount', [], [])
    if (not isinstance(values, list) or type(count) is not int
            or not 1 <= count <= 200 or len(values) != count):
        reader.note('v1 streams unavailable')
        return fallback
    discovered = []
    kinds = ('rules', 'family', 'market', 'feeb', 'positions', 'positions_v3', 'source')
    for i, record in enumerate(values):
        value = verified_call(reader, token, 'doorAt', ['uint256'], [i])
        door = address(value.get('target')) if isinstance(value, dict) else None
        if (not door or not isinstance(record, dict) or address(record.get('target')) != door
                or type(record.get('kind')) is not int or not 0 <= record['kind'] < len(kinds)
                or value.get('kind') != (0, 1, 1, 2, 3, 5, 4)[record['kind']]):
            reader.note('v1 streams unavailable')
            return fallback
        existing = next((s for s in streams.get('streams', []) if isinstance(s, dict)
                         and address(s.get('target', s.get('door'))) == door), {}) if isinstance(streams, dict) else {}
        discovered.append({**existing, 'index': i, 'door': door, 'target': door,
                           'kind': kinds[record['kind']], 'doorKind': value.get('kind'),
                           'streamRecord': record, 'v1VerifiedView': True})
    reader.v1_doors = {s['index']: s['door'] for s in discovered}
    if any(s['kind'] == 'feeb' for s in discovered):
        reader.note('fee nft upstream payer unavailable')
    return {'status': 'ready', 'streams': discovered, 'count': len(discovered)}


def decoded_event(entry, log):
    if entry.get('anonymous') is not False:
        return None
    topics, data = log.get('topics', []), words(log.get('data'))
    if not topics or topics[0].lower() != '0x' + keccak256(signature(entry).encode()):
        return None
    indexed = [f for f in entry['inputs'] if f.get('indexed') is True]
    ordinary = [f for f in entry['inputs'] if f.get('indexed') is False]
    if len(indexed) + len(ordinary) != len(entry['inputs']) or len(topics) != len(indexed) + 1:
        return None
    if len(data) != sum(width(f) for f in ordinary) or any(width(f) != 1 for f in indexed):
        return None
    result, pos, topic = {}, 0, 1
    for field in entry['inputs']:
        name = field.get('name')
        if not name or name in result:
            return None
        if field['indexed']:
            result[name] = static_value(field, [topics[topic][2:]], 0)
            topic += 1
        else:
            result[name] = static_value(field, data, pos)
            pos += width(field)
    return result


def v1_receipt(reader, detail, token, receipt, doors):
    detail['v1Pulled'] = []
    detail['v1TokenEvents'] = []
    token_abi = abi(reader, token)
    for log in receipt['logs']:
        if (not isinstance(log, dict) or address(log.get('address')) != token
                or log.get('removed') is not False or hash32(log.get('transactionHash')) != detail['txHash']
                or integer(log.get('blockNumber')) != int(detail['blockNumber'])):
            continue
        for event in token_abi:
            if event.get('type') != 'event' or event.get('name') not in (
                    'DoorPulled', 'IncomeRecognized', 'IncomeReleased', 'Synced'):
                continue
            try:
                decoded = decoded_event(event, log)
            except (TypeError, ValueError, KeyError, IndexError, RecursionError):
                decoded = None
            if decoded is None:
                continue
            item = {'name': event['name'], 'event': decoded}
            if event['name'] == 'DoorPulled':
                item['door'] = getattr(reader, 'v1_doors', {}).get(decoded.get('door'))
                if item['door'] and decoded.get('ok') is True:
                    detail['doors'].append(item['door'])
            if event['name'] == 'Synced':
                detail['sync'] = True
            detail['v1TokenEvents'].append(item)
    detail['doors'] = sorted(set(detail['doors']))
    for door in doors:
        logs = [log for log in receipt['logs'] if isinstance(log, dict) and address(log.get('address')) == door
                and log.get('removed') is False and hash32(log.get('transactionHash')) == detail['txHash']
                and integer(log.get('blockNumber')) == int(detail['blockNumber'])]
        if not logs:
            continue
        events = [e for e in abi(reader, door) if isinstance(e, dict) and e.get('type') == 'event' and e.get('name') == 'Pulled']
        if not events:
            reader.note('v1 pulled abi unavailable')
        for log in logs:
            for event in events:
                try:
                    decoded = decoded_event(event, log)
                except (TypeError, ValueError, KeyError, IndexError):
                    decoded = None
                if decoded is not None:
                    detail['v1Pulled'].append({'door': door, 'event': decoded, 'origin': 'fee-nft adapter',
                                               'payer': None, 'outsideSync': not detail['sync']})
    # Never interpret unverified 'other' as a currency, infer an NFT payer, or
    # subtract a v2 per-delivery leg from this generation's actual movements.
    detail['legBasis'] = 'minted supply; no v2 delivery subtraction'


def payment(reader, row, door):
    """Prove plain transfers or EIP-3009 x402 settlement by tx and asset log.

    Relayed authorizations with a different funds owner and unknown settlement
    selectors stay unknown. A method name alone is never a payment proof.
    """
    from .hookr_collect import edge
    e = edge(row)
    if not e or e['to'] != door:
        return None
    tx = reader.get_tx(e['txHash'])
    receipt = reader.optional_es(module='proxy', action='eth_getTransactionReceipt', txhash=e['txHash'])
    if (not isinstance(tx, dict) or not isinstance(receipt, dict) or hash32(tx.get('hash')) != e['txHash']
            or hash32(receipt.get('transactionHash')) != e['txHash'] or integer(receipt.get('status')) != 1
            or integer(tx.get('blockNumber')) != int(e['blockNumber'])
            or integer(receipt.get('blockNumber')) != int(e['blockNumber'])
            or address(tx.get('from')) != e['from'] or address(receipt.get('from')) != e['from']
            or address(receipt.get('to')) != address(tx.get('to')) or not isinstance(receipt.get('logs'), list)):
        return None
    calldata = tx.get('input')
    if not isinstance(calldata, str):
        return None
    if e['currency'] == ZERO:
        if address(tx.get('to')) == door and calldata == '0x' and integer(tx.get('value')) == int(e['amount']):
            return {**e, 'paymentKind': 'plain native transfer'}
        return None
    if address(tx.get('to')) != e['currency'] or integer(tx.get('value', '0x0')) != 0:
        return None
    args = words('0x' + calldata[10:])
    plain = (calldata[:10] == '0xa9059cbb' and len(args) == 2
             and word_address(args[0]) == door and int(args[1], 16) == int(e['amount']))
    settlement = False
    if not plain:
        for name in ('transferWithAuthorization', 'receiveWithAuthorization'):
            types = ['address', 'address', 'uint256', 'uint256', 'uint256', 'bytes32', 'uint8', 'bytes32', 'bytes32']
            sig = name + '(' + ','.join(types) + ')'
            if (calldata[:10] == '0x' + keccak256(sig.encode())[:8] and len(args) == 9
                    and word_address(args[0]) == e['from'] and word_address(args[1]) == door
                    and int(args[2], 16) == int(e['amount'])
                    and any(f.get('type') == 'function' and f.get('name') == name
                            and [x.get('type') for x in f.get('inputs', [])] == types
                            for f in abi(reader, e['currency']) if isinstance(f, dict))):
                settlement = True
    if not (plain or settlement):
        return None
    matches = []
    for log in receipt.get('logs', []):
        if not isinstance(log, dict):
            continue
        topics, data = log.get('topics', []), words(log.get('data'))
        if (address(log.get('address')) == e['currency'] and isinstance(topics, list) and len(topics) == 3 and all(hash32(t) for t in topics) and topics[0].lower() == TRANSFER
                and word_address(topics[1][2:]) == e['from'] and word_address(topics[2][2:]) == door
                and len(data) == 1 and int(data[0], 16) == int(e['amount']) and log.get('removed') is False
                and hash32(log.get('transactionHash')) == e['txHash']
                and integer(log.get('blockNumber')) == int(e['blockNumber'])):
            matches.append(log)
    if len(matches) != 1:
        return None
    return {**e, 'paymentKind': 'plain erc20 transfer' if plain else 'eip3009 settlement'}


def agent_payments(reader, sidecar, start):
    """Keep verified incoming payments separate from unproved balance sweeps.

    History is not a balance proof: same-block native trace order, forced sends,
    token hooks and rebases are not reconstructed by this adapter.
    """
    from .hookr_collect import history, edge, relationships, sender_evidence, native_funding, identity_pass, select_income
    identity, window = sidecar['identity'], sidecar['income']['window']
    sidecar['live']['agentPayments'] = []
    for door in identity.get('agentDoors', []):
        data = history(reader, door, start, window['throughBlock'])
        seen = set()
        eligible = [r for r in data['rows'] if (e := edge(r)) and e['to'] == door
                    and e['time'] is not None and window['from'] <= e['time'] <= window['through']]
        unique = {}
        for row in eligible:
            e = edge(row)
            unique.setdefault((e['txHash'], e['from'], e['currency'], e['amount']), row)
        chosen = {tuple(sorted(e.items())) for e in select_income([edge(r) for r in unique.values()], reader.trace_share_bps)}
        ordered = sorted(unique.values(), key=lambda r: (edge(r)['currency'], -int(edge(r)['amount'])))
        for row in ordered:
            e = edge(row)
            if not e or e['to'] != door or e['time'] is None or not window['from'] <= e['time'] <= window['through']:
                continue
            marker = (e['txHash'], e['from'], e['currency'], e['amount'])
            if marker in seen: continue
            seen.add(marker)
            funding = {'source': 'direct-transfer', 'token': identity['token'], 'to': door,
                       'currency': e['currency'], 'amount': e['amount'], 'txHash': e['txHash'],
                       'blockNumber': e['blockNumber'], 'time': e['time'], 'origin': 'agent-door payment'}
            if tuple(sorted(e.items())) not in chosen:
                if len(sidecar['funding']) < 200:
                    sidecar['funding'].append(funding)
                    sidecar['attributions'].append({'line': deepcopy(funding), 'allocations': [],
                                                    'reason': 'below trace threshold'})
                else:
                    reader.hit('direct funding row', 200)
                continue
            proven = payment(reader, row, door)
            exact = bool(proven and native_funding(reader, e, recipient_eoa=False))
            e['nativeFundingVerified'] = exact
            rel = ({'accounts': {}, 'paths': []} if not exact or identity_pass(e['from'], identity) is not None else
                   relationships(reader, e['from'], int(e['blockNumber']), int(e['blockNumber']),
                                 {identity['token'], *identity['doors']}, e['time']))
            ev = identity_pass(e['from'], identity) or sender_evidence(e['from'], identity, rel, False)
            if len(sidecar['funding']) < 200:
                sidecar['funding'].append(funding)
                sidecar['attributions'].append({'line': deepcopy(funding), 'allocations': [{
                    'basis': 'payer-trace', 'complete': True, 'amount': e['amount'], 'path': [e],
                    'origin': 'agent-door payment', 'directFunding': True, 'destination': door,
                    'hubRules': HUB_RULES, 'accounts': rel['accounts'], 'senderEvidence': ev}] if exact else []})
            else:
                reader.hit('direct funding row', 200)
            sidecar['live']['agentPayments'].append({'payment': proven, 'relationship': ev,
                                                    'deliveryAttributed': False})
        reader.note('agent balance interval unresolved')


def yield_arguments(schema, pool, window):
    """Use only arguments advertised by tools/list, never fabricate a cursor."""
    properties = schema.get('properties', {})
    if not isinstance(properties, dict) or 'poolId' not in properties:
        return None
    candidates = {'poolId': pool, 'days': window['days'], 'from': window['from'], 'through': window['through']}
    args = {k: v for k, v in candidates.items() if k in properties}
    if 'window' in properties:
        spec = properties['window']
        if spec.get('type') == 'object':
            fields = spec.get('properties', {})
            if 'from' in fields and 'through' in fields:
                args['window'] = {k: window[k] for k in ('from', 'through')}
        else:
            # Match only an explicitly advertised enum; never guess units.
            choices = spec.get('enum', [])
            value = next((v for v in (f"{window['days']}d", window['days']) if v in choices), None)
            if value is not None:
                args['window'] = value
    for k, v in list(args.items()):
        if properties[k].get('type') == 'string' and type(v) is int:
            args[k] = str(v)
    if any(k not in args for k in schema.get('required', [])):
        return None
    return args


# The live MCP is rolling. Permit at most one hour (0.60% of seven days)
# at each endpoint, recording signed offsets. Amount equality is still exact
# for recapture; tolerance never manufactures missing Donate observations.
YIELD_WINDOW_TOLERANCE_SECONDS = 3600


def yield_rows(raw, pool, page, window):
    """Read raw currency lanes from captured structuredContent, never APR/USD."""
    page.pop('yieldEvidence', None)
    if not isinstance(raw, dict):
        return None
    if 'result' in raw:
        result = raw.get('result')
        if not isinstance(result, dict) or result.get('isError') is True:
            return None
        raw = result.get('structuredContent')
    elif 'structuredContent' in raw:
        if raw.get('isError') is True:
            return None
        raw = raw.get('structuredContent')
    if not isinstance(raw, dict):
        return None
    income = raw.get('income')
    if isinstance(income, dict):
        if (hash32(raw.get('poolId')) != pool or hash32(income.get('poolId')) != pool
                or income.get('status') != 'ready' or len(page.get('currencies', [])) != 2
                or income.get('quoteSide') not in ('currency0', 'currency1')):
            return None
        through = number(income.get('throughTimestamp'))
        block = number(income.get('throughBlock'))
        entries = income.get('windows')
        if through is None or block is None or not isinstance(entries, list):
            return None
        matching = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            since, span = number(entry.get('since')), number(entry.get('spanSeconds'))
            if since is None or span is None or type(entry.get('sinceOpen')) is not bool:
                continue
            if abs(through - window['through']) > YIELD_WINDOW_TOLERANCE_SECONDS:
                continue
            if abs(since - window['from']) > YIELD_WINDOW_TOLERANCE_SECONDS:
                continue
            opened = through - span
            if entry['sinceOpen'] is True:
                lifetime = income.get('lifetime', {})
                if (not window['from'] <= opened <= window['through'] or opened < since
                        or not isinstance(lifetime, dict) or number(lifetime.get('since')) != opened):
                    continue
            elif since + span != through or abs(span - (window['through'] - window['from'])) > YIELD_WINDOW_TOLERANCE_SECONDS:
                continue
            matching.append(entry)
        if len(matching) != 1:
            return None
        entry, lanes = matching[0], {}
        for lane in ('swapFees', 'recapture', 'lpRewards', 'other'):
            values = entry.get('income', {}).get(lane)
            if not isinstance(values, dict):
                return None
            amounts = [number(values.get('amount0')), number(values.get('amount1'))]
            if None in amounts:
                return None
            lanes[lane] = dict(zip(page['currencies'], amounts))
        page['yieldEvidence'] = {'window': deepcopy(entry), 'throughBlock': block,
            'throughTimestamp': through, 'quoteSide': income['quoteSide'],
            'startOffsetSeconds': number(entry['since']) - window['from'],
            'endOffsetSeconds': through - window['through'],
            'toleranceSeconds': YIELD_WINDOW_TOLERANCE_SECONDS, 'lanes': lanes}
        return {c: lanes['swapFees'][c] + lanes['recapture'][c] for c in page['currencies']}
    # Retain the earlier explicit, exactly-bound currency-row contract.
    if not isinstance(raw, dict) or hash32(raw.get('poolId')) != pool or raw.get('status') != 'ready':
        return None
    w = raw.get('window', {})
    if not isinstance(w, dict) or not (
            number(w.get('fromBlock')) == number(page['fromBlock']) and number(w.get('throughBlock')) == number(page['throughBlock'])
            or number(w.get('from')) == number(window['from']) and number(w.get('through')) == number(window['through'])):
        return None
    fees = raw.get('swapFees')
    if isinstance(fees, dict):
        a, b = raw.get('currency0'), raw.get('currency1')
        if isinstance(a, dict): a = a.get('address')
        if isinstance(b, dict): b = b.get('address')
        if [address(a, zero=True), address(b, zero=True)] != page['currencies']:
            return None
        fees = [{'currency': a, 'amount': fees.get('amount0')}, {'currency': b, 'amount': fees.get('amount1')}]
    if not isinstance(fees, list):
        return None
    result = {}
    for row in fees:
        if not isinstance(row, dict): return None
        c, amount = address(row.get('currency'), zero=True), number(row.get('amount'))
        if c not in page['currencies'] or c in result or amount is None: return None
        result[c] = amount
    return result


def prepare_yields(reader, pages, window):
    try:
        tools = reader.mcp('tools/list').get('tools', [])
        schemas = [t.get('inputSchema') for t in tools if isinstance(t, dict) and t.get('name') == 'hookr_pool_yield']
        schema = schemas[0] if len(schemas) == 1 and isinstance(schemas[0], dict) else {}
    except ReadStopped:
        schema = {}
    for pool, page in pages.items():
        raw, reference = None, None
        args = yield_arguments(schema, pool, window)
        if args is not None:
            try:
                raw = reader.mcp('hookr_pool_yield', args)
                reference = yield_rows(raw, pool, page, window)
            except ReadStopped:
                pass
        page['yieldReference'] = reference
        if reference is None:
            reader.note('pool yield unavailable' if raw is None else 'pool yield window unavailable')


def reconcile_pools(reader, sidecar, pages, start):
    results = sidecar['live']['reconciliation'] = []
    if not pages:
        return
    window = sidecar['income']['window']
    if any('yieldReference' not in p for p in pages.values()):
        prepare_yields(reader, pages, window)
    for pool, page in pages.items():
        reference = page.get('yieldReference')
        # The numerator is the delivered allocation, not its normalized weight.
        # Fractional pool splits retain exact rational precision until rounding.
        allocated = defaultdict(Fraction)
        for attr in sidecar['attributions']:
            for part in attr.get('allocations', []):
                if part.get('basis') != 'pool-fee-weight-estimate': continue
                weight = sum(int(t['feeWeight']) for t in part['swaps'] if t['poolId'] == pool)
                allocated[attr['line']['currency']] += Fraction(int(part['amount']) * weight, int(part['weight']))
        # Selection must not shrink the reconciliation denominator. This basis
        # includes all observed deliveries and swaps, with no payer assertion.
        if 'allocationBasis' in page:
            allocated = defaultdict(Fraction, page['allocationBasis'])
        scanned = Counter()
        for t in page['trades']:
            scanned[t['currency']] += int(t['feeAmount'])
        donated = Counter()
        for d in page.get('recaptures', []):
            donated[d['currency']] += int(d['amount'])
        lanes = page.get('yieldEvidence', {}).get('lanes')
        page['reconciliation'] = {}
        for currency in page.get('currencies', []):
            denominator = reference.get(currency) if reference is not None else None
            fee_allocation = allocated[currency]
            combined = fee_allocation + (donated[currency] if lanes else 0)
            amount = combined.numerator // combined.denominator
            status = 'unavailable'
            if denominator is not None and page['complete']:
                status = ('matched' if abs(combined - denominator) * 10000 <=
                          denominator * reader.reconciliation_tolerance_bps else 'mismatch')
            recapture_status = 'unavailable'
            if lanes:
                recapture_status = ('matched' if page.get('donateComplete') is True
                    and donated[currency] == lanes['recapture'][currency] else 'mismatch')
                fee_reference = lanes['swapFees'][currency]
                fee_match = abs(fee_allocation - fee_reference) * 10000 <= fee_reference * reader.reconciliation_tolerance_bps
                if status == 'matched' and (not fee_match or recapture_status != 'matched'):
                    status = 'mismatch'
                if recapture_status != 'matched':
                    reader.note('recapture yield mismatch')
            item = {'poolId': pool, 'currency': currency, 'status': status,
                    'allocatedSwapFees': str(fee_allocation.numerator // fee_allocation.denominator), 'allocationNumerator': str(combined.numerator),
                    'allocationDenominator': str(combined.denominator),
                    'swapFees': str(lanes['swapFees'][currency] if lanes else denominator) if denominator is not None else None,
                    'allocatedSwapFeesAndRecapture': str(amount),
                    'swapFeesAndRecapture': str(denominator) if denominator is not None else None,
                    'donateAmount': str(donated[currency]), 'recaptureStatus': recapture_status,
                    'yieldEvidence': deepcopy(page.get('yieldEvidence')),
                    'scannedFeeProxy': str(scanned[currency]), 'toleranceBps': reader.reconciliation_tolerance_bps,
                    'fromBlock': page['fromBlock'], 'throughBlock': page['throughBlock']}
            results.append(item)
            page['reconciliation'][currency] = item
            for attr in sidecar['attributions']:
                if attr['line']['currency'] != currency: continue
                for part in attr.get('allocations', []):
                    if any(t['poolId'] == pool for t in part.get('swaps', [])):
                        if part.get('basis') == 'pool-donate-recapture-exact':
                            part['recaptureReconciliation'] = deepcopy(item)
                        part.setdefault('reconciliationStatuses', {})[pool] = status
                        part['reconciliationOk'] = all(v == 'matched' for v in part['reconciliationStatuses'].values())
                        if part.get('basis') == 'pool-donate-recapture-exact' and status != 'matched':
                            part['reason'] = 'recapture yield mismatch' if recapture_status == 'mismatch' else 'pool yield unavailable'
            if status != 'matched':
                if status == 'mismatch': reader.note('pool yield mismatch')
                for attr in sidecar['attributions']:
                    if attr['line']['currency'] != currency: continue
                    for part in attr.get('allocations', []):
                        if any(t['poolId'] == pool for t in part.get('swaps', [])):
                            part['reconciliationOk'] = False
                            part['confidence'] = 'low; pool yield ' + status
