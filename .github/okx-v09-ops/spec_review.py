"""Versioned operational spec review. The frozen strategy and its guards stay intact."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path

from v09.data import timestamp
from v09.forward.freeze import digest
from v09.forward.state import event, financial_hash, validate
from v09.forward.engine import check_snapshot

REVIEW_PATH = Path(__file__).with_name('contract-review.json')
REVIEW_SHA256 = 'e161205333341bb0e95ea73bc8f681c04f31104216e0909dc889beaef9b29a4f'
CRITICAL = ('instId', 'instType', 'ctType', 'settleCcy', 'ctVal', 'ctValCcy',
            'ctMult', 'lotSz', 'minSz', 'tickSz', 'lever', 'state', 'listTime')
POSITIVE = ('ctVal', 'ctMult', 'lotSz', 'minSz', 'tickSz', 'lever')


class ContractSpecReviewRequired(ValueError):
    def __init__(self, reason, differences=None):
        self.reason = reason
        self.differences = differences or []
        super().__init__('contract specification review required: ' + json.dumps(
            dict(reason=reason, differences=self.differences), sort_keys=True))


def load_review():
    raw = REVIEW_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != REVIEW_SHA256:
        raise ContractSpecReviewRequired('review file changed; new independent review required')
    review = json.loads(raw)
    if review['changes'] != [dict(instId='BCH-USDT-SWAP', field='tickSz', previous='0.1', current='0.01')]:
        raise ContractSpecReviewRequired('unsupported review scope')
    evidence = REVIEW_PATH.parent / 'evidence' / 'official-bch-response.json'
    if hashlib.sha256(evidence.read_bytes()).hexdigest() != review['evidence']['sha256']:
        raise ContractSpecReviewRequired('official evidence checksum mismatch')
    row = json.loads(evidence.read_bytes())
    if row['code'] != '0' or len(row['data']) != 1 or row['data'][0]['instId'] != 'BCH-USDT-SWAP' or row['data'][0]['tickSz'] != '0.01':
        raise ContractSpecReviewRequired('official evidence does not support review')
    return review


def specification_differences(previous, current):
    if set(previous) != set(current):
        raise ContractSpecReviewRequired('instrument universe changed', [dict(
            missing=sorted(set(previous)-set(current)), added=sorted(set(current)-set(previous)))])
    differences = []
    for inst in sorted(previous):
        for label, spec in (('previous', previous[inst]), ('current', current[inst])):
            if any(k not in spec or not isinstance(spec[k], str) or not spec[k] for k in CRITICAL):
                raise ContractSpecReviewRequired('missing or malformed critical fields: ' + inst + ' ' + label)
            if spec['instId'] != inst or any(spec[k] != v for k, v in dict(
                    instType='SWAP', ctType='linear', settleCcy='USDT', state='live').items()):
                raise ContractSpecReviewRequired('invalid contract identity/status: ' + inst + ' ' + label)
            for field in POSITIVE:
                try:
                    value = Decimal(spec[field])
                except (InvalidOperation, ValueError):
                    raise ContractSpecReviewRequired('invalid numeric specification: ' + inst + ' ' + field)
                if not value.is_finite() or value <= 0:
                    raise ContractSpecReviewRequired('nonpositive/nonfinite specification: ' + inst + ' ' + field)
        for field in CRITICAL:
            if previous[inst][field] != current[inst][field]:
                differences.append(dict(instId=inst, field=field,
                                        previous=previous[inst][field], current=current[inst][field]))
    return differences


def apply_review(previous, snapshot, review, at):
    """Return an append-only copy; never initialize, rewrite fills, or mutate the input."""
    if (previous['study_id'] != review['study_id'] or previous['config_hash'] != review['config_hash']
            or previous['freeze']['code_commit_sha'] != review['model_commit']):
        raise ContractSpecReviewRequired('review belongs to another study/config/model')
    differences = specification_differences(previous['instruments'], snapshot['instruments'])
    reviews = previous.get('contract_spec_reviews', {})
    marker = reviews.get(review['review_id'])
    if not differences:
        # After the one-time transition, provenance remains required on every restore.
        for change in review['changes']:
            value = previous['instruments'][change['instId']][change['field']]
            if value == change['previous']:
                raise ContractSpecReviewRequired('review pending: fetch a fresh current snapshot; legacy replay cannot advance the checkpoint')
            if value != change['current']:
                raise ContractSpecReviewRequired('restored specification outside reviewed version')
            events = [e for e in previous['journal'] if e['kind'] == 'CONTRACT_SPEC_REVIEW_APPLIED' and e['key'] == review['review_id']]
            if (not marker or marker.get('review_digest') != digest(review)
                    or marker.get('source_state_hash') != review['source']['state_hash']
                    or marker.get('changes') != review['changes']
                    or len(events) != 1 or events[0]['payload'] != marker):
                raise ContractSpecReviewRequired('new specification lacks a verified transition marker')
        return previous
    if differences != review['changes']:
        raise ContractSpecReviewRequired('unreviewed contract change; no blanket acceptance', differences)
    if marker:
        raise ContractSpecReviewRequired('repeated/rolled-back transition refused', differences)
    source = review['source']
    if (digest(previous) != source['state_hash'] or previous['revision'] != source['revision']
            or previous['last_success_run_id'] != source['run_id']):
        raise ContractSpecReviewRequired('reviewed source state changed; never use an older fallback', differences)
    if previous['positions'] or previous['pending']:
        raise ContractSpecReviewRequired('open exposure requires a separate execution/rounding review', differences)
    evidence_at = timestamp(review['evidence']['fetched_at_utc'])
    if timestamp(at) < timestamp(snapshot['observed_at_utc']) or timestamp(snapshot['observed_at_utc']) < evidence_at:
        raise ContractSpecReviewRequired('transition predates verified observation; no backdating', differences)
    result = deepcopy(previous)
    payload = dict(review_id=review['review_id'], operational_version=review['operational_version'],
                   review_digest=digest(review), source_state_hash=digest(previous),
                   evidence=review['evidence'], changes=differences, observed_at_utc=snapshot['observed_at_utc'],
                   applied_at_utc=at, official_effective_time_utc=None,
                   note='Effective time unknown; no historical fills or specifications rewritten')
    for change in differences:
        result['instruments'][change['instId']][change['field']] = change['current']
    result.setdefault('contract_spec_reviews', {})[review['review_id']] = payload
    if not event(result, 'CONTRACT_SPEC_REVIEW_APPLIED', review['review_id'], payload):
        raise ContractSpecReviewRequired('duplicate review event')
    if financial_hash(result) != financial_hash(previous):
        raise AssertionError('spec migration altered financial ledger')
    if result['journal'][:len(previous['journal'])] != previous['journal']:
        raise AssertionError('spec migration rewrote journal history')
    return result


def reviewed_advance(original, previous, freeze, snapshot, run_id, at, parent_completed,
                     review, allow_test=False):
    validate(previous, freeze, at)
    check_snapshot(snapshot, freeze, allow_test)
    approved = apply_review(previous, snapshot, review, at)
    # Original engine still enforces spec equality, funding, cursor integrity,
    # next-open execution, risk/news/quality gates and no retrospective entries.
    return original(approved, freeze, snapshot, run_id, at, parent_completed, allow_test)


def install(cli):
    from v09.forward.engine import advance as frozen_advance
    if cli.advance is not frozen_advance:
        raise ContractSpecReviewRequired('unregistered advance adapter already installed')
    review = load_review()

    def advance(previous, freeze, snapshot, run_id, at, parent_completed, allow_test=False):
        return reviewed_advance(frozen_advance, previous, freeze, snapshot, run_id, at,
                                parent_completed, review, allow_test)

    cli.advance = advance
