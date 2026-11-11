"""Synthetic source and full gate coverage."""
from copy import deepcopy
from urllib.parse import urlencode
import json
import pytest

from rh_preflight.card import WORDING, agent_card
from rh_preflight.hey import Client
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.settings import Settings
from rh_preflight.sources import priors, sherwood, hookr
from demo.fake_hey import ACTIVE, REPO, fake_server
from demo.fake_network import AGENT, HOOK, POOL, VAULT, agent_payload, routes


@pytest.fixture
def network(server):
    routes(server[1])
    return server


@pytest.mark.parametrize('module,target,path', [
    (priors, ACTIVE, '/api/check?address=' + ACTIVE),
    (sherwood, VAULT, '/api/v1/vaults'),
    (hookr, POOL, '/api/hooks/catalog'),
])
@pytest.mark.parametrize('code', [200, 500, 429])
def test_each_source_transport(network, module, target, path, code):
    origin, state = network
    client = module.Client(origin)
    if code != 200:
        state['overrides'][path] = (code, {'error': 'unavailable'}, {'Retry-After': '30'})
    result = client.lookup(target)
    assert not WORDING.search(result.line)
    assert result.line.endswith(client.page) or 'https://' in result.line
    if code != 200:
        assert result.state == ('rate_limited' if code == 429 else 'unreachable')
        assert 'could not be reached' in result.line
        count = len(state['requests'])
        client.lookup(target)
        if code == 429:
            assert len(state['requests']) == count
    else:
        assert result.state != 'unreachable'
        count = len(state['requests'])
        assert client.lookup(target) == result
        assert len(state['requests']) == count


def test_repayment_fields_only(network):
    origin, state = network
    path = '/api/check?agent=' + AGENT
    data = state['overrides'][path][1]
    data['verdict'] = 'repaid'
    data['score'] = 999888
    data['agents'][0]['scoreV2'] = {'score': 987654, 'rungName': 'recorded'}
    result = priors.Client(origin).lookup(AGENT, agent=True)
    assert '3 loans repaid, 27 USDG repaid, defaulted: no, frozen: no' in result.line
    assert '999888' not in result.line and '987654' not in result.line
    assert not WORDING.search(result.line)


def test_absent_and_unknown_fields(network):
    origin, state = network
    assert priors.Client(origin).lookup(ACTIVE).state == 'missing'
    client = sherwood.Client(origin)
    assert client.lookup(ACTIVE).line == ''
    assert not any('/api/v1/vault/' in p for p, _ in state['requests'])
    assert hookr.Client(origin).lookup('0x' + 'fe' * 32).line == ''
    row = state['overrides']['/api/v1/vaults'][1][0]
    assert '125 USDG' in client.lookup(VAULT).line
    bundle = deepcopy(state['overrides']['/api/v1/vault/' + VAULT][1])
    del bundle['info']['totalAssets']
    assert 'total assets not recorded' in client.parse(row, bundle).line


def app(network, tmp_path, **settings):
    return Service(settings=Settings(**settings), client=Client(network[0]), ledger=Ledger(tmp_path), presence=lambda: False)


def test_payment_real_entry_and_order(network, tmp_path):
    service = app(network, tmp_path)
    directive = service.pre_tool_call('x402_pay', {'chainId': 4663, 'payTo': ACTIVE})
    assert directive['action'] == 'approve'
    card = directive['message']
    assert card.index('Harbor Kit') < card.index(priors.LABEL + ' has no record') < card.index('Source: public')
    assert 'your rule: payee has no ' + priors.LABEL + ' record' in card
    assert not WORDING.search(card)


def test_vault_note_appends(network, tmp_path):
    service = app(network, tmp_path, mode='note')
    args = {'chainId': 4663, 'vault': VAULT}
    assert service.pre_tool_call('deposit', args) is None
    result = service.transform_tool_result('deposit', args, 'done')
    assert sherwood.LABEL + ': vault Pebble Pool' in result
    assert 'strategy active: no, open proposals 0, total assets 125 USDG, 1 agent' in result


def test_pool_and_install(network, tmp_path):
    service = app(network, tmp_path)
    assert service.pre_tool_call('buy', {'chainId': 4663, 'token': ACTIVE, 'poolId': POOL}) is None
    assert hookr.LABEL + ': pool ' in service.ledger.rows(1)[0]['cards'][0]['card']
    assert service.pre_tool_call('plugin_install', {'url': REPO}) is None
    assert '/api/projects?q=example-lab&limit=48&offset=0' in [p for p, _ in network[1]['requests']]


