"""0.5.7 signature take and shared-contract attribution, entirely offline."""

from copy import deepcopy
from dataclasses import replace
from decimal import localcontext
import json
import hashlib
from pathlib import Path

import pytest

from rh_preflight.card import Card, WORDING
from rh_preflight.hey import Client as Hey, TemporaryUnavailable, Unavailable, Refused, RateLimited
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger
from rh_preflight.network import MODULES
from rh_preflight.policy import evaluate, SIGNATURE_TAKE_REASON
from rh_preflight.presentation import present, source_data
from rh_preflight.settings import Settings
from rh_preflight.sources.common import Reading
from rh_preflight.sources.toll import Client as Toll
from rh_preflight.take import UNREAD, RULE, PREFIX, amount, project as take_project
from rh_preflight.transactions import local_card, project, tx_intents

ROOT = Path(__file__).parent / 'fixtures' / '057'
USDG = '0x5fc5360d0400a0fd4f2af552add042d716f1d168'
SPENDER = '0x' + '1' * 40
SLUG = 'rajkaria-hunch-vpm-rh'
SAMPLES = ['permit-unlimited', 'eip3009-5usdg', 'permit-zero-balance', 'personal-sign']


def fixture(name, source='toll-live'):
    return json.loads((ROOT / source / name).read_text(), parse_float=str)


def sample(name):
    request = fixture('req-' + name + '.json')
    # personal_sign has no typed domain, so explicitly supply the call chain.
    request['chainId'] = 4663
    intent = tx_intents(request)[0]
    response = fixture('res-' + name + '.json')
    return project(local_card(intent), intent, response), request, intent, response


def one_row(**changes):
    value = fixture('res-permit-unlimited.json')['x-toll']['take']['takes'][0]
    value.update(changes)
    return value


def with_take(take, response=None):
    card, _, intent, default = sample('permit-unlimited')
    response = deepcopy(default if response is None else response)
    response['x-toll']['take'] = take
    response['warnings'] = []  # Isolate take handling from the capture's independent EOA stop.
    return project(local_card(intent), intent, response)


@pytest.mark.parametrize('name', SAMPLES)
def test_live_signature_samples(name, tmp_path):
    card, _, _, response = sample(name)
    printed = present(card, evaluate([card], Settings())).render()
    assert 'Toll:' in printed and 'ALLOW' not in printed and not WORDING.search(printed)
    assert '\x1b' not in printed and chr(0x2014) not in printed
    assert card.data['unchecked'] == (name in {'permit-zero-balance', 'personal-sign'})
    if name == 'permit-unlimited':
        assert ('Toll: this signature lets 0x1111…1111 take up to 8,324.467654 USDG '
                '(~$8,324.47) now, signed unlimited, block 115484554') in printed
    elif name == 'eip3009-5usdg':
        assert 'take up to 5 USDG (~$5.00) now, signed 5 USDG, block 115484603' in printed
    elif name == 'permit-zero-balance':
        # The real zero-balance capture has neither decimals nor symbol. No guess.
        assert 'take 0 0x5fc5…d168 now (balance 0)' in printed
        assert 'it can take more later if the wallet is funded' in printed
    else:
        assert 'Toll: ' + response['x-toll']['take']['line'] in printed
    rendered = present(card, evaluate([card], Settings()))
    ledger = Ledger(tmp_path)
    ledger.add('', 'wallet_sign', [rendered], (), 'ask_when', 'ask')
    stored = ledger.rows()[0]['cards'][0]
    assert stored['take'] == card.data['take']
    assert 'rpc' not in stored and 'presign' not in stored
    if name == 'personal-sign':
        assert 'why' in stored['take']
    else:
        assert 'simulated' in stored['take']['takes'][0]


