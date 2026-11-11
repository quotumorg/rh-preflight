"""Invented network shapes served entirely on loopback."""
from copy import deepcopy
from datetime import datetime, timezone
from .fake_hey import ACTIVE, QUIET, REPO

POOL = '0x' + 'ab' * 32
HOOK = '0x' + 'cd' * 20
VAULT = '0x' + 'ef' * 20
AGENT = '90901'


def agent_payload():
    now = datetime.now(timezone.utc).isoformat()
    return {
        'schema': 'hey.agent-intelligence-response', 'schemaVersion': '1',
        'capability': 'research_project', 'status': 'ok',
        'subject': {'kind': 'project', 'project': {'slug': 'harbor-kit', 'name': {'text': 'Harbor Kit'},
            'url': 'https://example.invalid/project/harbor-kit'}},
        'answer': {'text': 'Harbor Kit has one counted ship in the last 30 days.'},
        'claims': [{'id': 'identity.verified_builder', 'value': True}],
        'unknowns': [{'statement': {'text': 'The next release date is not recorded.'}}],
        'data': {'builderState': {'activityMeasured': True, 'meaningfulEvents30d': 1, 'lastMeaningfulShipAt': now},
                 'latestMeaningfulChange': {'countsAsBuilding': True, 'summary': {'text': 'Interface update'},
                     'evidence': [{'id': 'ship:invented-one', 'receiptUrl': 'https://example.invalid/api/evidence/ship:invented-one'}]}},
        'freshness': [{'freshnessStatus': 'live', 'dataAsOf': now}], 'asOf': now,
        'citation': {'text': 'Source: public builder research, https://example.invalid/project/harbor-kit'},
    }


def routes(state):
    state['agent_payload'] = agent_payload()
    vault = {'vault': VAULT, 'displayName': 'Pebble Pool', 'strategyActive': False}
    bundle = {'info': {'address': VAULT, 'assetSymbol': 'USDG', 'assetDecimals': 6, 'totalAssets': '125000000'},
              'proposals': [], 'agents': [{'address': ACTIVE}]}
    bodies = {
        '/api/check?address=' + ACTIVE: {'agents': []},
        '/api/check?agent=' + AGENT: {'agents': [{'agentId': int(AGENT), 'name': 'Pebble Courier',
            'record': {'loansRepaid': 3, 'volumeRepaidUsdg': 27, 'defaulted': False, 'frozen': False},
            'links': {'page': 'https://example.org/agents/' + AGENT}}]},
        '/api/v1/vaults': [vault], '/api/v1/vault/' + VAULT: bundle,
        '/api/hooks/catalog': {'root': {'address': HOOK, 'name': 'Pebble Gate'},
                               'scope': 'Adds a documented pool fee.', 'blocks': []},
        '/api/hooks/integrations': {'integrations': []},
        '/api/discover/pools': {'pools': [{'poolId': POOL, 'chainId': 4663, 'hook': {'address': HOOK}}]},
    }
    state['overrides'].update({path: (200, deepcopy(body), {}) for path, body in bodies.items()})
    return bodies
