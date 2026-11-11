"""Local transaction and EIP-712 facts, plus fixed Toll projections."""
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import re
import json
import shlex

from .card import Card, WORDING, text, link
from .extract import ADDRESS, _chain, objects, json_values, chain_context, robinhood_rpc
from .sources.common import field

PERMIT2 = '0x000000000022d473030f116ddee9f6b43ac78ba3'
MAX = 2 ** 256 - 1
APPROVE = {'095ea7b3', '39509351', '87517c45', 'd505accf', '2b67b570', 'a22cb465', '8fcbaf0c'}
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
        return int(str(value), 16 if str(value).lower().startswith('0x') else 10)
    except (ValueError, TypeError):
        return None


def date(value):
    try:
        return datetime.fromtimestamp(integer(value), timezone.utc).isoformat().replace('+00:00', 'Z')
    except (TypeError, ValueError, OverflowError, OSError):
        return 'not recorded'


def unlimited(value, bits=256):
    return integer(str(value).split(' ', 1)[0]) == 2 ** bits - 1


def cast_sends(args, assume_chain):
    """Read literal cast sends without executing, expanding or retaining secrets."""
    signatures = {'approve(address,uint256)': ('095ea7b3', ('address', 'uint')),
                  'increaseAllowance(address,uint256)': ('39509351', ('address', 'uint')),
                  'transfer(address,uint256)': ('a9059cbb', ('address', 'uint')),
                  'transferFrom(address,address,uint256)': ('23b872dd', ('address', 'address', 'uint')),
                  'setApprovalForAll(address,bool)': ('a22cb465', ('address', 'bool'))}
    for literal in _strings(args):
        if not re.search(r'\bcast\s+send\b', literal):
            continue
        try:
            lexer = shlex.shlex(literal, posix=True, punctuation_chars=';&|\n')
            lexer.whitespace = ' \t\r'
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            continue
        for index in range(len(tokens) - 1):
            if tokens[index:index + 2] != ['cast', 'send']:
                continue
            segment = []
            for token in tokens[index + 2:]:
                if token and all(c in ';&|\n' for c in token):
                    break
                segment.append(token)
            command = ' '.join(segment)
            if not chain_context(args, assume_chain, command):
                continue
            options, positional = {}, []
            i = 0
            flags = {'--async', '--legacy', '--unlocked', '--json', '--create', '--help'}
            while i < len(segment):
                token = segment[i]
                if token.startswith('-'):
                    key, equal, value = token.partition('=')
                    if key not in flags and not equal and i + 1 < len(segment):
                        i += 1
                        value = segment[i]
                    options[key] = value
                else:
                    positional.append(token)
                i += 1
            target = addr(positional[0]) if positional else None
            sender = addr(options.get('--from', options.get('-f')))
            payload = {'chainId': 4663, 'to': target, 'data': options.get('--data', '0x')}
            if sender:
                payload['from'] = sender
            if '--value' in options:
                # Cast accepts decimal unit suffixes. Only send exact base units.
                value = options['--value']
                match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)(ether|gwei|wei)', value)
                if match:
                    value = Decimal(match[1]) * 10 ** {'ether': 18, 'gwei': 9, 'wei': 0}[match[2]]
                    value = str(int(value)) if value == int(value) else None
                payload['value'] = value
            decoded = target is not None and '--create' not in options
            if len(positional) > 1:
                signature = positional[1]
                if re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*', signature) and len(positional) == 2:
                    payload['data'] = signature
                else:
                    spec = signatures.get(signature)
                    words = []
                    if spec and len(positional[2:]) == len(spec[1]):
                        for kind, value in zip(spec[1], positional[2:]):
                            number = (integer(value) if kind == 'uint' else
                                      int(value, 16) if kind == 'address' and addr(value) else
                                      int(value == 'true') if kind == 'bool' and value in {'true', 'false'} else None)
                            if number is None or not 0 <= number <= MAX:
                                break
                            words.append(f'{number:064x}')
                    if spec and len(words) == len(spec[1]):
                        payload['data'] = '0x' + spec[0] + ''.join(words)
                    else:
                        decoded = False
            decoded &= isinstance(payload['data'], str) and bool(re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*', payload['data']))
            decoded &= 'value' not in payload or integer(payload['value']) is not None
            yield {'payload': payload if decoded else None, 'typed': None, 'sender': sender,
                   **({'unparsed_send': True} if not decoded else {})}


def typed_payload(row):
    return isinstance(row, dict) and isinstance(row.get('domain'), dict) and isinstance(row.get('types'), dict) and isinstance(row.get('message'), dict) and isinstance(row.get('primaryType'), str)


def read_only_tool(tool_name):
    """Recognize declared reads, without exempting combined execution tools."""
    name = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', tool_name.rsplit('__', 1)[-1]).lower()
    words = set(re.split(r'[^a-z0-9]+', name))
    if words & {'execute', 'send', 'sign', 'approve', 'permit', 'transfer', 'pay', 'submit', 'broadcast'}:
        return False
    if words & {'and', 'then'} and words & {'swap', 'buy', 'sell', 'trade', 'mint', 'deposit', 'stake', 'bridge'}:
        return False
    return bool(re.match(r'^(?:get|read|search|research|fetch|quote|price|estimate|browse)(?:_|$)', name)
                or name in {'web_search', 'web_fetch', 'plugins_install', 'plugin_install',
                            'install_plugin', 'mcp_add'})


WRITE_RPC = {'eth_sendTransaction', 'eth_sendRawTransaction', 'eth_signTypedData_v4', 'personal_sign'}
READ_RPC = {'eth_call', 'eth_estimateGas', 'eth_createAccessList', 'debug_traceCall', 'wallet_simulateCalls'}


def intent_rows(value, hypothetical=False, depth=0):
    """Skip simulated params, while retaining independent nested write requests."""
    if depth > 24:
        return
    if isinstance(value, dict):
        method = value.get('method')
        if method in WRITE_RPC:
            hypothetical = False
        if not hypothetical and method not in READ_RPC:
            yield value
        for key, child in value.items():
            yield from intent_rows(child, hypothetical or (method in READ_RPC and key == 'params'), depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from intent_rows(child, hypothetical, depth + 1)
    elif isinstance(value, str):
        for parsed in json_values(value):
            yield from intent_rows(parsed, hypothetical, depth + 1)


def execution_requested(rows):
    for row in rows:
        for key, value in row.items():
            key = str(key).lower().replace('_', '')
            if key in {'execute', 'send', 'sign', 'submit', 'broadcast'} and value is True:
                return True
            if key in {'quoteonly', 'readonly', 'dryrun', 'simulateonly'} and value is False:
                return True
            if key in {'mode', 'action', 'operation'} and isinstance(value, str) and value.lower() in {
                    'execute', 'send', 'sign', 'submit', 'broadcast', 'swap', 'approve', 'transfer', 'pay'}:
                return True
    return False


def tx_intents(args, assume_chain=None, tool_name=None):
    """Project only unsigned transaction fields or the supported wallet request."""
    rows = list(intent_rows(args))
    explicit_write = any(row.get('method') in WRITE_RPC for row in rows)
    # Concrete structured transactions on quote tools differ from document or
    # research examples. Content reads still yield to explicit execution evidence.
    content_read = tool_name is not None and bool(re.search(
        r'(?:^|__)(?:read_file|web_search|web_fetch|research|browse)(?:_|$)', tool_name, re.I))
    unsigned = any(addr(row.get('to')) and any(k in row for k in ('data', 'input', 'calldata', 'value'))
                   for row in rows)
    if (tool_name is not None and read_only_tool(tool_name) and not explicit_write
            and not execution_requested(rows) and not (unsigned and not content_read)
            and not any(cast_sends(args, assume_chain))):
        return []
    # JSON-RPC tools commonly expose method/params directly, without rpc.
    rows += [{'rpc': row} for row in rows if row.get('method') in
             {'eth_sendTransaction', 'eth_signTypedData_v4', 'personal_sign'}]
    rows += [{**tx, 'data': tx.get('data', tx.get('input', tx.get('calldata', '0x')))}
             for row in rows for rpc in [row.get('rpc')]
             if isinstance(rpc, dict) and rpc.get('method') == 'eth_sendTransaction'
             and isinstance(rpc.get('params'), list)
             for tx in rpc['params'] if isinstance(tx, dict)]
    typed_children = {id(child) for row in rows if typed_payload(row)
                      for child in objects(row) if child is not row}
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
            # Preserve a shell RPC URL without reintroducing a serialized
            # signing domain into the call's explicit chain context.
            return ([context(v) for v in parsed] +
                    (['https://rpc.mainnet.chain.robinhood.com'] if robinhood_rpc(value) else [])) if parsed else value
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
    # Signed bytes cannot be submitted to Toll's unsigned simulation route.
    # Gate the broadcast locally and never retain or relay its signature.
    if on_chain and any(row.get('method') == 'eth_sendRawTransaction' for row in rows):
        result.append({'payload': None, 'typed': None, 'sender': None, 'unparsed_send': True})
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
        elif id(row) not in typed_children and addr(row.get('to')) and any(k in row for k in ('data', 'input', 'calldata', 'value')):
            declared = _chain(row.get('chainId')) if 'chainId' in row else None
            if declared != 4663 and ('chainId' in row or not on_chain):
                continue
            payload = {k: row[k] for k in ('chainId', 'from', 'to', 'data', 'value') if k in row}
            payload['data'] = row.get('data', row.get('input', row.get('calldata', '0x')))
            if not isinstance(payload['data'], str) or not re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*', payload['data']):
                result.append({'payload': None, 'typed': None, 'sender': addr(row.get('from')), 'unparsed_send': True})
                continue
            payload.setdefault('chainId', 4663)
            if any(item['payload'] == payload for item in result):
                continue
            result.append({'payload': payload, 'typed': None, 'sender': addr(row.get('from'))})
    # Action arguments are not an unsigned transaction. Keep their local facts,
    # but never treat an unavailable simulation as a completed check.
    for row in rows:
        if (not on_chain or typed_payload(row) or id(row) in typed_children
                or any(k in row for k in ('data', 'input', 'calldata'))):
            continue
        amount = next((row[k] for k in ('amount', 'amountIn', 'amountOut', 'value', 'allowance') if k in row), None)
        if amount is None:
            continue
        if {'tokenIn', 'tokenOut'} <= row.keys():
            kind = 'swap'
        elif addr(row.get('spender')):
            kind = 'approval'
        elif any(addr(row.get(k)) for k in ('recipient', 'payTo', 'payee', 'to')):
            if addr(row.get('to')) and 'value' in row:
                continue  # Already represented by the unsigned native transaction.
            kind = 'payment'
        else:
            continue
        result.append({'payload': None, 'typed': None,
                       'sender': addr(row.get('from') or row.get('wallet')),
                       'shape': row, 'shape_kind': kind, 'shape_amount': amount})
    result.extend(cast_sends(args, assume_chain))
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
        if sender is None:
            data['unchecked'] = True
            lines.append('signing wallet not recorded; sender effects could not be checked')
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
                    allowed = row.get('allowed')
                    is_unlimited = allowed is True or unlimited(quantity, 160 if permit2 else 256)
                    amount = 'unlimited' if is_unlimited else '0' if allowed is False else field(quantity)
                    deadline = message.get('deadline', message.get('sigDeadline', message.get('expiry', entry.get('expiration'))))
                    deadline_text = ('no expiry' if integer(deadline) == MAX or
                                     'expiry' in message and integer(deadline) == 0 else date(deadline))
                    lines.append(f'signs a {primary} for {token_address}: spender {spender}, amount {amount}, deadline {deadline_text}, nonce {field(row.get("nonce", entry.get("nonce")))}')
                    data['approvals'].append({'spender': spender, 'token': token_address, 'unlimited': is_unlimited, 'kind': 'not read'})
    elif payload and 'to' in payload:
        target = payload['to'].lower()
        raw = payload['data'][2:].lower()
        selector, words = raw[:8], [raw[i:i+64] for i in range(8, len(raw), 64)]
        lines.append(f'unsigned transaction to {target} on chain 4663')
        if sender is None:
            data['unchecked'] = True
            lines.append('sending wallet not recorded; sender effects could not be checked')
        data['moves'] = integer(payload.get('value', 0)) not in {0, None} or selector in SWAPS | APPROVE or selector in {'a9059cbb', '23b872dd'}
        positions = {'095ea7b3': (0, 1, None), '39509351': (0, 1, None),
                     '87517c45': (1, 2, 0), 'd505accf': (1, 2, None),
                     '2b67b570': (5, 2, 1), 'a22cb465': (0, 1, None),
                     '8fcbaf0c': (1, 4, None)}
        if selector in positions:
            spender_at, amount_at, token_at = positions[selector]
            if len(words) > max(spender_at, amount_at, token_at or 0):
                spender = '0x' + words[spender_at][-40:]
                quantity = int(words[amount_at], 16)
                token = '0x' + words[token_at][-40:] if token_at is not None else target
                all_items = selector == 'a22cb465' and quantity != 0
                is_unlimited = (quantity != 0 if selector == '8fcbaf0c' else all_items or
                                unlimited(quantity, 160 if selector in {'87517c45', '2b67b570'} else 256))
                data['approvals'].append({'spender': spender, 'token': token, 'unlimited': is_unlimited, 'kind': 'not read',
                                          **({'all_items': True} if selector == 'a22cb465' else {})})
                data['permit'] = selector in {'d505accf', '2b67b570', '8fcbaf0c'}
        # HookrRouter's fixture calldata stores recipient and deadline here.
        if selector == '19b36adf' and len(words) >= 12:
            recipient = '0x' + words[9][-40:]
            _payee(data, recipient, sender)
        if selector in {'a9059cbb', '23b872dd'} and len(words) >= 2:
            _payee(data, '0x' + words[0 if selector == 'a9059cbb' else 1][-40:], sender)
        if not raw and integer(payload.get('value', 0)) not in {None, 0}:
            _payee(data, target, sender)
    else:
        shape = intent.get('shape') or {}
        if intent.get('unparsed_send'):
            target = 'transaction on chain 4663'
            data['unchecked'] = True
            lines.append('transaction arguments could not be decoded locally')
        elif shape:
            kind = intent.get('shape_kind', 'swap')
            target = kind + ' on chain 4663'
            data['unchecked'] = True
            data['moves'] = True
            lines.append(kind + ' arguments without calldata; no transaction simulation requested')
            if kind == 'approval':
                data['approvals'].append({'spender': addr(shape['spender']),
                    'token': addr(shape.get('token')) or 'not recorded',
                    'unlimited': unlimited(intent.get('shape_amount')), 'kind': 'not read'})
            else:
                payee = next((addr(shape.get(k)) for k in ('recipient', 'payTo', 'payee', 'to')
                              if addr(shape.get(k))), None)
                _payee(data, payee, sender)
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
            entry.setdefault('symbol', symbol)
        decimals = row.get('decimals')
        if type(decimals) is int and 0 <= decimals <= 255:
            entry.setdefault('decimals', decimals)

    for container in (response, x):
        for row in container.get('coins') or []:
            add(row)
    for row in x.get('decoded') or []:
        add(row)
        for key in ('bought', 'sold'):
            for token in row.get(key) or []:
                add(token)
    return metadata, add


def project(card, intent, response=None, kinds=None, hookr=None, coin_findings="silent"):
    """Project fixed facts, metadata and the first Hookr refusal sentence only."""
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
    hookr = hookr if isinstance(hookr, dict) else {}
    if hookr.get('status') == 'ok':
        for name in ('pays', 'gets'):
            add_metadata(hookr.get(name))
        d['lines'].append("Hookr: this pool's rules allow this wallet")
    elif hookr.get('status') == 'refused':
        d['hookr_refused'] = True
        reason = hookr.get('reason')
        reason = re.split(r'(?<=[.!?])\s', reason.strip(), maxsplit=1)[0] if isinstance(reason, str) else 'reason not recorded'
        d['lines'].append("Hookr: this pool's rules refuse this wallet: " + text(reason))
    if coin_findings == 'line':
        listed = {addr((w.get('x-toll-evidence') or {}).get('token')) for w in warnings
                  if w['code'] in {'coin-careful', 'coin-against'}} - {None}
        coins = [c for c in x.get('coins') or [] if isinstance(c, dict)]
        coins += [{'token': t} for t in sorted(listed) if not any(addr(c.get('token')) == t for c in coins)]
        seen = set()
        for coin in coins:
            if not isinstance(coin, dict):
                continue
            tally = coin.get('tally') or {}
            tally = tally if isinstance(tally, dict) else {}
            token = addr(coin.get('token'))
            if (token in seen or (token not in listed and
                    not any(type(tally.get(k)) is int and tally[k] > 0 for k in ('against', 'careful')))):
                continue
            seen.add(token)
            label = metadata.get(token, {}).get('symbol') or (short_address(token) if token else 'this coin')
            page = coin.get('check')
            public = link(page) if isinstance(page, str) and page.startswith('https://t0ll.xyz/check/') else None
            d['lines'].append(f'Toll lists findings for {label}' + (' · ' + public if public else ''))

    def approval(row, force_unlimited=False, no_code=False, all_items=False):
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
        if all_items:
            existing['all_items'] = True
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
            all_items = code == 'approval-for-all-to-eoa'
            matched = approval(evidence, force_unlimited=True, no_code=no_code, all_items=all_items)
            if not matched and len(d['approvals']) == 1:
                d['approvals'][0]['unlimited'] = True
                if no_code:
                    d['approvals'][0]['kind'] = 'no code'
                if all_items:
                    d['approvals'][0]['all_items'] = True
            elif not matched:
                d['approval_all_eoa' if all_items else 'approval_eoa' if no_code else 'unbounded_approval'] = True
                d['lines'].append('approves all items in a collection to a spender with no code; spender not recorded'
                                  if all_items else 'approves unlimited spending to a spender with no code; spender not recorded'
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
        grant = f"all items in collection {item['token']}" if item.get('all_items') else f"{quantity} {item['token']}"
        if item.get('all_items') and not item['unlimited']:
            d['lines'].append(f"revokes approval for {grant} from {item['spender']}; spender {item['kind']}")
        else:
            d['lines'].append(f"approves {grant} to {item['spender']}; spender {item['kind']}")
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
