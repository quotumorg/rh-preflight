"""Only Toll's free agent, hook and stock reads; fixed fact projections."""
import json
import re
from urllib.parse import quote
from .common import plural, Absent, Keyless, Reading, field, money
from ..card import link
from ..extract import ADDRESS

TOOLS = {'agent': ('check_agent', 'id'), 'hook': ('check_hook', 'address'), 'stock': ('stock_token', 'id')}
NUMBER = r'[0-9]+(?:\.[0-9]+)?'


class Client(Keyless):
    origin = 'https://t0ll.xyz'
    label = 'Toll'
    page = origin
    probe_path = '/mcp'
    accept = 'application/json, text/event-stream'

    def _decode(self, raw):
        # Streamable HTTP may answer with JSON or a single SSE JSON-RPC event.
        if raw.lstrip().startswith((b'data:', b'event:', b':')):
            events = raw.decode('utf-8').replace('\r\n', '\n').split('\n\n')
            for event in events:
                parts = [line[5:].lstrip() for line in event.splitlines() if line.startswith('data:')]
                if parts:
                    row = json.loads('\n'.join(parts))
                    if isinstance(row, dict) and row.get('id') == 1:
                        return row
            raise ValueError()
        return super()._decode(raw)

    def target_page(self, target=None, kind='stock', **kwargs):
        if kind == 'agent' and str(target).isdigit():
            return self.page + '/agents/census?agent=' + str(target)
        if kind == 'hook' and ADDRESS.fullmatch(str(target)):
            return self.page + '/api/hook/' + target
        return self.page + '/markets/' + quote(target, safe='') if isinstance(target, str) else self.page

    def fetch(self, target, deadline, kind='stock'):
        if kind == 'stock':
            if not isinstance(target, str) or not re.fullmatch(r'[A-Za-z0-9.]{1,60}|0x[0-9a-fA-F]{40}', target):
                return Reading(self.label, 'unlisted', '')
            # This free route establishes Robinhood Stock Token membership. A
            # non-stock 404 on a ticker ends here, without calling another method.
            try:
                data = self.read('/api/markets/stock/' + quote(target, safe=''), deadline)
            except Absent:
                if not ADDRESS.fullmatch(target):
                    raise
                # Not a Stock Token: Toll's free coin check, fixed Figures lines only.
                return self.coin(target, deadline)
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

    def stock(self, data, target):
        address = data.get('address')
        if ADDRESS.fullmatch(str(target)) and (not ADDRESS.fullmatch(str(address))
                                              or address.lower() != target.lower()):
            return Reading(self.label, 'present',
                           'Toll: response address differs from requested address; '
                           'official Stock Token status not established · https://t0ll.xyz')
        prices = data['prices']
        chain, reference = prices['chain'], prices['reference']
        line = (f"chain price ${money(chain['usd'])} read {field(chain['as_of'])}; "
                f"Chainlink reference ${money(reference['usd'])} updated {field(reference['updated_at'])}")
        registry = data.get('registry') or {}
        ticker = data.get('ticker')
        official = (registry.get('source') == 'https://api.robinhood.com/rhj/assets'
                    and ADDRESS.fullmatch(str(data.get('address')))
                    and ADDRESS.fullmatch(str(target)) and address.lower() == target.lower()
                    and isinstance(ticker, str) and re.fullmatch(r'[A-Z0-9.]{1,20}', ticker)
                    and re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}[0-9]', str(data.get('isin')))
                    and data.get('status') == 'ASSET_STATUS_ACTIVE')
        if official:
            line = (f'official Robinhood Stock Token {field(ticker)}, ISIN {field(data["isin"])}, '
                    f'status {field(data["status"])}, registry as of {field(registry.get("as_of"))}; ' + line)
        return Reading(self.label, 'present', f'{self.label}: {line} · {self.public_page(data.get("page"))}',
                       stock_ticker=ticker if official else None)

    def coin(self, target, deadline):
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'check_coin', 'arguments': {'address': target}}}
        result = self.read('/mcp', deadline, payload=payload)['result']
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
                facts.append(f'{count} flagged {plural(count, "wallet")} bought {match[2]}%')
            match = re.fullmatch(r'- Maker holds now: (' + NUMBER + r')% of supply.*', line)
            if match:
                facts.append(f'maker holds {match[1]}%')
        page = self.page + '/check/' + target.lower()
        return Reading(self.label, 'present' if facts else 'unlisted',
                       f'{self.label}: ' + '; '.join(facts[:4]) + ' · ' + page if facts else '')

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
