"""HoodLock token locks over RPC and informational v2/v3/v4 LP API records."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
import math
import re
import time
from ..extract import ADDRESS
from .chain import Client as RPC, call, results
from .common import Absent, Keyless, Unavailable, Refused, RateLimited, field, money, plural, Reading

LOCKER = '0xd0f7d8c6e9f6d80c297bebe4f7fd1b9c8125c32f'
# Keep the released token-lock page and item bounds.
PAGE_SIZE = 20
MAX_PAGES = 10
MAX_ITEMS = 200

VESTING = '0x910e19bcC4bce46999994Ed7297E0Fc4431ec72E'
LP_LOCKER = '0x91aa2da1956c77f8448e18dcaf53383bcc658e67'
# HoodLock's own hook classes; anything else is shown, never treated as safe.
QUIET_HOOKS = ('none', 'verified', 'safe')
STALE_SECONDS = 3600
LP_PATH = '/api/v1/lp-locks'


class LPHTTP(Keyless):
    origin = 'https://hoodlock.tech'


def words(raw):
    if not isinstance(raw, str) or not re.fullmatch(r'0x(?:[0-9a-fA-F]{64})+', raw):
        raise ValueError()
    return [int(raw[i:i+64], 16) for i in range(2, len(raw), 64)]


def ids(raw):
    row = words(raw)
    if len(row) < 2 or row[0] != 32 or len(row) != row[1] + 2:
        raise ValueError()
    if len(set(row[2:])) != row[1]:
        raise ValueError()
    return row[2:]


def lp_value(raw):
    try:
        value = Decimal(str(raw))
        # keep USD formatting bounded to the finite JSON float range.
        if value.is_finite() and value >= 0 and math.isfinite(float(value)):
            return value
    except (DecimalException, ValueError, OverflowError):
        pass
    return None


def amount(raw):
    if not isinstance(raw, str) or not re.fullmatch(r'[0-9]{1,78}', raw):
        return None
    return int(raw)


def in_range(rows):
    flags = [row.get('inRange') for row in rows]
    if not all(type(flag) is bool for flag in flags):
        return ''
    inside = [row for row in rows if row['inRange']]
    active = {amount(row.get('poolActiveLiquidity')) for row in rows}
    if len(active) != 1 or None in active:
        return f', {len(inside)} of {len(rows)} in range'
    active = active.pop()
    if not inside:
        return ', none in range' + (', pool has no active liquidity' if active == 0 else '')
    held = [amount(row.get('liquidity')) for row in inside]
    if None in held or not active or sum(held) > active:
        return f', {len(inside)} of {len(rows)} in range'
    # floor so a share just under the pool never reads as all of it
    share = math.floor(Decimal(sum(held)) * 1000 / Decimal(active)) / Decimal(10)
    return f', {len(inside)} of {len(rows)} in range holding {share}% of active pool liquidity'


def hook_note(rows):
    states = {row.get('hookStatus') for row in rows}
    if 'unverified' in states:
        return ', hook not reviewed by HoodLock and can act when liquidity is removed'
    return ', hook status not recognised' if states - set(QUIET_HOOKS) else ''


class Client(RPC):
    label = 'HoodLock'
    page = 'https://hoodlock.tech/lock-checker'

    def __init__(self, *args, wall_clock=time.time, **kwargs):
        super().__init__(*args, **kwargs)
        self.wall_clock = wall_clock
        self.lp_http = LPHTTP(base_url=self.base_url if self.base_url != self.origin else None,
                              timeout=self.timeout, clock=self.clock)

    def _read(self, path, deadline, payload=None):
        if path == LP_PATH:
            return self.lp_http._read(path, deadline)
        return super()._read(path, deadline, payload)

    def lookup(self, target, deadline=None, **kwargs):
        # Leave time to assemble the partial card before Network's collection cut.
        end = self.clock() + self.timeout
        end = (min(deadline, end) if deadline is not None else end) - .01
        try:
            reading = super().lookup(target, deadline=end, **kwargs)
        except (OSError, RecursionError):
            # platform dates and hostile RPC nesting must not block LP facts.
            reading = self.unavailable(target, **kwargs)
        if (not ADDRESS.fullmatch(str(target)) or self.clock() >= end
                or reading.state in ('payment_required', 'refused', 'rate_limited')):
            return reading
        try:
            return self.with_lp(reading, target, self.read(LP_PATH, end))
        except (Absent, Unavailable, KeyError, TypeError, ValueError, AttributeError,
                IndexError, OverflowError, OSError, DecimalException, RecursionError):
            # An optional LP read must not erase the independent token-lock facts.
            return reading

    def with_lp(self, reading, target, data):
        lines = self.lp_lines(target, data)
        token_line = reading.line.replace(
            "token locks and vesting only; LP locks in HoodLock's Liquidity Locker and other lockers not read",
            'token locks and vesting only; other lockers not read')
        return replace(reading, line='\n'.join(filter(None, [token_line, *lines])))

    def lp_lines(self, target, data):
        if (not isinstance(data, dict) or data.get('chainId') != 4663
                or type(data.get('complete')) is not bool or not isinstance(data.get('locks'), list)):
            raise ValueError()
        contracts = data.get('contracts')
        if isinstance(contracts, dict) and str(contracts.get('lpLocker', LP_LOCKER)).lower() != LP_LOCKER:
            raise ValueError()
        pools = {}
        lock_ids = set()
        now = self.wall_clock()
        for row in data['locks']:
            if not isinstance(row, dict):
                raise ValueError()
            if row.get('version') not in ('v2', 'v3', 'v4') or row.get('status') != 'locked':
                continue
            # a position not held by the Liquidity Locker is not a HoodLock lock
            if 'holder' in row and str(row['holder']).lower() != LP_LOCKER:
                continue
            tokens = (row.get('token0'), row.get('token1'))
            if not all(isinstance(token, str) and ADDRESS.fullmatch(token) for token in tokens):
                raise ValueError()
            if target.lower() not in (token.lower() for token in tokens):
                continue
            unlock = row.get('unlockTime')
            if type(unlock) is not int:
                raise ValueError()
            if unlock <= now:
                continue
            pool = row.get('pool')
            pool_pattern = r'0x[0-9a-fA-F]{64}' if row['version'] == 'v4' else ADDRESS
            if not isinstance(pool, str) or not re.fullmatch(pool_pattern, pool):
                raise ValueError()
            ident = row.get('lockId')
            if type(ident) is not int or ident < 0 or ident in lock_ids:
                raise ValueError()
            lock_ids.add(ident)
            rows = pools.setdefault(pool.lower(), [])
            if rows and tuple(token.lower() for token in tokens) != tuple(
                    rows[0]['token' + str(i)].lower() for i in (0, 1)):
                raise ValueError()
            rows.append(row)
        lines = []
        partial = '; incomplete API result' if not data['complete'] else ''
        stamp = data.get('timestamp')
        if type(stamp) is int and now - stamp > STALE_SECONDS:
            partial += '; API data from ' + datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y-%m-%d %H:%MZ')
        for rows in pools.values():
            symbols = '/'.join(field(rows[0].get('symbol' + str(i))) for i in (0, 1))
            values = [lp_value(row.get('valueUsd')) for row in rows]
            usd = (f', ${money(sum(values))} locked'
                   if all(value is not None for value in values) else '')
            until = datetime.fromtimestamp(min(row['unlockTime'] for row in rows), timezone.utc).strftime('%Y-%m-%d')
            # Prefer API USD for size; use pool-local liquidity when USD is incomplete.
            sizes = values if all(value is not None for value in values) else [
                lp_value(row.get('liquidity')) for row in rows]
            # The proof link backs "first unlock": the earliest lock, the larger one on a tie.
            first = min(range(len(rows)), key=lambda i: (
                rows[i]['unlockTime'], -(sizes[i] if sizes[i] is not None else -1)))
            proof = rows[first].get('proof')
            link = (f' · {proof}' if isinstance(proof, str) and re.fullmatch(
                r'https://hoodlock\.tech/proof/position/[0-9]+', proof) else '')
            lines.append(f'HoodLock LP: {len(rows)} locked {plural(len(rows), "position")} in {symbols}{usd}'
                         f'{in_range(rows)}, first unlock {until}{hook_note(rows)}{partial}{link}')
        if not data['complete'] and not lines:
            lines.append('HoodLock LP: incomplete API result')
        return lines

    def target_page(self, target=None, **kwargs):
        return self.page

    def fetch(self, target, deadline):
        if not ADDRESS.fullmatch(str(target)):
            return Reading(self.label, 'unlisted', '')
        self.verify(deadline)
        arg = target[2:].lower().zfill(64)
        def eth(to, data, ident, block='latest'):
            return call('eth_call', [{'to': to, 'data': data}, block], ident)
        rows = results(self.rpc([
            eth(LOCKER, '0x03091773' + arg, 1),
            eth(target, '0x18160ddd', 2),
            eth(VESTING, '0xfb2657db' + arg, 3),
            call('eth_blockNumber', [], 4)], deadline))
        locks, schedules = ids(rows[1]['result']), ids(rows[3]['result'])
        scope = "token locks and vesting only; LP locks in HoodLock's Liquidity Locker and other lockers not read"
        if not locks and not schedules:
            return Reading(self.label, 'present', f'HoodLock holds no token lock or vesting for this token ({scope}) · {self.page}', token_lock=False)
        supply = words(rows[2]['result'])
        if len(supply) != 1 or supply[0] == 0:
            raise ValueError()
        block = rows[4]['result']
        if not re.fullmatch(r'0x[0-9a-fA-F]+', str(block)):
            raise ValueError()
        selected = locks[:min(MAX_ITEMS, PAGE_SIZE * MAX_PAGES)]
        active, withdrawn, unlocks = 0, 0, []
        for offset in range(0, len(selected), PAGE_SIZE):
            page = selected[offset:offset + PAGE_SIZE]
            detail = results(self.rpc([
                eth(LOCKER, '0xf4dadc61' + format(ident, '064x'), i+1, block)
                for i, ident in enumerate(page)], deadline))
            for i in range(len(page)):
                row = words(detail[i+1]['result'])
                if len(row) != 5 or row[0] >= 2**160 or row[1] != int(target, 16) or row[4] not in (0, 1):
                    raise ValueError()
                withdrawn += row[4]
                if not row[4] and row[3] > self.wall_clock():
                    active += row[2]
                    unlocks.append(row[3])
        percent = format(Decimal(active) * 100 / Decimal(supply[0]), '.2f')
        until = datetime.fromtimestamp(min(unlocks), timezone.utc).strftime('%Y-%m-%d') if unlocks else 'no future unlock'
        bound = f'; first {len(selected)} shown of {len(locks)} locks; totals cover shown locks only' if len(locks) > len(selected) else ''
        return Reading(self.label, 'present', f'HoodLock: {percent}% of supply locked until {until}, {withdrawn} of {len(selected)} {plural(len(selected), "lock")} already withdrawn, {len(schedules)} vesting {plural(len(schedules), "schedule")} ({scope}{bound}) · {self.page}', token_lock=bool(active) if len(locks) == len(selected) or active else None)
