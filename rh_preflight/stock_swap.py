"""Value Stock Token swaps using only facts already read for this call."""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import re

from .extract import chain_context
from .hookr_swap import decode
from .sources.common import field, money
from .transactions import (addr, integer, typed_payload, cast_sends, tool_words,
                           read_only_tool, execution_requested, READ_RPC, SIGN_RPC, TOLL_CODES)

RULE = 'stock_swap_above_usd'
PREFIX = 'your rule: Stock Token swap above $'


def action_rows(args):
    """The optional rule accepts only a root action and direct calls entries."""
    if not isinstance(args, dict):
        return []
    def excluded(row):
        rpc = row.get('rpc')
        return (typed_payload(row) or row.get('method') in READ_RPC | SIGN_RPC
                or isinstance(rpc, dict) and rpc.get('method') in READ_RPC | SIGN_RPC)
    if excluded(args):
        return []
    calls = args.get('calls') if not any(key in args for key in ('method', 'rpc', 'command')) else None
    return [row for row in [args, *(calls if isinstance(calls, list) else [])]
            if isinstance(row, dict) and not excluded(row)]


def execution_scope(root, row, tool_name):
    hints = [{str(key).lower().replace('_', ''): value}
             for entry in (root, row) for key, value in entry.items()
             if str(key).lower().replace('_', '') in
             {'quoteonly', 'readonly', 'dryrun', 'simulateonly', 'mode', 'action', 'operation',
              'execute', 'send', 'sign', 'submit', 'broadcast'}]
    return (all(execution_requested([hint]) for hint in hints)
            and (tool_name is None or not read_only_tool(tool_name)
                 or execution_requested([root, row])))


def restrict_transactions(intents, args, settings, tool_name):
    """Match existing intents against only the documented execution envelopes."""
    rows = action_rows(args)
    commands = ' '.join(row['command'] for row in rows if isinstance(row.get('command'), str))
    supported = []
    if chain_context(args, settings.assume_chain, commands):
        for row in rows:
            if not execution_scope(args, row, tool_name):
                continue
            rpc = row.get('rpc', row)
            if isinstance(rpc, dict) and rpc.get('method') == 'eth_sendTransaction':
                if not execution_scope(row, rpc, None):
                    continue
                params = rpc.get('params')
                entries = params if isinstance(params, list) and len(params) == 1 else []
            elif 'method' not in row and 'rpc' not in row:
                entries = [row]
                if isinstance(row.get('command'), str) and re.fullmatch(r'\s*cast[ \t]+send[ \t]+[^;&|$`()<>\n\r]*\s*', row['command']):
                    sends = list(cast_sends(
                        {'chainId': 4663, 'command': row['command']}, settings.assume_chain,
                        execution_only=True))
                    if len(sends) == 1 and sends[0]['payload'] is not None:
                        supported.append(sends[0]['payload'])
            else:
                entries = []
            for entry in entries:
                if (not isinstance(entry, dict) or not addr(entry.get('to'))
                        or not any(key in entry for key in ('data', 'input', 'calldata', 'value'))
                        or not execution_scope(row, entry, None)):
                    continue
                payload = {key: entry[key] for key in ('chainId', 'from', 'to', 'data', 'value') if key in entry}
                payload['data'] = entry.get('data', entry.get('input', entry.get('calldata', '0x')))
                payload.setdefault('chainId', 4663)
                supported.append(payload)
    for intent in intents:
        intent['stock_swap_supported'] = intent['payload'] is not None and intent['payload'] in supported


def token_rows(token):
    while isinstance(token, dict):
        yield token
        token = token.get('token')


def token_addresses(token):
    return {address for row in token_rows(token) for key in ('address', 'token')
            if (address := addr(row.get(key)))}


def token_address(token):
    for row in token_rows(token):
        address = addr(row.get('address')) or addr(row.get('token'))
        if address:
            return address
    return None


def conflicting_identity(token):
    return len(token_addresses(token)) > 1


