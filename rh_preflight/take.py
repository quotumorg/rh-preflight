"""Validated signature spending facts from Toll, with exact decimal arithmetic."""

from decimal import Decimal, InvalidOperation, localcontext
import re

from .card import WORDING, text
from .extract import ADDRESS

RULE = 'take_above_usd'
PREFIX = 'your rule: signature take above $'
UNREAD = 'Toll: what this signature lets someone take is not read'


def digits(value):
    # Token quantities are uint256 base units. Bound input before Decimal work.
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,78}', value):
        raise ValueError()
    if int(value) >= 2 ** 256:
        raise ValueError()
    return value


def dollars(value):
    if type(value) not in (str, int, float, Decimal):
        raise ValueError()
    raw = str(value)
    if len(raw) > 120 or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]{1,3})?', raw):
        raise ValueError()
    number = Decimal(raw)
    if not number.is_finite() or number < 0 or number.adjusted() > 100 or number.as_tuple().exponent < -100:
        raise ValueError()
    return number


def amount(raw, decimals):
    with localcontext() as ctx:
        ctx.prec = 200
        value = Decimal(raw).scaleb(-decimals)
        rendered = format(value, ',f')
    return rendered.rstrip('0').rstrip('.') if '.' in rendered else rendered


def money(value):
    with localcontext() as ctx:
        ctx.prec = 220
        return format(value, ',.2f')


def row(value):
    if not isinstance(value, dict):
        raise ValueError()
    result = {}
    for key in ('token', 'owner', 'spender'):
        address = value.get(key)
        if not isinstance(address, str) or not ADDRESS.fullmatch(address):
            raise ValueError()
        result[key] = address.lower()
    for key in ('signed', 'balance', 'takeable'):
        result[key] = 'unlimited' if key == 'signed' and value.get(key) == 'unlimited' else digits(value.get(key))
    block = value.get('block')
    if type(block) is not int or not 0 <= block < 2 ** 256:
        raise ValueError()
    result['block'] = block
    if value.get('nonceOk') is not None and type(value['nonceOk']) is not bool:
        raise ValueError()
    if type(value.get('expired')) is not bool:
        raise ValueError()
    result.update(nonceOk=value.get('nonceOk'), expired=value['expired'])
    decimals = value.get('decimals')
    if 'decimals' in value and (type(decimals) is not int or not 0 <= decimals <= 36):
        raise ValueError()
    if 'decimals' in value:
        result['decimals'] = decimals
    symbol = value.get('symbol')
    if 'symbol' in value:
        if not isinstance(symbol, str) or not re.fullmatch(r'\$?[A-Za-z0-9._-]{1,24}', symbol) or WORDING.search(symbol):
            raise ValueError()
        result['symbol'] = symbol
    if value.get('usd') is not None:
        # Store a string so the existing JSON ledger keeps every decimal digit.
        result['usd'] = str(dollars(value['usd']))
    simulated = value.get('simulated')
    if simulated is not None:
        if (not isinstance(simulated, dict) or not isinstance(simulated.get('status'), str)
                or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', simulated['status'])):
            raise ValueError()
        sim = {'status': text(simulated['status'])[:80]}
        for key in ('moved', 'received'):
            if key in simulated:
                sim[key] = digits(simulated[key])
        for key in ('to', 'spender'):
            if simulated.get(key) is not None:
                if not isinstance(simulated[key], str) or not ADDRESS.fullmatch(simulated[key]):
                    raise ValueError()
                sim[key] = simulated[key].lower()
            elif key in simulated:
                sim[key] = None
        if simulated.get('block') is not None:
            if type(simulated['block']) is not int or not 0 <= simulated['block'] < 2 ** 256:
                raise ValueError()
            sim['block'] = simulated['block']
        elif 'block' in simulated:
            sim['block'] = None
        for key in ('method', 'why'):
            if key in simulated:
                if not isinstance(simulated[key], str):
                    raise ValueError()
                sim[key] = text(simulated[key])[:160]
        result['simulated'] = sim
    if int(result['takeable']) > int(result['balance']):
        raise ValueError()
    if result['signed'] != 'unlimited' and int(result['takeable']) > int(result['signed']):
        raise ValueError()
    return result


def line(item):
    token = item['token']
    label = item.get('symbol') or token[:6] + '…' + token[-4:]
    decimals = item.get('decimals', 0)
    units = ' base units' if 'decimals' not in item and int(item['takeable']) else ''
    signed = item['signed']
    if signed != 'unlimited':
        signed = amount(signed, decimals) + ' ' + label + (' base units' if 'decimals' not in item else '')
    spender = item['spender'][:6] + '…' + item['spender'][-4:]
    suffix = f', signed {signed}, block {item["block"]}'
    if item['expired'] or item['nonceOk'] is False:
        reasons = []
        if item['expired']:
            reasons.append('expired')
        if item['nonceOk'] is False:
            reasons.append('the nonce is already used')
        return f'Toll: this signature: {"; ".join(reasons)}' + suffix
    if int(item['takeable']) == 0:
        balance = amount(item['balance'], decimals) + (' base units' if 'decimals' not in item and int(item['balance']) else '')
        later = ': it can take more later if the wallet is funded' if (item['signed'] == 'unlimited' or int(item['signed'])) and int(item['balance']) == 0 else ''
        return f'Toll: this signature lets {spender} take 0 {label} now (balance {balance})' + suffix + later
    usd = ' (~$' + money(dollars(item['usd'])) + ')' if 'usd' in item else ''
    return f'Toll: this signature lets {spender} take up to {amount(item["takeable"], decimals)} {label}{units}{usd} now' + suffix


def project(take):
    """One bad row becomes one incomplete fact, without losing valid siblings."""
    if not isinstance(take, dict):
        return {'status': 'not-read'}, [UNREAD], True
    if take.get('status') == 'not-read':
        own = take.get('line')
        own = text(own, '') if isinstance(own, str) else ''
        data = {'status': 'not-read', 'line': own or UNREAD[6:]}
        if isinstance(take.get('why'), str):
            data['why'] = text(take['why'])
        return data, ['Toll: ' + data['line']], True
    if take.get('status') != 'read' or not isinstance(take.get('takes'), list) or not take['takes']:
        return {'status': 'not-read'}, [UNREAD], True
    rows, lines, incomplete = [], [], False
    for value in take['takes']:
        try:
            item = row(value)
            lines.append(line(item))
            rows.append(item)
        except (ValueError, TypeError, InvalidOperation, OverflowError):
            rows.append({'status': 'not-read'})
            lines.append(UNREAD)
            incomplete = True
    return {'status': 'read', 'takes': rows}, lines, incomplete


def total(cards):
    with localcontext() as ctx:
        ctx.prec = 220
        value = Decimal(0)
        for card in cards:
            if card.kind != 'transaction':
                continue
            take = card.data.get('take') or {}
            for item in take.get('takes', []) if take.get('status') == 'read' else []:
                if item.get('status') == 'not-read' or item.get('expired') or item.get('nonceOk') is False or not int(item['takeable']):
                    continue
                if 'usd' in item:
                    value += dollars(item['usd'])
        return value
