"""FDV and listing identifier, without repeating a pair price."""
from ..extract import ADDRESS
from .common import Keyless, Reading, field, money


class Client(Keyless):
    origin = 'https://api.geckoterminal.com'
    label = 'GeckoTerminal'
    page = 'https://www.geckoterminal.com/robinhood/tokens'

    def target_page(self, target=None, **kwargs):
        return self.page + "/" + target if isinstance(target, str) and ADDRESS.fullmatch(target) else self.page

    def rate_limited(self, target=None, **kwargs):
        return Reading(self.label, 'rate_limited', f'{self.label} rate-limited this read · {self.target_page(target)}')

    def fetch(self, target, deadline):
        if not isinstance(target, str) or not ADDRESS.fullmatch(target):
            return Reading(self.label, 'unlisted', '')
        row = self.read('/api/v2/networks/robinhood/tokens/' + target, deadline)['data']['attributes']
        return Reading(self.label, 'present', f"{self.label}: FDV ${money(row['fdv_usd'])}, "
            f"CoinGecko listing {field(row['coingecko_coin_id'], 'none')} · {self.page}/{target}")