@pytest.mark.parametrize('rule,expected', [(False, None), (True, 'approve')])
def test_source_failure_changes_only_dependent_rule(network, tmp_path, rule, expected):
    network[1]['overrides']['/api/check?address=' + ACTIVE] = (500, {}, {})
    service = app(network, tmp_path, ask_when={'payee_no_priors_record': rule})
    result = service.pre_tool_call('pay', {'chainId': 4663, 'payTo': ACTIVE})
    assert (result['action'] if result else None) == expected
    assert priors.LABEL + ' could not be reached' in service.ledger.rows(1)[0]['cards'][0]['card']


def test_disabled_sources_and_vault_rule(network, tmp_path):
    service = app(network, tmp_path, sources={'priors': 'off'}, ask_when={'vault_strategy_inactive': True})
    assert service.pre_tool_call('pay', {'chainId': 4663, 'payTo': ACTIVE}) is None
    assert not any('/api/check' in p for p, _ in network[1]['requests'])
    assert 'your rule: vault strategy inactive' in service.pre_tool_call('deposit', {'chainId': 4663, 'vault': VAULT})['message']


@pytest.mark.parametrize('status', ['ambiguous', 'not_found'])
def test_agent_does_not_pick_candidates(network, status):
    payload = agent_payload()
    payload.update(status=status, subject=None, data=None, claims=[])
    payload['answer'] = {'text': 'HEY does not guess: send a project slug.'}
    path = '/api/agent/research_project?' + urlencode({'q': 'pebble'})
    network[1]['overrides'][path] = (404 if status == 'not_found' else 200, payload, {})
    result = Client(network[0]).lookup('pebble')
    assert payload['answer']['text'] in result.render()
    assert len(network[1]['requests']) == 1


def test_all_source_renderers_filter_external_text(network):
    for forbidden in WORDING.pattern.split('(?:', 1)[1].split(')', 1)[0].split('|'):
        p = agent_payload()
        p['subject']['project']['name']['text'] = forbidden
        p['answer']['text'] = forbidden
        p['citation']['text'] = forbidden
        p['data']['latestMeaningfulChange']['summary']['text'] = forbidden
        assert not WORDING.search(agent_card(ACTIVE, p).render())
        data = deepcopy(network[1]['overrides']['/api/check?agent=' + AGENT][1])
        data['agents'][0]['name'] = forbidden
        assert not WORDING.search(priors.Client(network[0]).parse(data).line)
        assert not WORDING.search(hookr.Client(network[0]).parse(HOOK, {}, {HOOK: (forbidden, forbidden)}).line)
        data = deepcopy(network[1]['overrides']['/api/v1/vault/' + VAULT][1])
        assert not WORDING.search(sherwood.Client(network[0]).parse({'displayName': forbidden}, data).line)


def test_agent_identifier_and_pool_without_token(network, tmp_path):
    service = app(network, tmp_path, mode='ask_when')
    args = {'chainId': 4663, 'agent_id': AGENT}
    assert service.pre_tool_call('x402', args) is None
    output = service.transform_tool_result('x402', args, 'result')
    assert '3 loans repaid' in output
    assert 'HEY not queried for agent or pool ids' in output
    assert 'activity not measured' not in output
    args = {'chainId': 4663, 'poolId': POOL}
    assert service.pre_tool_call('swap', args) is None
    assert hookr.LABEL + ': pool' in service.transform_tool_result('swap', args, 'result')
    assert service.pre_tool_call('swap', {'chainId': 1, 'poolId': POOL}) is None
    assert not any(path.startswith(('/api/agent/', '/api/v1/builder')) for path, _ in network[1]['requests'])


@pytest.mark.parametrize('module,target,path,ttl', [
    (priors, ACTIVE, '/api/check?address=' + ACTIVE, 600),
    (sherwood, VAULT, '/api/v1/vaults', 60),
    (hookr, POOL, '/api/discover/pools', 60),
])
def test_independent_cache_windows(network, module, target, path, ttl):
    ticks = [0.0]
    client = module.Client(network[0], clock=lambda: ticks[0])
    assert client.lookup(target).state != 'unreachable'
    before = sum(p == path for p, _ in network[1]['requests'])
    ticks[0] = ttl - 1
    client.lookup(target)
    assert sum(p == path for p, _ in network[1]['requests']) == before
    ticks[0] = ttl
    client.lookup(target)
    assert sum(p == path for p, _ in network[1]['requests']) == before + 1


def test_agent_429_does_not_fallback(network):
    path = '/api/agent/builder_status?address=' + ACTIVE
    network[1]['overrides'][path] = (429, {'error': 'later'}, {'Retry-After': '60'})
    client = Client(network[0])
    assert client.lookup(ACTIVE).kind == 'unreachable'
    assert client.lookup(ACTIVE).kind == 'unreachable'
    assert len(network[1]['requests']) == 1


