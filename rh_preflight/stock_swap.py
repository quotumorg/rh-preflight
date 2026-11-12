"""Value Stock Token swaps using only facts already read for this call."""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext

from .extract import objects, chain_context, _chain
from .hookr_swap import decode
from .sources.common import field, money
from .transactions import addr, integer

RULE = 'stock_swap_above_usd'
PREFIX = 'your rule: Stock Token swap above $'


def token_address(token):
    nested = token.get('token')
    return addr(token.get('address')) or addr(nested.get('address') if isinstance(nested, dict) else nested)


def conflicting_identity(token):
    nested = token.get('token')
    nested_address = addr(nested.get('address') if isinstance(nested, dict) else nested)
    address = addr(token.get('address'))
    return bool(address and nested_address and address != nested_address)


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
    sides = []
    for side, direction in (('In', 'sold'), ('Out', 'bought')):
        original = row['token' + side]
        token = dict(original) if isinstance(original, dict) else {'address': original}
        candidates = [token['raw']] if 'raw' in token else []
        if 'amount' + side + 'Raw' in row:
            candidates.append(row['amount' + side + 'Raw'])
        decimals = [token[k] for k in ('decimals',) if k in token]
        decimals += [row[k] for k in ('decimals' + side, 'token' + side + 'Decimals') if k in row]
        token.update(address=token_address(token), direction=direction,
                     raw=None if conflicting_identity(token) else raw_amount(candidates),
                     decimals=known_decimals(decimals))
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
    if not swaps:
        sides = argument_sides(intent.get('shape') or {})
    spec = decode(intent)
    if not sides and spec:
        sides = [{'address': spec[direction], 'direction': direction} for direction in ('sold', 'bought')]
    if spec and any(token['address'] != spec[token['direction']] for token in sides):
        ambiguous = True

    # Metadata may be used by exact address, but a quote's raw amount belongs
    # to an independent quote, not this call. Deltas can include fees, rebases,
    # refunds, other transfers or several hops, even with the expected sign.
    rows = [*(response.get('coins') or []), *(x.get('coins') or [])]
    rows += [token for row in swaps for direction in ('sold', 'bought') for token in row.get(direction) or []]
    sim = x.get('simulation') or {}
    rows += (sim.get('effects') or sim).get('deltas', [])
    rows += [(warning.get('x-toll-evidence') or {}) for warning in response.get('warnings', [])]
    if hookr.get('status') == 'ok':
        rows += [hookr.get(direction) for direction in ('pays', 'gets')]
    for token in sides:
        address = token['address']
        values = []
        def collect(row):
            if not isinstance(row, dict):
                return
            nested = row.get('token')
            if isinstance(nested, dict):
                collect(nested)
            if token_address(row) == address and 'decimals' in row:
                values.append(row['decimals'])
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


def annotate(cards, args, settings):
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
    if not swaps and not any(card.kind == 'transaction' for card in cards):
        swaps = [(next(i for i, c in enumerate(cards) if c.kind == 'network'), sides)
                 for row in objects(args)
                 if ('chainId' not in row or _chain(row['chainId']) == 4663)
                 and chain_context(args, settings.assume_chain)
                 and not any(key in row for key in ('data', 'input', 'calldata'))
                 if (sides := argument_sides(row))]
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
