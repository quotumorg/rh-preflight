"""Pure payer attribution primitives, independent of Hookr, cards and CLI.

API: fee_weights(swaps) groups integer input*feePips weights by currency and
sender (None preserves unresolved weight). hub_status consumes code/history
observations and returns True, False or None (unread). relationship consumes
normalized identity, chronological paths and account observations; it never
performs IO. Callers supply complete bounded project outgoing history, plus
block-tagged code observations and any sender-side leads.
Amounts are raw integers; currencies must never be combined.
"""
from collections import Counter, defaultdict
import re

ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}')
HASH = re.compile(r'0x[0-9a-fA-F]{64}')
CREATOR = 'creator or deployer wallet'
LOOP = 'self-funded loop'
EXTERNAL = 'external payer'
UNKNOWN = 'unknown'
HUB_RULES = 'eoa-no-hubs/v1'
BOUNDED_RULE = 'project-outgoing-window/v2'
ZERO = '0x' + '00' * 20
# Chain 4663 infrastructure. Other deployments can pass their own known_hubs.
KNOWN_HUBS = frozenset({
    '0x8366a39cc670b4001a1121b8f6a443a643e40951',  # PoolManager
    '0x58daec3116aae6d93017baaea7749052e8a04fa7',  # PositionManager
    '0x8876789976decbfcbbbe364623c63652db8c0904',
    '0xc2bc9a3fe6c4ce8c2f7eb4655b0869b01d8c93c9',
    '0x000000000022d473030f116ddee9f6b43ac78ba3',  # Permit2
    '0xd29c85f15df544ba632c9e25829fd29d767d7978',  # depositV3 bridge
    '0xfb3c1501c727f30883cd7b36ec380381af7412ef',
    '0x08de138e558a9258c6b4077e8bdeb9ad1fb46a4b',
    '0x342baa22e202954593b04b7ed1663621e0565d46',
    '0x9655a9aead9c33032649e3a70e152d831acecd1a',
    '0x7309b6adc24d590e19079c732be7e11ef6ba485f',
    '0xc24ca4cdc35689b9cd41c64e4f9ffcb772fdaab7',
})


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


def identities(identity):
    return {a for k in ('creator', 'deployer', 'streamManager') if (a := address(identity.get(k)))}


def fee_weights(swaps):
    result = defaultdict(Counter)
    for swap in swaps:
        currency = address(swap.get('currency'), zero=True)
        units, fee = number(swap.get('amount')), number(swap.get('feePips'), maximum=1000000)
        if currency is None or units is None or fee is None:
            raise ValueError('invalid swap weight')
        result[currency][address(swap.get('sender'))] += units * fee
    return dict(result)


def hub_status(wallet, code, rows=(), *, history_complete=False, max_counterparties=50,
               known_hubs=KNOWN_HUBS):
    """Code and observed hub evidence decide status, not history completeness."""
    wallet = address(wallet)
    peers = {address(r.get(k)) for r in rows for k in ('from', 'to')
             if wallet in (address(r.get('from')), address(r.get('to')))} - {None, wallet}
    if wallet in known_hubs or len(peers) > max_counterparties:
        return True
    if not isinstance(code, str) or not re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*', code):
        return None
    if code != '0x':
        return True
    return False


def project_origins(identity):
    return identities(identity) | {a for value in (
        identity.get('deploymentSender'), identity.get('token'), identity.get('dropBox'),
        *identity.get('doors', [])) if (a := address(value))}


def swap_eoa(observation, blocks):
    """Require explicit empty code at every action block and its predecessor."""
    if not isinstance(observation, dict):
        return False
    checks = observation.get('codeChecks', {})
    return (isinstance(checks, dict) and observation.get('hub') is False
            and observation.get('code') == '0x'
            and all(number(b) not in (None, 0) and checks.get(str(number(b))) == '0x'
                    and checks.get(str(number(b) - 1)) == '0x' for b in blocks))


