"""Exact outbound requests and error answers, with local HTTP and saved captures."""
import argparse
import json
from demo.captures import capture_path

import pytest

from rh_preflight import cli, tools
from rh_preflight.card import agent_card, text
from rh_preflight.hey import Client
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from demo import live_card
from demo.fake_hey import ACTIVE
from demo.fake_network import routes


@pytest.fixture
def network(server):
    routes(server[1])
    return server


def paths(network):
    return [path for path, _ in network[1]['requests']]


@pytest.mark.parametrize('target,project,expected', [
    ('hookr', 'hookr', '/api/agent/research_project?project=hookr'),
    ('Hookr.fun', None, '/api/agent/research_project?q=Hookr.fun'),
    ('$HOOKR', None, '/api/agent/research_project?q=%24HOOKR'),
    (ACTIVE.upper().replace('0X', '0x'), None, '/api/agent/builder_status?address=' + ACTIVE),
])
def test_one_exact_selector(network, target, project, expected):
    Client(network[0]).lookup(target, project=project)
    assert paths(network) == [expected]


@pytest.mark.parametrize('argv', [['--project', 'hookr'], ['x', '--project', 'hookr']])
def test_live_demo_exact_slug_request(network, tmp_path, monkeypatch, capsys, argv):
    service = Service(client=Client(network[0]), ledger=Ledger(tmp_path))
    monkeypatch.setattr(live_card, 'Service', lambda **kwargs: service)
    monkeypatch.setattr('sys.argv', ['live_card.py'] + argv)
    live_card.main()
    assert [p for p in paths(network) if p.startswith('/api/agent/')] == [
        '/api/agent/research_project?project=hookr']
    assert 'Harbor Kit' in capsys.readouterr().out


@pytest.mark.parametrize('surface,args,expected', [
    ('tool', {'project': 'hookr'}, '/api/agent/research_project?project=hookr'),
    ('tool', {'project': 'Hookr.fun'}, '/api/agent/research_project?q=Hookr.fun'),
    ('tool', {'project': '$HOOKR'}, '/api/agent/research_project?q=%24HOOKR'),
    ('tool', {'address': ACTIVE}, '/api/agent/builder_status?address=' + ACTIVE),
    ('cli', ['card', '--project', 'hookr'], '/api/agent/research_project?project=hookr'),
    ('cli', ['card', 'x', '--project', 'hookr'], '/api/agent/research_project?project=hookr'),
    ('cli', ['card', 'Hookr.fun'], '/api/agent/research_project?q=Hookr.fun'),
    ('cli', ['card', '$HOOKR'], '/api/agent/research_project?q=%24HOOKR'),
    ('cli', ['card', ACTIVE], '/api/agent/builder_status?address=' + ACTIVE),
])
def test_tool_and_cli_exact_queries(network, tmp_path, capsys, surface, args, expected):
    service = Service(client=Client(network[0]), ledger=Ledger(tmp_path))
    if surface == 'tool':
        assert 'error' not in json.loads(tools.handler(service, args))
    else:
        parser = argparse.ArgumentParser()
        cli.setup(parser)
        assert cli.run(service, parser.parse_args(args)) == 0
        capsys.readouterr()
    assert [p for p in paths(network) if p.startswith('/api/agent/')] == [expected]


@pytest.mark.parametrize('status', [400, 401, 403, 404, 422, 429])
@pytest.mark.parametrize('envelope', [True, False])
def test_json_client_error_is_an_answer(network, status, envelope):
    message = 'research_project: name exactly one of project (or slug), q (a name or $TICKER) or address.'
    body = {'status': 'bad_request', 'answer': {'text': message}} if envelope else {
        'error': 'bad_request', 'message': message}
    path = '/api/agent/research_project?q=Hookr.fun'
    network[1]['overrides'][path] = (status, body, {})
    output = Client(network[0]).lookup('Hookr.fun').render()
    assert message in output
    assert 'could not be reached' not in output
    assert paths(network) == [path]


