"""Socket-free 2026-11-09 capture replay. Missing captures are labelled, never invented."""
import json
from pathlib import Path
from demo.captures import capture_path
from urllib.parse import parse_qs, urlsplit
from rh_preflight.card import Card, agent_card
from rh_preflight.hey import Unavailable
from rh_preflight.network import Network
from rh_preflight.settings import Settings
from rh_preflight.sources import chain, hoodlock
from demo.capture_replay import capture as r5


def capture(name, folder='r6'):
    return json.JSONDecoder().raw_decode(capture_path(name, folder).read_text())[0]


PRIORS = capture('sentinel-token-priors.json')['address']
NVDA = r5('toll-markets-stock-nvda.json')['address'].lower()
DELTA = capture('hoodlock-rpc-delta-index.json')['request'][1]['params'][0]['to']


def answer(name, path, payload=None):
    query = parse_qs(urlsplit(path).query)
    if name == 'sentinelhood':
        action = query['action'][0]
        return 200, capture('sentinel-deployer-priors.json' if action == 'deployer' else 'sentinel-token-priors.json')
    if name == 'trnch':
        return 200, capture('trnch-check-priors.json')
    if name == 'borro':
        return 200, capture({'/markets.json': 'borro-markets.json', '/prices.json': 'borro-prices.json'}[path])
    if name == 'hoodlock':
        if path == hoodlock.LP_PATH:
            # No LP answer was captured on 2026-11-09.
            raise Unavailable()
        if isinstance(payload, dict):
            return 200, {'jsonrpc': '2.0', 'id': payload['id'], 'result': '0x1237'}
        first = payload[0]['params'][0]['data']
        token = '0x' + first[-40:]
        if first.startswith('0xf4dadc61'):
            saved = capture('hoodlock-rpc-delta-locks.json')['response']
            # Captured details contain 2 locks; never fabricate further ones.
            return 200, saved[:len(payload)]
        saved = capture('hoodlock-rpc-delta-index.json' if token == DELTA else 'hoodlock-rpc-priors-index.json')['response']
        vest = capture('hoodlock-rpc-vesting-index.json')['response'][0]
        return 200, [saved[0], saved[1], {**vest, 'id': 3}, {**saved[2], 'id': 4}]
    raise Unavailable()


class CaptureCard(Card):
    def _render(self, *, include_cache_label=True):
        return f'NVDA · {self.target}\nHEY response for NVDA was not captured; no builder reading shown'


def cards():
    network = Network()
    available = {'priors', 'sentinelhood', 'hoodlock', 'trnch', 'borro', 'wormhole', 'hookr', 'dexscreener', 'geckoterminal', 'toll'}
    for name, client in network.clients.items():
        client._read = lambda path, deadline, payload=None, name=name: answer(name, path, payload)
    # Match each saved response only to the token it actually describes.
    network.clients['priors']._read = lambda path, deadline, payload=None: (200, capture('priors-check-address.json', folder=''))
    network.clients['wormhole']._read = lambda path, deadline, payload=None: (404, r5('wormhole-token-priors.json'))
    def hook_read(path, deadline, payload=None):
        if path != '/hooks':
            raise Unavailable()
        raw = capture_path('hookr-hooks.html', 'r4').read_bytes()
        return 200, network.clients['hookr']._decode(raw)
    network.clients['hookr']._read = hook_read
    network.clients['dexscreener']._read = lambda path, deadline, payload=None: (200, r5('dexscreener-priors.json'))
    network.clients['geckoterminal']._read = lambda path, deadline, payload=None: (200, r5('gecko-token-priors.json'))
    network.clients['toll']._read = lambda path, deadline, payload=None: ((200, r5('toll-check-agent-7622.json')) if payload else (404, r5('toll-markets-stock-priors.json')))
    base = agent_card(PRIORS, capture('hey-builder_status-addr.json', folder=''))
    # Raw RPC is disabled here: Sentinelhood establishes the displayed identity.
    settings = Settings(sources={name: 'off' for name in network.clients if name not in available})
    priors = network.enrich([base], 'read', {}, settings, all_sources=True)[0]
    # Only NVDA captures are available for these two sources. The HEY identity
    # response for NVDA was not captured and is explicitly left unknown.
    settings = Settings(sources={name: 'off' for name in network.clients if name not in {'borro', 'toll'}})
    network.clients['toll'].cache.clear()
    network.clients['toll']._read = lambda path, deadline, payload=None: (200, r5('toll-markets-stock-nvda.json'))
    nvda = network.enrich([CaptureCard(NVDA, 'unreachable', {'read_error': 'HEY response for NVDA was not captured'})], 'read', {}, settings, all_sources=True)[0]
    nvda = Card(nvda.target, nvda.kind, {**nvda.data, 'hey_asked': False})
    return priors, nvda


