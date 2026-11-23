"""Attribute named Trnch fields from requested single and batch token checks."""
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
import re
from urllib.parse import urlsplit

from ..extract import ADDRESS
from ..hey import Unavailable, RateLimited
from .common import Keyless, Reading, field


# Public risk categories as Trnch publishes them (trnch.fun/llms-full.txt).
# Only these exact codes skip the wording filter; any other string keeps it.
CATEGORIES = frozenset({
    'SCAM_CONTRACT', 'LINKED_TO_PAST_RUG', 'HEAVY_SELL_TAX', 'CANNOT_SELL',
    'COORDINATED_LAUNCH', 'COPYCAT', 'LIQUIDITY_PULLED', 'DUMPED', 'OTHER_RISK'})


def category(value):
    return value if value in CATEGORIES else field(value)


@dataclass(frozen=True)
class TrnchReading(Reading):
    """Provenance for the narrowly allowed Trnch wording on a card."""
    summary_line: str | None = None
    verified: bool | None = None


def summary(value):
    # Reject rather than repair multiline, terminal-control or invisible text.
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        return None
    value = re.sub(r"\s*[\u2013\u2014]\s*", ", ", value.strip())
    if len(value) > 240:
        boundary = value.rfind(' ', 0, 240)
        value = value[:boundary if boundary > 0 else 239] + '…'
    return value