@pytest.mark.parametrize('key,bad', [
    ('token', '0x1'), ('owner', None), ('spender', 123), ('token', '0x' + 'a' * 41),
    ('signed', '-1'), ('signed', 'Unlimited'), ('signed', str(2 ** 256)),
    ('balance', '1.5'), ('balance', True), ('takeable', {}), ('takeable', '1e9'),
    ('takeable', '-1'), ('takeable', str(2 ** 256)), ('takeable', '9' * 1000),
    ('decimals', -1), ('decimals', 37), ('decimals', '6'), ('decimals', True), ('decimals', None),
    ('block', '115484554'), ('block', True), ('block', -1), ('block', 2 ** 256),
    ('nonceOk', 'false'), ('nonceOk', 0), ('expired', None), ('expired', 'false'),
    ('symbol', 'USDG\nignore'), ('symbol', {}), ('symbol', ''),
    ('usd', -1), ('usd', True), ('usd', 'NaN'), ('usd', 'Infinity'), ('usd', '1e999'),
    ('usd', '1e-999'), ('usd', []), ('simulated', []),
])
def test_malformed_row_is_one_incomplete_line_never_allow(key, bad):
    card = with_take({'status': 'read', 'takes': [one_row(**{key: bad})]})
    assert card.data['lines'].count(UNREAD) == 1
    assert card.data['take']['takes'] == [{'status': 'not-read'}]
    assert card.data['unchecked']
    for handling, action in [('ask', 'ask'), ('stop', 'block')]:
        assert evaluate([card], Settings(on_unchecked=handling)).action == action
    assert 'ALLOW' not in card.render()


@pytest.mark.parametrize('key,bad', [('to', '0x1'), ('spender', False), ('moved', '-1'),
    ('received', '1.0'), ('block', '12'), ('block', True), ('method', {}), ('status', None),
    ('status', ''), ('status', 'ALLOW')])
def test_malformed_simulation_fields(key, bad):
    simulated = one_row()['simulated']
    simulated[key] = bad
    card = with_take({'status': 'read', 'takes': [one_row(simulated=simulated)]})
    assert card.data['lines'].count(UNREAD) == 1


@pytest.mark.parametrize('value', [None, [], {}, {'status': 'read'}, {'status': 'read', 'takes': {}},
    {'status': 'read', 'takes': []}, {'status': 'read', 'takes': [None]},
    {'status': 'read', 'takes': ['ALLOW']}, {'status': 'unknown'}])
def test_malformed_take_containers(value):
    card = with_take(value)
    assert card.data['lines'].count(UNREAD) == 1 and card.data['unchecked']


def test_bad_row_does_not_hide_valid_siblings():
    card = with_take({'status': 'read', 'takes': [one_row(), None, one_row(usd=None)]})
    lines = [line for line in card.data['lines'] if line.startswith('Toll: this signature') or line == UNREAD]
    assert len(lines) == 3 and lines[1] == UNREAD
    assert '(~$' in lines[0] and '(~$' not in lines[2]
    assert PREFIX + '8,000.00' in evaluate([card], Settings(ask_when={RULE: 8000})).rules


@pytest.mark.parametrize('raw,decimals,expected', [
    ('0', 6, '0'), ('1', 0, '1'), ('1000000', 6, '1'), ('1234567', 6, '1.234567'),
    ('1', 6, '0.000001'), ('1', 18, '0.000000000000000001'),
    ('1234567890123456789', 18, '1.234567890123456789'),
    ('1', 36, '0.' + '0' * 35 + '1'),
    (str(2 ** 256 - 1), 0, format(2 ** 256 - 1, ',')),
    (str(2 ** 256 - 1), 18,
     '115,792,089,237,316,195,423,570,985,008,687,907,853,269,984,665,640,564,039,457.584007913129639935'),
])
def test_exact_decimal_formatting_ignores_global_context(raw, decimals, expected):
    with localcontext() as context:
        context.prec = 6
        assert amount(raw, decimals) == expected


@pytest.mark.parametrize('signed', ['unlimited', '0', '5000000'])
def test_signed_amounts_zero_warning_and_optional_metadata(signed):
    data, lines, incomplete = take_project({'status': 'read', 'takes': [one_row(
        signed=signed, takeable='0', balance='0', usd=None)]})
    assert not incomplete and len(lines) == 1 and 'take 0 USDG now (balance 0)' in lines[0]
    assert ('more later' in lines[0]) == (signed != '0')
    assert '(~$' not in lines[0]
    assert data['takes'][0]['signed'] == signed


@pytest.mark.parametrize('flags,expected', [({'expired': True}, 'expired'),
    ({'nonceOk': False}, 'the nonce is already used'),
    ({'expired': True, 'nonceOk': False}, 'expired; the nonce is already used')])
def test_expired_and_used_nonce_never_claim_available_now(flags, expected):
    card = with_take({'status': 'read', 'takes': [one_row(**flags)]})
    take_line = next(line for line in card.data['lines'] if line.startswith('Toll: this signature'))
    assert expected in take_line and 'take up to' not in take_line
    assert all(RULE not in rule and not rule.startswith(PREFIX) for rule in evaluate(
        [card], Settings(ask_when={RULE: 1})).rules)


