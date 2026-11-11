"""Saved r4 responses and bounded local HTTP regressions. No remote reads."""
import json
from demo.captures import capture_path

import pytest

from rh_preflight.hey import Client, TemporaryUnavailable
from rh_preflight.sources.hookr import Client as Hookr


REPO = 'https://github.com/offrouter/mcp'


def capture(name):
    # Read only the bundled public response body.
    return json.JSONDecoder().raw_decode(capture_path(name, 'r4').read_text())[0]


def requests(server):
    return [p for p, _ in server[1]['requests']]


def test_captured_offrouter_exact_registered_source(server):
    detail = capture('hey-dossier-offrouter.json')
    assert not detail.get('repo_url')
    assert any(s['url'] == REPO and s['sourceType'] == 'GITHUB_REPO'
               and s['contextOnly'] is False for s in detail['sources'])
    server[1]['overrides']['/api/projects?q=offrouter&limit=48&offset=0'] = (
        200, capture('hey-search-offrouter.json'), {})
    server[1]['details']['offrouter'] = detail
    server[1]['snapshots']['offrouter'] = capture('hey-snapshot-offrouter.json')
    card = Client(server[0]).lookup(REPO + '.git/')
    assert requests(server)[:2] == [
        '/api/projects?q=offrouter&limit=48&offset=0', '/api/projects/offrouter']
    assert not any('?q=mcp' in p for p in requests(server))
    assert card.kind == 'repo'
    assert 'HEY registers this exact repository: ' + REPO in card.render()
    assert 'OffRouter' in card.render()
    assert card.data['activity_measured'] == capture('hey-snapshot-offrouter.json')['build']['activityMeasured']
    assert 'could not be reached' not in card.render()


def test_repo_basename_fallback_and_global_dossier_limit(server):
    server[1]['overrides']['/api/projects?q=offrouter&limit=48&offset=0'] = (
        200, {'items': [], 'total': 0}, {})
    server[1]['overrides']['/api/projects?q=mcp&limit=48&offset=0'] = (
        200, capture('hey-search-mcp.json'), {})
    card = Client(server[0]).lookup(REPO)
    dossiers = [p for p in requests(server) if p.startswith('/api/projects/')]
    assert len(dossiers) == 8
    assert 'HEY lists 27 projects for "mcp"; none of the first 8 registers this repository' in card.render()
    assert '/api/projects?q=mcp' in card.render()
    assert 'could not be reached' not in card.render()


def test_repo_candidates_order_and_shared_limit(server):
    slugs = ['other-' + str(i) for i in range(12)] + ['mcp', 'offrouter', 'offrouter-mcp']
    server[1]['search'] = {'items': [{'slug': s} for s in slugs], 'total': len(slugs)}
    card = Client(server[0]).lookup(REPO)
    dossiers = [p for p in requests(server) if p.startswith('/api/projects/')]
    assert dossiers[:3] == ['/api/projects/offrouter-mcp', '/api/projects/offrouter', '/api/projects/mcp']
    assert len(dossiers) == len(set(dossiers)) == 8
    assert any('?q=mcp&' in p for p in requests(server))
    assert 'could not be reached' not in card.render()


def test_deadline_after_search_keeps_hey_answer(monkeypatch):
    client = Client()
    calls = []

    def read(path, deadline):
        calls.append(path)
        if path.startswith('/api/projects?'):
            return 200, {'items': [{'slug': 'offrouter'}], 'total': 1}
        raise TemporaryUnavailable()

    monkeypatch.setattr(client, '_read', read)
    output = client.lookup(REPO).render()
    assert 'HEY lists 1 projects for "offrouter"; none of the first 0 registers this repository' in output
    assert 'could not be reached' not in output
    assert len(calls) == 2


@pytest.mark.parametrize('registration', ['repo_url', 'source', 'context-only'])
def test_name_match_never_establishes_ownership(server, registration):
    other = 'https://github.com/offrouter/different'
    detail = {'name': 'OffRouter'}
    if registration == 'repo_url':
        detail['repo_url'] = other
    else:
        detail['sources'] = [{'sourceType': 'GITHUB_REPO', 'contextOnly': registration == 'context-only',
                              'url': REPO if registration == 'context-only' else other}]
    server[1]['search'] = {'items': [{'slug': 'offrouter'}], 'total': 1}
    server[1]['details']['offrouter'] = detail
    output = Client(server[0]).lookup(REPO).render()
    assert 'registers this exact repository' not in output
    if registration != 'context-only':
        assert f'HEY registers {other} for OffRouter, not this repository' in output
    assert not any(p.endswith('/snapshot') for p in requests(server))