class Client(Keyless):
    origin = 'https://trnch.fun'
    label = 'Trnch'
    page = origin

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.batch_calls = deque()

    def _read(self, path, deadline, payload=None):
        if path == '/api/bot/v1/check/batch':
            # One attempt per page. A failed page falls back to single checks.
            return self._read_once(path, deadline, payload)
        return super()._read(path, deadline, payload)

    def lookup_many(self, targets, deadline=None):
        """Read only requested addresses, deduplicated, without a dataset cache."""
        end = self.clock() + self.timeout
        deadline = min(deadline, end) if deadline is not None else end
        targets = list(dict.fromkeys(str(target).lower() for target in targets))
        readings = {target: Reading(self.label, 'unlisted', '', asked=False)
                    for target in targets if not ADDRESS.fullmatch(target)}
        tokens = [target for target in targets if ADDRESS.fullmatch(target)]
        if len(tokens) == 1:
            readings[tokens[0]] = self.lookup(tokens[0], deadline=deadline)
            return readings
        limited = False
        for offset in range(0, len(tokens), 20):
            page = tokens[offset:offset + 20]
            start = self.clock()
            if limited or start >= deadline:
                readings.update((token, self.rate_limited(token) if limited else self.unavailable(token))
                                for token in page)
                continue
            try:
                with self.lock:
                    while self.batch_calls and start - self.batch_calls[0] >= 60:
                        self.batch_calls.popleft()
                    if len(self.batch_calls) >= 10 or start < self.cooldown:
                        raise RateLimited()
                    self.batch_calls.append(start)
                    self.misses += len(page)
                status, data = self._read('/api/bot/v1/check/batch', deadline, {'tokens': page})
                if status == 429:
                    raise RateLimited()
                if status != 200 or not isinstance(data, dict) or not isinstance(data.get('results'), list):
                    raise Unavailable()
                by_address, duplicates = {}, set()
                for item in data['results']:
                    if not isinstance(item, dict) or not isinstance(item.get('token'), str):
                        continue
                    token = item['token'].lower()
                    if token not in page:
                        continue
                    if token in by_address:
                        duplicates.add(token)
                    by_address[token] = item
                for token in page:
                    item = by_address.get(token)
                    try:
                        if item is None or token in duplicates or 'error' in item:
                            raise Unavailable()
                        readings[token] = self.project(token, item)
                    except (Unavailable, KeyError, TypeError, ValueError, AttributeError, OverflowError):
                        readings[token] = self.unavailable(token)
            except RateLimited:
                limited = True
                readings.update((token, self.rate_limited(token)) for token in page)
            except (Unavailable, KeyError, TypeError, ValueError, AttributeError, IndexError, OverflowError):
                for token in page:
                    if limited or self.clock() >= deadline:
                        readings[token] = self.rate_limited(token) if limited else self.unavailable(token)
                    else:
                        readings[token] = self.lookup(token, deadline=deadline)
                        limited = readings[token].state == 'rate_limited'
            for token in page:
                readings[token] = replace(readings[token], elapsed=max(0.0, self.clock() - start))
        return readings

    def target_page(self, target=None, **kwargs):
        return self.page + '/token/' + target.lower() if ADDRESS.fullmatch(str(target)) else self.page

    def prefetch(self, deadline=None):
        # Permission covers only a token requested by a check.
        return None

    def probe(self):
        # Doctor has no requested token and must not fetch an index or listing.
        return {'reachable': None, 'latency_ms': None}

    def fetch(self, target, deadline):
        if not ADDRESS.fullmatch(str(target)):
            return Reading(self.label, 'unlisted', '', asked=False)
        target = target.lower()
        if self.clock() >= deadline:
            raise Unavailable()
        # Bypass both cache reads and writes; retain only the projected line.
        with self.lock:
            self.misses += 1
        status, data = self._read('/api/bot/v1/check?token=' + target, deadline)
        if status == 429:
            raise RateLimited()
        if status != 200:
            raise Unavailable()
        return self.project(target, data)

    def project(self, target, data):
        """Validate identity and independent observations before optional prose."""
        if (not isinstance(data, dict) or type(data['chainId']) is not int or
                data['chainId'] != 4663 or not isinstance(data['token'], str) or
                data['token'].lower() != target or
                type(data['rugged']) is not bool or type(data['dumped']) is not bool or
                data['status'] not in ('in_heat', 'seen', 'kept_out', 'unknown')):
            raise ValueError()
        if data['status'] == 'seen':
            if not isinstance(data['seenAt'], str):
                raise ValueError()
            datetime.fromisoformat(data['seenAt'].replace('Z', '+00:00'))
        elif data['status'] == 'kept_out':
            if (not isinstance(data['category'], str) or
                    not any(c.isprintable() and not c.isspace() for c in data['category'])):
                raise ValueError()
        # The captures expose separate boolean observations. Either overrides
        # a lifecycle status; the stronger rugged observation wins if both hold.
        if data['rugged']:
            phrase = 'rugged'
        elif data['dumped']:
            phrase = 'dumped'
        elif data['status'] == 'in_heat':
            phrase = 'passed the rug screen, live in the Heat'
        elif data['status'] == 'seen':
            phrase = 'no rug, no dump on record · first seen ' + field(data['seenAt'])
        elif data['status'] == 'kept_out':
            phrase = 'blocked · ' + category(data['category'])
        elif data['status'] == 'unknown':
            phrase = 'not screened yet'
        else:
            raise ValueError()
        state = 'unlisted' if phrase == 'not screened yet' else 'present'
        verified = data.get('verified') if type(data.get('verified')) is bool else None
        description = summary(data.get('summary'))
        if description:
            details = []
            # A summary cannot erase independent adverse or no-record facts.
            if data['rugged'] and not re.search(r'\brugged\b', description, re.I):
                details.append('rugged')
            if data['dumped'] and not re.search(r'\bdumped\b', description, re.I):
                details.append('dumped')
            if data['status'] == 'unknown':
                details.append('not screened yet')
            elif data['status'] == 'kept_out':
                details.append('blocked · ' + category(data['category']))
            elif data['status'] == 'seen' and not (data['rugged'] or data['dumped']):
                details.append('first seen ' + field(data['seenAt']))
            if verified and not re.search(r'\bverified\b', description, re.I):
                details.append('verified by trnch')
            phrase = ' · '.join([description, *details])
        elif verified:
            phrase += ' · verified by trnch'
        page = self.target_page(target)
        candidate = data.get('tokenUrl')
        if isinstance(candidate, str) and candidate.isprintable():
            try:
                url = urlsplit(candidate)
                if (url.scheme == 'https' and url.netloc == 'trnch.fun' and
                        url.path.startswith('/token/') and url.path[7:].lower() == target and
                        not url.query and not url.fragment and not any(c.isspace() for c in candidate)):
                    page = candidate
            except ValueError:
                pass
        line = 'Trnch: ' + phrase + ' · ' + page
        # Store only the validated projection, never the raw response.
        modern = description is not None or verified is True or page != self.target_page(target)
        return TrnchReading(self.label, state, line,
                            summary_line=line if modern else None, verified=verified)