def test_not_read_own_line_sanitized_and_capped():
    card = with_take({'status': 'not-read', 'why': 'missing',
        'line': 'not read\n\x1b\u200b' + chr(0x2014) + 'x' * 1000})
    line = next(line for line in card.data['lines'] if line.startswith('Toll: not read'))
    assert len(line) <= 606 and not any(c in line for c in ('\n', '\x1b', '\u200b', chr(0x2014)))
    assert card.data['unchecked']


@pytest.mark.parametrize('value', [1, 250.5, None])
def test_take_threshold_valid_settings(value):
    assert Settings(ask_when={RULE: value}).ask_when[RULE] == value


@pytest.mark.parametrize('value', [True, False, 0, -1, float('nan'), float('inf'), '1', {}, []])
def test_take_threshold_invalid_settings(value):
    with pytest.raises(ValueError, match='ask_when.take_above_usd'):
        Settings(ask_when={RULE: value})


def policy_card(usd):
    # Use a bounded authorization without Permit's independent approval rule.
    card, request, intent, response = sample('eip3009-5usdg')
    response['x-toll']['take']['takes'][0]['usd'] = usd
    response['x-toll']['simulation'] = {'status': 'success', 'block': 12}
    return project(local_card(intent), intent, response), request, response


@pytest.mark.parametrize('usd,expected', [('250.00000000000000000001', 'ask'), ('250', 'pass'), ('249.9999', 'pass')])
def test_strict_usd_boundary(usd, expected):
    card, _, _ = policy_card(usd)
    decision = evaluate([card], Settings(ask_when={RULE: 250}))
    assert decision.action == expected
    assert bool(decision.rules) == (expected == 'ask')


def test_sum_across_rows_and_signatures_without_float_rounding():
    card, _, _ = policy_card('0.1')
    card.data['take']['takes'].append({**card.data['take']['takes'][0], 'usd': '0.2'})
    settings = Settings(ask_when={RULE: 0.3})
    assert evaluate([card], settings).action == 'pass'
    more = deepcopy(card)
    more.data['take']['takes'] = [{**more.data['take']['takes'][0], 'usd': '0.00000000000000000001'}]
    assert evaluate([card, more], settings).action == 'ask'


@pytest.mark.parametrize('ask_when', [{}, {RULE: None}])
def test_take_threshold_default_off(ask_when):
    card, _, _ = policy_card('9999')
    decision = evaluate([card], Settings(ask_when=ask_when))
    assert decision.action == 'ask' and decision.rules == (SIGNATURE_TAKE_REASON,)


def pull_card(rows=None):
    card, request, intent, response = sample('eip3009-5usdg')
    if rows is not None:
        response['x-toll']['take']['takes'] = rows
    return project(local_card(intent), intent, response), request, response


@pytest.mark.parametrize('name', ['permit-unlimited', 'eip3009-5usdg'])
@pytest.mark.parametrize('on_unchecked', ['ask', 'stop', 'note'])
def test_moved_pull_completes_signature_check_but_keeps_approval(name, on_unchecked):
    card, _, _, response = sample(name)
    decision = evaluate([card], Settings(on_unchecked=on_unchecked))
    printed = present(card, decision).render()
    block = response['x-toll']['take']['takes'][0]['simulated']['block']
    assert f'Toll: pull simulated at block {block}' in printed
    assert 'not simulated' not in printed and 'could not be checked' not in printed
    assert not card.data['unchecked']
    assert SIGNATURE_TAKE_REASON in decision.rules
    if name == 'permit-unlimited':
        assert decision.action == 'block'
        assert 'your rule: unlimited approval to a spender with no code' in decision.rules
        assert 'your rule: Permit or Permit2 signature requires approval' in decision.rules
    else:
        assert decision.action == 'ask' and decision.rules == (SIGNATURE_TAKE_REASON,)


def test_pull_simulation_uses_max_simulated_block():
    card, _, _ = pull_card([one_row(block=32, simulated={'status': 'moved', 'block': 35}),
                            one_row(block=34, simulated={'status': 'moved', 'block': 37}),
                            one_row(block=33, simulated={'status': 'moved', 'block': 36})])
    assert 'Toll: pull simulated at block 37' in card.data['lines']
    assert not card.data['unchecked']


@pytest.mark.parametrize('simulated', [None, {'status': 'nothing'}, {'status': 'no-move'},
    {'status': 'reverted'}, {'status': 'success'}, {'status': 'unknown'}])
