"""Value Stock Token swaps using only facts already read for this call."""

from dataclasses import replace
from decimal import Decimal, InvalidOperation, localcontext

from .extract import objects
from .hookr_swap import decode
from .sources.common import field, money
from .transactions import addr, integer

RULE = 'stock_swap_above_usd'
PREFIX = 'your rule: Stock Token swap above $'


def argument_sides(row):
    if not {'tokenIn', 'tokenOut'} <= row.keys():
        return []
    sides = []
    for side in ('In', 'Out'):
        token = row['token' + side]
        token = dict(token) if isinstance(token, dict) else {'address': token}
        token.setdefault('decimals', row.get('decimals' + side, row.get('token' + side + 'Decimals')))
        token['raw'] = row.get('amount' + side, row.get('amount') if side == 'In' else None)
        sides.append(token)
    return sides


def transaction_sides(intent, response, hookr, metadata):
    """Preserve amounts and metadata, without keeping raw transaction arguments."""
    x = response.get('x-toll') or {}
    sides = []
    for row in x.get('decoded') or []:
        if row.get('kind') == 'swap':
            for side in ('sold', 'bought'):
                for token in row.get(side) or []:
                    sides.append(dict(token) if isinstance(token, dict) else {'address': token})
    if not sides:
        sides = argument_sides(intent.get('shape') or {})
    spec = decode(intent)
    if not sides and spec:
        sides = [{'address': spec[side]} for side in ('sold', 'bought')]
    simulation = x.get('simulation') or {}
    effects = simulation.get('effects') or simulation
    for token in sides:
        nested = token.get('token')
        address = addr(token.get('address')) or addr(nested.get('address') if isinstance(nested, dict) else nested)
        token['address'] = address
        for key, value in metadata.get(address, {}).items():
            if token.get(key) is None:
                token[key] = value
        raw = token.get('raw', token.get('amount'))
        if raw is None and spec and address == spec['sold']:
            raw = spec['raw']
        if hookr.get('status') == 'ok':
            trade = next((hookr.get(k) for k in ('pays', 'gets')
                          if addr((hookr.get(k) or {}).get('address')) == address), None)
            if trade:
                raw = trade.get('raw', raw)
                token.setdefault('decimals', trade.get('decimals'))
        if raw is None and intent.get('sender'):
            deltas = [r for r in effects.get('deltas', [])
                      if addr(r.get('holder')) == intent['sender'] and addr(r.get('token')) == address]
            if len(deltas) == 1:
                delta = integer(deltas[0].get('delta'))
                raw = abs(delta) if delta is not None else None
        token['raw'] = raw
    return sides


def number(value):
    try:
        if isinstance(value, bool) or value is None:
            return None
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def annotate(cards, args, settings):
    threshold = settings.ask_when.get(RULE)
    if threshold is None or settings.sources.get('toll', 'on') == 'off':
        return cards
    stocks = {card.target.lower(): reading for card in cards if card.kind == 'network'
              for reading in card.data.get('sources', [])
              if reading.source == 'Toll' and reading.state == 'present' and reading.stock_ticker}
    if not stocks:
        return cards
    swaps = [(i, card.data['swap_sides']) for i, card in enumerate(cards)
             if card.data.get('swap_sides')]
    if not swaps:
        swaps = [(next(i for i, c in enumerate(cards) if c.kind == 'network'), sides)
                 for row in objects(args) if (sides := argument_sides(row))]
    result = list(cards)
    for index, sides in swaps:
        values, missing = [], []
        for token in sides:
            stock = stocks.get(addr(token.get('address')) or addr(token.get('token')))
            if stock is None:
                continue
            raw = integer(token.get('raw', token.get('amount')))
            decimals = token.get('decimals')
            price = number(stock.stock_price_usd)
            absent = []
            if raw is None or raw < 0:
                absent.append('amount')
            if type(decimals) is not int or not 0 <= decimals <= 255:
                absent.append('decimals')
            if price is None or price == 0:
                absent.append('Toll chain price')
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
