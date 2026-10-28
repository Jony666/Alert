import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_forward_v09 as baseline
from v09.data import timestamp, iso
from v09.forward.engine import advance, qualification_reasons
from v09.forward.freeze import digest
from v09.forward.state import financial_hash
import spec_review as target


class ContractReviewTests(unittest.TestCase):
    def setUp(self):
        self.f, self.x, self.s = baseline.initial()
        self.enrich(self.s)
        self.x['instruments'] = copy.deepcopy(self.s['instruments'])
        self.x['instruments']['BTC-USDT-SWAP']['tickSz'] = '.01'
        self.review = copy.deepcopy(target.load_review())
        self.review.update(study_id=self.s['study_id'], config_hash=self.s['config_hash'],
                           model_commit=self.f['code_commit_sha'])
        self.review['changes'] = [dict(instId='BTC-USDT-SWAP', field='tickSz', previous='.1', current='.01')]
        self.review['evidence']['fetched_at_utc'] = iso(timestamp(self.x['observed_at_utc'])-1)
        self.bind()

    def enrich(self, s):
        s['instruments']['BTC-USDT-SWAP'].update(instType='SWAP', ctType='linear',
            settleCcy='USDT', ctMult='1', state='live', listTime='1573557408000')

    def bind(self):
        self.review['source'].update(state_hash=digest(self.s), revision=self.s['revision'],
                                     run_id=self.s['last_success_run_id'])

    def apply(self):
        return target.apply_review(self.s, self.x, self.review, self.x['observed_at_utc'])

    def refused(self):
        before = copy.deepcopy(self.s)
        with self.assertRaises(target.ContractSpecReviewRequired):
            self.apply()
        self.assertEqual(self.s, before)

    def test_published_review_and_evidence_are_sealed(self):
        self.assertEqual(target.load_review()['changes'][0]['instId'], 'BCH-USDT-SWAP')

    def test_tampered_review_cannot_expand_scope(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'review.json'; p.write_text('{}')
            with patch.object(target, 'REVIEW_PATH', p):
                with self.assertRaises(target.ContractSpecReviewRequired): target.load_review()

    def test_only_reviewed_field_changes_and_journal_is_append_only(self):
        before = copy.deepcopy(self.s); out = self.apply()
        self.assertEqual(before, self.s)
        self.assertEqual(financial_hash(out), financial_hash(before))
        self.assertEqual(out['journal'][:-1], before['journal'])
        self.assertEqual(out['journal'][-1]['kind'], 'CONTRACT_SPEC_REVIEW_APPLIED')
        self.assertEqual(out['root_run_id'], before['root_run_id'])
        self.assertEqual(out['research_start_timestamp_utc'], before['research_start_timestamp_utc'])
        self.assertEqual(out['config_hash'], before['config_hash'])
        self.assertEqual(out['revision'], before['revision'])
        for field in target.CRITICAL:
            if field != 'tickSz': self.assertEqual(out['instruments']['BTC-USDT-SWAP'][field], before['instruments']['BTC-USDT-SWAP'][field])

    def test_approved_current_spec_does_not_add_second_review(self):
        self.s = self.apply(); before=copy.deepcopy(self.s)
        self.assertIs(self.apply(), self.s); self.assertEqual(before, self.s)

    def test_new_tick_without_provenance_marker_is_refused(self):
        self.s['instruments']['BTC-USDT-SWAP']['tickSz'] = '.01'; self.refused()

    def test_transition_marker_must_match_immutable_journal_event(self):
        self.s = self.apply()
        self.s['contract_spec_reviews'][self.review['review_id']]['source_state_hash'] = '0'*64
        self.refused()

    def test_legacy_replay_cannot_move_checkpoint_before_transition(self):
        self.x['instruments']['BTC-USDT-SWAP']['tickSz'] = '.1'
        self.refused()

    def test_source_hash_mismatch_is_refused(self):
        self.s['revision'] += 1; self.refused()

    def test_other_study_refused(self):
        self.review['study_id'] = 'other'; self.refused()

    def test_other_model_refused(self):
        self.review['model_commit'] = 'f'*40; self.refused()

    def test_wrong_tick_refused(self):
        self.x['instruments']['BTC-USDT-SWAP']['tickSz'] = '.001'; self.refused()

    def test_decimal_format_change_is_not_silently_whitelisted(self):
        self.x['instruments']['BTC-USDT-SWAP']['tickSz'] = '.0100'; self.refused()

    def test_other_field_and_mixed_change_refused(self):
        self.x['instruments']['BTC-USDT-SWAP']['ctVal'] = '.02'; self.refused()

    def test_leverage_change_refused(self):
        self.x['instruments']['BTC-USDT-SWAP']['lever'] = '200'; self.refused()

    def test_missing_contract_refused(self):
        self.x['instruments'].clear(); self.refused()

    def test_added_contract_refused(self):
        self.x['instruments']['OTHER'] = {}; self.refused()

    def test_missing_field_refused(self):
        del self.x['instruments']['BTC-USDT-SWAP']['tickSz']; self.refused()

    def test_numeric_types_and_invalid_numeric_refused(self):
        for value in (None, .01, '', 'NaN', 'Infinity', '-Infinity', '0', '-1', 'bad'):
            with self.subTest(value=value):
                self.x['instruments']['BTC-USDT-SWAP']['tickSz'] = value; self.refused()

    def test_wrong_identity_refused(self):
        self.x['instruments']['BTC-USDT-SWAP']['instId'] = 'BCH-USDT-SWAP'; self.refused()

    def test_suspended_contract_refused(self):
        self.x['instruments']['BTC-USDT-SWAP']['state'] = 'suspend'; self.refused()

    def test_backdating_refused(self):
        self.review['evidence']['fetched_at_utc'] = iso(timestamp(self.x['observed_at_utc'])+1); self.refused()

    def test_pending_exposure_refused(self):
        baseline.pending(self.s, self.x); self.bind(); self.refused()

    def test_position_exposure_refused(self):
        self.s['positions']['open'] = {'symbol':'BTC-USDT-SWAP'}; self.bind(); self.refused()

    def test_existing_closed_fills_and_costs_survive_unchanged(self):
        f, x, s = baseline.initial(); baseline.pending(s, x)
        (s, _), x = baseline.next_step(s, f, 842)
        (s, _), x = baseline.next_step(s, f, 843, '3')
        self.assertTrue(s['closed']); self.enrich(s)
        self.s=s; self.x=x; self.x['instruments']=copy.deepcopy(s['instruments'])
        self.x['instruments']['BTC-USDT-SWAP']['tickSz']='.01'; self.bind()
        before=copy.deepcopy(s); out=self.apply()
        self.assertEqual(out['closed'],before['closed'])
        self.assertEqual(out['cash_twd'],before['cash_twd'])
        self.assertEqual(out['expense_used_twd'],before['expense_used_twd'])
        self.assertEqual(out['equity'],before['equity'])
        self.assertEqual(out['daily_halts'],before['daily_halts'])
        self.assertEqual(financial_hash(out),financial_hash(before))

    def test_advance_keeps_original_guards_and_replay_is_idempotent(self):
        out, _ = target.reviewed_advance(advance,self.s,self.f,self.x,'2',self.x['observed_at_utc'],
                                         self.s['last_success_at'],self.review,allow_test=True)
        newer, diagnostics = target.reviewed_advance(advance,out,self.f,self.x,'3',self.x['observed_at_utc'],
                                                     out['last_success_at'],self.review,allow_test=True)
        self.assertTrue(diagnostics['duplicate_input'])
        self.assertEqual(financial_hash(out),financial_hash(newer))
        self.assertEqual(len(newer['closed']),0)
        self.assertEqual(sum(e['kind']=='CONTRACT_SPEC_REVIEW_APPLIED' for e in newer['journal']),1)

    def test_expired_state_still_stops_before_migration(self):
        with self.assertRaisesRegex(ValueError,'expired'):
            target.reviewed_advance(advance,self.s,self.f,self.x,'2',iso(timestamp(self.x['observed_at_utc'])+8*86400000),
                                    self.s['last_success_at'],self.review,allow_test=True)
        self.assertNotIn('contract_spec_reviews',self.s)

    def test_rollback_after_applied_review_refused(self):
        self.s = self.apply()
        self.x['instruments']['BTC-USDT-SWAP']['tickSz'] = '.1'; self.refused()

    def test_strict_quality_cross_and_news_gates_are_unchanged(self):
        out=self.apply(); reasons=qualification_reasons(out['freeze'],self.x,self.x['observed_at_utc'],'BTC-USDT-SWAP')
        self.assertIn('WIN_RATE_NOT_VERIFIED',reasons)
        self.assertIn('CROSS_NOT_VERIFIED',reasons)
        self.assertIn('NEWS_UNKNOWN_NOT_NO_EVENT',reasons)
        self.assertFalse(out['pending']); self.assertFalse(out['positions'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
