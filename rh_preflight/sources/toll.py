"""Toll project reads and optional unsigned transaction simulation."""
import json
from dataclasses import dataclass, replace
import threading
import time
import re
import unicodedata
from urllib.parse import quote
from .common import plural, Absent, Keyless, Reading, field, money
from ..card import link
from ..extract import ADDRESS
from ..hey import Unavailable
from ..settings import toll_header

TOOLS = {'agent': ('check_agent', 'id'), 'hook': ('check_hook', 'address'), 'stock': ('stock_token', 'id')}
NUMBER = r'[0-9]+(?:\.[0-9]+)?'
# a whole check_coin answer is kept this long per address, so repeats do not spend the caller's free daily quota
COIN_TTL = 600
# Toll's tx route takes a caller time budget in whole milliseconds and answers
# inside it with what it has read by then. Bounds are Toll's documented ones.
BUDGET_MIN_MS, BUDGET_MAX_MS = 300, 30000
# Left out of the budget for the request and the reply to cross the network.
BUDGET_ROUND_TRIP = .4
BUY_BUDGET_MAX_MS = 25000


@dataclass(frozen=True)
class BeforeBuyReading(Reading):
    """Only the validated, Toll-attributed before-buy projection may carry prose."""
    decision: str = ''
    wait_seconds: int | None = None


def parse_before_buy(data, target):
    result = data['result']
    if result.get('isError'):
        raise ValueError()
    blocks = result['content']
    if not isinstance(blocks, list):
        raise ValueError()
    body = '\n'.join(b['text'] for b in blocks if b.get('type') == 'text')
    if any(c != '\n' and (not c.isprintable() or unicodedata.category(c).startswith('C')) for c in body):
        raise ValueError()
    lines = body.split('\n')
    decisions = [line for line in lines if line.lstrip().startswith('Decision:')]
    tokens = [line for line in lines if line.lstrip().startswith('Token:')]
    if len(decisions) != 1 or not re.fullmatch(r'Decision: (BLOCK|WARN|ALLOW|WAIT)', decisions[0]):
        raise ValueError()
    if len(tokens) != 1 or not re.fullmatch(r'Token: ' + re.escape(target) + r'(?: \(Robinhood Chain\))?', tokens[0], re.I):
        raise ValueError()
    word = decisions[0].split(': ')[1]
    reasons = []
    if lines.count('Reasons:') > 1:
        raise ValueError()
    in_reasons = False
    for line in lines:
        if line == 'Reasons:':
            if in_reasons or reasons:
                raise ValueError()
            in_reasons = True
        elif in_reasons and line.startswith('- '):
            reason = line[2:]
            if not reason or re.search(r'https?://|www\.', reason, re.I):
                raise ValueError()
            reason = re.sub(r'\s*[\u2013\u2014]\s*', ', ', reason)
            reasons.append(reason if len(reason) <= 160 else reason[:159].rstrip() + '…')
        else:
            in_reasons = False
    if len(reasons) > 3 or (word in {'WARN', 'BLOCK'} and not reasons):
        raise ValueError()
    wait_seconds = None
    if word == 'WAIT':
        waits = re.findall(r'Call pre_buy_check again in ([0-9]{1,8}) s\b', body)
        wait_seconds = int(waits[0]) if len(waits) == 1 else None
        detail = 'still reading' + (f', ask again in {waits[0]} s' if len(waits) == 1 else '')
    elif word == 'ALLOW':
        detail = 'nothing against was found in what was read'
    else:
        detail = reasons[0] + (f' · +{len(reasons) - 1} more' if len(reasons) > 1 else '')
    ages = [line for line in lines if line.startswith('Made from our last whole read, ')]
    if len(ages) > 1:
        raise ValueError()
    if ages:
        age = re.fullmatch(r'Made from our last whole read, ([0-9]{1,8}) (s|min) ago(?:[.;].*)?', ages[0])
        if not age:
            raise ValueError()
        detail += f' · from a read {age[1]} {age[2]} ago'
    reports = [line for line in lines if line.startswith('Full report:')]
    if len(reports) > 1:
        raise ValueError()
    if reports:
        page = re.fullmatch(r'Full report: (https://t0ll\.xyz/check/' + re.escape(target) + r')(?:\?[^\s#]*)?', reports[0], re.I)
        if not page or not reports[0].startswith('Full report: https://t0ll.xyz/check/'):
            raise ValueError()
        detail += ' · https://t0ll.xyz/check/' + target.lower()
    return BeforeBuyReading('Toll', 'present', f'Toll before buy: {word} · {detail}',
                            decision=word, wait_seconds=wait_seconds)