def test_only_documented_moved_status_completes_pull_check(simulated):
    card, _, _ = pull_card([one_row(), one_row(simulated=simulated)])
    assert card.data['unchecked'] and 'Toll: not simulated' in card.data['lines']
    assert not any('pull simulated' in line for line in card.data['lines'])
    assert evaluate([card], Settings(on_unchecked='stop')).action == 'block'


@pytest.mark.parametrize('generic_simulation', [False, True])
def test_mixed_moved_and_malformed_rows_keep_unchecked(generic_simulation):
    card, request, response = pull_card([one_row(), one_row(block='bad')])
    if generic_simulation:
        response['x-toll']['simulation'] = {'status': 'success', 'block': 12}
        intent = tx_intents(request)[0]
        card = project(local_card(intent), intent, response)
    assert UNREAD in card.data['lines'] and card.data['unchecked']
    assert not any('pull simulated' in line for line in card.data['lines'])
    decision = evaluate([card], Settings())
    assert SIGNATURE_TAKE_REASON in decision.rules
    assert 'your rule: transaction could not be checked' in decision.rules


@pytest.mark.parametrize('warning', ['typed-data-unread', 'coin-partly-read'])
def test_moved_pull_preserves_independent_unchecked_reasons(warning):
    _, request, response = pull_card()
    response['warnings'] = [{'code': warning}]
    intent = tx_intents(request)[0]
    card = project(local_card(intent), intent, response)
    assert card.data['unchecked']
    assert any('pull simulated at block' in line for line in card.data['lines'])
    assert 'Toll: not simulated' not in card.data['lines']
    assert evaluate([card], Settings(on_unchecked='stop')).action == 'block'


@pytest.mark.parametrize('changes', [dict(takeable='0'), dict(expired=True), dict(nonceOk=False),
    dict(expired=True, nonceOk=False), dict(takeable='0', balance='0')])
@pytest.mark.parametrize('status', ['moved', 'nothing'])
def test_no_default_signature_take_for_zero_expired_or_used_nonce(changes, status):
    card, _, _ = pull_card([one_row(**changes, simulated={'status': status})])
    assert not card.data['signature_take']
    decision = evaluate([card], Settings())
    assert SIGNATURE_TAKE_REASON not in decision.rules
    assert decision.action == ('pass' if status == 'moved' else 'ask')
    assert bool(card.data['unchecked']) == (status != 'moved')


@pytest.mark.parametrize('usd', [None, '0', '5'])
@pytest.mark.parametrize('nonce', [None, True])
def test_default_signature_take_uses_token_amount_without_requiring_usd(usd, nonce):
    card, _, _ = pull_card([one_row(usd=usd, nonceOk=nonce)])
    decision = evaluate([card], Settings())
    assert decision.action == 'ask' and decision.rules == (SIGNATURE_TAKE_REASON,)


@pytest.mark.parametrize('threshold,action', [(4, 'ask'), (5, 'pass'), (6, 'pass')])
def test_threshold_replaces_default_ask_after_successful_pull(threshold, action):
    card, _, _ = pull_card()
    decision = evaluate([card], Settings(ask_when={RULE: threshold}, on_unchecked='stop'))
    printed = present(card, decision).render()
    assert decision.action == action and SIGNATURE_TAKE_REASON not in decision.rules
    assert 'not simulated' not in printed and 'could not be checked' not in printed
    assert 'take up to 5 USDG' in printed  # The exposure stays visible below the threshold.
    assert decision.rules == ((PREFIX + f'{threshold:.2f}',) if action == 'ask' else ())


@pytest.mark.parametrize('on_unchecked,action', [('ask', 'ask'), ('stop', 'block'), ('note', 'pass')])
def test_personal_sign_and_not_read_keep_existing_behavior(on_unchecked, action):
    personal, _, _, response = sample('personal-sign')
    decision = evaluate([personal], Settings(on_unchecked=on_unchecked))
    assert decision.action == action and SIGNATURE_TAKE_REASON not in decision.rules
    assert personal.data['lines'] == ['personal_sign request on chain 4663',
        'Toll: ' + response['x-toll']['take']['line'], 'Toll: not simulated', 'Toll · https://t0ll.xyz']
    _, request, response = pull_card()
    response['x-toll']['take'] = {'status': 'not-read', 'line': 'pull unavailable'}
    intent = tx_intents(request)[0]
    card = project(local_card(intent), intent, response)
    assert card.data['unchecked'] and 'Toll: pull unavailable' in card.data['lines']
    assert 'Toll: not simulated' in card.data['lines']
    decision = evaluate([card], Settings(on_unchecked=on_unchecked))
    assert decision.action == action and SIGNATURE_TAKE_REASON not in decision.rules


