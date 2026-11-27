"""QProof by QHooks: the saved token report from the free qproof_check read."""
import json
import math
import re
from ..extract import ADDRESS
from ..hey import RateLimited, Unavailable
from .common import Absent, Keyless, Reading, field

REPORT = re.compile(r'https://qhooks\.com/proof/robinhood/0x[0-9a-f]{40}(?:/[0-9a-f-]{36})?')
LEVELS = {'clear', 'watch', 'danger', 'incomplete'}
WHEN = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:[.][0-9]{1,3})?Z')
KEPT = ('version', 'state', 'subject', 'verdict', 'keyNumbers', 'freshness', 'checkedAt', 'stale', 'links')


def pct(item):
    if type(item) not in (int, float) or not math.isfinite(item) or not 0 <= item <= 100:
        return None
    return f'{item:.2f}'.rstrip('0').rstrip('.') + '%'


class Client(Keyless):
    origin = 'https://qhooks.com'
    label = 'QProof'
    page = 'https://qhooks.com/qproof'

    def target_page(self, target=None, **kwargs):
        return (self.origin + '/proof/robinhood/' + target.lower()
                if isinstance(target, str) and ADDRESS.fullmatch(target) else self.page)

    @staticmethod
    def payload(target):
        # request:false reads the saved report only and never queues a scan.
        return {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'qproof_check', 'arguments': {'chain': '4663', 'token': target.lower(), 'request': False}}}

    def _read(self, path, deadline, payload=None):
        # Saved reports are ~100-300 KB; one attempt uses the whole deadline,
        # and only the fields the line uses stay in the cache.
        status, envelope = self._read_once(path, deadline, payload)
        try:
            content = envelope['result']['content'][0]
            data = json.loads(content['text'])
        except (KeyError, IndexError, TypeError, ValueError, RecursionError):
            return status, envelope
        if isinstance(data, dict) and not envelope['result'].get('isError'):
            kept = {key: data[key] for key in KEPT if key in data}
            kept['report'] = None if data.get('report') is None else {}
            content['text'] = json.dumps(kept)
            envelope['result'] = {'content': [content]}
        return status, envelope

    def report(self, target, deadline):
        envelope = self.read('/mcp', deadline, payload=self.payload(target))
        result = envelope['result']
        body = result['content'][0]['text']
        if result.get('isError'):
            raise RateLimited() if 'retryAfter' in body or 'Retry-After' in body else Unavailable()
        try:
            data = json.loads(body)
        except RecursionError:
            raise ValueError() from None
        if not isinstance(data, dict):
            raise ValueError()
        return data

    def fetch(self, target, deadline):
        if not ADDRESS.fullmatch(str(target)):
            return Reading(self.label, 'unlisted', '')
        data = self.report(target, deadline)
        subject = data['subject']
        if subject['chain'] != 'robinhood' or subject['address'].lower() != target.lower():
            raise ValueError()
        if data['state'] == 'not_checked' or data.get('report') is None:
            return Reading(self.label, 'missing',
                f'{self.label} by QHooks has no saved report for this token; nothing read · {self.page}')
        verdict = data['verdict']
        level, counts = verdict['level'], verdict['counts']
        if level not in LEVELS:
            raise ValueError()
        total, gaps, required = counts['total'], counts['incomplete'], counts['requiredMissing']
        if (any(type(n) is not int or n < 0 for n in (total, gaps, required))
                or gaps > total or required > gaps or level == 'clear' and gaps >= total):
            raise ValueError()
        flags = [f for f in verdict.get('flags') or [] if isinstance(f, dict) and f.get('title')]
        if flags:
            reason = field(flags[0]['title']) + (f' +{len(flags) - 1} more' if len(flags) > 1 else '')
        elif verdict['level'] == 'clear':
            reason = f'no red flags in the {total - gaps} checks read' if gaps else f'no red flags in {total} checks'
        else:
            reason = field(verdict['reason'])
        # Their clear is shown only as no red flags, never as a bare clear.
        parts = [f'{self.label} by QHooks: ' + (reason if level == 'clear' else f'{level}, {reason}')]
        numbers = data.get('keyNumbers') or {}
        if numbers.get('sellable') is False:
            parts.append('sell test did not sell')
        taxes = [f'{side} tax {pct(numbers.get(key))}' for side, key in (('buy', 'buyTaxPct'), ('sell', 'sellTaxPct'))
                 if pct(numbers.get(key))]
        if taxes and numbers.get('sellableBasis') == 'sell_test':
            parts.append('sell test ' + ', '.join(taxes))
        charges = [f'{side} {pct(numbers.get(key))}' for side, key in (('buy', 'buyChargePct'), ('sell', 'sellChargePct'))
                   if pct(numbers.get(key)) and numbers.get(key)]
        if charges:
            parts.append('launch charges ' + ', '.join(charges))
        if pct(numbers.get('top10Pct')):
            parts.append(f"top 10 wallets hold {pct(numbers['top10Pct'])}")
        if pct(numbers.get('lpLockedPct')):
            unlock = numbers.get('lpUnlockAt')
            unlock = unlock[:10] if isinstance(unlock, str) and WHEN.fullmatch(unlock) else ''
            parts.append(f"LP locked {pct(numbers['lpLockedPct'])}" + (f', unlocks {unlock}' if unlock else ''))
        if gaps:
            parts.append(f'{gaps} of {total} checks not read' + (f', {required} required' if required else '') + ' (gaps, not clear)')
        freshness = data.get('freshness') or {}
        checked = freshness.get('checkedAt') or data.get('checkedAt')
        checked = checked if isinstance(checked, str) and WHEN.fullmatch(checked) else 'not recorded'
        parts.append(f'checked {checked}' + (', stale' if data.get('stale') is True else ''))
        link = (data.get('links') or {}).get('report')
        own = self.target_page(target)
        link = link if isinstance(link, str) and REPORT.fullmatch(link) and (link + '/').startswith(own + '/') else own
        return Reading(self.label, 'present', ' · '.join(parts) + ' · ' + link)

    def probe(self):
        start = self.clock()
        try:
            # A structured tool error for the zero address still proves the MCP answered.
            reached = 'result' in self.read('/mcp', start + self.timeout, ttl=0, payload=self.payload('0x' + '0' * 40))
        except Absent:
            reached = True
        except (Unavailable, KeyError, ValueError, TypeError, IndexError, AttributeError):
            reached = False
        return {'reachable': reached, 'latency_ms': round((self.clock() - start) * 1000, 2)}