def test_agent_missing_gate_fields_use_fallback(network):
    payload = agent_payload()
    payload['claims'] = []
    payload['data'] = {'inputs': [{'name': 'activityMeasured', 'value': True},
                                   {'name': 'meaningfulEvents30d', 'value': 1}]}
    network[1]['agent_payload'] = payload
    card = Client(network[0]).lookup(ACTIVE)
    assert card.kind == 'agent'
    assert card.data['verified_builder'] is True
    assert 'Interface' not in card.render()
    assert 'Evidence:' not in card.render()
    assert '/api/v1/builder?' in network[1]['requests'][1][0]


def test_research_repository_requires_exact_dossier_match(network):
    network[1]['details']['harbor-kit'] = {'name': 'Harbor Kit', 'sources': [], 'ships': []}
    card = Client(network[0]).lookup(REPO)
    assert card.kind == 'missing'


def test_source_down_in_note_and_unrelated_failure(network, tmp_path):
    network[1]['overrides']['/api/v1/vaults'] = (500, {}, {})
    service = app(network, tmp_path)
    assert service.pre_tool_call('swap', {'chainId': 4663, 'token': ACTIVE}) is None
    network[1]['overrides']['/api/check?address=' + ACTIVE] = (500, {}, {})
    service = app(network, tmp_path, mode='note')
    args = {'chainId': 4663, 'payTo': ACTIVE}
    assert service.pre_tool_call('pay', args) is None
    assert 'could not be reached' in service.transform_tool_result('pay', args, 'result')


def test_project_card_resolves_token_for_source_lines(network, tmp_path):
    payload = network[1]['agent_payload']
    payload['subject']['project']['token'] = {'chainId': 4663, 'address': ACTIVE}
    card = app(network, tmp_path).card('harbor-kit', project='harbor-kit')
    assert priors.LABEL + ' has no record' in card.render()
    assert card.render().splitlines()[-1] == payload['citation']['text']


def test_any_tool_known_vault_reads_missing_badge_in_note_mode(network, tmp_path):
    network[1]['agent_payload']['claims'] = []
    service = app(network, tmp_path, mode='note')
    args = {'chainId': 4663, 'address': VAULT}
    assert service.pre_tool_call('fund_action', args) is None
    assert sherwood.LABEL + ': vault' in service.transform_tool_result('fund_action', args, 'result')
    assert any('/api/v1/builder?' in p for p, _ in network[1]['requests'])


def test_agent_not_found_respects_original_no_record_rule(network, tmp_path):
    payload = agent_payload()
    payload.update(status='not_found', subject=None, data=None, claims=[])
    network[1]['overrides']['/api/agent/builder_status?address=' + ACTIVE] = (404, payload, {})
    service = app(network, tmp_path, ask_unmeasured=False, ask_quiet=False, ask_no_record=True)
    result = service.pre_tool_call('swap', {'chainId': 4663, 'token': ACTIVE})
    assert result['action'] == 'approve'
    assert 'your rule: no HEY record' in result['message']
    assert not any('/api/v1/builder?' in p for p, _ in network[1]['requests'])


def test_doctor_lists_disabled_sources_without_contact(network, tmp_path):
    service = app(network, tmp_path, sources={'priors': 'off'})
    result = service.doctor()
    assert result['sources']['priors']['enabled'] is False
    assert result['sources']['priors']['reachable'] is None
    assert not any('/api/check' in p for p, _ in network[1]['requests'])
    from rh_preflight.network import MODULES
    assert set(result['sources']) == {'hey', *MODULES}


@pytest.mark.parametrize('toggle', ['on', 'off'])
def test_existing_config_ignores_unknown_source_key(network, tmp_path, toggle):
    configured = {'retired_source': toggle, 'priors': 'off'}
    class Context:
        def get_config(self, name, default=None):
            return configured if name == 'sources' else default
    settings = Settings.from_context(Context())
    assert settings.sources == {'priors': 'off'}
    assert configured == {'retired_source': toggle, 'priors': 'off'}
    service = Service(settings=settings, client=Client(network[0]),
                      ledger=Ledger(tmp_path), presence=lambda: False)
    assert service.pre_tool_call('pay', {'chainId': 4663, 'payTo': ACTIVE}) is None
    assert not any('/api/check' in path for path, _ in network[1]['requests'])
    with pytest.raises(ValueError):
        Settings(sources={'priors': 'invalid'})
