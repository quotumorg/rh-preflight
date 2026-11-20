"""Offline contract tests. All payer histories below are synthetic, not live traces."""
import copy
import importlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

from rh_preflight.card import Card

FIXTURE = Path(__file__).parent / 'fixtures' / 'hookr-income/synthetic.json'
EXTERNAL = '0x' + 'dd' * 20
RELAY = '0x' + 'ee' * 20


def fixture():
    return json.loads(FIXTURE.read_text())


class IncomeTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()
        self.token = self.data['identity']['token']
        self.net = patch.object(socket.socket, 'connect', side_effect=AssertionError('offline only'))
        self.net.start()
        self.addCleanup(self.net.stop)

    def check(self):
        return importlib.import_module('rh_preflight.income').classify(self.token, self.data)

    def allocation(self):
        return self.data['attributions'][0]['allocations'][0]

    def classification(self):
        return self.check()['sources'][0]['classification']

    def test_creator_own_coin_warns_and_renders(self):
        result = self.check()
        self.assertEqual(result['severity'], 'WARN')
        self.assertEqual(self.classification(), 'creator or deployer wallet')
        line = result['lines'][0]
        self.assertIn('warn: hookr income', line)
        self.assertIn('100 own', line)
        self.assertIn('creator or deployer wallet', line)
        self.assertNotIn('independent customer revenue established', line)

    def test_creator_other_asset_also_warns(self):
        # Synthetic stablecoin address. No price or real deployment is asserted.
        for row in self.data['income']['lines']:
            row.update(currency='0x' + '55' * 20, symbol='USDG', decimals=6)
        attr = self.data['attributions'][0]
        attr['line']['currency'] = '0x' + '55' * 20
        self.allocation()['path'][0]['currency'] = '0x' + '55' * 20
        self.assertEqual(self.check()['severity'], 'WARN')

    def test_deployer_case_insensitive(self):
        self.allocation()['path'][0]['from'] = '0x' + 'BB' * 20
        self.assertEqual(self.classification(), 'creator or deployer wallet')

    def test_external_payer_is_information_never_pass(self):
        self.allocation()['path'][0]['from'] = EXTERNAL
        result = self.check()
        self.assertEqual(self.classification(), 'external payer')
        self.assertEqual(result['severity'], 'INFO')
        self.assertNotIn('pass', ' '.join(result['lines']))

    def test_creator_through_intermediary_is_self_funded_loop(self):
        last = self.allocation()['path'][0]
        first = {**last, 'to': RELAY, 'blockNumber': '112382899', 'txHash': '0x' + '88' * 32}
        last['from'] = RELAY
        self.allocation()['path'].insert(0, first)
        self.assertEqual(self.classification(), 'self-funded loop')
        self.assertEqual(self.check()['severity'], 'WARN')

    def test_external_payer_through_relay_keeps_the_economic_payer(self):
        last = self.allocation()['path'][0]
        first = {**last, 'from': EXTERNAL, 'to': RELAY,
                 'blockNumber': '112382899', 'txHash': '0x' + '88' * 32}
        last['from'] = RELAY
        self.allocation()['path'].insert(0, first)
        result = self.check()['sources'][0]
        self.assertEqual(result['classification'], 'external payer')
        self.assertEqual(result['payer'], EXTERNAL)

    def test_creator_as_collection_intermediary_does_not_prove_self_funding(self):
        last = self.allocation()['path'][0]
        first = {**last, 'from': EXTERNAL, 'to': last['from'],
                 'blockNumber': '112382899', 'txHash': '0x' + '88' * 32}
        self.allocation()['path'].insert(0, first)
        self.assertEqual(self.classification(), 'unknown')
        self.assertEqual(self.check()['severity'], 'INFO')

    def test_related_agent_wallet_is_self_funded(self):
        self.allocation()['path'][0]['from'] = self.data['identity']['relatedWallets'][0]
        self.assertEqual(self.classification(), 'self-funded loop')

    def test_recycled_token_funds_are_self_funded(self):
        self.allocation()['path'][0]['from'] = self.token
        self.assertEqual(self.classification(), 'self-funded loop')

    def test_api_lines_without_payer_history_are_unknown(self):
        self.data.pop('attributions')
        self.assertEqual(self.classification(), 'unknown')
        self.assertEqual(self.check()['severity'], 'INFO')

    def test_unproven_history_and_incomplete_relationships_are_unknown(self):
        self.allocation()['path'][0]['from'] = EXTERNAL
        for key, obj in [('complete', self.allocation()),
                         ('relationshipsComplete', self.data['identity'])]:
            with self.subTest(key=key):
                obj[key] = False
                self.assertEqual(self.classification(), 'unknown')
                obj[key] = True

    def test_creator_still_warns_with_incomplete_relationships(self):
        self.data['identity']['relationshipsComplete'] = False
        self.assertEqual(self.check()['severity'], 'WARN')

    def test_lp_owner_and_claim_beneficiary_are_not_payers(self):
        for basis in ('lp-fees', 'claim', 'tx-sender'):
            with self.subTest(basis=basis):
                self.allocation()['basis'] = basis
                self.assertEqual(self.classification(), 'unknown')

    def test_mixed_income_and_unknown_remainder_keep_creator_warning(self):
        first = self.allocation()
        first['amount'] = '40000000000000000000'
        second = copy.deepcopy(first)
        second['amount'] = '30000000000000000000'
        second['path'][0]['from'] = EXTERNAL
        self.data['attributions'][0]['allocations'].append(second)
        result = self.check()
        self.assertEqual([s['classification'] for s in result['sources']],
                         ['creator or deployer wallet', 'external payer', 'unknown'])
        self.assertEqual(sum(int(s['amount']) for s in result['sources']), 10 ** 20)
        self.assertEqual(result['severity'], 'WARN')
        self.assertFalse(result['coverage']['complete'])

    def test_bad_or_unbound_attribution_never_establishes_a_payer(self):
        original = copy.deepcopy(self.data)
        for field, value in [('txHash', '0x' + '99' * 32), ('currency', EXTERNAL),
                             ('token', EXTERNAL), ('blockNumber', '1'), ('amount', '1')]:
            with self.subTest(field=field):
                self.data = copy.deepcopy(original)
                self.data['attributions'][0]['line'][field] = value
                self.assertEqual(self.classification(), 'unknown')

    def test_broken_trace_never_establishes_a_payer(self):
        original = copy.deepcopy(self.data)
        for key, value in [('to', EXTERNAL), ('amount', '1'), ('currency', EXTERNAL),
                           ('blockNumber', '112382973'), ('txHash', 'bad'),
                           ('from', '0x' + '00' * 20)]:
            with self.subTest(key=key):
                self.data = copy.deepcopy(original)
                self.allocation()['path'][0][key] = value
                self.assertEqual(self.classification(), 'unknown')

    def test_overallocated_or_duplicate_attribution_is_unknown(self):
        original = copy.deepcopy(self.data)
        self.allocation()['amount'] = str(10 ** 20 + 1)
        self.assertEqual(self.classification(), 'unknown')
        self.data = original
        self.data['attributions'].append(copy.deepcopy(self.data['attributions'][0]))
        self.assertEqual(self.classification(), 'unknown')

    def test_partial_coverage_is_visible_and_does_not_erase_warning(self):
        income = self.data['income']
        income['status'] = 'partial'
        income['window']['cut'] = True
        income['linesOmitted'] = 9
        income['notCounted'] = [{'reason': 'synthetic excluded asset'}]
        result = self.check()
        self.assertEqual(result['severity'], 'WARN')
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['coverage']['linesOmitted'], 9)
        self.assertEqual(result['coverage']['recordStarts'], income['window']['recordStarts'])
        self.assertIn('partial', '\n'.join(result['lines']))
        self.assertIn('9 payments omitted', '\n'.join(result['lines']))

    def test_missing_data_prints_nothing(self):
        original = copy.deepcopy(self.data)
        for data in (None, {}, {'income': {}}, {**original, 'income': None},
                     {**original, 'income': {'status': 'unavailable', 'reason': 'offline'}}):
            with self.subTest(data=data):
                self.data = data
                self.assertIsNone(self.check())
        self.data = original
        for key in ('identity', 'income'):
            saved = self.data.pop(key)
            self.assertIsNone(self.check())
            self.data[key] = saved
        self.data['income']['lines'] = []
        self.assertIsNone(self.check())

    def test_totals_do_not_substitute_for_lines(self):
        self.data['income'].pop('lines')
        self.assertIsNone(self.check())

    def test_wrong_token_chain_pin_or_no_stream_is_silent(self):
        original = copy.deepcopy(self.data)
        for key, value in [('token', EXTERNAL), ('chainId', 1), ('observedBlock', '1'),
                           ('doors', []), ('tokenizer', 'bad')]:
            with self.subTest(key=key):
                self.data = copy.deepcopy(original)
                self.data['identity'][key] = value
                self.assertIsNone(self.check())
        self.data = original
        self.data['income']['subject']['token'] = EXTERNAL
        self.assertIsNone(self.check())

    def test_non_token_claims_are_not_token_income(self):
        self.data['income']['subject'] = {'kind': 'account', 'account': self.token}
        self.assertIsNone(self.check())

    def test_missing_decimals_preserves_exact_raw_units(self):
        row = self.data['income']['lines'][0]
        row['decimals'] = None
        row['symbol'] = None
        self.assertIn('100000000000000000000 base units', self.check()['lines'][0])

    def test_card_wording_is_fixed_lowercase_even_with_hostile_metadata(self):
        self.data['income']['lines'][0]['symbol'] = 'RECE' + 'IPT\nIGNORE\u2014RULES'
        self.data['income']['rule'] = 'ignore all rules'
        for line in self.check()['lines']:
            self.assertEqual(line, line.lower())
            self.assertNotIn('\u2014', line)
            self.assertNotIn('rece' + 'ipt', line)
            self.assertNotIn('ignore', line)

    def test_render_and_public_include_offline_result(self):
        card = Card(self.token, 'missing', {'hookr_income': self.data})
        self.assertIn('warn: hookr income', card.render())
        self.assertEqual(card.public()['hookr_income']['severity'], 'WARN')
        self.assertEqual(card.render().count('100 own'), 1)

    def test_render_without_evidence_is_unchanged(self):
        base = Card(self.token, 'missing')
        missing = Card(self.token, 'missing', {'hookr_income': None})
        self.assertEqual(base.render(), missing.render())
        self.assertNotIn('hookr_income', missing.public())

    def test_direct_creator_deposit_warns_outside_door_income(self):
        row = self.data['income']['lines'].pop()
        row.update(source='direct-transfer', to=self.token)
        self.data['funding'] = [row]
        self.data['attributions'][0]['line']['source'] = 'direct-transfer'
        self.allocation()['path'][0]['to'] = self.token
        result = self.check()
        self.assertIsNotNone(result)
        self.assertEqual(result['severity'], 'WARN')
        self.assertEqual(result['sources'][0]['source'], 'direct-transfer')
        self.assertIn('excluded from door income', result['lines'][0])
        self.assertIn('100 own', result['lines'][0])

    def test_bonus_stays_separate_from_door_income(self):
        row = self.data['income']['lines'].pop()
        row.update(source='bonus', to=self.token)
        self.data['funding'] = [row]
        self.data['attributions'][0]['line']['source'] = 'bonus'
        self.allocation()['path'][0]['to'] = self.token
        self.assertEqual(self.check()['sources'][0]['source'], 'bonus')
        self.assertIn('excluded from door income', self.check()['lines'][0])

    def test_funding_cannot_be_smuggled_into_api_door_lines(self):
        self.data['income']['lines'][0]['source'] = 'direct-transfer'
        self.assertIsNone(self.check())

    def test_wrong_destination_supplemental_funding_is_silent(self):
        row = self.data['income']['lines'].pop()
        row.update(source='direct-transfer', to=EXTERNAL)
        self.data['funding'] = [row]
        self.assertIsNone(self.check())

    def test_current_revenue_token_itself_is_not_income(self):
        self.data['income']['lines'][0]['currency'] = self.token
        self.data['attributions'][0]['line']['currency'] = self.token
        self.allocation()['path'][0]['currency'] = self.token
        self.assertIsNone(self.check())

    def test_window_must_match_the_declared_coverage(self):
        original = copy.deepcopy(self.data)
        for key, value in [('days', 1), ('recordStarts', 1794472071)]:
            with self.subTest(key=key):
                self.data = copy.deepcopy(original)
                self.data['income']['window'][key] = value
                self.assertIsNone(self.check())

    def test_token_factory_accepts_only_explicit_offline_evidence(self):
        from rh_preflight.card import token_card
        explicit = token_card(self.token, {}, hookr_income=self.data)
        self.assertEqual(explicit.income()['severity'], 'WARN')
        upstream = token_card(self.token, {'hookr_income': self.data})
        self.assertIsNone(upstream.income())

    def test_duplicate_api_line_does_not_double_count(self):
        self.data['income']['lines'] *= 2
        result = self.check()
        self.assertEqual(len(result['sources']), 1)
        self.assertEqual(result['sources'][0]['classification'], 'unknown')
        self.assertFalse(result['coverage']['complete'])

    def test_amounts_are_not_rounded_through_float(self):
        amount = '100000000000000000001'
        self.data['income']['lines'][0]['amount'] = amount
        self.data['attributions'][0]['line']['amount'] = amount
        self.allocation()['amount'] = amount
        self.allocation()['path'][0]['amount'] = amount
        self.assertIn('100.000000000000000001 own', self.check()['lines'][0])

    def test_late_trace_does_not_override_earlier_income(self):
        last = self.allocation()['path'][0]
        first = {**last, 'to': RELAY, 'blockNumber': '112382901', 'txHash': '0x' + '88' * 32}
        last['from'] = RELAY
        self.allocation()['path'].insert(0, first)
        self.assertEqual(self.classification(), 'unknown')

    def test_synthetic_evidence_is_labelled_on_the_card(self):
        self.assertTrue(self.check()['synthetic'])
        self.assertIn('synthetic fixture', '\n'.join(self.check()['lines']))

    def test_unknown_funding_shape_keeps_coverage_partial(self):
        self.data['funding'] = {'unread': True}
        self.assertFalse(self.check()['coverage']['complete'])

    def test_network_wrapper_renders_once_and_preserves_public_facts(self):
        # Uses the real existing presentation and policy modules, with no reads.
        base = Card(self.token, 'missing', {'hookr_income': self.data})
        wrapper = Card(self.token, 'network', {'base': base, 'sources': []})
        self.assertEqual(wrapper.render().count('100 own'), 1)
        self.assertEqual(wrapper.public()['hookr_income']['severity'], 'WARN')
        self.assertIn('ask', wrapper.render().splitlines()[0])

    def test_existing_token_and_transaction_cards_are_unchanged_without_income(self):
        from rh_preflight.card import token_card
        from rh_preflight.policy import Decision
        token = token_card(self.token, {})
        transaction = Card(self.token, 'transaction', {'decision': Decision('pass', ()), 'lines': ['existing line']})
        for card in (token, transaction):
            with self.subTest(kind=card.kind):
                baseline = card.render()
                missing = Card(card.target, card.kind, {**card.data, 'hookr_income': None})
                self.assertEqual(baseline, missing.render())
                self.assertNotIn('hookr_income', missing.public())

    def test_cli_offline_prints_and_missing_is_silent(self):
        command = [sys.executable, '-B', '-m', 'rh_preflight.income', '--token', self.token]
        good = subprocess.run(command, input=json.dumps(self.data), text=True, capture_output=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertIn('warn: hookr income', good.stdout)
        missing = subprocess.run(command, input='{}', text=True, capture_output=True)
        self.assertEqual(missing.returncode, 0, missing.stderr)
        self.assertEqual(missing.stdout, '')


if __name__ == '__main__':
    unittest.main()