def test_personal_sign_does_not_use_typed_signature_pull_completion():
    _, _, intent, response = sample('personal-sign')
    response['x-toll']['take'] = {'status': 'read', 'takes': [one_row()]}
    card = project(local_card(intent), intent, response)
    assert card.data['unchecked'] and not card.data.get('signature_take')
    assert 'Toll: not simulated' in card.data['lines']
    assert not any('pull simulated' in line for line in card.data['lines'])


def test_transaction_cannot_use_signature_take_to_complete_simulation():
    intent = tx_intents({'chainId': 4663, 'from': SPENDER, 'to': USDG, 'data': '0x'})[0]
    response = {'x-toll': {'take': {'status': 'read', 'takes': [one_row()]}}}
    card = project(local_card(intent), intent, response)
    assert 'take' not in card.data and not card.data.get('signature_take')
    assert card.data['unchecked'] and 'Toll: not simulated' in card.data['lines']


def test_moved_pull_does_not_clear_missing_signer_check():
    _, request, response = pull_card()
    intent = tx_intents(request)[0]
    intent['sender'] = None
    card = project(local_card(intent), intent, response)
    assert card.data['unchecked']
    assert any('signing wallet not recorded' in line for line in card.data['lines'])
    assert any('pull simulated at block' in line for line in card.data['lines'])
    assert 'Toll: not simulated' not in card.data['lines']


@pytest.mark.parametrize('mode,cron,allow,disposition', [
    ('ask_when', False, False, 'ask'), ('ask_when', False, True, 'ask'),
    ('note', False, False, 'note'), ('ask', False, False, 'ask'),
    ('ask_when', True, False, 'block'), ('ask_when', True, True, 'pass'),
    ('ask', True, False, 'block'), ('ask', True, True, 'pass'),
    ('note', True, False, 'note'), ('note', True, True, 'note'),
])
def test_signature_take_service_ledger_and_ask_semantics(tmp_path, mode, cron, allow, disposition):
    _, request, response = pull_card()
    settings = Settings(mode=mode, allow_unattended=allow, on_unchecked='stop',
        sources={s: 'off' for s in MODULES if s != 'toll'}, ask_no_record=False)
    service = Service(settings=settings, ledger=Ledger(tmp_path), presence=lambda: cron)
    service.client.lookup = lambda target, **kw: Card(target, 'missing')
    service.network.clients['toll'].lookup = lambda *a, **kw: Reading('Toll', 'unlisted', '')
    service.network.clients['toll'].check_tx = lambda *a: response
    directive = service.pre_tool_call('wallet_sign', request)
    row = service.ledger.rows()[0]
    assert row['disposition'] == disposition
    assert 'signature_take: ' + SIGNATURE_TAKE_REASON in row['rules']
    stored = next(card for card in row['cards'] if 'take' in card)
    assert SIGNATURE_TAKE_REASON in stored['rules']
    assert 'not simulated' not in stored['card']
    assert 'could not be checked' not in stored['card']
    if disposition in {'ask', 'block'}:
        assert directive['action'] == ('block' if disposition == 'block' else 'approve')
        assert 'this signature lets someone take funds' in directive['message']
    else:
        assert directive is None


@pytest.mark.parametrize('mode,cron,allow,disposition', [
    ('ask_when', False, False, 'ask'), ('note', False, False, 'note'),
    ('ask', False, False, 'ask'), ('ask_when', True, False, 'block'),
    ('ask_when', True, True, 'pass'), ('note', True, False, 'note'),
])
def test_take_rule_through_service_and_ledger(tmp_path, mode, cron, allow, disposition):
    card, request, response = policy_card('300')
    settings = Settings(mode=mode, allow_unattended=allow, ask_when={RULE: 250},
        sources={s: 'off' for s in MODULES if s != 'toll'}, ask_no_record=False)
    service = Service(settings=settings, ledger=Ledger(tmp_path), presence=lambda: cron)
    service.client.lookup = lambda target, **kw: Card(target, 'missing')
    service.network.clients['toll'].lookup = lambda *a, **kw: Reading('Toll', 'unlisted', '')
    service.network.clients['toll'].check_tx = lambda *a: response
    directive = service.pre_tool_call('wallet_sign', request)
    row = service.ledger.rows()[0]
    assert row['disposition'] == disposition
    if disposition in {'ask', 'block'}:
        assert directive['action'] == ('block' if disposition == 'block' else 'approve')
    else:
        assert directive is None
    assert RULE + ': ' + PREFIX + '250.00' in row['rules']
    assert not any(rule.startswith('signature_take:') for rule in row['rules'])
    assert row['cards'][0]['take']['takes'][0]['usd'] == '300'