def path_valid(path, *, before, accounts, origins=(), known_hubs=KNOWN_HUBS,
               currency=None, amount=1):
    """No hub can be an intermediate. Project contracts may be loop origins."""
    if not isinstance(path, list) or not 1 <= len(path) <= 16:
        return False
    previous, height, seen = None, 0, set()
    for i, edge in enumerate(path):
        if not isinstance(edge, dict):
            return False
        src, dst = address(edge.get('from')), address(edge.get('to'))
        block, units = number(edge.get('blockNumber')), number(edge.get('amount'))
        tx = edge.get('txHash')
        if (not src or not dst or src == dst or previous is not None and previous != src
                or block is None or not height < block < before or not units or units < amount
                or not isinstance(tx, str) or not HASH.fullmatch(tx)
                or address(edge.get('currency'), zero=True) is None
                or currency is not None and address(edge.get('currency'), zero=True) != currency
                or dst in seen):
            return False
        for node in (src, dst):
            if i == 0 and node == src and node in origins and node not in known_hubs:
                continue
            observation = accounts.get(node, {})
            if node in known_hubs or observation.get('hub') is not False or observation.get('code') != '0x':
                return False
        seen.add(src)
        previous, height = dst, block
    return True


def relationship(sender, identity, rel, project_complete=False):
    """Classify identities/direct EOA funding; common funders are private leads.

    A funding lead from a project contract alone is not a proven return loop.
    Graph paths alone cannot establish an unspent, non-reused return balance.
    """
    sender = address(sender)
    roots = identities(identity)
    base = {'sender': sender, 'classification': UNKNOWN, 'relationshipsComplete': False,
            'relationshipDepth': rel.get('depth', 0), 'hubRules': HUB_RULES}
    if sender in roots:
        return {**base, 'classification': CREATOR, 'relationship': 'literal identity'}
    accounts = rel.get('accounts', {})
    if not isinstance(accounts, dict) or not isinstance(rel.get('paths', []), list):
        return base
    hubs = KNOWN_HUBS | set(identity.get('hubs', []))
    paths = [p.get('path', []) for p in rel.get('paths', []) if isinstance(p, dict)]
    if any(not isinstance(p, list) or any(not isinstance(e, dict) for e in p) for p in paths):
        return base
    leads = set(identity.get('commonFunders', [])) | set(identity.get('relatedWallets', []))
    common = sender in leads or any(e.get('from') in leads for path in paths for e in path if isinstance(e, dict))
    before, stamp = number(rel.get('beforeBlock')), number(rel.get('beforeTime'))
    period = number(rel.get('lookbackSeconds'))
    if rel.get('hubRules') == HUB_RULES and before and stamp is not None and period:
        for path in paths:
            if (len(path) == 1 and path[0].get('from') in roots and path[0].get('to') == sender
                    and path_valid(path, before=before, accounts=accounts, known_hubs=hubs)
                    and path[0].get('nativeFundingVerified') is True
                    and path[0].get('currency') == ZERO
                    and (t := number(path[0].get('time'))) is not None and stamp - period <= t < stamp):
                return {**base, 'classification': CREATOR, 'relationship': 'direct prior EOA funding',
                        'fundingPath': path, 'accounts': accounts, 'beforeBlock': before,
                        'beforeTime': stamp, 'lookbackSeconds': period, 'pathComplete': True}
    if common:
        return {**base, 'note': 'common funder lead'}
    scope = rel.get('fundingScope', {})
    origins = project_origins(identity)
    bounded = (isinstance(scope, dict) and scope.get('rule') == BOUNDED_RULE
               and number(scope.get('fromBlock')) not in (None, 0)
               and number(scope.get('throughBlock')) is not None
               and number(scope['fromBlock']) <= before <= number(scope['throughBlock'])
               and number(scope.get('fromTime')) == max(0, stamp - period)
               and number(scope.get('throughTime')) is not None and number(scope['throughTime']) >= stamp
               and scope.get('projectOutgoingComplete') is True
               and isinstance(scope.get('projectOutgoingRoots'), list)
               and origins <= {address(a) for a in scope['projectOutgoingRoots']}
               and scope.get('directProjectFunding') is False) if before and stamp is not None and period else False
    complete = (bounded and project_complete
                and rel.get('hubRules') == HUB_RULES and number(rel.get('depth'), maximum=4) in (1, 2, 3, 4)
                and sender not in hubs and sender not in origins
                and swap_eoa(accounts.get(sender, {}), (before, scope.get('throughBlock'))))
    # An unexplained prior project-origin path cannot establish externality.
    if any(e.get('from') in origins for path in paths for e in path if isinstance(e, dict)):
        complete = False
    return {**base, 'classification': EXTERNAL if complete else UNKNOWN,
            'relationshipsComplete': bool(complete), 'accounts': accounts,
            'fundingLeads': rel.get('paths', []),
            'fundingScope': scope, 'beforeBlock': before, 'beforeTime': stamp, 'lookbackSeconds': period}
