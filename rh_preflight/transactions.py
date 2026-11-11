"""Local transaction and EIP-712 facts, plus fixed Toll projections."""
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import re
import json

from .card import Card, WORDING
from .extract import ADDRESS, _chain, objects, json_values, chain_context
from .sources.common import field

PERMIT2 = '0x000000000022d473030f116ddee9f6b43ac78ba3'
MAX = 2 ** 256 - 1
APPROVE = {'095ea7b3', '39509351', '87517c45', 'd505accf', '2b67b570'}
SWAPS = {'19b36adf', '3593564c', '24856bc3', '414bf389', 'c04b8d59', '04e45aaf', 'b858183f', 'db3e2198', 'f28c0498', '5ae401dc', 'ac9650d8'}

# Provider-stable codes only. None deliberately omits a coin judgement.
TOLL_CODES = {
    'unbounded-approval': 'approval',
    'unbounded-approval-to-eoa': 'approval_eoa',
    'approval-for-all-to-eoa': 'approval_all_eoa',
    'swap-pays-other': 'payout_other',
    'swap-deadline-expired': 'expired',
    'sim-reverted': 'reverted',
    'sim-insufficient-eth': 'short_eth',
    'coin-careful': None,
    'coin-against': None,
    'coin-not-read': 'coin_unread',
    'coin-partly-read': 'coin_partial',
    'router-third-party': 'router',
    'eip7702-delegation': 'delegation',
    'typed-data-unread': 'signature_unread',
}


def addr(value):
    return value.lower() if isinstance(value, str) and ADDRESS.fullmatch(value) else None


def integer(value):
    try:
        if isinstance(value, bool):
            return None
        return int(str(value), 16 if str(value).startswith('0x') else 10)
    except (ValueError, TypeError):
        return None


def date(value):
    try:
        return datetime.fromtimestamp(integer(value), timezone.utc).isoformat().replace('+00:00', 'Z')
    except (TypeError, ValueError, OverflowError, OSError):
        return 'not recorded'


def unlimited(value, bits=256):
    return integer(str(value).split(' ', 1)[0]) == 2 ** bits - 1


def typed_payload(row):
    return isinstance(row, dict) and isinstance(row.get('domain'), dict) and isinstance(row.get('types'), dict) and isinstance(row.get('message'), dict) and isinstance(row.get('primaryType'), str)


