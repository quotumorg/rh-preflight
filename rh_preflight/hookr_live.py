"""Opt-in, bounded Hookr income evidence collection on chain 4663.

Swap senders and historical funding links are observations, not proof of the
origin of a door's LP fees. See docs/HOOKR_INCOME.md for the approximation.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import re
import sys
import threading
import time
from urllib.parse import urlencode

from .income import SCHEMA, HASH, address, number, line_key, classify, MAX_INCOME_LINES, PublicParser
from .hey import Unavailable, RateLimited
from .inflight import KeyedLocks
from .hookr_swap import ZERO, keccak256
from .sources.common import Source
from .payer_attribution import KNOWN_HUBS

POOL_MANAGER = '0x8366a39cc670b4001a1121b8f6a443a643e40951'
TOKENIZER = '0x08de138e558a9258c6b4077e8bdeb9ad1fb46a4b'
TOKENIZER_V1 = '0xfb3c1501c727f30883cd7b36ec380381af7412ef'
MODIFY = '0x' + keccak256(b'ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)')
DONATE = '0x' + keccak256(b'Donate(bytes32,address,uint256,uint256)')
TRANSFER = '0x' + keccak256(b'Transfer(address,address,uint256)')
SWAP = '0x' + keccak256(b'Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)')
INITIALIZE = '0x' + keccak256(b'Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)')
DROP = '0x' + keccak256(b'drop(address,uint256)')[:8]
BASIS = 'pool-fee-weight-estimate'
ROUTERS = {'0x8876789976decbfcbbbe364623c63652db8c0904',
           '0xc2bc9a3fe6c4ce8c2f7eb4655b0869b01d8c93c9'}


def hash32(value):
    return value.lower() if isinstance(value, str) and HASH.fullmatch(value) else None


def integer(value):
    if isinstance(value, str) and re.fullmatch(r'0x[0-9a-fA-F]{1,64}', value):
        return int(value, 16)
    return number(value)


def log_index(value):
    # Etherscan getLogs encodes zero logIndex as "0x" in real captures.
    # This alias is allowed only for event indices, never amounts or blocks.
    return 0 if value == '0x' else integer(value)


def words(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x(?:[0-9a-fA-F]{64})+', value):
        return []
    return [value[i:i + 64].lower() for i in range(2, len(value), 64)]


def word_address(value):
    return address('0x' + value[-40:], zero=True) if re.fullmatch(r'0{24}[0-9a-fA-F]{40}', value) else None


def income_projection(raw):
    """Only validated public facts leave the remote answer."""
    window = raw.get('window')
    if (not isinstance(window, dict) or not isinstance(raw.get('lines'), list)
            or not isinstance(raw.get('subject'), dict)):
        raise ReadStopped('income shape unavailable')
    result = {'status': raw.get('status') if raw.get('status') in ('ready', 'partial') else 'unavailable',
              'subject': {'kind': 'token' if raw['subject'].get('kind') == 'token' else None,
                          'token': address(raw['subject'].get('token'))},
              'window': {k: number(window.get(k), maximum=2 ** 64 - 1)
                         for k in ('days', 'from', 'through', 'recordStarts', 'throughBlock')},
              'linesOmitted': number(raw.get('linesOmitted')), 'lines': [],
              'notCounted': [{} for _ in raw['notCounted']] if isinstance(raw.get('notCounted'), list) else None}
    result['window']['cut'] = window.get('cut') if type(window.get('cut')) is bool else None
    if len(raw['lines']) > MAX_INCOME_LINES:
        result['status'] = 'partial'
        result['linesOmitted'] = (result['linesOmitted'] or 0) + len(raw['lines']) - MAX_INCOME_LINES
    for row in raw['lines'][:MAX_INCOME_LINES]:
        if not line_key(row):
            result['lines'].append({})
            continue
        key = line_key(row)
        clean = dict(zip(('source', 'token', 'txHash', 'blockNumber', 'time', 'currency', 'amount'), key))
        clean['blockNumber'], clean['amount'] = str(key[3]), str(key[6])
        for field in ('payer', 'sender'):
            if hint := address(row.get(field)):
                clean[field] = hint
        symbol = row.get('symbol')
        if isinstance(symbol, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,23}', symbol):
            clean['symbol'] = symbol.lower()
        if type(row.get('decimals')) is int and 0 <= row['decimals'] <= 36:
            clean['decimals'] = row['decimals']
        result['lines'].append(clean)
    return result


class ReadStopped(Exception):
    """Only fixed, locally authored reasons may leave the collector."""


class RateLimiter:
    """Token bucket: 24 initial requests, one token replenished every 2 s."""
    def __init__(self, burst=24, interval=2.0, clock=time.monotonic, sleep=time.sleep):
        self.burst, self.interval = burst, interval
        self.clock, self.sleep = clock, sleep
        self.tokens, self.stamp = float(burst), clock()
        self.lock = threading.Lock()

    def acquire(self, deadline):
        with self.lock:
            now = self.clock()
            self.tokens = min(self.burst, self.tokens + max(0, now - self.stamp) / self.interval)
            self.stamp = now
            wait = max(0, (1 - self.tokens) * self.interval)
            if now + wait >= deadline:
                raise ReadStopped('run deadline reached')
            if wait:
                self.sleep(wait)
                self.tokens = min(self.burst, self.tokens + max(0, self.clock() - now) / self.interval)
                self.stamp = self.clock()
            if self.tokens + 1e-9 < 1 or self.clock() >= deadline:
                raise ReadStopped('run deadline reached')
            self.tokens = max(0.0, self.tokens - 1)


MCP_LIMITER = RateLimiter()
ES_LIMITER = RateLimiter(burst=1, interval=.4)


class MCPClient(Source):
    origin = 'https://hookr.fun'
    accept = 'application/json, text/event-stream'

    def _decode(self, raw):
        try:
            return json.loads(raw)
        except ValueError:
            # Streamable HTTP can frame the JSON-RPC answer as an SSE message.
            for event in raw.decode('utf-8').replace('\r\n', '\n').split('\n\n'):
                data = '\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
                if data:
                    try:
                        answer = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(answer, dict) and ('result' in answer or 'error' in answer):
                        return answer
            raise Unavailable() from None


class EtherscanClient(Source):
    origin = 'https://api.etherscan.io'


def mcp_result(envelope, request_id):
    if (not isinstance(envelope, dict) or envelope.get('jsonrpc') != '2.0'
            or envelope.get('id') != request_id or envelope.get('error')):
        raise ReadStopped('hookr answer unavailable')
    result = envelope.get('result')
    if not isinstance(result, dict) or result.get('isError'):
        raise ReadStopped('hookr answer unavailable')
    if 'structuredContent' in result:
        data = result['structuredContent']
    elif isinstance(result.get('content'), list):
        texts = [row.get('text') for row in result['content']
                 if isinstance(row, dict) and row.get('type') == 'text']
        try:
            data = json.loads(texts[0]) if len(texts) == 1 else None
        except (ValueError, TypeError):
            data = None
    else:
        data = result  # tools/list
    if not isinstance(data, dict):
        raise ReadStopped('hookr answer unavailable')
    return data


def delivery(token, row, tx, receipt, internal, doors, generation='v2'):
    """Decode only successful matching transactions and known event emitters."""
    key = line_key(row)
    if (not key or not isinstance(tx, dict) or not isinstance(receipt, dict)
            or hash32(tx.get('hash')) != key[2] or hash32(receipt.get('transactionHash')) != key[2]
            or integer(tx.get('blockNumber')) != key[3] or integer(receipt.get('blockNumber')) != key[3]
            or integer(receipt.get('status')) != 1 or not address(tx.get('from'))
            or address(tx.get('to')) not in ({token, *doors} if generation == 'v1' else {token})
            or address(receipt.get('to')) != address(tx.get('to'))
            or address(receipt.get('from')) != address(tx['from'])
            or not isinstance(receipt.get('logs'), list)):
        return None
    keeper = address(tx['from'])
    out = {'txHash': key[2], 'blockNumber': str(key[3]), 'keeper': keeper,
           'sync': tx.get('input') == '0xfff6cae9', 'pools': [], 'doors': [],
           'forwarding': [], 'keeperTransfers': [], 'internalRead': isinstance(internal, list),
           'generation': generation, 'rulesCredits': []}
    rules_movements = []
    for log in receipt['logs']:
        if (not isinstance(log, dict) or log.get('removed') is not False
                or hash32(log.get('transactionHash')) != key[2]
                or integer(log.get('blockNumber')) != key[3]):
            continue
        emitter, topics, data = address(log.get('address')), log.get('topics'), words(log.get('data'))
        if not isinstance(topics, list) or not topics or not all(hash32(t) for t in topics):
            continue
        if emitter in doors:
            out['doors'].append(emitter)
        event = topics[0].lower()
        if (emitter == '0x342baa22e202954593b04b7ed1663621e0565d46'
                and event == '0x913c992353dc81b7a8ba31496c484e9b6306bd2f6c509a649a38fdf5e1c953b2'
                and len(topics) == 4 and len(data) == 1
                and word_address(topics[3][2:]) == token):
            currency, beneficiary = word_address(topics[1][2:]), word_address(topics[2][2:])
            if currency and beneficiary:
                out['rulesCredits'].append({'currency': currency, 'beneficiary': beneficiary,
                    'amount': str(int(data[0], 16)), 'origin': 'rules credit', 'payer': None})
        if emitter == POOL_MANAGER and event in (MODIFY, DONATE) and len(topics) == 3:
            if len(data) != (4 if event == MODIFY else 2):
                continue
            if integer(log.get('logIndex')) is None or word_address(topics[2][2:]) is None:
                continue
            out['pools'].append({'poolId': topics[1].lower(),
                                 'event': 'modify-liquidity' if event == MODIFY else 'donate',
                                 'sender': word_address(topics[2][2:]),
                                 'collect': event == MODIFY and int(data[2], 16) == 0,
                                 'logIndex': str(integer(log.get('logIndex'))),
                                 'position': data if event == MODIFY else None})
        if event == TRANSFER and len(topics) == 3 and len(data) == 1:
            sender, destination = (word_address(t[2:]) for t in topics[1:])
            if sender == POOL_MANAGER and destination == token:
                rules_movements.append({'from': sender, 'to': token, 'currency': emitter,
                    'amount': str(int(data[0], 16)), 'txHash': key[2], 'blockNumber': str(key[3])})
            if sender in doors and destination == token:
                out['doors'].append(sender)
                out['forwarding'].append({'from': sender, 'to': token, 'currency': emitter,
                                          'amount': str(int(data[0], 16)), 'txHash': key[2],
                                          'blockNumber': str(key[3])})
    for transfer in internal if isinstance(internal, list) else []:
        if (not isinstance(transfer, dict) or transfer.get('isError') != '0'
                or integer(transfer.get('blockNumber')) != key[3]
                or transfer.get('hash') is not None and hash32(transfer['hash']) != key[2]
                or not number(transfer.get('value'))):
            continue
        sender, destination = address(transfer.get('from')), address(transfer.get('to'))
        edge = {'from': sender, 'to': destination, 'currency': ZERO,
                'amount': str(number(transfer['value'])), 'txHash': key[2], 'blockNumber': str(key[3])}
        if sender in doors and destination == token:
            out['doors'].append(sender)
            out['forwarding'].append(edge)
        if sender == POOL_MANAGER and destination == token:
            rules_movements.append(edge)
        if out['sync'] and sender == token and destination == keeper:
            out['keeperTransfers'].append(edge)
    # Rules can settle its credit through PoolManager directly to the token.
    # Keep that known non-swap component out of the fee-door allocation. Only
    # bind an exact same-currency match; the event alone is not another transfer.
    for currency in {e['currency'] for e in out['rulesCredits']}:
        credits = sum(int(e['amount']) for e in out['rulesCredits'] if e['currency'] == currency)
        moves = [e for e in rules_movements if e['currency'] == currency]
        if credits and sum(int(e['amount']) for e in moves) == credits:
            for move in moves:
                out['forwarding'].append({**move, 'movementFrom': POOL_MANAGER,
                    'from': '0x342baa22e202954593b04b7ed1663621e0565d46', 'origin': 'rules credit'})
    out['doors'] = sorted(set(out['doors']))
    return out


class Reader:
    """One sequential run; caps count HTTP attempts, including failed ones."""
    def __init__(self, *, max_etherscan=800, max_mcp=40, depth=2, timeout=10,
                 run_seconds=900, max_swap_txs=64, hub_counterparties=50,
                 funding_lookback_days=7, reconciliation_tolerance_bps=1000, etherscan_rps=4,
                 trace_share_bps=8000, workers=5,
                 environ=None, clock=time.monotonic, sleep=time.sleep):
        if (type(max_etherscan) is not int or not 0 <= max_etherscan <= 4000
                or type(max_mcp) is not int or not 0 <= max_mcp <= 40
                or type(depth) is not int or not 0 <= depth <= 4
                or type(timeout) not in (int, float) or not 0 < timeout <= 30
                or type(run_seconds) not in (int, float) or not 0 < run_seconds <= 3600):
            raise ValueError('invalid live read limits')
        if type(max_swap_txs) is not int or not 0 <= max_swap_txs <= 400:
            raise ValueError('invalid swap sender limit')
        if (type(hub_counterparties) is not int or not 1 <= hub_counterparties <= 1000
                or type(funding_lookback_days) is not int or not 1 <= funding_lookback_days <= 30
                or type(reconciliation_tolerance_bps) is not int or not 0 <= reconciliation_tolerance_bps <= 10000):
            raise ValueError('invalid attribution limits')
        self.max_swap_txs = max_swap_txs
        if type(etherscan_rps) not in (int, float) or not .1 <= etherscan_rps <= 8:
            raise ValueError('invalid etherscan rate')
        if type(trace_share_bps) is not int or not 1 <= trace_share_bps <= 10000:
            raise ValueError('invalid trace share')
        if type(workers) is not int or not 1 <= workers <= 16:
            raise ValueError('invalid worker limit')
        self.trace_share_bps, self.workers = trace_share_bps, workers
        self.state_lock = threading.RLock()
        self.es_flights = KeyedLocks()
        self.etherscan_rps = etherscan_rps
        self.failures = []
        self.hub_counterparties = hub_counterparties
        self.funding_lookback_seconds = funding_lookback_days * 86400
        self.reconciliation_tolerance_bps = reconciliation_tolerance_bps
        self.hubs = set(KNOWN_HUBS)
        self.limit_hits = []
        self.started = clock()
        self.caps = {'etherscan': max_etherscan, 'mcp': max_mcp}
        self.counts = dict.fromkeys(self.caps, 0)
        self.depth, self.timeout, self.run_seconds = depth, timeout, run_seconds
        self.clock, self.sleep = clock, sleep
        env = os.environ if environ is None else environ
        self._key = env.get('ETHERSCAN_API_KEY', '')
        if not isinstance(self._key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', self._key):
            self._key = ''
        self.mcp_client = MCPClient(timeout=timeout, clock=clock)
        self.es_client = EtherscanClient(timeout=timeout, clock=clock)
        self.es_client.slots = threading.BoundedSemaphore(workers)
        self.limiters = ({'mcp': MCP_LIMITER, 'etherscan': ES_LIMITER} if clock is time.monotonic else
                         {'mcp': RateLimiter(clock=clock, sleep=sleep),
                          'etherscan': RateLimiter(1, .4, clock=clock, sleep=sleep)})
        self.limiters['etherscan'] = RateLimiter(1, 1 / etherscan_rps, clock=clock, sleep=sleep)
        self.cache, self.reasons = {}, []
        self.trade_rows = 0
        self.deadline = clock() + run_seconds
        self.used = False

    def note(self, reason):
        with self.state_lock:
            if reason not in self.reasons:
                self.reasons.append(reason)

    def hit(self, kind, limit, **scope):
        item = {'kind': kind, 'limit': limit, **scope}
        with self.state_lock:
            if item not in self.limit_hits:
                self.limit_hits.append(item)
        self.note(kind + ' cap reached')

    def request(self, source, path, payload=None):
        client = self.mcp_client if source == 'mcp' else self.es_client
        attempts = 4 if source == 'etherscan' else 3
        for attempt in range(attempts):
            with self.state_lock:
                if self.counts[source] >= self.caps[source]:
                    self.hit(source + ' call', self.caps[source], attempted=self.counts[source])
                    raise ReadStopped(source + ' call cap reached')
            if self.clock() >= self.deadline:
                raise ReadStopped('run deadline reached')
            if source == 'etherscan' and not self._key:
                raise ReadStopped('etherscan key unavailable')
            self.limiters[source].acquire(self.deadline)
            # Reserve the attempt atomically; concurrent retries share the cap.
            with self.state_lock:
                if self.counts[source] >= self.caps[source]:
                    self.hit(source + ' call', self.caps[source], attempted=self.counts[source])
                    raise ReadStopped(source + ' call cap reached')
                if self.clock() >= self.deadline:
                    raise ReadStopped('run deadline reached')
                self.counts[source] += 1
            try:
                status, data = client._read_once(path, min(self.deadline, self.clock() + self.timeout), payload)
                # Etherscan also signals throttling in HTTP 200 result strings.
                message = json.dumps(data).lower() if isinstance(data, dict) else ''
                limited = status == 429 or any(t in message for t in
                    ('rate limit', 'rate-limit', 'too many requests', 'max calls per sec'))
                if limited:
                    raise RateLimited()
                if status >= 500 or any(t in message for t in (
                        'query timeout', 'server too busy', 'temporarily unavailable', 'timeout occurred')):
                    raise Unavailable()
                if status != 200 or not isinstance(data, dict):
                    raise Unavailable()
                if self.clock() >= self.deadline:
                    raise ReadStopped('run deadline reached')
                return data
            except (RateLimited, Unavailable, OSError) as error:
                if attempt == attempts - 1:
                    if isinstance(error, RateLimited):
                        self.hit(source + ' rate-limit retry', attempts - 1, attempts=attempts)
                    reason = ' rate limit retries exhausted' if isinstance(error, RateLimited) else ' read unavailable'
                    raise ReadStopped(source + reason) from None
                delay = max(2 ** attempt, client.cooldown - self.clock())
                if self.clock() + delay >= self.deadline:
                    raise ReadStopped('run deadline reached') from None
                self.sleep(delay)
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                raise ReadStopped(source + ' read unavailable') from None

    def mcp(self, name, arguments=None):
        if name not in ('hookr_revenue_token', 'hookr_income', 'hookr_trades', 'hookr_pool_yield', 'tools/list'):
            raise ValueError('unsupported read method')
        key = ('mcp', name, json.dumps(arguments or {}, sort_keys=True))
        if key in self.cache:
            return deepcopy(self.cache[key])
        ident = self.counts['mcp'] + 1
        payload = {'jsonrpc': '2.0', 'id': ident, 'method': 'tools/call',
                   'params': {'name': name, 'arguments': arguments or {}}}
        if name == 'tools/list':
            payload.update(method=name, params={})
        result = mcp_result(self.request('mcp', '/api/mcp', payload), ident)
        self.cache[key] = deepcopy(result)
        return result

    def parallel(self, function, items):
        # map preserves value priority in returned results. The limiter is shared
        # by every worker and every retry, never a per-worker allowance.
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(function, items))

    def es(self, **params):
        key = tuple(sorted(params.items()))
        if not self.es_flights.acquire(key, timeout=max(0, self.deadline - self.clock())):
            raise ReadStopped('run deadline reached')
        try:
            return self._es(**params)
        finally:
            self.es_flights.release(key)

    def _es(self, **params):
        if (params.get('module'), params.get('action')) not in (
                ('proxy', 'eth_getTransactionByHash'), ('proxy', 'eth_getTransactionReceipt'),
                ('proxy', 'eth_getCode'), ('proxy', 'eth_call'), ('contract', 'getabi'),
                ('account', 'txlist'), ('account', 'txlistinternal'), ('account', 'tokentx'),
                ('contract', 'getcontractcreation'), ('logs', 'getLogs'), ('block', 'getblocknobytime')):
            raise ValueError('unsupported read method')
        key = tuple(sorted(params.items()))
        if key in self.cache:
            result = self.cache[key]
            if isinstance(result, ReadStopped):
                raise result
            return result
        path = '/v2/api?' + urlencode({'chainid': 4663, **params, 'apikey': self._key})
        try:
            data = self.request('etherscan', path)
            result = data.get('result')
            if params['action'] in ('eth_getCode', 'eth_call'):
                if data.get('error') or not isinstance(result, str) or not re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*', result):
                    raise ReadStopped('contract read unavailable')
            elif params['action'] == 'getabi':
                if data.get('status') != '1' or not isinstance(result, str):
                    raise ReadStopped('verified abi unavailable')
                try:
                    result = json.loads(result)
                except (ValueError, RecursionError):
                    raise ReadStopped('verified abi unavailable') from None
                if not isinstance(result, list) or len(result) > 1000:
                    raise ReadStopped('verified abi unavailable')
            elif params['module'] == 'proxy':
                if data.get('error') or not isinstance(result, dict):
                    raise ReadStopped('etherscan transaction unavailable')
            elif params['module'] == 'block':
                if data.get('status') != '1' or number(result) is None:
                    raise ReadStopped('window block unavailable')
            elif data.get('status') != '1' or not isinstance(result, list):
                if data.get('status') == '0' and result == [] and data.get('message') in ('No transactions found', 'No records found'):
                    result = []
                else:
                    raise ReadStopped('etherscan history unavailable')
            self.cache[key] = result
            return result
        except ReadStopped as error:
            self.failures.append({'module': params['module'], 'action': params['action'], 'reason': str(error),
                                  'parameters': dict(params)})
            self.cache[key] = error
            raise

    def optional_es(self, **params):
        try:
            return self.es(**params)
        except ReadStopped as error:
            self.note(str(error))
            return None

    def get_tx(self, tx_hash):
        return self.optional_es(module='proxy', action='eth_getTransactionByHash', txhash=tx_hash)

    def code(self, wallet, block=None):
        # One request per address, including cached failures. Current nonempty
        # code excludes a link; it never proves historical wallet control.
        return self.optional_es(module='proxy', action='eth_getCode', address=wallet,
                                tag=hex(block) if block is not None else 'latest')

    def get_delivery(self, token, row, doors):
        tx_hash = row['txHash'].lower()
        tx = self.get_tx(tx_hash)
        receipt = self.optional_es(module='proxy', action='eth_getTransactionReceipt', txhash=tx_hash)
        internal = self.optional_es(module='account', action='txlistinternal', txhash=tx_hash)
        result = delivery(token, row, tx, receipt, internal, doors,
                          generation=getattr(self, 'generation', 'v2'))
        if result and getattr(self, 'generation', 'v2') == 'v1':
            from .hookr_sources import v1_receipt
            v1_receipt(self, result, token, receipt, doors)
        if result is None:
            self.note('delivery transaction not resolved')
        return result

    def trade_schema(self):
        """Legacy round 1 parser support; collect() never calls this method."""
        if 'trade_schema' not in self.cache:
            result = self.mcp('tools/list')
            schemas = [t.get('inputSchema') for t in result.get('tools', [])
                       if isinstance(t, dict) and t.get('name') == 'hookr_trades']
            self.cache['trade_schema'] = schemas[0] if len(schemas) == 1 and isinstance(schemas[0], dict) else {}
        return self.cache['trade_schema'].get('properties', {})

    def trades(self, pool, start, end, through_block):
        """Legacy parser for callers with supplied pages; not used by read()."""
        args, seen, rows, previous, complete = {'poolId': pool}, {}, [], None, False
        ambiguous = False
        currencies = None
        scanned_from, scanned_through, oldest = None, None, None
        try:
            while True:
                page = self.mcp('hookr_trades', args)
                batch = page.get('trades')
                if (hash32(page.get('poolId')) != pool or not isinstance(batch, list)
                        or len(batch) > 1000 or type(page.get('more')) is not bool):
                    raise ReadStopped('trade page unavailable')
                pair = (address((page.get('token') or {}).get('address')),
                        address((page.get('quote') or {}).get('address'), zero=True))
                if not all(pair) or currencies is not None and currencies != pair:
                    raise ReadStopped('trade currencies unavailable')
                currencies = pair
                parsed = []
                for item in batch:
                    if not isinstance(item, dict):
                        raise ReadStopped('trade row unavailable')
                    block, stamp = number(item.get('block')), number(item.get('time'))
                    tx = hash32(item.get('transactionHash'))
                    if not block or stamp is None or not tx or item.get('side') not in ('buy', 'sell'):
                        raise ReadStopped('trade row unavailable')
                    parsed.append((block, stamp, tx, item))
                if any(a[0] < b[0] or a[1] < b[1] for a, b in zip(parsed, parsed[1:])):
                    raise ReadStopped('trade ordering unavailable')
                scan = number(page.get('scannedFromBlock'))
                head = number(page.get('blockNumber'))
                if scan is not None:
                    scanned_from = min(scan, scanned_from) if scanned_from is not None else scan
                if scanned_through is None:
                    scanned_through = head
                if parsed:
                    stamp = min(t for _, t, _, _ in parsed)
                    oldest = min(oldest, stamp) if oldest is not None else stamp
                signature = tuple((b, t, h) for b, t, h, _ in parsed)
                if signature == previous and page['more']:
                    raise ReadStopped('trade paging did not advance')
                previous = signature
                in_page = set()
                for block, stamp, tx, item in parsed:
                    # A transaction can swap more than once. Without an event
                    # index, repeated tx rows cannot be allocated unambiguously.
                    event = (tx, item.get('logIndex'))
                    fingerprint = (block, stamp, item.get('side'), item.get('tokenAmount'),
                                   item.get('quoteAmount'), item.get('feePips'))
                    if event in seen:
                        if event in in_page or seen[event] != fingerprint:
                            self.note('ambiguous trade event omitted')
                            rows = [r for r in rows if r['txHash'] != tx]
                            seen[event] = None
                            ambiguous = True
                        continue
                    seen[event] = fingerprint
                    in_page.add(event)
                    if start <= stamp <= end and block <= through_block:
                        if self.trade_rows >= 500:
                            raise ReadStopped('trade row cap reached')
                        self.trade_rows += 1
                        sold = pair[1] if item['side'] == 'buy' else pair[0]
                        amount = number(item.get('quoteAmount' if item['side'] == 'buy' else 'tokenAmount'))
                        fee = number(item.get('feePips'), maximum=999999)
                        rows.append({'txHash': tx, 'blockNumber': str(block), 'time': stamp,
                                     'currency': sold, 'amount': str(amount) if amount is not None else None,
                                     'feePips': fee, 'poolId': pool})
                reached_start = scanned_from == 0 or oldest is not None and oldest < start
                if page['more'] is False or reached_start:
                    complete = (page.get('complete') is True and not ambiguous and reached_start
                                and scanned_through is not None and scanned_through >= through_block)
                    if not complete:
                        self.note('trade history incomplete')
                    break
                properties = self.trade_schema()
                cursor = page.get('nextCursor')
                if ('cursor' in properties and isinstance(cursor, str)
                        and re.fullmatch(r'[A-Za-z0-9_:.=-]{1,256}', cursor)
                        and cursor != args.get('cursor')):
                    args = {'poolId': pool, 'cursor': cursor}
                elif 'offset' in properties:
                    if not batch:
                        raise ReadStopped('trade paging did not advance')
                    args = {'poolId': pool, 'offset': args.get('offset', 0) + len(batch)}
                elif 'beforeBlock' in properties and parsed:
                    # Overlap the boundary block for both inclusive and
                    # exclusive servers. A full block that cannot be drained
                    # stalls visibly instead of silently skipping its swaps.
                    boundary = min(b for b, _, _, _ in parsed) + 1
                    value = str(boundary) if properties['beforeBlock'].get('type') == 'string' else boundary
                    if value == args.get('beforeBlock'):
                        raise ReadStopped('trade block boundary incomplete')
                    args = {'poolId': pool, 'beforeBlock': value}
                else:
                    raise ReadStopped('trade paging unavailable')
        except ReadStopped as error:
            self.note(str(error))
        return {'poolId': pool, 'trades': rows, 'complete': complete,
                'scannedFromBlock': scanned_from, 'throughBlock': scanned_through}

    def funding(self, wallet, block):
        """First observed inbound per transfer kind, bounded pages and depth.

        These are historical relationship leads, never project-control labels
        and never edges in an income allocation's economic payer path.
        """
        paths, visited = [], set()
        queue = [(wallet, [], block)]
        while queue:
            current, path, before = queue.pop(0)
            if len(path) >= self.depth or current in visited:
                continue
            visited.add(current)
            for action in ('txlist', 'txlistinternal', 'tokentx'):
                found = None
                for page in range(1, 4):
                    data = self.optional_es(module='account', action=action, address=current,
                                            startblock=0, endblock=before, page=page, offset=100, sort='asc')
                    if data is None:
                        break
                    for row in data:
                        if not isinstance(row, dict):
                            continue
                        sender, destination = address(row.get('from')), address(row.get('to'))
                        height, amount, tx = number(row.get('blockNumber')), number(row.get('value')), hash32(row.get('hash'))
                        if (not sender or sender == current or destination != current or not height
                                or height > before or not amount or not tx
                                or row.get('isError', '0') != '0'
                                or row.get('txreceipt_status', '1') != '1'):
                            continue
                        currency = address(row.get('contractAddress')) if action == 'tokentx' else ZERO
                        if not currency:
                            continue
                        found = {'from': sender, 'to': current, 'currency': currency,
                                 'amount': str(amount), 'txHash': tx, 'blockNumber': str(height),
                                 'kind': action}
                        break
                    if found or len(data) < 100:
                        break
                    if page == 3:
                        self.note('funding history page cap reached')
                if found:
                    observed = [found, *path]
                    paths.append({'path': observed, 'edgesRead': True, 'controlProven': False})
                    queue.append((found['from'], observed, int(found['blockNumber'])))
        return {'wallet': wallet, 'depth': self.depth, 'paths': paths,
                'relationshipsComplete': False, 'scope': 'first inbound per transfer kind; bounded history'}

    def creation(self, token, block):
        rows = self.optional_es(module='contract', action='getcontractcreation', contractaddresses=token)
        if not isinstance(rows, list) or len(rows) != 1:
            return {}
        row = rows[0]
        if not isinstance(row, dict) or address(row.get('contractAddress')) != token:
            return {}
        tx_hash = hash32(row.get('txHash'))
        if not tx_hash:
            return {}
        tx = self.get_tx(tx_hash)
        if (not isinstance(tx, dict) or hash32(tx.get('hash')) != tx_hash
                or not integer(tx.get('blockNumber')) or integer(tx['blockNumber']) > block):
            return {}
        sender = address(tx.get('from'))
        result = {'txHash': tx_hash, 'deploymentSender': sender,
                  'factory': address(row.get('contractCreator')), 'deployer': None}
        if tx.get('to') is None:
            receipt = self.optional_es(module='proxy', action='eth_getTransactionReceipt', txhash=tx_hash)
            if (isinstance(receipt, dict) and hash32(receipt.get('transactionHash')) == tx_hash
                    and integer(receipt.get('status')) == 1 and address(receipt.get('contractAddress')) == token
                    and integer(receipt.get('blockNumber')) == integer(tx['blockNumber'])):
                result['deployer'] = sender
        return result

    def read(self, token, days=7):
        from .hookr_collect import collect
        return collect(self, token, days)


def read_income(token, **options):
    days = options.pop('days', 7)
    return Reader(**options).read(token, days)


def main(argv=None):
    parser = PublicParser(description='read bounded hookr income evidence on chain 4663')
    parser.add_argument('--token', required=True)
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--depth', type=int, default=2)
    parser.add_argument('--max-etherscan', type=int, default=800)
    parser.add_argument('--run-seconds', type=float, default=900)
    parser.add_argument('--max-swap-txs', type=int, default=64)
    parser.add_argument('--etherscan-rps', type=float, default=4)
    parser.add_argument('--trace-share-bps', type=int, default=8000)
    parser.add_argument('--workers', type=int, default=5)
    parser.add_argument('--hub-counterparties', type=int, default=50)
    parser.add_argument('--funding-lookback-days', type=int, default=7)
    parser.add_argument('--reconciliation-tolerance-bps', type=int, default=1000)
    parser.add_argument('--public', action='store_true', default=True, help='public text: classes and shares, no wallet addresses')
    parser.add_argument('--max-mcp', type=int, default=40)
    parser.add_argument('--json', action='store_true', help='private evidence sidecar; takes precedence over --public')
    parser.add_argument('--card', action='store_true', help='attach the live evidence to a token card')
    args = parser.parse_args(argv)
    if not address(args.token) or not 1 <= args.days <= 30 or not 0 <= args.depth <= 4 or not 0 <= args.max_etherscan <= 4000 or not 0 <= args.max_mcp <= 40 or not 0 < args.run_seconds <= 3600 or not 0 <= args.max_swap_txs <= 400 or not .1 <= args.etherscan_rps <= 8 or not 1 <= args.hub_counterparties <= 1000 or not 1 <= args.funding_lookback_days <= 30 or not 0 <= args.reconciliation_tolerance_bps <= 10000:
        parser.exit(2, 'invalid live read arguments\n')
    if not 1 <= args.trace_share_bps <= 10000 or not 1 <= args.workers <= 16:
        parser.exit(2, 'invalid live read arguments\n')
    data = read_income(args.token, days=args.days, depth=args.depth,
                       max_etherscan=args.max_etherscan, max_mcp=args.max_mcp,
                       run_seconds=args.run_seconds, max_swap_txs=args.max_swap_txs,
                       hub_counterparties=args.hub_counterparties, funding_lookback_days=args.funding_lookback_days,
                       reconciliation_tolerance_bps=args.reconciliation_tolerance_bps, etherscan_rps=args.etherscan_rps,
                       trace_share_bps=args.trace_share_bps, workers=args.workers)
    if args.json:
        from .income import private_sidecar
        print(json.dumps(private_sidecar(data), sort_keys=True))
    elif args.card:
        from .card import token_card
        from .income import public_text
        print(public_text(token_card(args.token, {}, hookr_income=data).render()))
        if classify(args.token, data) is None:
            print('hookr live: partial · ' + '; '.join(data['live']['reasons']))
    else:
        result = classify(args.token, data)
        print('\n'.join(result['lines']) if result else 'hookr live: partial · ' + '; '.join(data['live']['reasons']))


if __name__ == '__main__':
    from rh_preflight.hookr_live import main as entry
    entry()
