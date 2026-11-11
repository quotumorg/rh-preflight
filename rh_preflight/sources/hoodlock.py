"""HoodLock-only token locks and vesting indices over the public RPC."""
from datetime import datetime, timezone
from decimal import Decimal
import re
import time
from ..extract import ADDRESS
from .chain import Client as RPC, call, results
from .common import plural, Reading

LOCKER = '0xd0f7d8c6e9f6d80c297bebe4f7fd1b9c8125c32f'
VESTING = '0x910e19bcC4bce46999994Ed7297E0Fc4431ec72E'


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


class Client(RPC):
    label = 'HoodLock'
    page = 'https://hoodlock.tech/lock-checker'

    def __init__(self, *args, wall_clock=time.time, **kwargs):
        super().__init__(*args, **kwargs)
        self.wall_clock = wall_clock

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
        selected = locks[:20]
        detail = results(self.rpc([eth(LOCKER, '0xf4dadc61' + format(ident, '064x'), i+1, block)
                                   for i, ident in enumerate(selected)], deadline)) if selected else {}
        active, withdrawn, unlocks = 0, 0, []
        for i in range(len(selected)):
            row = words(detail[i+1]['result'])
            if len(row) != 5 or row[0] >= 2**160 or row[1] != int(target, 16) or row[4] not in (0, 1):
                raise ValueError()
            withdrawn += row[4]
            if not row[4] and row[3] > self.wall_clock():
                active += row[2]
                unlocks.append(row[3])
        percent = format(Decimal(active) * 100 / Decimal(supply[0]), '.2f')
        until = datetime.fromtimestamp(min(unlocks), timezone.utc).strftime('%Y-%m-%d') if unlocks else 'no future unlock'
        bound = f'; first 20 of {len(locks)} locks' if len(locks) > 20 else ''
        return Reading(self.label, 'present', f'HoodLock: {percent}% of supply locked until {until}, {withdrawn} of {len(selected)} {plural(len(selected), "lock")} already withdrawn, {len(schedules)} vesting {plural(len(schedules), "schedule")} ({scope}{bound}) · {self.page}', token_lock=bool(active) if len(locks) <= 20 or active else None)
