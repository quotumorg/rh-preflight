"""Socket-free Hookr MCP and Toll transaction capture replay."""
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rh_preflight.hooks import Service
from rh_preflight.settings import Settings
from rh_preflight.transactions import tx_intents, local_card
from rh_preflight.policy import evaluate
from rh_preflight.presentation import present
from rh_preflight.sources.hookr import Client

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/hookr'
HOOK = '0x89c9fb50d1f03192230ffb1c279bb6e7d3fe6acc'


def capture(name):
    return json.loads((FIXTURES / name).read_text())


def replay_client():
    client = Client()
    def read(path, deadline, payload):
        params = payload['params']
        name = {'hookr_hook': 'hookr_hook.json', 'hookr_scan': 'hookr_scan.json',
                'hookr_pools': 'pools-clear.json'}.get(params['name'])
        if params['name'] == 'hookr_check_trade':
            name = ('check-trade-0xdc36d5.json' if params['arguments']['wallet'].startswith('0xdc36')
                    else 'check-trade-0x7fbd93.json')
        return 200, capture(name)
    client._read_once = read
    return client


def cards():
    # Calling _tx_cards directly avoids the builder, ledger and host runtime.
    service = Service.__new__(Service)
    service.network = type('OfflineNetwork', (), {'clients': {'hookr': replay_client()}})()
    class Toll:
        def check_tx(self, *args):
            return self.response
    toll = service.network.clients['toll'] = Toll()
    settings = Settings(sources={'chain': 'off'})
    for name in ('clean-swap', 'payout-other-wallet'):
        folder = ROOT / 'tests/fixtures/toll-tx' / name
        intent = tx_intents(json.loads((folder / 'request.json').read_text()))[0]
        toll.response = json.loads((folder / 'response.json').read_text())
        card = service._tx_cards([intent], [local_card(intent)], settings, time.monotonic() + 1)[0]
        yield name, present(card, evaluate([card], settings)).render()
    yield 'hook scan', service.network.clients['hookr'].screen(HOOK, time.monotonic() + 1).line


if __name__ == '__main__':
    for name, card in cards():
        print(name + '\n' + card + '\n')