@pytest.mark.parametrize('mode', ['ask_when', 'note', 'ask'])
def test_take_threshold_keeps_hard_stops(mode):
    card, _, _ = policy_card('300')
    card = replace(card, data={**card.data, 'approval_eoa': True})
    assert evaluate([card], Settings(mode=mode, ask_when={RULE: 250})).action == 'block'


def test_toll_transport_preserves_usd_number_tokens():
    raw = b'{"x-toll":{"take":{"status":"read","takes":[{"usd":250.00000000000000000001}]}},"other":1.5}'
    data = Toll()._decode(raw)
    assert data['x-toll']['take']['takes'][0]['usd'] == '250.00000000000000000001'
    assert data['other'] == 1.5


@pytest.fixture
def hey_http(monkeypatch):
    """Real Client resolution, caching and rendering, replacing HTTP bytes only."""
    client, reads = Hey(), []
    bodies = {USDG: fixture('bs-usdg.json', 'hey-live')}
    detail = fixture('project-rajkaria-hunch-vpm-rh.json', 'hey-live')
    def read(path, deadline, payload=None):
        reads.append(path)
        if path.startswith('/api/agent/builder_status?address='):
            target = path.split('address=')[1]
            body = bodies[target]
            return (404 if body['status'] == 'not_found' else 200), deepcopy(body)
        if path == '/api/projects/' + SLUG:
            return 200, deepcopy(detail)
        raise AssertionError('unexpected HTTP read: ' + path)
    monkeypatch.setattr(client, '_read', read)
    return client, reads, bodies, detail


def test_real_usdg_context_only_suppresses_every_project_claim(hey_http):
    client, reads, _, _ = hey_http
    card = client.lookup(USDG, required=())
    assert card.data == {'context_only': True, 'context_reason': 'third_party_contract'}
    assert card.render() == "HEY: shared contract, not one project's own"
    assert not card.evidence_ids
    assert reads == ['/api/agent/builder_status?address=' + USDG, '/api/projects/' + SLUG]
    for forbidden in ('hunch', 'rajkaria', 'Builder', 'ship'):
        assert forbidden not in card.render()
    wrapped = present(Card(USDG, 'network', {'base': card, 'sources': [Reading('Toll', 'present', 'Toll: USDG')]}),
        evaluate([card], Settings()))
    assert "HEY: shared contract" not in wrapped.render()
    assert wrapped.public()['context_only'] is True
    assert wrapped.public()['context_reason'] == 'third_party_contract'
    assert source_data(wrapped)[0]['link'] is None
    # No snapshot can reintroduce a project or ships for this address.
    assert client.attach_locks(card, client.clock() + 1) == card
    assert len(reads) == 2


@pytest.mark.parametrize('url,match', [
    ('https://explorer.example/address/' + USDG.upper().replace('0X', '0x'), True),
    ('https://explorer.example/address/' + USDG + '?tag=1', True),
    ('https://explorer.example/address/' + USDG + '#code', True),
    ('https://explorer.example/address/' + USDG.upper(), True),
    ('https://explorer.example/address/' + USDG + '/other', False),
    (USDG, False),
    ('https://explorer.example/address/' + USDG + '0', False),
    ('https://explorer.example/address/1' + USDG, False),
    ('https://explorer.example/address/' + '0x' + '0' * 40, False),
    ('https://explorer.example/?address=' + USDG, False),
    (None, False),
])
def test_context_source_exact_path_matching(hey_http, url, match):
    client, _, _, detail = hey_http
    detail['sources'] = [{'url': url, 'contextOnly': True, 'contextReason': 'third_party_contract'}]
    # Supply missing legacy badge fields locally to isolate ownership matching.
    client._address = lambda *a: Card(USDG, 'token', {})
    card = client.lookup(USDG, required=())
    assert (card.data.get('context_only') is True) == match
    assert ('hunch' not in card.render()) == match