def toll_key():
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        return None
    return get_secret('TOLL_API_KEY')


class Client(Keyless):
    origin = 'https://t0ll.xyz'
    label = 'Toll'
    page = origin
    probe_target = 'NVDA'
    accept = 'application/json, text/event-stream'

    def __init__(self, *args, sleep=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tx_headers = threading.local()
        self.sleep = sleep if sleep is not None else time.sleep

    def request_headers(self, path):
        # Send the optional partner key only on the exact production host
        # and check route.
        from urllib.parse import urlsplit
        if (path not in {'/api/tx/check', '/mcp'} or urlsplit(self.base_url).hostname != 't0ll.xyz'
                or path == '/mcp' and not hasattr(self.tx_headers, 'key_header')):
            return {}
        key = toll_key()
        if key:
            header = toll_header(getattr(self.tx_headers, 'key_header', 'x-toll-pass'))
            return {header: 'Bearer ' + key if header == 'Authorization' else key}
        return {}

    def _read(self, path, deadline, payload=None):
        if path == '/mcp' and isinstance(payload, dict) and (payload.get('params') or {}).get('name') == 'pre_buy_check':
            # One full-deadline attempt, including cooldown and Retry-After.
            return self._read_once(path, deadline, payload)
        if path != '/api/tx/check':
            return super()._read(path, deadline, payload)
        from ..hey import TemporaryUnavailable
        # A normal swap can take 14 seconds. Do not interrupt a healthy first
        # attempt after the builder transport's two-second retry window.
        try:
            return self._read_once(path, deadline, self.budgeted(payload, deadline))
        except TemporaryUnavailable:
            if deadline - self.clock() <= .01:
                raise
            # The retry has less time left, so it states a smaller budget.
            return self._read_once(path, deadline, self.budgeted(payload, deadline))

    def budgeted(self, payload, deadline):
        """Add budgetMs: the time left before this deadline, less the round trip.

        Toll then answers before the deadline. A coin it has not read by then is
        the stable coin-not-read warning beside the completed simulation, in place
        of a cut connection. Too little time left sends the request unchanged.
        """
        if not isinstance(payload, dict):
            return payload
        left = (deadline - self.clock() - BUDGET_ROUND_TRIP) * 1000
        # The comparison is false for NaN, so an unusable clock sends no budget.
        if not left >= BUDGET_MIN_MS:
            return payload
        return {**payload, 'budgetMs': round(min(left, BUDGET_MAX_MS))}

    def check_tx(self, payload, deadline, key_header="x-toll-pass"):
        from ..hey import Unavailable
        self.tx_headers.key_header = key_header
        try:
            status, data = self._read('/api/tx/check', deadline, payload)
            if status == 200 and isinstance(data.get('x-toll'), dict):
                return data
        except Unavailable:
            pass
        finally:
            del self.tx_headers.key_header
        return None

    def _decode(self, raw):
        # Streamable HTTP may answer with JSON or a single SSE JSON-RPC event.
        if raw.lstrip().startswith((b'data:', b'event:', b':')):
            events = raw.decode('utf-8').replace('\r\n', '\n').split('\n\n')
            for event in events:
                parts = [line[5:].lstrip() for line in event.splitlines() if line.startswith('data:')]
                if parts:
                    row = self._decode_json('\n'.join(parts))
                    if isinstance(row, dict) and row.get('id') == 1:
                        return row
            raise ValueError()
        return self._decode_json(raw)

    @staticmethod
    def _decode_json(raw):
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get('x-toll'), dict) and 'take' in data['x-toll']:
            # Keep USD number tokens exact without changing other Toll schemas.
            data['x-toll']['take'] = json.loads(raw, parse_float=str)['x-toll']['take']
        return data

    def target_page(self, target=None, kind='stock', **kwargs):
        if kind == 'agent' and str(target).isdigit():
            return self.page + '/agents/census?agent=' + str(target)
        if kind == 'hook' and ADDRESS.fullmatch(str(target)):
            return self.page + '/api/hook/' + target
        return self.page + '/markets/' + quote(target, safe='') if isinstance(target, str) else self.page

    @staticmethod
    def reader_path(target):
        return '/api/markets/stock/' + quote(target, safe='')

    def fetch(self, target, deadline, kind='stock', coin_findings='silent', before_buy=False,
              key_header='x-toll-pass', wait_max_seconds=30, on_wait=None):
        if kind == 'stock':
            if not isinstance(target, str) or not re.fullmatch(r'[A-Za-z0-9.]{1,60}|0x[0-9a-fA-F]{40}', target):
                return Reading(self.label, 'unlisted', '')
            # This free route establishes Robinhood Stock Token membership. A
            # non-stock 404 on a ticker ends here, without calling another method.
            try:
                data = self.read(self.reader_path(target), deadline)
            except Absent:
                if not ADDRESS.fullmatch(target):
                    raise
                # Not a Stock Token: Toll's free coin check, fixed Figures lines only.
                coin = self.coin(target, deadline) if coin_findings == 'line' else Reading(self.label, 'unlisted', '')
                if before_buy:
                    buy = self.before_buy(target, deadline, key_header, wait_max_seconds, on_wait)
                    if buy.line:
                        return replace(buy, line='\n'.join(line for line in (coin.line, buy.line) if line))
                return coin
            return self.stock(data, target)
        tool, argument = TOOLS[kind]
        if kind == 'hook' and not ADDRESS.fullmatch(str(target)):
            raise ValueError()
        if kind == 'agent' and (not str(target).isdigit() or not 0 <= int(target) <= 1000000000):
            raise ValueError()
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': tool, 'arguments': {argument: int(target) if kind == 'agent' else target}}}
        data = self.read('/mcp', deadline, payload=payload)
        return self.parse(data, kind)

    def before_buy(self, target, deadline, key_header='x-toll-pass', wait_max_seconds=30, on_wait=None):
        """The caller has established a non-stock 404 under this deadline."""
        silent = Reading(self.label, 'unlisted', '')
        if not isinstance(target, str) or not ADDRESS.fullmatch(target):
            return silent
        target = target.lower()
        flight_key = ('before_buy', target)
        retry_deadline = deadline + wait_max_seconds
        joined = not self.inflight.acquire(flight_key, timeout=0)
        if joined:
            if not self.inflight.acquire(flight_key, timeout=max(0, deadline - self.clock())):
                return silent
        try:
            with self.lock:
                kept = getattr(self, '_before_buys', {}).get(target)
                if kept and self.clock() - kept[0] < COIN_TTL:
                    elapsed = int(max(0, self.clock() - kept[0]))
                    def age(match):
                        seconds = int(match[1]) * (60 if match[2] == 'min' else 1) + elapsed
                        return f'from a read {seconds // 60} min ago' if seconds >= 60 else f'from a read {seconds} s ago'
                    return replace(kept[1], line=re.sub(r'from a read ([0-9]+) (s|min) ago', age, kept[1].line))
            # A caller joining this flight must not start another WAIT loop.
            # Whole answers above are shared; an unfinished or failed read is silent.
            if joined:
                return silent
            self.tx_headers.key_header = key_header
            waited, waits = 0, 0
            call_deadline = deadline
            for attempt in range(3):
                left = (call_deadline - self.clock() - BUDGET_ROUND_TRIP) * 1000
                if self.clock() >= call_deadline or left != left:
                    return silent
                budget = max(BUDGET_MIN_MS, round(min(BUY_BUDGET_MAX_MS, left)))
                payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': 'pre_buy_check', 'arguments': {'address': target, 'budget_ms': budget}}}
                status, data = self._read('/mcp', call_deadline, payload)
                if status != 200:
                    return silent
                reading = parse_before_buy(data, target)
                if self.clock() >= call_deadline:
                    return silent
                seconds = reading.wait_seconds
                if (reading.decision != 'WAIT' or attempt == 2 or wait_max_seconds == 0 or seconds is None
                        or seconds > wait_max_seconds - waited or self.clock() + seconds >= retry_deadline):
                    break
                if on_wait is not None:
                    on_wait()
                self.sleep(seconds)
                waited += seconds
                waits += 1
                call_deadline = retry_deadline
            if waits:
                detail = f'after one wait of {waited} s' if waits == 1 else f'after {waits} waits totalling {waited} s'
                reading = replace(reading, line=reading.line + ' · ' + detail)
            if reading.decision != 'WAIT':
                with self.lock:
                    cache = self.__dict__.setdefault('_before_buys', {})
                    cache[target] = (self.clock(), reading)
                    while len(cache) > 512:
                        cache.pop(next(iter(cache)))
            return reading
        except (Unavailable, Absent, KeyError, TypeError, ValueError, AttributeError, IndexError, OverflowError):
            return silent
        finally:
            self.tx_headers.__dict__.pop('key_header', None)
            self.inflight.release(flight_key)

    def buy_check(self, target, deadline, key_header='x-toll-pass', wait_max_seconds=30, on_wait=None):
        """Establish non-stock membership first; failures add no extra line."""
        try:
            reading = self.fetch(target, deadline, before_buy=True, key_header=key_header,
                                 wait_max_seconds=wait_max_seconds, on_wait=on_wait)
            if isinstance(reading, BeforeBuyReading):
                return reading
        except (Unavailable, Absent, KeyError, TypeError, ValueError, AttributeError, IndexError, OverflowError):
            pass
        return Reading(self.label, 'unlisted', '')

    def stock(self, data, target):
        address = data.get('address')
        if ADDRESS.fullmatch(str(target)) and (not ADDRESS.fullmatch(str(address))
                                              or address.lower() != target.lower()):
            return Reading(self.label, 'present',
                           'Toll: response address differs from requested address; '
                           'official Stock Token status not established · https://t0ll.xyz')
        registry = data.get('registry') or {}
        ticker = data.get('ticker')
        official = (registry.get('source') == 'https://api.robinhood.com/rhj/assets'
                    and ADDRESS.fullmatch(str(data.get('address')))
                    and ADDRESS.fullmatch(str(target)) and address.lower() == target.lower()
                    and isinstance(ticker, str) and re.fullmatch(r'[A-Z0-9.]{1,20}', ticker)
                    and re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}[0-9]', str(data.get('isin')))
                    and data.get('status') == 'ASSET_STATUS_ACTIVE')
        prices = data.get('prices')
        prices = prices if isinstance(prices, dict) else {}
        chain, reference = prices.get('chain'), prices.get('reference')
        chain = chain if isinstance(chain, dict) else {}
        reference = reference if isinstance(reference, dict) else {}
        facts = dict(stock_address=address.lower() if official else None,
                     stock_price_usd=chain.get('usd') if official and type(chain.get('usd')) in (int, float, str) else None,
                     stock_price_as_of=chain.get('as_of') if official and isinstance(chain.get('as_of'), str) else None)
        if (not {'usd', 'as_of'} <= chain.keys()
                or not {'usd', 'updated_at'} <= reference.keys()):
            # Keep the pre-0.5.6 source state and HEY rules. Independently
            # established identity is available only to the optional valuation.
            return replace(self.unavailable(target), **facts)
        line = (f"chain price ${money(chain['usd'])} read {field(chain['as_of'])}; "
                f"Chainlink reference ${money(reference['usd'])} updated {field(reference['updated_at'])}")
        if official:
            line = (f'official Robinhood Stock Token {field(ticker)}, ISIN {field(data["isin"])}, '
                    f'status {field(data["status"])}, registry as of {field(registry.get("as_of"))}; ' + line)
        return Reading(self.label, 'present', f'{self.label}: {line} · {self.public_page(data.get("page"))}',
                       stock_ticker=ticker if official else None, **facts)

    def coin(self, target, deadline):
        flight_key = ('coin', target.lower())
        if not self.inflight.acquire(flight_key, timeout=max(0, deadline - self.clock())):
            return Reading(self.label, 'unlisted', '')
        try:
            return self._coin(target, deadline)
        except (Unavailable, Absent, KeyError, TypeError, ValueError, AttributeError, IndexError):
            return Reading(self.label, 'unlisted', '')
        finally:
            self.inflight.release(flight_key)

    def _coin(self, target, deadline):
        key = target.lower()
        with self.lock:
            kept = getattr(self, '_coins', {}).get(key)
            if kept and self.clock() - kept[0] < COIN_TTL:
                return kept[1]
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'check_coin', 'arguments': {'address': key}}}
        try:
            # ttl=0: only a whole answer is kept (below); a still-reading one is asked again next time
            result = self.read('/mcp', deadline, ttl=0, payload=payload)['result']
        except (Unavailable, KeyError, TypeError, ValueError):
            # a timeout, 429, 5xx or unreadable answer is silent, like a 404 or still reading
            return Reading(self.label, 'unlisted', '')
        if result.get('isError'):
            return Reading(self.label, 'unlisted', '')
        blocks = result['content']
        if not isinstance(blocks, list):
            raise ValueError()
        lines = [l.strip() for l in '\n'.join(b['text'] for b in blocks if b.get('type') == 'text').splitlines()]
        # Only a whole answer about this exact token: a still-reading answer, or one naming another
        # token, gives no facts. Verdict words, finding prose and names are never copied.
        if 'Code: still_reading' in lines or not any(l.lower() == 'token: ' + target.lower() for l in lines):
            return Reading(self.label, 'unlisted', '')
        facts = []
        for line in lines:
            match = re.fullmatch(r'Launched on ([A-Za-z][A-Za-z0-9 .]{0,30}), ([0-9]+) days? ago\..*', line)
            if match:
                facts.append(f'launched on {field(match[1])} {match[2]} {plural(match[2], "day")} ago')
            match = re.fullmatch(r"- Maker's launches: ([0-9,]+) launch(?:es)?(?:\b.*)?", line)
            if match:
                count = match[1].replace(',', '')
                facts.append(f'maker has {count} {plural(count, "launch")}')
            match = re.fullmatch(r'- Flagged wallets: ([0-9,]+), bought (' + NUMBER + r')%', line)
            if match:
                count = match[1].replace(',', '')
                facts.append(f"{count} {plural(count, 'wallet')} on Toll's flag list bought {match[2]}%")
            match = re.fullmatch(r'- Maker holds now: (' + NUMBER + r')% of supply.*', line)
            if match:
                facts.append(f'maker holds {match[1]}%')
        page = self.page + '/check/' + key
        reading = Reading(self.label, 'present' if facts else 'unlisted',
                          f'{self.label}: ' + '; '.join(facts[:4]) + ' · ' + page if facts else '')
        if facts:
            with self.lock:
                coins = self.__dict__.setdefault('_coins', {})
                coins[key] = (self.clock(), reading)
                while len(coins) > 512:
                    coins.pop(next(iter(coins)))
        return reading

    def public_page(self, candidate):
        # Never relay paid API URLs from a response, even as a citation.
        candidate = link(candidate) if isinstance(candidate, str) else None
        return link(candidate) if isinstance(candidate, str) and re.fullmatch(r'https://t0ll\.xyz/(?:markets/[^\s?]+|agents/census\?[^\s]+|api/hook/[^\s]+)', candidate) and '/api/x402/' not in candidate else self.page

    def parse(self, data, kind):
        result = data['result']
        if result.get('isError'):
            return Reading(self.label, 'unlisted', '')
        blocks = result['content']
        if not isinstance(blocks, list):
            raise ValueError()
        # Inspect lines with complete fixed patterns. Never copy unmatched prose,
        # card names, descriptions, rankings, or agent-authored quotes.
        lines = '\n'.join(b['text'] for b in blocks if b.get('type') == 'text').splitlines()
        facts = []
        page = self.page
        for line in lines:
            if re.fullmatch(r'(?:Full census row: |Full page: )?https://t0ll\.xyz/\S+', line):
                page = self.public_page(line.split(' ')[-1])
        if kind == 'hook':
            if 'Its code can be changed by its owner: it is an upgradeable proxy.' in lines:
                facts.append('hook is upgradeable')
            elif any(l in lines for l in ('Its code cannot be changed: it is not a proxy.', 'Its code is fixed: it is not an upgradeable proxy.')):
                facts.append('hook code is fixed')
            for line in lines:
                match = re.fullmatch(r"Toll's chain data has ([0-9]+) Uniswap v4 pools? on this hook on Robinhood Chain\.", line)
                if match:
                    facts.append(f'{field(match[1])} {plural(match[1], "pool")} on this hook')
        elif kind == 'agent':
            card = 'Card: loads and parses, host data:.' in lines or any(re.fullmatch(r'Card: loads and parses, host https?://[^\s]+\.', l) for l in lines)
            mcp = 'MCP: declared, did not answer an initialize call.' in lines
            paid_answer = 'x402: answered HTTP 402 with payment terms.' in lines
            mcp_answer = any(l in lines for l in ('MCP: answered an initialize call.', 'MCP: declared, answered an initialize call.'))
            if card:
                facts.append('card loads' + ('; MCP did not answer' if mcp else '; MCP answered' if mcp_answer else '') + ('; x402 answered HTTP 402' if paid_answer else ''))
            if 'payTo is the registered agentWallet: yes.' in lines:
                facts.append('payTo matches registered agentWallet')
            elif 'payTo is the registered agentWallet: no.' in lines:
                facts.append('payTo differs from registered agentWallet')
        elif kind == 'stock':
            for line in lines:
                match = re.fullmatch(r'- difference vs reference: -?' + NUMBER + r'% \(chain \$(' + NUMBER + r') read ([0-9 :\-]+ UTC); reference \$(' + NUMBER + r') updated ([0-9 :\-]+ UTC)\)\.', line)
                if match:
                    facts.append(f'chain price ${match[1]} read {field(match[2])}; Chainlink reference ${match[3]} updated {field(match[4])}')
        else:
            raise ValueError()
        return Reading(self.label, 'present' if facts else 'unlisted', f'{self.label}: ' + '. '.join(facts[:2]) + ' · ' + page if facts else '')
