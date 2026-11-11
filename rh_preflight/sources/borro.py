"""Whole-file Borro reads gated on official Stock Token membership."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from .common import Keyless, Reading, field, money


def rate(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError() from error
    if not number.is_finite() or number < 0:
        raise ValueError()
    return format(number * 100, 'f') + '%'


class Client(Keyless):
    origin = 'https://borro.markets'
    label = 'Borro'
    page = origin

    def fetch(self, target, deadline, stock_ticker=None):
        if not isinstance(stock_ticker, str) or not re.fullmatch(r'[A-Za-z0-9.]{1,60}', stock_ticker):
            return Reading(self.label, 'unlisted', '')
        markets = self.read('/markets.json', deadline)
        if markets['chain']['id'] != 4663 or not isinstance(markets['stocks'], list):
            raise ValueError()
        market = next((row for row in markets['stocks'] if row['symbol'] == stock_ticker), None)
        if market is None:
            return Reading(self.label, 'unlisted', f'Borro has no lending market for {field(stock_ticker)} · {self.page}')
        prices = self.read('/prices.json', deadline)
        price = next((row for row in prices['stocks'] if row['symbol'] == stock_ticker), None)
        if price is None:
            raise ValueError()
        at = datetime.fromtimestamp(prices['at'], timezone.utc).isoformat().replace('+00:00', 'Z')
        return Reading(self.label, 'present', f'Borro: {field(stock_ticker)} lending market present, lending fees {rate(market["lend"]["apr"])}/year before BORRO drip, short borrow fees {rate(market["short"]["yearly"])}/year, Borro short desk price ${money(price["mid"])} vs feed ${money(price["feedPrice"])} at {at} · {self.page}')