def lp_cards():
    """Replay captured card text, projecting only the new LP response at capture time."""
    from rh_preflight.sources.common import Reading
    root = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'hoodlock'
    sample = json.loads((root / 'lp-locks-2026-11-18.json').read_text())
    client = hoodlock.Client(wall_clock=lambda: sample['timestamp'])
    output = {}
    for symbol, name in [('HISS', 'card-lpbranch-hiss-1118.txt'),
                         ('LOCK', 'cards-058-lock-usdg-1118.txt')]:
        captured = (root / name).read_text().split('===== ')[1]
        target, body = captured.split('\n', 1)
        body = body.split('===== ')[0].rstrip()
        # HISS was captured on the old branch; use the released answer wording.
        body = body.replace('if Hermes swapped this now:', 'if Hermes Agent swapped this now:')
        lines = []
        for line in body.splitlines():
            if line.startswith('HoodLock:') or line.startswith('HoodLock holds '):
                line = client.with_lp(Reading('HoodLock', 'present', line), target, sample).line
            lines.append(line)
        output[symbol] = '\n'.join(lines) + '\n'
    return output


def trnch_cards():
    """Replay only supplied checks; other HISS/USDG facts retain capture dates."""
    from rh_preflight.card import source_line
    from rh_preflight.sources.trnch import Client
    from unittest.mock import patch
    root = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures'
    saved = root / 'trnch'
    hiss = json.loads((saved / 'check-hiss-2026-11-19.json').read_text())
    batches = json.loads((saved / 'batch-fixture-tokens-2026-11-19.json').read_text())
    rows = [row for batch in batches for row in batch['results']]
    samples = {'HISS': hiss,
               'USDG': next(row for row in rows if row['token'].startswith('0x5fc5')),
               'PULLED': next(row for row in rows if row['token'] == '0x47b537f7c2c2183967fe5ba883291b8ada490db6')}
    texts = {'HISS': lp_cards()['HISS']}
    usdg = (root / 'hoodlock' / 'cards-058-lock-usdg-1118.txt').read_text()
    texts['USDG'] = usdg.split('===== ' + samples['USDG']['token'] + '\n', 1)[1].rstrip() + '\n'
    output = {}
    # Fail closed if an offline replay ever tries to open a socket.
    with patch('socket.socket', side_effect=AssertionError('offline replay: no sockets')):
        for symbol, data in samples.items():
            client = Client()
            client._read = lambda *args, data=data: (200, data)
            reading = client.lookup(data['token'])
            if symbol in texts:
                output[symbol] = '\n'.join(source_line(reading) if line.startswith('Trnch: ')
                                            else line for line in texts[symbol].splitlines()) + '\n'
            else:
                # No full card for this token was supplied. Do not invent HEY
                # or other source records to fill the missing observations.
                settings = {name: 'off' for name in Network().clients if name != 'trnch'}
                output[symbol] = Card(data['token'], 'network', {
                    'base': Card(data['token'], 'unreachable', {'read_error':
                        'HEY response for this token was not captured; no builder reading shown'}),
                    'sources': [reading], 'hey_asked': False, 'source_settings': settings}).render() + '\n'
    return output


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--lp', choices=('HISS', 'LOCK'))
    group.add_argument('--trnch', choices=('HISS', 'USDG', 'PULLED'))
    args = parser.parse_args()
    print(trnch_cards()[args.trnch] if args.trnch else lp_cards()[args.lp], end='')
