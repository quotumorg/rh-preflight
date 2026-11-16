"""Offline regression coverage for HoodLock pages, reader probes and CA overrides."""
import argparse
import builtins
import json
import ssl

import pytest

from demo.r6_replay import answer, capture, DELTA, PRIORS
from rh_preflight import cli, trust
from rh_preflight.hey import Client as Hey
from rh_preflight.network import MODULES
from rh_preflight.sources import hoodlock, chain
from rh_preflight.sources.common import Absent


@pytest.fixture
def recorded_locker():
    client = hoodlock.Client(wall_clock=lambda: 1794248544)
    chain.VERIFIED.discard(client.base_url)
    requests = []

    def read(path, deadline, payload=None):
        requests.append(payload)
        if isinstance(payload, list) and payload[0]['params'][0]['data'].startswith('0xf4dadc61'):
            saved = capture('hoodlock-rpc-delta-locks.json')
            by_id = {int(req['params'][0]['data'][10:], 16): res
                     for req, res in zip(saved['request'], saved['response'])
                     if req['method'] == 'eth_call'}
            return 200, [{**by_id[int(req['params'][0]['data'][10:], 16)], 'id': req['id']}
                         for req in payload]
        return answer('hoodlock', path, payload)

    client._read = read
    return client, requests


def test_recorded_locks_read_all_detail_pages(recorded_locker, monkeypatch):
    client, requests = recorded_locker
    monkeypatch.setattr(hoodlock, 'PAGE_SIZE', 1)
    result = client.lookup(DELTA)
    assert result.state == 'present'
    assert '10.00% of supply locked until 2027-01-31, 1 of 2 locks already withdrawn' in result.line
    assert 'first' not in result.line
    pages = requests[2:]
    assert len(pages) == 2
    assert all(len(page) == 1 for page in pages)
    assert all(row['params'][1] == '0x692c84f' for page in pages for row in page)


@pytest.mark.parametrize('bound', ['MAX_PAGES', 'MAX_ITEMS'])
@pytest.mark.parametrize('reverse', [False, True])
def test_recorded_cap_is_explicit_and_preserves_unknown(recorded_locker, monkeypatch, bound, reverse):
    client, requests = recorded_locker
    monkeypatch.setattr(hoodlock, 'PAGE_SIZE', 1)
    monkeypatch.setattr(hoodlock, bound, 1)
    if reverse:
        original = client._read
        def read(path, deadline, payload=None):
            status, rows = original(path, deadline, payload)
            if isinstance(payload, list) and len(payload) == 4:
                index = hoodlock.words(rows[0]['result'])
                rows[0]['result'] = '0x' + ''.join(format(n, '064x') for n in index[:2] + index[2:][::-1])
            return status, rows
        client._read = read
    result = client.lookup(DELTA)
    assert 'first 1 shown of 2 locks; totals cover shown locks only' in result.line
    assert result.token_lock is (None if reverse else True)
    assert len(requests) == 3


def test_recorded_empty_has_no_detail_calls_or_lp_claim(recorded_locker):
    client, requests = recorded_locker
    result = client.lookup(PRIORS)
    assert result.token_lock is False
    assert 'holds no token lock or vesting' in result.line
    assert 'LP locks in HoodLock\'s Liquidity Locker and other lockers not read' in result.line
    assert len(requests) == 2


def test_stress_pages_and_shared_deadline(recorded_locker, monkeypatch):
    # Stress input expands the recorded lock tuple; these extra IDs are synthetic.
    client, requests = recorded_locker
    original = client._read
    monkeypatch.setattr(hoodlock, 'PAGE_SIZE', 3)
    monkeypatch.setattr(hoodlock, 'MAX_PAGES', 4)
    monkeypatch.setattr(hoodlock, 'MAX_ITEMS', 11)
    deadlines = []
    def read(path, deadline, payload=None):
        deadlines.append(deadline)
        if isinstance(payload, list) and payload[0]['params'][0]['data'].startswith('0xf4dadc61'):
            requests.append(payload)
            row = capture('hoodlock-rpc-delta-locks.json')['response'][0]
            return 200, [{**row, 'id': req['id']} for req in payload]
        status, rows = original(path, deadline, payload)
        if isinstance(payload, list):
            rows[0]['result'] = '0x' + ''.join(format(n, '064x') for n in [32, 30, *range(30)])
        return status, rows
    client._read = read
    result = client.lookup(DELTA)
    assert 'first 11 shown of 30 locks' in result.line
    assert [len(page) for page in requests[2:]] == [3, 3, 3, 2]
    assert len(set(deadlines)) == 1


