"""Highest-liquidity Robinhood Chain pair only."""
from datetime import datetime, timezone
from decimal import Decimal
from ..extract import ADDRESS
from .common import Keyless, Reading, field, money
from ..card import link


class Client(Keyless):
    origin = 'https://api.dexscreener.com'
    label = 'DexScreener'
    page = 'https://dexscreener.com/robinhood'

    def target_page(self, target=None, **kwargs):
        return self.page + "/" + target if isinstance(target, str) and ADDRESS.fullmatch(target) else self.page

    def fetch(self, target, deadline, has_code=None):
        if not isinstance(target, str) or not ADDRESS.fullmatch(target):
            return Reading(self.label, 'unlisted', '')
        data = self.read('/latest/dex/tokens/' + target, deadline)
        rows = data['pairs']
        if rows is not None and not isinstance(rows, list):
            raise ValueError()
        rows = [r for r in rows or [] if r['chainId'] == 'robinhood']
        if not rows:
            return Reading(self.label, 'missing', f'{self.label} lists no Robinhood Chain pair for this token · {self.page}/{target}' if has_code is True else '')
        def liquidity(row):
            n = Decimal(str((row.get('liquidity') or {}).get('usd', 0)))
            if not n.is_finite() or n < 0:
                raise ValueError()
            return n
        row = max(rows, key=liquidity)
        date = datetime.fromtimestamp(row['pairCreatedAt'] / 1000, timezone.utc).date().isoformat()
        labels = row.get('labels') or []
        if not isinstance(labels, list):
            raise ValueError()
        return Reading(self.label, 'present', f"{self.label}: {field(row['dexId'])} {field(' '.join(labels), '')} pair, "
            f"liquidity ${money((row.get('liquidity') or {}).get('usd'))}, 24h volume ${money(row['volume'].get('h24'))}, "
            f"pair since {date} · {link(row['url']) if link(row['url']).startswith('https://') else self.page + '/' + target}")