def raw_amount(candidates):
    # Only explicitly labelled base units. An integer-looking `amount` can
    # equally be a whole-token amount, a minimum, or an exact-output bound.
    values = [integer(value) for value in candidates]
    if not values or any(value is None or not 0 <= value < 2 ** 256 for value in values):
        return None
    return values[0] if len(set(values)) == 1 else None


def argument_sides(row):
    if not {'tokenIn', 'tokenOut'} <= row.keys():
        return []
    ambiguous = any(not (token_address(token) if isinstance(token, dict) else addr(token))
                    or isinstance(token, dict) and conflicting_identity(token)
                    for token in (row['tokenIn'], row['tokenOut']))
    sides = []
    for side, direction in (('In', 'sold'), ('Out', 'bought')):
        original = row['token' + side]
        token = dict(original) if isinstance(original, dict) else {'address': original}
        candidates = [token['raw']] if 'raw' in token else []
        if 'amount' + side + 'Raw' in row:
            candidates.append(row['amount' + side + 'Raw'])
        decimals = [entry['decimals'] for entry in token_rows(token) if 'decimals' in entry]
        decimals += [row[k] for k in ('decimals' + side, 'token' + side + 'Decimals') if k in row]
        if conflicting_identity(token):
            decimals.append('conflicting identity')
        token.update(address=token_address(token), direction=direction,
                     raw=None if ambiguous else raw_amount(candidates),
                     decimals=known_decimals(decimals), _decimal_values=decimals)
        sides.append(token)
    return sides


def known_decimals(values):
    values = [value for value in values if value is not None]
    if not values or any(type(value) is not int or not 0 <= value <= 255 for value in values):
        return None
    return values[0] if len(set(values)) == 1 else None


def transaction_sides(intent, response, hookr, metadata):
    """Preserve side identity and explicit base units; never infer net flows."""
    x = response.get('x-toll') or {}
    swaps = [row for row in x.get('decoded') or [] if row.get('kind') == 'swap']
    sides = []
    ambiguous = len(swaps) > 1
    for row in swaps:
        ambiguous |= bool(row.get('unresolved'))
        for direction in ('sold', 'bought'):
            tokens = row.get(direction) or []
            ambiguous |= len(tokens) > 1
            for original in tokens:
                token = dict(original) if isinstance(original, dict) else {'address': original}
                ambiguous |= conflicting_identity(token)
                ambiguous |= token.get('raw') is not None and raw_amount([token['raw']]) is None
                token.update(address=token_address(token), direction=direction,
                             raw=raw_amount([token['raw']]) if 'raw' in token else None)
                sides.append(token)
    spec = decode(intent)
    if not sides and spec:
        sides = [{'address': spec[direction], 'direction': direction} for direction in ('sold', 'bought')]
    if spec and any(token['address'] != spec[token['direction']] for token in sides):
        ambiguous = True

    # Metadata may be used by exact address, but a quote's raw amount belongs
    # to an independent quote, not this call. Deltas can include fees, rebases,
    # refunds, other transfers or several hops, even with the expected sign.
    rows = [*(response.get('coins') or []), *(x.get('coins') or [])]
    # token_metadata also ingests each decoded row itself, including non-swaps.
    # Inspect those originals before its first-wins projection loses conflicts.
    rows += x.get('decoded') or []
    rows += [token for row in x.get('decoded') or [] for direction in ('sold', 'bought')
             for token in row.get(direction) or []]
    sim = x.get('simulation') or {}
    rows += (sim.get('effects') or sim).get('deltas', [])
    rows += [(warning.get('x-toll-evidence') or {}) for warning in response.get('warnings', [])
             if isinstance(warning, dict) and warning.get('code') in TOLL_CODES]
    if hookr.get('status') == 'ok':
        rows += [hookr.get(direction) for direction in ('pays', 'gets')]
    for token in sides:
        address = token['address']
        values = list(token.pop('_decimal_values', []))
        def collect(row):
            if not isinstance(row, dict):
                return
            if conflicting_identity(row):
                if address in token_addresses(row):
                    values.append('conflicting identity')
                return
            for entry in token_rows(row):
                if token_address(entry) == address and 'decimals' in entry:
                    values.append(entry['decimals'])
        for row in [token, {'address': address, **metadata.get(address, {})}, *rows]:
            collect(row)
        token['decimals'] = known_decimals(values)
        raw = token.get('raw')
        if spec and token['direction'] == 'sold' and address == spec['sold']:
            candidates = [spec['raw']] + ([raw] if raw is not None else [])
            raw = raw_amount(candidates)
        token['raw'] = None if ambiguous else raw
    return sides


