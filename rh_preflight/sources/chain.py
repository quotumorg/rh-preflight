"""Read-only RPC contract identity at one explicit block."""
import re
import threading
from ..extract import ADDRESS
from .common import Keyless, Reading, field, Unavailable

# Successful chain identity is checked once per process and origin. Failed reads
# are retriable. Loopback origins remain independent for offline tests.
VERIFIED = set()
VERIFY_LOCK = threading.Lock()


def call(method, params, ident=1):
    return {'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params}


def results(data):
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise ValueError()
    ids = [r['id'] for r in data]
    if len(ids) != len(set(ids)):
        raise ValueError()
    return {r['id']: r for r in data}


def abi_string(row):
    try:
        if 'error' in row:
            return 'not readable'
        raw = bytes.fromhex(row['result'][2:])
        # ERC-20s may return either ABI strings or legacy bytes32 strings.
        if len(raw) == 32:
            return field(raw.rstrip(b'\0').decode('utf-8'), 'not readable')
        if len(raw) < 64 or int.from_bytes(raw[:32], 'big') != 32:
            return 'not readable'
        size = int.from_bytes(raw[32:64], 'big')
        if not size or size > len(raw) - 64:
            return 'not readable'
        return field(raw[64:64+size].decode('utf-8'), 'not readable')
    except (ValueError, KeyError, TypeError, UnicodeError):
        return 'not readable'


class Client(Keyless):
    origin = 'https://rpc.mainnet.chain.robinhood.com'
    label = 'Robinhood Chain'
    page = 'https://robinhoodchain.blockscout.com'

    def rpc(self, payload, deadline):
        return self.read('', deadline, payload=payload)

    def verify(self, deadline):
        if not VERIFY_LOCK.acquire(timeout=max(0, deadline - self.clock())):
            raise Unavailable()
        try:
            if self.base_url not in VERIFIED:
                row = self.rpc(call('eth_chainId', []), deadline)
                if row.get('result') != '0x1237':
                    raise Unavailable()
                VERIFIED.add(self.base_url)
        finally:
            VERIFY_LOCK.release()

    def target_page(self, target=None, **kwargs):
        return self.page + "/address/" + target if isinstance(target, str) and ADDRESS.fullmatch(target) else self.page

    def fetch(self, target, deadline, hey_name=None):
        if not isinstance(target, str) or not ADDRESS.fullmatch(target):
            return Reading(self.label, 'unlisted', '')
        self.verify(deadline)
        rows = results(self.rpc([call('eth_getCode', [target, 'latest']), call('eth_blockNumber', [], 2)], deadline))
        code = rows[1]['result']
        if not isinstance(code, str) or not re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*', code):
            raise ValueError()
        page = self.page + '/address/' + target
        if code == '0x':
            return Reading(self.label, 'no_code', f'{self.label}: no contract code at this address · {page}')
        head = rows.get(2, {}).get('result')
        if head is not None and (not isinstance(head, str) or not re.fullmatch(r'0x[0-9a-fA-F]+', head)):
            raise ValueError()
        block = str(int(head, 16)) if head else 'not readable'
        read_block = head or 'latest'
        metadata = results(self.rpc([
            call('eth_call', [{'to': target, 'data': '0x06fdde03'}, read_block]),
            call('eth_call', [{'to': target, 'data': '0x95d89b41'}, read_block], 2)], deadline))
        name, symbol = abi_string(metadata.get(1, {})), abi_string(metadata.get(2, {}))
        note = f', HEY project name {field(hey_name)}' if hey_name and field(hey_name) != name else ''
        return Reading(self.label, 'code', f'{self.label}: contract, name {name}, symbol {symbol}{note}, read at block {block} · {page}')