def tx_intents(args, assume_chain=None):
    """Project only unsigned transaction fields or the supported wallet request."""
    rows = list(objects(args))
    # A signing domain is evidence about the signature, not the call's network.
    def context(value):
        if typed_payload(value):
            return {}
        if isinstance(value, dict):
            return {k: context(v) for k, v in value.items()}
        if isinstance(value, list):
            return [context(v) for v in value]
        if isinstance(value, str):
            parsed = list(json_values(value))
            return [context(v) for v in parsed] if parsed else value
        return value
    call_context = context(args)
    command = ' '.join(_strings(args))
    on_chain = chain_context(call_context, assume_chain, command)
    explicit_call_chain = any(str(key).lower().replace('_', '') in {'chain', 'chainid'}
                              for row in objects(call_context) for key in row)
    explicit_call_chain |= bool(re.search(r'--chain(?:-id)?(?:=|\s+)', command))
    def typed_chain(typed):
        return on_chain or (not explicit_call_chain and _chain(typed['domain'].get('chainId')) == 4663)
    result, seen = [], set()
    for row in rows:
        rpc = row.get('rpc')
        if not isinstance(rpc, dict) or rpc.get('method') not in {'eth_sendTransaction', 'eth_signTypedData_v4', 'personal_sign'}:
            continue
        params = rpc.get('params')
        if not isinstance(params, list):
            continue
        if rpc['method'] == 'eth_sendTransaction':
            continue  # The nested unsigned object is handled below.
        typed = next((v for p in params for v in objects(p) if typed_payload(v)), None)
        if not on_chain and not (typed and typed_chain(typed)):
            continue
        signer_at = 1 if rpc['method'] == 'personal_sign' else 0
        sender = addr(params[signer_at]) if len(params) > signer_at else None
        key = json.dumps(rpc, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append({'payload': {'rpc': {'method': rpc['method'], 'params': params}}, 'typed': typed, 'sender': sender})
    for row in rows:
        if typed_payload(row):
            # Avoid the standalone duplicate of a wallet's typed-data payload.
            if any(item.get('typed') == row for item in result):
                continue
            if typed_chain(row):
                result.append({'payload': None, 'typed': row, 'sender': None})
        elif addr(row.get('to')) and isinstance(row.get('data'), str) and re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*', row['data']):
            declared = _chain(row.get('chainId')) if 'chainId' in row else None
            if declared != 4663 and ('chainId' in row or not on_chain):
                continue
            payload = {k: row[k] for k in ('chainId', 'from', 'to', 'data', 'value') if k in row}
            payload.setdefault('chainId', 4663)
            if any(item['payload'] == payload for item in result):
                continue
            result.append({'payload': payload, 'typed': None, 'sender': addr(row.get('from'))})
    # Swap-shaped arguments without calldata still expose a local recipient fact.
    for row in rows:
        if on_chain and {'tokenIn', 'tokenOut', 'amount', 'recipient'} <= row.keys() and 'data' not in row:
            result.append({'payload': None, 'typed': None, 'sender': addr(row.get('from') or row.get('wallet')), 'shape': row})
    return result


def _strings(value):
    from .extract import walk
    return walk(value)


def local_card(intent):
    payload, typed, sender = intent['payload'], intent.get('typed'), intent.get('sender')
    data = {'lines': [], 'approvals': [], 'permit': False, 'wrong_chain': False,
            'payout_other': False, 'moves': False, 'unchecked': False}
    lines = data['lines']
    target = 'signature on chain 4663'
    if typed:
        domain, message = typed['domain'], typed['message']
        token = addr(domain.get('verifyingContract')) or 'not recorded'
        primary = field(typed['primaryType'])
        permit2 = token == PERMIT2
        permit = permit2 or 'permit' in primary.lower() or any(
            k in {'spender', 'allowance'} for row in objects(message) for k in row)
        data['permit'] = permit
        data['moves'] = permit
        data['wrong_chain'] = 'chainId' in domain and _chain(domain['chainId']) != 4663
        lines.append(f"EIP-712 domain {field(domain.get('name'))}, chainId {field(domain.get('chainId'))}, verifyingContract {token}; primaryType {primary}" + ('; Permit2' if permit2 else ''))
        for row in objects(message):
            spender = addr(row.get('spender'))
            if spender:
                detail = message.get('details') or {}
                details = detail if isinstance(detail, list) else [detail]
                for entry in details:
                    entry = entry if isinstance(entry, dict) else {}
                    quantity = row.get('value', row.get('amount', entry.get('amount')))
                    token_address = addr(entry.get('token')) or token
                    is_unlimited = unlimited(quantity, 160 if permit2 else 256)
                    amount = 'unlimited' if is_unlimited else field(quantity)
                    deadline = message.get('deadline', message.get('sigDeadline', entry.get('expiration')))
                    lines.append(f'signs a {primary} for {token_address}: spender {spender}, amount {amount}, deadline {date(deadline)}, nonce {field(row.get("nonce", entry.get("nonce")))}')
                    data['approvals'].append({'spender': spender, 'token': token_address, 'unlimited': is_unlimited, 'kind': 'not read'})
    elif payload and 'to' in payload:
        target = payload['to'].lower()
        raw = payload['data'][2:].lower()
        selector, words = raw[:8], [raw[i:i+64] for i in range(8, len(raw), 64)]
        lines.append(f'unsigned transaction to {target} on chain 4663')
        data['moves'] = integer(payload.get('value', 0)) not in {0, None} or selector in SWAPS | APPROVE or selector in {'a9059cbb', '23b872dd'}
        positions = {'095ea7b3': (0, 1, None), '39509351': (0, 1, None),
                     '87517c45': (1, 2, 0), 'd505accf': (1, 2, None),
                     '2b67b570': (5, 2, 1)}
        if selector in positions:
            spender_at, amount_at, token_at = positions[selector]
            if len(words) > max(spender_at, amount_at, token_at or 0):
                spender = '0x' + words[spender_at][-40:]
                quantity = int(words[amount_at], 16)
                token = '0x' + words[token_at][-40:] if token_at is not None else target
                data['approvals'].append({'spender': spender, 'token': token, 'unlimited': unlimited(quantity, 160 if selector in {'87517c45', '2b67b570'} else 256), 'kind': 'not read'})
                data['permit'] = selector in {'d505accf', '2b67b570'}
        # HookrRouter's fixture calldata stores recipient and deadline here.
        if selector == '19b36adf' and len(words) >= 12:
            recipient = '0x' + words[9][-40:]
            _payee(data, recipient, sender)
        if selector in {'a9059cbb', '23b872dd'} and len(words) >= 2:
            _payee(data, '0x' + words[0 if selector == 'a9059cbb' else 1][-40:], sender)
    else:
        shape = intent.get('shape') or {}
        if shape:
            target = 'swap on chain 4663'
            lines.append('swap arguments without calldata; no transaction simulation requested')
            _payee(data, addr(shape.get('recipient')), sender)
        else:
            method = (payload or {}).get('rpc', {}).get('method', 'personal_sign')
            lines.append(f'{method} request on chain 4663')
            if method == 'eth_signTypedData_v4':
                data['unchecked'] = True
                lines.append('typed data could not be decoded locally')
    return Card(target, 'transaction', data)


def _payee(data, payee, sender):
    if payee and sender and payee != sender:
        data['payout_other'] = True
        line = f'pays out to {payee}, not the sending wallet'
        if line not in data['lines']:
            data['lines'].append(line)


def short_address(token):
    return token[:6] + '…' + token[-4:]


def token_metadata(response, x):
    """Join structured metadata by address, never by a warning's message."""
    metadata = {}

    def add(row):
        if not isinstance(row, dict):
            return
        token = row.get('token')
        if isinstance(token, dict):
            add(token)
            token = token.get('address')
        token = addr(token) or addr(row.get('address'))
        if not token:
            return
        entry = metadata.setdefault(token, {})
        symbol = row.get('symbol', row.get('sym'))
        if (isinstance(symbol, str) and re.fullmatch(r'\$?[A-Za-z0-9._-]{1,24}', symbol)
                and not WORDING.search(symbol)):
            entry['symbol'] = symbol
        decimals = row.get('decimals')
        if type(decimals) is int and 0 <= decimals <= 255:
            entry['decimals'] = decimals

    for container in (response, x):
        for row in container.get('coins') or []:
            add(row)
    for row in x.get('decoded') or []:
        add(row)
        for key in ('bought', 'sold'):
            for token in row.get(key) or []:
                add(token)
    return metadata, add


def project(card, intent, response=None, kinds=None):
    """Never retain remote prose, decisions, reports or scanner classifications."""
    from copy import deepcopy
    d = deepcopy(card.data)
    kinds = kinds or {}
    response = response if isinstance(response, dict) else {}
    x = response.get('x-toll') or {}
    warnings = [w for w in response.get('warnings', [])
                if isinstance(w, dict) and w.get('code') in TOLL_CODES]
    codes = {w['code'] for w in warnings}
    metadata, add_metadata = token_metadata(response, x)
    sender = intent.get('sender')

    def approval(row, force_unlimited=False, no_code=False):
        spender = addr(row.get('spender'))
        if not spender:
            return False
        token = addr(row.get('token')) or 'not recorded'
        existing = next((a for a in d['approvals'] if a['spender'] == spender and a['token'] == token), None)
        if existing is None:
            existing = {'spender': spender, 'token': token,
                        'unlimited': unlimited(row.get('amount')), 'kind': 'not read'}
            d['approvals'].append(existing)
        existing['unlimited'] |= force_unlimited
        if no_code:
            existing['kind'] = 'no code'
        return True

    for row in x.get('decoded') or []:
        if row.get('kind') == 'approve':
            approval(row)
        for payee in row.get('payees', []):
            _payee(d, addr(payee), sender)
    sim = x.get('simulation') or {}
    effects = sim.get('effects') or sim
    for row in effects.get('approvals', []):
        approval(row)
    for item in d['approvals']:
        item['kind'] = kinds.get(item['spender'], 'not read')
    for warning in warnings:
        code = warning['code']
        evidence = warning.get('x-toll-evidence') or {}
        if code in {'unbounded-approval', 'unbounded-approval-to-eoa', 'approval-for-all-to-eoa'}:
            no_code = code != 'unbounded-approval'
            matched = approval(evidence, force_unlimited=True, no_code=no_code)
            if not matched and len(d['approvals']) == 1:
                d['approvals'][0]['unlimited'] = True
                if no_code:
                    d['approvals'][0]['kind'] = 'no code'
            elif not matched:
                d['approval_eoa' if no_code else 'unbounded_approval'] = True
                d['lines'].append('approves unlimited spending to a spender with no code; spender not recorded'
                                  if no_code else 'approves unlimited spending; spender not recorded')
        elif code == 'swap-pays-other' and not d['payout_other']:
            _payee(d, addr(evidence.get('payee')), sender)
            if not d['payout_other']:
                d['payout_other'] = True
                d['lines'].append('pays out to another wallet; recipient not recorded')
        elif code in {'coin-not-read', 'coin-partly-read'}:
            token = addr(evidence.get('token'))
            add_metadata(evidence)
            label = metadata.get(token, {}).get('symbol') or (short_address(token) if token else 'this coin')
            if code == 'coin-not-read':
                d['lines'].append(f'Toll has not read {label} yet')
            else:
                d['lines'].append(f'Toll could not read all findings for {label}')
                d['unchecked'] = True
        elif code == 'router-third-party':
            router = addr(evidence.get('router'))
            if router:
                d['lines'].append(f'transaction uses third-party router {router}')
        elif code == 'eip7702-delegation':
            delegate = addr(evidence.get('delegate'))
            d['delegation'] = True
            d['lines'].append('Toll: EIP-7702 delegation to ' + (delegate or 'delegate not recorded'))
        elif code == 'typed-data-unread':
            d['unchecked'] = True
            d['lines'].append('Toll could not decode this signature')
    for item in d['approvals']:
        quantity = 'unlimited' if item['unlimited'] else 'a bounded amount of'
        d['lines'].append(f"approves {quantity} {item['token']} to {item['spender']}; spender {item['kind']}")
    d['reverted'] = effects.get('status') == 'reverted' or 'sim-reverted' in codes
    d['expired'] = 'swap-deadline-expired' in codes
    d['short_eth'] = sim.get('status') == 'insufficient-eth' or 'sim-insufficient-eth' in codes
    if d['short_eth']:
        d['lines'].append('sender short of ETH for this transaction')
    if d['reverted']:
        d['lines'].append('simulation reverted')
    if d['expired']:
        d['lines'].append('transaction deadline passed')
    simulated = sim.get('status') == 'success' or d['reverted'] or d['short_eth']
    if simulated and not d['reverted'] and not d['short_eth']:
        d['lines'].append(f"Toll: simulated at block {field(sim.get('block'))}")
    if not simulated and intent['payload'] is not None:
        d['unchecked'] = True
        d['lines'].append('Toll: not simulated')
    for delta in effects.get('deltas', []):
        if addr(delta.get('holder')) != sender or sender is None:
            continue
        token = addr(delta.get('token'))
        number = integer(delta.get('delta'))
        if token and number is not None:
            add_metadata(delta)
            info = metadata.get(token, {})
            decimals = info.get('decimals')
            label = info.get('symbol') or short_address(token)
            if token in {'0x' + 'e' * 40, '0x' + '0' * 40}:
                decimals, label = 18, 'ETH'
            if decimals is not None:
                with localcontext() as ctx:
                    ctx.prec = max(90, len(str(abs(number))) + decimals + 1)
                    quantity = format(Decimal(number).scaleb(-decimals), '+f')
            else:
                quantity, label = f'{number:+d}', label + ' base units'
            d['lines'].append(f'simulated sender balance change: {quantity} {label}')
    if response or intent['payload'] is not None:
        d['lines'].append('Toll · https://t0ll.xyz')
    return Card(card.target, card.kind, d)
