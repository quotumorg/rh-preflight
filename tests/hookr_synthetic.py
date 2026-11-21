"""Synthetic Hookr models and transports. No chain captures or wallet data."""
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit
from rh_preflight import hookr_live as live
from rh_preflight.payer_attribution import HUB_RULES, BOUNDED_RULE, project_origins

TOKEN, OWNER, PAYER, DOOR, BOX, ASSET, OUTSIDER = ('0x' + c * 40 for c in '1234567')
POOL = '0x' + 'ab' * 32

class Clock:
    def __init__(self):
        self.now, self.waits = 100., []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

def reader(**kwargs):
    clock = Clock()
    return live.Reader(environ={'ETHERSCAN_API_KEY': 'test-only'}, clock=clock, sleep=clock.sleep, **kwargs)

def synthetic_log(block=20, index=0, tx=1, units=100, fee=10000, currency=0):
    a, b = (-units, 20) if currency == 0 else (20, -units)
    return {'address': live.POOL_MANAGER, 'topics': [live.SWAP, POOL, '0x' + '0' * 24 + '8' * 40],
            'removed': False, 'blockNumber': hex(block), 'logIndex': hex(index),
            'transactionHash': '0x' + f'{tx:064x}', 'timeStamp': hex(1000 + block),
            'data': '0x' + ''.join(f'{n % (1 << 256):064x}' for n in (a, b, 1, 1, 0, fee))}

def synthetic_model(payer=PAYER, weight=600, total=1000):
    data = {'status': 'ready', 'subject': {'kind': 'token', 'token': TOKEN},
            'window': {'days': 7, 'from': 1_799_395_200, 'through': 1_800_000_000,
                       'throughBlock': '1000', 'recordStarts': 1_799_000_000, 'cut': False},
            'linesOmitted': 0, 'notCounted': [],
            'lines': [{'source': 'door', 'token': TOKEN, 'txHash': '0x' + 'cd' * 32,
                       'blockNumber': '1000', 'time': 1_800_000_000,
                       'currency': live.ZERO, 'symbol': 'ETH', 'decimals': 18, 'amount': '101'}]}
    row = data['lines'][0]
    identity = {'token': TOKEN, 'tokenizer': live.TOKENIZER, 'generation': '2026-10-29', 'chainId': 4663,
                'observedBlock': data['window']['throughBlock'], 'doors': [DOOR], 'creator': OWNER,
                'deployer': OWNER, 'deploymentSender': OWNER, 'streamManager': OWNER,
                'dropBox': BOX, 'relatedWallets': [], 'relationshipsComplete': False,
                'projectRelationshipsComplete': True, 'excludedPayers': [DOOR, live.POOL_MANAGER]}
    part = {'basis': live.BASIS, 'amount': str(101 * weight // total), 'complete': False, 'approximate': True, 'reconciliationOk': True,
            'sender': payer, 'senderResolved': True, 'scanComplete': True, 'weight': str(weight), 'totalWeight': str(total),
            'attributableAmount': '101', 'swaps': [{'txHash': '0x' + '11' * 32, 'poolId': POOL,
              'blockNumber': str(int(row['blockNumber']) - 10), 'logIndex': 1, 'feeWeight': str(weight), 'time': row['time'] - 100}],
            'senderEvidence': {'sender': payer, 'relationshipsComplete': True, 'relationshipDepth': 2,
                               'hubRules': HUB_RULES, 'accounts': {payer: {'code': '0x', 'hub': False}}}}
    part['senderEvidence'].update(beforeBlock=int(row['blockNumber']) - 10, beforeTime=row['time'] - 100,
        lookbackSeconds=604800, fundingScope={'rule': BOUNDED_RULE,
        'fromBlock': 1, 'throughBlock': int(row['blockNumber']) - 10,
        'fromTime': row['time'] - 100 - 604800, 'throughTime': row['time'] - 100,
        'candidateHistoryComplete': True, 'projectOutgoingComplete': True, 'directProjectFunding': False,
        'projectOutgoingRoots': sorted(project_origins(identity))})
    before = int(row['blockNumber']) - 10
    part['senderEvidence']['accounts'][payer]['codeChecks'] = {str(before): '0x', str(before - 1): '0x'}
    return {'schema': live.SCHEMA, 'identity': identity, 'income': data, 'attributions': [{'line': deepcopy(row), 'allocations': [part]}],
            'live': {'status': 'partial', 'linesRead': 1, 'calls': {'mcp': 2, 'etherscan': 15}, 'reasons': []}}

def funding_row(sender, destination, block=10, amount=100, asset=live.ZERO, tx=15, kind='txlist', stamp=1000):
    return {'from': sender, 'to': destination, 'blockNumber': str(block), 'value': str(amount),
            'hash': '0x' + f'{tx:064x}', 'contractAddress': asset, 'timeStamp': str(stamp),
            'isError': '0', 'txreceipt_status': '1', '_kind': kind}

def history_transport(r, rows, missing=None):
    calls = []
    def read(path, deadline, payload=None):
        args = {k: v[0] for k, v in parse_qs(urlsplit(path).query).items()}
        calls.append(args)
        if args['action'] == 'getblocknobytime':
            return 200, {'status': '1', 'result': '1'}
        if args['action'] in ('eth_getTransactionByHash', 'eth_getTransactionReceipt'):
            row = next((v for v in rows if v['hash'] == args['txhash'] and v['_kind'] == 'txlist'), None)
            if row is None: return 200, {'result': None}
            fields = {'from': row['from'], 'to': row['to'], 'blockNumber': hex(int(row['blockNumber']))}
            return 200, {'result': {**fields, 'hash': row['hash'], 'transactionHash': row['hash'],
                'value': hex(int(row['value'])), 'status': '0x1', 'input': row.get('input', '0x')}}
        if args['action'] == 'eth_getCode':
            return 200, {'result': '0x'}  # Synthetic EOA, not a recorded code observation.
        if (args['address'], args['action']) == missing:
            return 200, {'status': '0', 'result': 'not captured'}
        values = [v for v in rows if args['address'] in (v['from'], v['to']) and args['action'] == v['_kind']
                  and int(args['startblock']) <= int(v['blockNumber']) <= int(args['endblock'])]
        lo = (int(args['page']) - 1) * int(args['offset'])
        return 200, {'status': '1', 'result': deepcopy(values[lo:lo + int(args['offset'])])}
    r.es_client._read_once = read
    return calls
