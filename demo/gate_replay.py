"""An explicit, socket-free simulated host scenario using bundled captures."""
import argparse
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from demo.r6_replay import PRIORS, capture
from rh_preflight import hooks, ledger, network
from rh_preflight.card import agent_card
from rh_preflight.hey import Client
from rh_preflight.settings import Settings


HEY = capture('hey-builder_status-unknown.json', folder='')
TARGET = HEY['query']['q'].lower()
INSTANT = datetime.fromisoformat(HEY['asOf'].replace('Z', '+00:00'))
SCENARIO = 'deny-retry-unattended'
MULTI_SCENARIO = 'two-target-deny-retry-unattended'


def forbidden(*args, **kwargs):
    raise AssertionError('socket creation and uncaptured reads are forbidden in this replay')


@contextmanager
def replay_clock():
    # Patch module bindings only; production clocks and global time stay intact.
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return INSTANT.astimezone(tz) if tz else INSTANT.replace(tzinfo=None)

    clock = SimpleNamespace(monotonic=lambda: INSTANT.timestamp())
    with patch.object(hooks, 'time', clock), patch.object(network, 'time', clock), \
            patch.object(hooks, 'datetime', FrozenDateTime), \
            patch.object(ledger, 'datetime', FrozenDateTime), \
            patch('socket.socket', side_effect=forbidden):
        yield clock.monotonic


def replay_service(directory, clock, multi_target=False):
    client = Client(clock=clock)
    def hey_read(path, deadline, payload=None):
        if path != '/api/agent/builder_status?address=' + TARGET or payload is not None:
            forbidden()
        return 200, capture('hey-builder_status-unknown.json', folder='')
    client._read = hey_read
    if multi_target:
        lookup = client.lookup
        # Project the bundled HEY record; no uncaptured badge follow-up.
        client.lookup = lambda target, **kw: (
            agent_card(target, capture('hey-builder_status-addr.json', folder=''))
            if target == PRIORS.lower() else lookup(target, **kw))
    net = network.Network()
    for source in net.clients.values():
        source.clock, source._read = clock, forbidden
    # HoodLock's unlock comparison uses a separate wall clock, also fixture time.
    net.clients['hoodlock'].wall_clock = clock
    def sentinel_read(path, deadline, payload=None):
        if multi_target and payload is None:
            for action, filename in [('token', 'sentinel-token-priors.json'),
                                     ('deployer', 'sentinel-deployer-priors.json')]:
                if path == '/api/service?action=' + action + '&value=' + PRIORS.lower():
                    return 200, capture(filename)
        if path != '/api/service?action=token&value=' + TARGET or payload is not None:
            forbidden()
        return 200, capture('sentinel-token-dead.json')
    net.clients['sentinelhood']._read = sentinel_read
    settings = Settings(sources={name: 'on' if name == 'sentinelhood' else 'off'
                                 for name in net.clients})
    return hooks.Service(settings=settings, client=client, network=net,
                         ledger=ledger.Ledger(directory), presence=lambda: False)


def simulated_approval_event(tool_name, directive, session_key, *, session_id, tool_call_id, turn_id=''):
    """The CLI post_approval_response payload for a simulated denial."""
    key = 'plugin_rule:' + directive['rule_key']
    return dict(command=f'<{tool_name}> (plugin approval rule)',
                description=directive['message'], pattern_key=key,
                pattern_keys=[key], session_key=session_key, surface='cli', choice='deny',
                session_id=session_id, tool_call_id=tool_call_id, turn_id=turn_id,
                telemetry_schema_version='hermes.observer.v1')


def walkthrough(service, execute=forbidden, multi_target=False):
    """Simulate host dispatch; return real directives and reopened ledger rows."""
    executions = 0
    def dispatch(directive, choice=None):
        nonlocal executions
        if directive is None or (directive['action'] == 'approve' and choice == 'once'):
            executions += 1
            execute()

    args = {'chainId': 4663, 'token': TARGET}
    if multi_target:
        args = {'chainId': 4663, 'tokens': [PRIORS.lower(), TARGET]}
    def check(call_id):
        return service.pre_tool_call('swap', args, session_id='simulated-replay', tool_call_id=call_id)

    first = check('first')
    event = simulated_approval_event('swap', first, 'simulated-replay',
                                     session_id='simulated-replay', tool_call_id='first')
    service.post_approval_response(**event)
    dispatch(first, event['choice'])
    retry = check('retry')
    dispatch(retry)
    service.presence = lambda: True
    unattended = check('unattended')
    dispatch(unattended)
    rows = ledger.Ledger(service.ledger.directory).rows()
    denied = next(row for row in rows if 'rh-preflight:' + row['id'] == first['rule_key'])
    retried = next(row for row in rows if 'rh-preflight:' + row['id'] == retry['rule_key'])
    stopped = next(row for row in rows if row['disposition'] == 'block')
    assert first['action'] == retry['action'] == 'approve'
    assert first['rule_key'] != retry['rule_key']
    assert denied['decision'] == 'deny' and retried['decision'] is None
    assert unattended['action'] == 'block' and executions == 0
    def say(line):
        print('simulated · ' + line)
    scenario = MULTI_SCENARIO if multi_target else SCENARIO
    say('offline gate replay · ' + scenario + ' · clock: ' + HEY['asOf'])
    say('rh-preflight by quotum with HEY Research Lab · https://heyresearch.xyz')
    say('bundled captures; uncaptured sources off; temporary ledger; no Hermes prompt')
    say('swap · chain 4663 · ' + (PRIORS.lower() + ', ' if multi_target else '') + TARGET)
    for line in first['message'].splitlines():
        say(line)
    if multi_target:
        say('PRIORS uses the bundled HEY projection; uncaptured badge follow-up omitted')
    say('hook directive: approve (ask) · approval key: request-1 (display alias)')
    say('host event via post_approval_response · human response: ' + event['choice'])
    say('ledger request-1 · disposition: ' + denied['disposition'] +
        ' · human response: ' + {'deny': 'denied'}[denied['decision']])
    say('retry: fresh approval required · approval key: request-2 (display alias)')
    say('ledger request-2 · disposition: ' + retried['disposition'] + ' · human response: not recorded')
    say('unattended · hook directive: ' + unattended['action'] + ' · ' + stopped['cards'][0]['card'].splitlines()[0])
    say('action executions: ' + str(executions))
    return first, retry, unattended, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=[SCENARIO, MULTI_SCENARIO], default=SCENARIO)
    multi_target = parser.parse_args().scenario == MULTI_SCENARIO
    with TemporaryDirectory() as directory, replay_clock() as clock:
        walkthrough(replay_service(directory, clock, multi_target), multi_target=multi_target)


if __name__ == '__main__':
    main()