def test_vesting_index_is_not_silently_cut_to_first_page(recorded_locker):
    client, _ = recorded_locker
    original = client._read
    def read(path, deadline, payload=None):
        status, rows = original(path, deadline, payload)
        if isinstance(payload, list) and len(payload) == 4:
            rows[2]['result'] = '0x' + ''.join(format(n, '064x') for n in [32, 45, *range(45)])
        return status, rows
    client._read = read
    assert '45 vesting schedules' in client.lookup(DELTA).line


@pytest.mark.parametrize('name', MODULES)
def test_every_source_doctor_uses_reader_path(name):
    client = MODULES[name].Client()
    calls = []
    # Capture the actual reader request rather than duplicating its URLs in the test.
    class Captured(Exception):
        pass
    def capture_read(path, deadline, *args, **kwargs):
        calls.append((path, kwargs.get('payload')))
        raise Captured()
    client.read = capture_read
    client._read = capture_read
    if name == 'trnch':
        assert client.probe() == {'reachable': None, 'latency_ms': None}
        assert not calls  # Permission explicitly excludes tokenless doctor reads.
        return
    with pytest.raises(Captured):
        client.probe()
    probe = calls.pop()
    if name in ('chain', 'hoodlock'):
        chain.VERIFIED.discard(client.base_url)
    target = getattr(client, 'probe_target', DELTA)
    with pytest.raises(Captured):
        client.fetch(target, client.clock() + 4, **({'stock_ticker': 'NVDA'} if name == 'borro' else {}))
    reader = calls[0]
    assert probe[0] == reader[0]
    if name in ('chain', 'hoodlock'):
        assert probe[1] == reader[1]


def test_hey_probe_uses_builder_reader_endpoint():
    client = Hey()
    requests = []
    def read(path, deadline):
        requests.append(path)
        return 404, {'status': 'not_found', 'answer': {}}
    client._read = read
    assert client.probe()['reachable'] is True
    client.lookup('0x' + '0' * 40)
    assert requests[0] == requests[1]


def test_hookr_probe_follows_reader_html_fallback():
    client = MODULES['hookr'].Client()
    paths = []
    def read(path, deadline, ttl=600):
        paths.append(path)
        return {'html_page': True}
    client.read = read
    assert client.probe()['reachable'] is True
    probe = paths[:]
    paths.clear()
    client.fetch('', client.clock() + 4)
    assert paths == probe
    assert paths[-1] == '/hooks'


@pytest.mark.parametrize('name', ['wormhole', 'toll', 'geckoterminal'])
def test_doctor_counts_per_target_404_as_reachable(name):
    client = MODULES[name].Client()
    client.read = lambda *args, **kwargs: (_ for _ in ()).throw(Absent({}))
    assert client.probe()['reachable'] is True


def _source_bundle():
    try:
        import certifi
        return __import__('pathlib').Path(certifi.where())
    except ImportError:
        paths = ssl.get_default_verify_paths()
        for candidate in (paths.cafile, paths.openssl_cafile):
            if candidate and __import__('pathlib').Path(candidate).is_file():
                return __import__('pathlib').Path(candidate)
    pytest.skip('no CA bundle on this host')


