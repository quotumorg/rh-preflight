"""Regressions derived from the live run, using invented local records only."""
from copy import deepcopy
from urllib.parse import urlencode

import pytest

from rh_preflight import __version__
from rh_preflight.card import agent_card
from rh_preflight.hey import Client
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.settings import Settings
from rh_preflight.sources import priors, sherwood, hookr
from demo.fake_hey import ACTIVE, UNKNOWN
from demo.fake_network import POOL, VAULT, agent_payload, routes


@pytest.fixture
def network(server):
    routes(server[1])
    return server


@pytest.mark.parametrize('module,target', [(priors, ACTIVE), (sherwood, VAULT), (hookr, POOL)])
def test_named_user_agent_on_every_source_request(network, module, target):
    module.Client(network[0]).lookup(target)
    assert network[1]['requests']
    for _, headers in network[1]['requests']:
        headers = {k.lower(): v for k, v in headers.items()}
        assert headers['user-agent'] == f'rh-preflight/{__version__} (+https://github.com/quotumorg/rh-preflight)'
        assert 'x-hey-integration' not in headers


def test_priors_http_403_is_a_refusal(network):
    network[1]['overrides']['/api/check?address=' + ACTIVE] = (403, b'forbidden', {})
    result = priors.Client(network[0]).lookup(ACTIVE)
    assert result.state == 'refused'
    assert 'Priors refused the request (HTTP 403)' in result.line
    assert 'could not be reached' not in result.line


@pytest.mark.parametrize('code', [200, 404])
@pytest.mark.parametrize('target', [UNKNOWN, 'unknown-kit'])
def test_hey_not_found_is_an_answer(network, code, target):
    payload = agent_payload()
    payload.update(status='not_found', subject=None, data=None, claims=[], freshness=[], citation=None)
    payload['answer'] = {'text': 'HEY has no published project matching this address.'}
    endpoint = 'builder_status' if target == UNKNOWN else 'research_project'
    selector = 'address' if target == UNKNOWN else 'q'
    network[1]['overrides']['/api/agent/' + endpoint + '?' + urlencode({selector: target})] = (code, payload, {})
    card = Client(network[0]).lookup(target)
    assert card.kind == 'agent'
    if target == UNKNOWN:
        assert 'no published project for this contract' in card.render()
        assert 'Verified Builder:' not in card.render()
    else:
        assert payload['answer']['text'] in card.render()
        assert "Verified Builder: not in HEY's answer" in card.render()
    assert 'could not be reached' not in card.render()
    assert len(network[1]['requests']) == 1


@pytest.mark.parametrize('by_name', [False, True])
@pytest.mark.parametrize('badge', [True, False, None])
def test_badge_from_partner_card_without_claims(network, by_name, badge):
    payload = network[1]['agent_payload']
    payload['claims'] = []
    payload['subject']['project']['token'] = {'chainId': 4663, 'address': ACTIVE}
    network[1]['cards'][ACTIVE]['verified_builder'] = badge
    card = Client(network[0]).lookup('harbor-kit' if by_name else ACTIVE, required=())
    assert card.data['verified_builder'] is badge
    expected = 'yes (HEY)' if badge is True else 'no (HEY)' if badge is False else "not in HEY's answer"
    assert 'Verified Builder: ' + expected in card.render()
    assert any('/api/v1/builder?' in p for p, _ in network[1]['requests'])


@pytest.mark.parametrize('record,expected', [
    ({'researchLevel': 'VERIFIED_BUILDER'}, True),
    ({'catalogStatus': 'VERIFIED_BUILDER'}, True),
    ({'researchLevel': 'RESEARCHED', 'catalogStatus': 'QUALIFIED'}, False),
    ({}, None),
])
def test_badge_from_tokenless_project_record(network, record, expected):
    network[1]['agent_payload']['claims'] = []
    network[1]['overrides']['/api/projects/harbor-kit'] = (200, record, {})
    card = Client(network[0]).lookup('harbor-kit', required=())
    assert card.data['verified_builder'] is expected
    assert not any('/api/v1/builder?' in p for p, _ in network[1]['requests'])


def test_slow_source_does_not_starve_other_sources_or_hey(network, tmp_path, blocked,
                                                          controlled_budget, monkeypatch):
    import threading
    origin, state = network
    service = Service(settings=Settings(timeout=4, mode='note'), client=Client(origin, timeout=4), ledger=Ledger(tmp_path))
    expired = threading.Event()
    with blocked() as (gate, entered, returned):
        # Block the adapter itself so no socket timeout can decide its verdict.
        def priors_lookup(*args, **kwargs):
            from rh_preflight.sources.common import Reading
            gate()
            return Reading('Priors', 'unlisted', '')
        monkeypatch.setattr(service.network.clients['priors'], 'lookup', priors_lookup)
        def is_expired(name, args):
            return (name == 'source_read' and args[0] == 'priors'
                    or name == 'agent_read' and args[1] is not None)
        def expire():
            assert entered.wait(30)
            # These actual reads must finish before we declare Priors out of time.
            ready = [f for f in jobs if f.test_job[0] == 'core'
                     or f.test_job[0] == 'source_read' and f.test_job[1][0] != 'priors']
            assert {f.test_job[1][0] for f in ready if f.test_job[0] == 'source_read'} == {'hookr', 'sherwood'}
            for future in ready:
                future.result(timeout=30)
            expired.set()
        jobs = controlled_budget(is_expired, service, expire)
        args = {'chainId': 4663, 'payTo': ACTIVE, 'poolId': POOL, 'vault': VAULT}
        assert service.pre_tool_call('swap_pay_deposit', args) is None
        output = service.transform_tool_result('swap_pay_deposit', args, 'done')
        assert expired.is_set() and entered.is_set() and not returned.is_set()
        assert 'Priors could not be reached' in output
        assert 'Harbor Kit' in output and 'HEY could not be reached' not in output
        assert 'Hookr: pool' in output
        assert 'Sherwood: vault Pebble Pool' in output