def number(value):
    try:
        if isinstance(value, bool) or value is None:
            return None
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def price_time(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace(' UTC', '+00:00').replace('Z', '+00:00'))
        return value if stamp.tzinfo is not None else None
    except ValueError:
        return None


def annotate(cards, args, settings, tool_name=None):
    threshold = settings.ask_when.get(RULE)
    if threshold is None or settings.sources.get('toll', 'on') == 'off':
        return cards
    stocks = {reading.stock_address: reading for card in cards if card.kind == 'network'
              for reading in card.data.get('sources', [])
              if reading.source == 'Toll' and addr(reading.stock_address)}
    if not stocks:
        return cards
    swaps = [(i, card.data['swap_sides']) for i, card in enumerate(cards)
             if card.data.get('swap_sides')]
    rows = action_rows(args)
    if rows and chain_context(args, settings.assume_chain):
        index = next(i for i, card in enumerate(cards) if card.kind == 'network')
        for row in rows:
            if any(key in row for key in ('data', 'input', 'calldata')):
                continue
            if (tool_name is not None and read_only_tool(tool_name)
                    and not execution_requested([args, row])):
                continue
            sides = argument_sides(row)
            if not sides:
                continue
            if any(key in row for key in ('method', 'rpc', 'command')) or not execution_scope(args, row, tool_name):
                sides = [{**token, 'raw': None} for token in sides]
            swaps.append((index, sides))
        # A declared swap with no supported Stock Token side has no readable
        # value. Do not search arbitrary argument objects to explain that fact.
        if (tool_name is not None and {'swap', 'buy', 'sell', 'trade'} & set(tool_words(tool_name).split('_'))
                and not read_only_tool(tool_name)
                and not any(card.target == 'signature on chain 4663' for card in cards)
                and not any(addr(token.get('address')) in stocks for _, sides in swaps for token in sides)):
            swaps.append((index, [{'address': address} for address in stocks]))
    result = list(cards)
    for index, sides in swaps:
        values, missing = [], []
        for token in sides:
            stock = stocks.get(addr(token.get('address')))
            if stock is None:
                continue
            raw = raw_amount([token.get('raw')])
            decimals = token.get('decimals')
            price = number(stock.stock_price_usd)
            absent = []
            if raw is None:
                absent.append('amount')
            if type(decimals) is not int or not 0 <= decimals <= 255:
                absent.append('decimals')
            if price is None or price == 0:
                absent.append('Toll chain price')
            if price_time(stock.stock_price_as_of) is None:
                absent.append('Toll chain price read time')
            if absent:
                missing.extend(absent)
                continue
            with localcontext() as ctx:
                ctx.prec = max(100, len(str(raw)) + len(price.as_tuple().digits) + decimals + 10)
                values.append((Decimal(raw) / Decimal(10) ** decimals * price, stock.stock_price_as_of))
        if not values and not missing:
            continue
        card = result[index]
        data = {**card.data, 'lines': list(card.data.get('lines', []))}
        if missing:
            data['lines'].append('swap value not read: ' + ', '.join(dict.fromkeys(missing)))
        else:
            value, as_of = max(values, key=lambda item: item[0])
            data['lines'].append(f'swap value ${money(value)} (amount × Toll chain price read {field(as_of)})')
            if value > Decimal(str(threshold)):
                data[RULE] = True
        result[index] = replace(card, data=data)
    return result
