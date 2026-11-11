"""Four synthetic calls, without executing an action."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rh_preflight.hey import Client
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from demo.fake_hey import ACTIVE, REPO, fake_server
from demo.fake_network import POOL, VAULT, routes


def main():
    with TemporaryDirectory() as directory, fake_server() as (origin, state):
        routes(state)
        service = Service(client=Client(origin), ledger=Ledger(directory), presence=lambda: False)
        calls = [('buy', {'chainId': 4663, 'token': ACTIVE, 'poolId': POOL}),
                 ('x402_pay', {'chainId': 4663, 'payTo': ACTIVE}),
                 ('deposit', {'chainId': 4663, 'vault': VAULT}),
                 ('plugin_install', {'url': REPO})]
        print("rh-preflight by quotum with HEY Research Lab · https://heyresearch.xyz")
        print("invented records from a local fake HEY server, not HEY data")
        print("11 sources: HEY Research Lab plus ten Robinhood Chain projects")
        asked = 0
        for index, (tool, args) in enumerate(calls):
            directive = service.pre_tool_call(tool, args, session_id='example-session', tool_call_id=str(index))
            asked += bool(directive and directive['action'] == 'approve')
            row = service.ledger.rows(1)[0]
            print('\n\n'.join(card['card'] for card in row['cards']))
            print('Decision:', 'human approval requested' if directive else 'proceeds; no enabled rule fired')
            print('\n'.join(row['rules']))
        from demo.capture_replay import card_readings
        print('\nSaved source lines from 2026-11-09, multiple targets; RPC head was not captured:')
        print(card_readings().render())
        from demo.r6_replay import cards
        print('\nSaved captures from 2026-11-09 for PRIORS and NVDA. Uncaptured sources are omitted:')
        for card in cards():
            print(card.render())
        print(f'4 synthetic calls checked; {asked} human approval requests; no actions executed')


if __name__ == '__main__':
    main()