def test_install_searches_repository_name_and_prints_400(network, tmp_path):
    path = '/api/projects?q=offrouter&limit=48&offset=0'
    network[1]['overrides'][path] = (400, {'error': 'bad_request', 'message': 'Synthetic search rejection.'}, {})
    service = Service(client=Client(network[0]), ledger=Ledger(tmp_path), presence=lambda: False)
    service.pre_tool_call('plugin_install', {'url': 'https://github.com/offrouter/mcp'})
    output = service.ledger.rows(1)[0]['cards'][0]['card']
    assert 'Synthetic search rejection.' in output
    assert 'HTTP 400' in output
    assert 'could not be reached' not in output
    assert path in paths(network)
    assert not any(p.startswith('/api/agent/') for p in paths(network))


def capture(name):
    return json.loads(capture_path(name).read_text())


@pytest.mark.parametrize('match', ['exact', 'other-owner', 'context-only', 'no-hits'])
def test_v01_exact_repository_fixtures(server, match):
    saved = capture('v01/card-hey.json')
    repo = saved['repo_url']
    name = repo.rsplit('/', 2)[-2]
    search = '/api/projects?q=' + name + '&limit=48&offset=0'
    slug = 'hey-research-lab'
    # The supplied captures have no dossier. Assemble its identity link from
    # the Partner Card and use the saved snapshot/card for the actual facts.
    detail = {'slug': slug, 'name': 'Hey Research Lab', 'sources': [
        {'sourceType': 'GITHUB_REPO', 'contextOnly': match == 'context-only', 'url': repo}],
        'token': {'chainId': saved['chain_id'], 'contractAddress': saved['contract_address']}}
    if match == 'other-owner':
        detail['sources'][0]['url'] = repo.replace('/hey-research-lab/', '/other-owner/')
    state = server[1]
    state['search'] = capture('v01/search-qhooks.json') if match == 'no-hits' else {
        'items': [{'slug': slug}], 'nextOffset': None}
    state['details'][slug] = detail
    state['cards'][saved['contract_address']] = saved
    state['snapshots'][slug] = capture('v01/snapshot-hey.json')
    card = Client(server[0]).lookup(repo.upper().replace('HTTPS://', 'https://') + '.git/')
    assert paths(server)[0] == search
    assert not any(p.startswith('/api/agent/research_project') for p in paths(server))
    if match == 'exact':
        assert card.kind == 'token'
        assert card.data['verified_builder'] is saved['verified_builder']
        assert card.data['meaningful_ships_30d'] == saved['meaningful_ships_30d']
        assert saved['latest_meaningful_ship']['title'] in card.render()
    else:
        assert card.kind == 'missing'
        assert 'HEY has no project record for this repository' in card.render()
        assert not any(p.startswith('/api/v1/builder') for p in paths(server))


def test_v01_tokenless_exact_repository_snapshot(server):
    saved = capture('v01/card-hey.json')
    repo = saved['repo_url']
    server[1]['search'] = {'items': [{'slug': 'hey-research-lab'}]}
    server[1]['details']['hey-research-lab'] = {'name': 'Hey Research Lab', 'repo_url': repo + '.git'}
    snapshot = capture('v01/snapshot-hey.json')
    server[1]['snapshots']['hey-research-lab'] = snapshot
    card = Client(server[0]).lookup(repo)
    assert card.kind == 'repo'
    assert card.data['activity_measured'] is snapshot['build']['activityMeasured']
    assert card.data['as_of'] == snapshot['asOf']
    assert card.data['meaningful_ships_30d'] is None
    assert paths(server)[-1] == '/api/projects/hey-research-lab/snapshot'


def test_captured_citation_retains_upstream_spacing(network, tmp_path):
    payload = capture('hey-research_project-name.json')
    citation = text(payload['citation']['text'])
    assert 'Robinhood Chain, https' in citation
    # Only the captured long dash is normalized; HEY's other citation text stays.
    assert agent_card('hookr', payload).render().splitlines()[-1] == citation
    network[1]['agent_payload'] = payload
    service = Service(client=Client(network[0]), ledger=Ledger(tmp_path))
    assert service.card('hookr', project='hookr').render().splitlines()[-1] == citation
