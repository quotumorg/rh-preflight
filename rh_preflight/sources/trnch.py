"""Attribute only named Trnch fields from one live token check."""
from dataclasses import dataclass
from datetime import datetime

from ..extract import ADDRESS
from ..hey import Unavailable
from .common import Keyless, Reading, field


@dataclass(frozen=True)
class TrnchReading(Reading):
    """Provenance for the narrowly allowed Trnch wording on a card."""


class Client(Keyless):
    origin = 'https://trnch.fun'
    label = 'Trnch'
    page = origin

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
        # Bypass both cache reads and writes; retain only the projected line.
        with self.lock:
            self.misses += 1
        status, data = self._read('/api/bot/v1/check?token=' + target, deadline)
        if status != 200:
            raise Unavailable()
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
            phrase = 'blocked · ' + field(data['category'])
        elif data['status'] == 'unknown':
            phrase = 'not screened yet'
        else:
            raise ValueError()
        state = 'unlisted' if phrase == 'not screened yet' else 'present'
        return TrnchReading(self.label, state, 'Trnch: ' + phrase + ' · ' + self.target_page(target))