@pytest.mark.parametrize('status', [200, 404])
def test_captured_slug_not_found_resolves_with_one_q_request(server, status):
    project_path = '/api/agent/research_project?project=hookr'
    q_path = '/api/agent/research_project?q=hookr'
    server[1]['overrides'][project_path] = (status, capture('hey-agent-project-hookr.json'), {})
    server[1]['overrides'][q_path] = (200, capture('hey-agent-q-hookr.json'), {})
    card = Client(server[0]).lookup('hookr', project='hookr')
    assert [p for p in requests(server) if p.startswith('/api/agent/')] == [project_path, q_path]
    assert 'slug hookr not found; HEY resolved "hookr" to hookr-fun' in card.render()
    assert 'Hookr.fun is shipping consistently' in card.render()
    assert card.data['payload']['subject']['project']['slug'] == 'hookr-fun'


@pytest.mark.parametrize('followup', ['not_found', 'timeout', 'ambiguous'])
def test_failed_slug_resolution_preserves_original_answer(server, followup):
    original = capture('hey-agent-project-hookr.json')
    server[1]['overrides']['/api/agent/research_project?project=hookr'] = (404, original, {})
    response = original if followup == 'not_found' else {
        'status': 'ambiguous', 'answer': {'text': 'Several projects match.'}}
    server[1]['overrides']['/api/agent/research_project?q=hookr'] = (
        503 if followup == 'timeout' else 200, response, {})
    output = Client(server[0]).lookup('hookr', project='hookr').render()
    assert original['answer']['text'] in output
    assert 'HEY resolved' not in output
    assert len(requests(server)) == 2


@pytest.mark.parametrize('catalog', [b'<html><body>No registry</body></html>', b'{"changed": true}', b'invalid-json'])
def test_captured_hookr_page_is_an_answer_and_cached(server, catalog):
    raw = capture_path('hookr-hooks.html', 'r4').read_bytes()
    assert len(raw) < 2_000_000
    assert b'poolId' not in raw and b'deployments' not in raw
    server[1]['overrides']['/api/hooks/catalog'] = (200, catalog, {})
    server[1]['overrides']['/hooks'] = (200, raw, {})
    client = Hookr(server[0])
    for target in ('0x' + 'ab' * 32, '0x' + 'cd' * 20):
        reading = client.lookup(target)
        assert reading.state == 'unreadable'
        assert reading.line == "Hookr's page no longer lists hooks in a readable form · https://hookr.fun/hooks"
    assert requests(server).count('/hooks') == 1
    assert not client.pool_index and not client.hook_index


def test_hookr_transport_failure_remains_outage(server):
    server[1]['down'] = True
    reading = Hookr(server[0]).lookup('0x' + 'ab' * 32)
    assert reading.state == 'unreachable'
    assert 'could not be reached' in reading.line


@pytest.mark.parametrize('path', ['/api/hooks/integrations', '/api/discover/pools'])
def test_hookr_changed_index_schema_uses_public_page(server, path):
    server[1]['overrides']['/api/hooks/catalog'] = (200, {'root': {}}, {})
    server[1]['overrides']['/api/hooks/integrations'] = (200, {'integrations': []}, {})
    server[1]['overrides']['/api/discover/pools'] = (200, {'pools': []}, {})
    server[1]['overrides'][path] = (200, b'<html>Changed format</html>', {})
    server[1]['overrides']['/hooks'] = (200, capture_path('hookr-hooks.html', 'r4').read_bytes(), {})
    assert Hookr(server[0]).lookup('0x' + 'ab' * 32).state == 'unreadable'


def test_hookr_refusal_keeps_http_wording(server):
    server[1]['overrides']['/api/hooks/catalog'] = (403, {'message': 'Not allowed.'}, {})
    reading = Hookr(server[0]).lookup('0x' + 'ab' * 32)
    assert reading.state == 'refused'
    assert 'HTTP 403' in reading.line
    assert requests(server) == ['/api/hooks/catalog']


def test_incomplete_repo_search_is_not_cached_as_absence(server):
    server[1]['search'] = {'items': [{'slug': 'item-' + str(i)} for i in range(12)], 'total': 12}
    client = Client(server[0])
    assert client.lookup(REPO).kind == 'unreachable'
    server[1]['search'] = {'items': [{'slug': 'offrouter'}], 'total': 1}
    server[1]['details']['offrouter'] = {'name': 'OffRouter', 'repo_url': REPO}
    assert client.lookup(REPO).kind == 'repo'