@pytest.fixture
def ca_environment(monkeypatch):
    for name in ('SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(trust, '_CONFIGS', {})


@pytest.mark.parametrize('variable', ['SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE'])
@pytest.mark.parametrize('mode', ['missing', 'unreadable', 'valid'])
def test_ca_override_fallback_once_and_doctor(variable, mode, ca_environment, monkeypatch, tmp_path, capsys):
    bundle = tmp_path / 'bundle.pem'
    if mode != 'missing':
        bundle.write_bytes(_source_bundle().read_bytes())
    if mode == 'unreadable':
        original = builtins.open
        def denied(path, *args, **kwargs):
            if str(path) == str(bundle):
                raise PermissionError('root-only bundle')
            return original(path, *args, **kwargs)
        monkeypatch.setattr(builtins, 'open', denied)
    monkeypatch.setenv(variable, str(bundle))
    clients = [Hey(), Hey(), hoodlock.Client()]
    context, check = trust.configuration()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.cert_store_stats()['x509_ca'] > 0
    assert check['passed'] is (mode == 'valid')
    clients[0]._read = lambda *args: (200, {})
    assert clients[0].probe()['tls_trust'] == check
    output = capsys.readouterr().err
    if mode == 'valid':
        assert output == ''
    else:
        assert len(output.splitlines()) == 1
        assert variable in output and str(bundle) in output
        assert 'make the bundle readable or unset the variable' in output
        assert check['failures'] == [{'variable': variable, 'path': str(bundle),
                                      'fix': 'make the bundle readable or unset the variable'}]
    assert __import__('os').environ[variable] == str(bundle)


def test_cli_warns_at_startup_and_reports_failed_check(ca_environment, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('SSL_CERT_FILE', str(tmp_path / 'missing.pem'))
    class Service:
        def doctor(self):
            return {'tls_trust': trust.check()}
    assert cli.run(Service(), argparse.Namespace(rh_preflight_command='doctor')) == 0
    output = capsys.readouterr()
    assert len(output.err.splitlines()) == 1
    assert json.loads(output.out)['tls_trust']['passed'] is False


def test_ca_fallback_without_certifi(ca_environment, monkeypatch, tmp_path):
    monkeypatch.setenv('SSL_CERT_FILE', str(tmp_path / 'missing.pem'))
    original = builtins.__import__
    def no_certifi(name, *args, **kwargs):
        if name == 'certifi':
            raise ImportError()
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', no_certifi)
    context, check = trust.configuration()
    assert check['passed'] is False
    assert context.cert_store_stats()['x509_ca'] > 0
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_both_bad_overrides_emit_one_line(ca_environment, monkeypatch, tmp_path, capsys):
    for variable in ('SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE'):
        monkeypatch.setenv(variable, str(tmp_path / variable))
    Hey()
    Hey()
    check = trust.check()
    assert len(check['failures']) == 2
    warning = capsys.readouterr().err
    assert len(warning.splitlines()) == 1
    assert all(name in warning for name in ('SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE'))


def test_failed_later_page_does_not_claim_complete_totals(recorded_locker, monkeypatch):
    client, _ = recorded_locker
    monkeypatch.setattr(hoodlock, 'PAGE_SIZE', 1)
    original = client._read
    def read(path, deadline, payload=None):
        status, rows = original(path, deadline, payload)
        if isinstance(payload, list) and len(payload) == 1 and payload[0]['params'][0]['data'].endswith('013f'):
            return 200, [{'id': 1, 'result': '0x'}]
        return status, rows
    client._read = read
    result = client.lookup(DELTA)
    assert result.state == 'unreachable'
    assert result.token_lock is None
    assert '% of supply locked' not in result.line


def test_hey_doctor_follows_legacy_fallback_when_agent_route_is_down():
    from rh_preflight.hey import TemporaryUnavailable
    client = Hey()
    paths = []
    def read(path, deadline):
        paths.append(path)
        if path.startswith('/api/agent/'):
            raise TemporaryUnavailable()
        return 404, {}
    client._read = read
    assert client.probe()['reachable'] is True
    probe = paths[:]
    paths.clear()
    client.lookup('0x' + '0' * 40)
    assert paths == probe
    assert len(paths) == 3
