"""Round 5 same-payment accounting regressions. All inputs are synthetic."""
import contextlib
from copy import deepcopy
import io
import json
from unittest.mock import patch

import pytest

from rh_preflight import hookr_collect as collect, hookr_live as live, income
from rh_preflight.card import token_card
from hookr_synthetic import (TOKEN, OWNER, PAYER, DOOR, BOX, ASSET, reader,
                               synthetic_model, funding_row, history_transport)


def collected(rows=None):
    model = synthetic_model()
    model['funding'] = []
    row = model['income']['lines'][0]
    transfer = funding_row(OWNER, TOKEN, amount=100, block=int(row['blockNumber']) - 1,
                           stamp=row['time'])
    r = reader()
    history_transport(r, rows(transfer) if rows else [transfer])
    collect.direct_funding(r, model, 1, set())
    return model, r


def warnings(model):
    return [s for s in income.classify(TOKEN, model)['sources'] if s['severity'] == 'WARN']


def public(model):
    output = io.StringIO()
    with patch('sys.argv', ['income', '--token', TOKEN, '--public']), \
            patch('sys.stdin', io.StringIO(json.dumps(model))), contextlib.redirect_stdout(output):
        income.main()
    return output.getvalue(), token_card(TOKEN, {}, hookr_income=model).render()


@pytest.mark.parametrize('markers', [({}, {}), ({'traceId': '0'}, {'traceId': '1'}),
    ({'logIndex': '0'}, {'logIndex': '1'}),
    ({'traceId': [], 'logIndex': {}}, {'traceId': 'anything', 'logIndex': True})])
def test_native_hash_ignores_unsolicited_trace_and_log_fields(markers):
    model, _ = collected(lambda row: [{**row, **m} for m in markers])
    assert len(model['funding']) == 1 and model['funding'][0]['amount'] == '100'
    assert len(model['attributions'][-1]['allocations']) == 1
    assert sum(int(s['amount']) for s in warnings(model)) == 100


@pytest.mark.parametrize('field,value', [('value', '99'), ('value', 'invalid'),
    ('blockNumber', '11'), ('timeStamp', '0'), ('from', PAYER), ('to', DOOR),
    ('input', '0x1234'), ('isError', '1'), ('txreceipt_status', '0')])
def test_conflicting_native_observations_are_unknown_never_additive(field, value):
    model, r = collected(lambda row: [row, {**row, field: value}])
    assert not warnings(model)
    assert all(int(row['amount']) <= 100 for row in model['funding'])
    assert 'funding event identity unavailable' in r.reasons
    for text in public(model):
        assert 'warn:' not in text
        assert all(a not in text for a in (OWNER, PAYER, DOOR, BOX, ASSET))


@pytest.mark.parametrize('field,value', [('nativeFundingVerified', False), ('nativeFundingVerified', 1),
    ('kind', 'tokentx'), ('kind', 'txlistinternal'), ('txHash', '0x' + '12' * 32),
    ('blockNumber', '1'), ('time', 0), ('amount', '101'), ('currency', ASSET), ('to', DOOR)])
def test_exact_payment_contract_rejects_unbound_terminal(field, value):
    model, _ = collected()
    assert len(warnings(model)) == 1
    model['attributions'][-1]['allocations'][0]['path'][0][field] = value
    assert not warnings(model)
    for text in public(model):
        assert 'warn:' not in text
        assert income.PAYMENT_REASON in text


@pytest.mark.parametrize('field,value', [('complete', False), ('directFunding', False),
                                        ('destination', DOOR)])
def test_exact_payment_contract_rejects_legacy_allocation(field, value):
    model, _ = collected()
    model['attributions'][-1]['allocations'][0][field] = value
    assert not warnings(model)


def test_verified_native_cannot_be_split_or_reused_for_a_second_line():
    model, _ = collected()
    original = deepcopy(model)
    part = model['attributions'][-1]['allocations'][0]
    part['amount'] = '50'
    model['attributions'][-1]['allocations'].append(deepcopy(part))
    assert not warnings(model)
    model = original
    row = deepcopy(model['funding'][0])
    row['time'] += 1
    model['funding'].append(row)
    attr = deepcopy(model['attributions'][-1])
    attr['line'] = deepcopy(row)
    attr['allocations'][0]['path'][0]['time'] = row['time']
    model['attributions'].append(attr)
    assert not warnings(model)


@pytest.mark.parametrize('origin', [None, 'agent-door payment', 'native verified', 'direct payment'])
def test_origin_label_cannot_allocate_native_deposit_to_later_door_income(origin):
    model, _ = collected()
    part = deepcopy(model['attributions'][-1]['allocations'][0])
    part['origin'] = origin
    part['path'][0]['to'] = part['destination'] = DOOR
    row = model['income']['lines'][0]
    row.update(currency=live.ZERO, amount='100')
    model['attributions'][0] = {'line': deepcopy(row), 'allocations': [part]}
    model['funding'] = []
    model['attributions'] = model['attributions'][:1]
    assert not warnings(model)
    for text in public(model):
        assert 'warn:' not in text and income.PAYMENT_REASON in text


@pytest.mark.parametrize('kind,marker', [('tokentx', 'logIndex'), ('txlistinternal', 'traceId')])
def test_event_indices_are_scoped_to_their_own_history_kind(kind, marker):
    def rows(row):
        row.update(_kind=kind, contractAddress=ASSET if kind == 'tokentx' else live.ZERO)
        return [{**row, marker: '1'}, {**row, marker: '2'}, {**row, marker: '2'}]
    model, _ = collected(rows)
    assert model['funding'][0]['amount'] == '200'
    assert not warnings(model)


def test_token_log_identity_conflicts_do_not_create_additive_events():
    def rows(row):
        row.update(_kind='tokentx', contractAddress=ASSET, logIndex='1')
        return [row, {**row, 'value': '99', 'traceId': '2'}]
    model, r = collected(rows)
    assert model['funding'][0]['amount'] == '100'
    assert 'funding event identity unavailable' in r.reasons
    assert not warnings(model)