@pytest.mark.parametrize('value', [False, None, 'true', 1])
def test_context_flag_must_be_true(hey_http, value):
    client, _, _, detail = hey_http
    for source in detail['sources']:
        source['contextOnly'] = value
    client._address = lambda *a: Card(USDG, 'token', {})
    assert not client.lookup(USDG, required=()).data.get('context_only')


@pytest.mark.parametrize('name', ['bs-nvda.json', 'bs-weth.json'])
def test_not_found_controls_do_not_read_project(hey_http, name):
    client, reads, bodies, _ = hey_http
    body = fixture(name, 'hey-live')
    target = body['query']['address']
    bodies[target] = body
    card = client.lookup(target)
    assert 'no published project for this contract' in card.render()
    assert not card.data.get('context_only')
    assert reads == ['/api/agent/builder_status?address=' + target]


@pytest.mark.parametrize('failure', [Unavailable(), TemporaryUnavailable(), Refused(403), RateLimited(),
    (404, {}), (200, {}), (200, {'sources': None})])
def test_project_read_failure_retains_current_card_with_ownership_note(hey_http, monkeypatch, failure):
    client, reads, _, _ = hey_http
    read = client._read
    def failed(path, deadline, payload=None):
        if path == '/api/projects/' + SLUG:
            reads.append(path)
            if isinstance(failure, Exception):
                raise failure
            return failure
        return read(path, deadline, payload)
    monkeypatch.setattr(client, '_read', failed)
    client._address = lambda *a: Card(USDG, 'token', {})
    card = client.lookup(USDG, required=())
    assert card.kind == 'agent' and 'hunch' in card.render()
    assert card.render().splitlines()[-1] == 'HEY: project ownership of this address not confirmed'
    assert card.data['ownership_unconfirmed'] is True
    assert not client.cache  # Retry an unconfirmed ownership read on the next check.


@pytest.mark.parametrize('ttl', [0, 600])
def test_project_dossier_and_context_card_cache(hey_http, ttl):
    client, reads, _, _ = hey_http
    client.ttl = ttl
    ticks = [0.0]
    client.clock = lambda: ticks[0]
    client.lookup(USDG)
    client.lookup(USDG)
    assert len(reads) == (4 if ttl == 0 else 2)
    if ttl:
        cached = client.lookup(USDG)
        assert cached.cached_read_at == 0
        assert cached.render() == "HEY: shared contract, not one project's own"
        # Dossier reuse even when target card is not cached.
        client.cache.pop((USDG, None, ('verified_builder', 'activity_measured', 'meaningful_ships_30d')))
        client.lookup(USDG)
        assert reads.count('/api/projects/' + SLUG) == 1
        ticks[0] = 600
        client.lookup(USDG)
        assert reads.count('/api/projects/' + SLUG) == 2


def test_shared_contract_policy_does_not_inherit_foreign_badge_or_activity(hey_http):
    client, _, _, _ = hey_http
    card = client.lookup(USDG)
    decision = evaluate([card], Settings(ask_not_verified=True))
    assert decision.rules == ('your rule: no HEY record',)
    assert evaluate([card], Settings(ask_no_record=False)).action == 'pass'


RAW_SHA256 = {'hey-live/bs-nvda.json': '177bf60a2f0364f0af63ca03b5a136dd3cb9435a05a15654200bb2e09f21048e',
 'hey-live/bs-usdg.json': '91bd515959874a80f428190de22d4d49f9a72f136b56cddf76e4e0e9ed8aa2df',
 'hey-live/bs-weth.json': 'ecf3d14c0d25a4f2779b71dc29e8c7a4a519213374fe5d2d6f6fcc3196e04734',
 'hey-live/project-rajkaria-hunch-vpm-rh.json': 'c9b7e4bea948b6f1b7e572f495e8abb1991807affe0590179498e2a57143de01',
 'toll-live/req-eip3009-5usdg.json': 'f816c3fd42bc46de51ecfa9114b2733035f20c18a27f07565812a3cec5b7a73f',
 'toll-live/req-permit-unlimited.json': '79f8e02e02657b2dc1bf36666eaf6983932eb34e9260dbe884d7f08275e430b8',
 'toll-live/req-permit-zero-balance.json': '34608dbb65f18995f98b53000b2bfecc943fa6760408730523414a5dce67cc1b',
 'toll-live/req-personal-sign.json': '4eaa153b2b81d95f4407733c346efdd14dfeab80bbda24ad8ed79a7474aed088',
 'toll-live/res-eip3009-5usdg.json': '61f6e88f3d92becd60768f5a9be6c876ee83eb85d9bcd86e272fa0d6ebae880f',
 'toll-live/res-permit-unlimited.json': '24365d41eb62a7e73aeae54e1285d924ed6f7a452e807f262ab69a4dc4480b45',
 'toll-live/res-permit-zero-balance.json': '1d14e00a96384ba9206088ddc753b026ca4be929fc550791c0f0864633181d89',
 'toll-live/res-personal-sign.json': '3e840f781da6503841900f8563fa7d527d8cd3d3adf93cdb06fa5c96284cb0ba'}


