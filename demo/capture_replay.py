"""Replay 2026-11-09 source lines without opening a socket or making a payment."""
from dataclasses import dataclass
import json
from demo.captures import capture_path
from rh_preflight.hey import Unavailable
from rh_preflight.sources import chain

def capture(name):
    raw = capture_path(name, 'r5').read_text()
    return json.JSONDecoder().raw_decode(raw)[0]


def replay(client, responses):
    def read(path, deadline, payload=None):
        key = path
        if payload is not None:
            if isinstance(payload, list):
                key = payload[0]['method']
            else:
                key = payload.get('params', {}).get('name') if payload.get('method') == 'tools/call' else payload['method']
        answer = responses[key]
        if isinstance(answer, str):
            return 200, capture(answer)
        return answer
    client._read = read
    # This identity verification uses the capture and must not inherit another
    # client's process cache in an embedded offline demonstration.
    if isinstance(client, chain.Client):
        chain.VERIFIED.discard(client.base_url)
    return client


@dataclass(frozen=True)
class CaptureLines:
    sources: tuple

    def render(self):
        return "\n".join(reading.line for reading in self.sources if reading.line)


def card_readings():
    from rh_preflight.sources import wormhole, toll, dexscreener, geckoterminal, offrouter
    token = capture('wormhole-token-nxra.json')['token']
    contract = capture('gecko-token-priors.json')['data']['attributes']['address']
    agent = '7622'
    hook = '0x987e330f00b79864a07fe81dfac344d0078abcc0'
    stock_address = capture('toll-markets-stock-nvda.json')['address']
    rpc = replay(chain.Client(), {
        'eth_chainId': 'rpc-chainid.json',
        # No head was captured. Its absence remains explicit in the card.
        'eth_getCode': (200, [capture('rpc-getcode-priors.json')]),
        'eth_call': 'rpc-batch-erc20-priors.json'})
    readings = [rpc.lookup(contract)]
    readings.append(replay(wormhole.Client(), {'/api/v1/token/4663/' + token: 'wormhole-token-nxra.json'}).lookup(token))
    readings.append(replay(dexscreener.Client(), {'/latest/dex/tokens/' + contract: 'dexscreener-priors.json'}).lookup(contract, has_code=True))
    readings.append(replay(geckoterminal.Client(), {'/api/v2/networks/robinhood/tokens/' + contract: 'gecko-token-priors.json'}).lookup(contract))
    client = replay(toll.Client(), {'check_agent': 'toll-check-agent-7622.json', 'check_hook': 'toll-check-hook.json',
        '/api/markets/stock/' + stock_address: 'toll-markets-stock-nvda.json'})
    readings.extend([client.lookup(agent, kind='agent'), client.lookup(hook, kind='hook'), client.lookup(stock_address)])
    readings.append(replay(offrouter.Client(), {'/.well-known/x402': 'offrouter-x402.json', '/models': 'offrouter-models.json'}).lookup('offrouter'))
    # Each line keeps its captured identifier/link. These targets do not share
    # a policy decision or a coverage count.
    return CaptureLines(tuple(readings))