def test_lazy_lists_warmed_once_and_second_target_uses_cache(network, tmp_path):
    service = Service(client=Client(network[0]), ledger=Ledger(tmp_path))
    state = network[1]
    assert not state['requests']
    service.card('harbor-kit')
    lists = ['/api/v1/vaults', '/api/hooks/catalog', '/api/hooks/integrations', '/api/discover/pools']
    assert all(sum(path == p for p, _ in state['requests']) == 1 for path in lists)
    # An unrelated address also uses the same indexed list cache.
    service.card(UNKNOWN)
    assert all(sum(path == p for p, _ in state['requests']) == 1 for path in lists)


@pytest.mark.parametrize('fresh,asof,expected', [
    ([], '2026-11-09', 'asOf: 2026-11-09'),
    ([], None, None),
    ([{'freshnessStatus': 'live'}], None, 'Freshness: live'),
    ([{'dataAsOf': '2026-11-09'}], None, 'Freshness: 2026-11-09'),
])
def test_freshness_prints_only_present_values(fresh, asof, expected):
    payload = agent_payload()
    payload.update(freshness=fresh, asOf=asof, citation=None)
    output = agent_card(ACTIVE, payload).render()
    assert 'Freshness:  ·' not in output
    if expected:
        assert output.splitlines()[-1] == expected
    else:
        assert 'Freshness:' not in output and 'asOf:' not in output


def test_citation_spacing_is_upstream(network):
    citation = 'Source: public research for Robinhood Chain : https://example.org/project/harbor'
    network[1]['agent_payload']['citation'] = {'text': citation}
    assert Client(network[0]).lookup(ACTIVE).render().splitlines()[-1] == citation


@pytest.mark.parametrize('argv', [['--project', 'harbor-kit'], ['x', '--project', 'harbor-kit']])
def test_live_demo_project_without_positional_target(monkeypatch, capsys, argv):
    from demo import live_card
    calls = []
    class StubService:
        def __init__(self, **kwargs):
            pass
        def card(self, target, project=None):
            calls.append((target, project))
            return agent_card(target, agent_payload())
    monkeypatch.setattr(live_card, 'Service', StubService)
    monkeypatch.setattr('sys.argv', ['live_card.py'] + argv)
    live_card.main()
    assert calls == [('harbor-kit', 'harbor-kit')]
    assert 'Harbor Kit' in capsys.readouterr().out


@pytest.mark.parametrize('code,body,expected', [
    (403, b'forbidden', 'HEY refused the request (HTTP 403)'),
    (429, {}, 'HEY answered with a rate limit (HTTP 429)'),
    (200, {}, "HEY's response did not establish a builder record"),
])
def test_hey_http_answer_is_not_a_transport_outage(network, code, body, expected):
    network[1]['overrides']['/api/agent/builder_status?address=' + ACTIVE] = (code, body, {})
    output = Client(network[0]).lookup(ACTIVE).render()
    assert expected in output
    assert 'could not be reached' not in output


def test_drip_response_cannot_extend_source_deadline():
    from types import SimpleNamespace
    from rh_preflight.hey import TemporaryUnavailable
    now, socket_timeouts, reads = [0.0], [], []
    class Drip:
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=socket_timeouts.append)))
        def isclosed(self):
            return False
        def read1(self, size):
            reads.append(size)
            now[0] += .04
            return b'12345678'
    client = priors.Client(clock=lambda: now[0])
    with pytest.raises(TemporaryUnavailable):
        client._body(Drip(), .12)
    assert len(reads) == 3
    assert socket_timeouts == pytest.approx([.12, .08, .04])


@pytest.mark.parametrize('enabled', [False, True])
def test_refusal_keeps_dependent_payee_rule(network, tmp_path, enabled):
    network[1]['overrides']['/api/check?address=' + ACTIVE] = (403, {}, {})
    service = Service(settings=Settings(ask_when={'payee_no_priors_record': enabled}),
                      client=Client(network[0]), ledger=Ledger(tmp_path), presence=lambda: False)
    result = service.pre_tool_call('pay', {'chainId': 4663, 'payTo': ACTIVE})
    assert (result['action'] if result else None) == ('approve' if enabled else None)
    output = service.transform_tool_result('pay', {'chainId': 4663, 'payTo': ACTIVE}, 'done')
    assert 'Priors refused the request (HTTP 403)' in output
    assert 'Priors could not be reached' not in output


def test_source_cache_lock_wait_respects_deadline(network, monkeypatch):
    client = priors.Client(network[0], timeout=.06)
    path = '/api/check?address=' + ACTIVE
    from types import SimpleNamespace
    waits = []
    client.clock = lambda: 100.0
    def acquire(*, timeout):
        waits.append(timeout)
        return False  # The held stripe's injected budget has expired.
    client.stripes[hash(path) % len(client.stripes)] = SimpleNamespace(acquire=acquire)
    assert client.lookup(ACTIVE).state == 'unreachable'
    assert waits == pytest.approx([.06])
    assert network[1]['requests'] == []