@pytest.mark.parametrize('name,digest', RAW_SHA256.items())
def test_live_captures_remain_byte_for_byte(name, digest):
    assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize('missing', ['usd', 'symbol', 'decimals', 'simulated'])
def test_optional_fields_are_not_guessed(missing):
    item = one_row()
    item.pop(missing)
    card = with_take({'status': 'read', 'takes': [item]})
    assert UNREAD not in card.data['lines']
    line = next(line for line in card.data['lines'] if line.startswith('Toll: this signature'))
    if missing == 'usd':
        assert '(~$' not in line
    elif missing == 'symbol':
        assert '0x5fc5…d168' in line and 'USDG' not in line
    elif missing == 'decimals':
        assert '8,324,467,654 USDG base units' in line
    assert missing not in card.data['take']['takes'][0]


def test_take_does_not_enrich_an_unsigned_transaction():
    intent = tx_intents({'chainId': 4663, 'from': SPENDER, 'to': USDG, 'data': '0x'})[0]
    response = fixture('res-permit-unlimited.json')
    card = project(local_card(intent), intent, response)
    assert 'take' not in card.data


def test_unknown_remote_fields_are_not_stored():
    item = one_row()
    item['unrelated'] = 'discard-this-field'
    item['simulated']['unrelated'] = 'discard-this-field'
    card = with_take({'status': 'read', 'takes': [item], 'unrelated': 'discard-this-field'})
    assert 'discard-this-field' not in json.dumps(card.data)


def test_toll_sse_preserves_take_usd_precision():
    raw = b'data: {"id":1,"x-toll":{"take":{"takes":[{"usd":1.00000000000000000001}]}}}\n\n'
    assert Toll()._decode(raw)['x-toll']['take']['takes'][0]['usd'] == '1.00000000000000000001'


def test_take_does_not_replace_an_existing_simulation_with_success():
    card, request, response = policy_card('1')
    response['x-toll']['simulation'] = {'status': 'reverted'}
    intent = tx_intents(request)[0]
    card = project(local_card(intent), intent, response)
    assert card.data['reverted']
    assert evaluate([card], Settings()).action == 'ask'


def test_shared_contract_keeps_official_stock_no_record_exemption(hey_http):
    client, _, _, _ = hey_http
    base = client.lookup(USDG)
    card = Card(USDG, 'network', {'base': base, 'sources': [
        Reading('Toll', 'present', 'Toll: official Stock Token', stock_ticker='EXAMPLE')]})
    assert evaluate([card], Settings()).action == 'pass'
    assert evaluate([card], Settings(ask_when={'stock_no_hey_record': True})).rules == ('your rule: no HEY record',)


@pytest.mark.parametrize('usd', ['249.99', '250'])
def test_service_take_below_or_at_threshold_does_not_ask(tmp_path, usd):
    _, request, response = policy_card(usd)
    settings = Settings(ask_when={RULE: 250}, ask_no_record=False,
        sources={s: 'off' for s in MODULES if s != 'toll'})
    service = Service(settings=settings, ledger=Ledger(tmp_path), presence=lambda: False)
    service.client.lookup = lambda target, **kw: Card(target, 'missing')
    service.network.clients['toll'].lookup = lambda *a, **kw: Reading('Toll', 'unlisted', '')
    service.network.clients['toll'].check_tx = lambda *a: response
    assert service.pre_tool_call('wallet_sign', request) is None
    assert service.ledger.rows()[0]['disposition'] == 'pass'
    assert service.ledger.rows()[0]['rules'] == []


def test_missing_usd_does_not_fire_take_threshold():
    card, _, _ = policy_card(None)
    assert evaluate([card], Settings(ask_when={RULE: 1})).action == 'pass'
