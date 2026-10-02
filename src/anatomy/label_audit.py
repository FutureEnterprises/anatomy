"""Local comparison of supplied model labels and separately supplied reviews.

No inference is performed. The files contain only opaque join keys and label
enums. Neither a file's name nor its contents establish human authorship,
independence, representative sampling or correctness of a review.
"""
from __future__ import annotations

import hashlib
import json
import math
import random

from .coach import CLASSIFIER_ID, KEY, LABELS, UNKNOWN, Refusal
from .privacy import PrivacyError, assert_clean, gate

FORMAT = 'anatomy-label-audit-v1'
SAMPLE_FORMAT = 'anatomy-label-audit-sample-v1'
ALL_LABELS = LABELS + (UNKNOWN,)
LIMITS = (
    'Human authorship and independence of supplied reviews are not verified.',
    'Correctness and representativeness of supplied reviews are not verified.',
    'Conditional accuracy excludes unknown reviews and unresolved predictions.',
    'End-to-end recall includes unknown and missing predictions as misses.',
    'Task outcomes and causal savings are not measured.',
    'Model rankings and agent error rates are not measured.',
)
COUNT_FIELDS = (
    'prediction_keys', 'review_keys', 'matched_keys', 'unreviewed_prediction_keys',
    'unmatched_review_keys', 'known_predictions', 'unknown_predictions',
    'known_reviews', 'unknown_reviews', 'matched_known_reviews',
    'paired_known_labels', 'unknown_predictions_on_known_reviews',
    'missing_predictions_on_known_reviews', 'correct_known_pairs',
)
CORRECTION_COUNTS = (
    'true_positives', 'false_positives', 'false_negatives',
    'false_negatives_known_predictions', 'unknown_prediction_corrections',
    'missing_prediction_corrections', 'reviewed_corrections',
    'unassessed_correction_predictions',
)


def _key(value) -> bool:
    return isinstance(value, str) and KEY.fullmatch(value) is not None


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _ratio(numerator: int, denominator: int):
    return numerator / denominator if denominator else None


def _mapping(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise Refusal('%s must be a key-to-label mapping' % name)
    for key, label in value.items():
        if not _key(key) or not isinstance(label, str) or label not in ALL_LABELS:
            raise Refusal('%s contain an invalid key or label' % name)
    return value


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def load_reviews(path: str) -> dict:
    """Read exact JSONL {key,label} records; preserve explicit unknown labels.

    This same strict loader can read model labels. Extra fields are refused so
    review files cannot silently retain prompt text or private annotations.
    Identical repeats collapse; conflicting repeats are refused.
    """
    result = {}
    try:
        with open(path, encoding='utf-8', errors='strict') as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line, object_pairs_hook=_unique_fields)
                except (ValueError, TypeError):
                    raise Refusal('review file line %d is not strict JSON' % number) from None
                if not isinstance(row, dict) or set(row) != {'key', 'label'}:
                    raise Refusal('review file line %d must contain only key and label' % number)
                if not _key(row['key']) or not isinstance(row['label'], str) or row['label'] not in ALL_LABELS:
                    raise Refusal('review file line %d has an invalid key or label' % number)
                if row['key'] in result and result[row['key']] != row['label']:
                    raise Refusal('review file line %d conflicts with an earlier label' % number)
                result[row['key']] = row['label']
    except UnicodeDecodeError:
        raise Refusal('review file is not UTF-8') from None
    if not result:
        raise Refusal('review file contains no labels')
    return result


def evaluate(labels: dict, reviews: dict, classifier: str = 'labels-file') -> dict:
    """Compare supplied labels, retaining unresolved predictions in recall.

    Confusion rows are review classes; columns are prediction classes. The
    five-class matrix conditions on both labels being known. Correction recall
    and end-to-end agreement instead include every known review, including
    reviews whose prediction is unknown or entirely absent.
    """
    _mapping(labels, 'predictions')
    _mapping(reviews, 'reviews')
    if not reviews:
        raise Refusal('no reviews supplied')
    if not isinstance(classifier, str) or CLASSIFIER_ID.fullmatch(classifier) is None:
        raise Refusal('classifier id has an invalid format')
    gate({'classifier': classifier})
    intersection = labels.keys() & reviews.keys()
    known_reviews = {key: label for key, label in reviews.items() if label != UNKNOWN}
    confusion = {review: {prediction: 0 for prediction in LABELS} for review in LABELS}
    unknown_predictions = missing_predictions = correct = 0
    unknown_corrections = missing_corrections = 0
    for key, review in known_reviews.items():
        if key not in labels:
            missing_predictions += 1
            missing_corrections += int(review == 'correction')
        elif labels[key] == UNKNOWN:
            unknown_predictions += 1
            unknown_corrections += int(review == 'correction')
        else:
            confusion[review][labels[key]] += 1
            correct += int(review == labels[key])
    paired = sum(sum(row.values()) for row in confusion.values())
    tp = confusion['correction']['correction']
    fp = sum(confusion[label]['correction'] for label in LABELS if label != 'correction')
    fn_known = sum(confusion['correction'][label] for label in LABELS if label != 'correction')
    human_corrections = sum(label == 'correction' for label in known_reviews.values())
    counts = {
        'prediction_keys': len(labels), 'review_keys': len(reviews),
        'matched_keys': len(intersection),
        'unreviewed_prediction_keys': len(labels.keys() - reviews.keys()),
        'unmatched_review_keys': len(reviews.keys() - labels.keys()),
        'known_predictions': sum(label != UNKNOWN for label in labels.values()),
        'unknown_predictions': sum(label == UNKNOWN for label in labels.values()),
        'known_reviews': len(known_reviews),
        'unknown_reviews': len(reviews) - len(known_reviews),
        'matched_known_reviews': len(known_reviews.keys() & labels.keys()),
        'paired_known_labels': paired,
        'unknown_predictions_on_known_reviews': unknown_predictions,
        'missing_predictions_on_known_reviews': missing_predictions,
        'correct_known_pairs': correct,
    }
    correction = {
        'true_positives': tp, 'false_positives': fp,
        'false_negatives': fn_known + unknown_corrections + missing_corrections,
        'false_negatives_known_predictions': fn_known,
        'unknown_prediction_corrections': unknown_corrections,
        'missing_prediction_corrections': missing_corrections,
        'reviewed_corrections': human_corrections,
        'unassessed_correction_predictions': sum(
            label == 'correction' and reviews.get(key, UNKNOWN) == UNKNOWN
            for key, label in labels.items()),
        'precision': _ratio(tp, tp + fp),
        'recall': _ratio(tp, human_corrections),
        'recall_on_known_predictions': _ratio(tp, tp + fn_known),
    }
    report = {
        'format': FORMAT, 'basis': 'observed', 'classifier': classifier,
        'counts': counts, 'confusion': confusion,
        'accuracy': {
            'conditional_known_labels': _ratio(correct, paired),
            'end_to_end_known_reviews': _ratio(correct, len(known_reviews)),
        },
        'coverage': {
            'known_prediction_fraction': _ratio(counts['known_predictions'], len(labels)),
            'reviewed_prediction_fraction': _ratio(len(intersection), len(labels)),
            'known_prediction_fraction_on_known_reviews': _ratio(paired, len(known_reviews)),
            'known_review_fraction': _ratio(len(known_reviews), len(reviews)),
        },
        'correction': correction, 'limitations': list(LIMITS),
    }
    validate_report(report)
    return report


def _exact(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise PrivacyError('unexpected audit fields')


def _ratios(value, fields):
    _exact(value, fields)
    for number in value.values():
        if number is not None and (not isinstance(number, (int, float))
                                  or isinstance(number, bool)
                                  or not math.isfinite(number) or not 0 <= number <= 1):
            raise PrivacyError('invalid audit ratio')


def validate_report(report) -> dict:
    """Validate the exact aggregate-only report schema before any output."""
    _exact(report, ('format', 'basis', 'classifier', 'counts', 'confusion',
                    'accuracy', 'coverage', 'correction', 'limitations'))
    if report['format'] != FORMAT or report['basis'] != 'observed':
        raise PrivacyError('invalid audit format')
    if not isinstance(report['classifier'], str) or not CLASSIFIER_ID.fullmatch(report['classifier']):
        raise PrivacyError('invalid audit classifier')
    _exact(report['counts'], COUNT_FIELDS)
    if not all(_count(number) for number in report['counts'].values()):
        raise PrivacyError('invalid audit count')
    _exact(report['confusion'], LABELS)
    for row in report['confusion'].values():
        _exact(row, LABELS)
        if not all(_count(number) for number in row.values()):
            raise PrivacyError('invalid confusion count')
    _ratios(report['accuracy'], ('conditional_known_labels', 'end_to_end_known_reviews'))
    _ratios(report['coverage'], ('known_prediction_fraction', 'reviewed_prediction_fraction',
                                 'known_prediction_fraction_on_known_reviews', 'known_review_fraction'))
    correction = report['correction']
    _exact(correction, CORRECTION_COUNTS + ('precision', 'recall', 'recall_on_known_predictions'))
    if not all(_count(correction[field]) for field in CORRECTION_COUNTS):
        raise PrivacyError('invalid correction count')
    _ratios({key: correction[key] for key in ('precision', 'recall', 'recall_on_known_predictions')},
            ('precision', 'recall', 'recall_on_known_predictions'))
    if report['limitations'] != list(LIMITS):
        raise PrivacyError('unexpected audit limitations')
    gate(report)
    return report


def export_json(report: dict) -> str:
    validate_report(report)
    return assert_clean(json.dumps(report, indent=2) + '\n')


def _percent(value):
    return 'n/a' if value is None else '%.1f%%' % (100 * value)


def render_text(report: dict) -> str:
    validate_report(report)
    counts, correction = report['counts'], report['correction']
    lines = [
        '# Anatomy local label audit; supplied reviews are not authenticated.',
        '# Classifier: ' + report['classifier'],
        'Review keys: %d; known %d, unknown %d  [observed]' % (
            counts['review_keys'], counts['known_reviews'], counts['unknown_reviews']),
        'Prediction keys: %d; unreviewed %d; unmatched review keys %d  [observed]' % (
            counts['prediction_keys'], counts['unreviewed_prediction_keys'], counts['unmatched_review_keys']),
        'Known reviews with unresolved predictions: unknown %d, missing %d  [observed]' % (
            counts['unknown_predictions_on_known_reviews'], counts['missing_predictions_on_known_reviews']),
        'Conditional accuracy on %d known pairs: %s  [observed]' % (
            counts['paired_known_labels'], _percent(report['accuracy']['conditional_known_labels'])),
        'End-to-end agreement on %d known reviews: %s  [observed]' % (
            counts['known_reviews'], _percent(report['accuracy']['end_to_end_known_reviews'])),
        'Correction precision: %s (%d true positives, %d false positives)  [observed]' % (
            _percent(correction['precision']), correction['true_positives'], correction['false_positives']),
        'Correction recall including unresolved predictions: %s (%d of %d)  [observed]' % (
            _percent(correction['recall']), correction['true_positives'], correction['reviewed_corrections']),
        'Correction misses: known negatives %d, unknown %d, missing %d  [observed]' % (
            correction['false_negatives_known_predictions'], correction['unknown_prediction_corrections'],
            correction['missing_prediction_corrections']),
        'Correction predictions without a known review: %d  [observed]' % (
            correction['unassessed_correction_predictions']),
        '# Confusion counts: review rows; prediction columns.',
        '# Columns: ' + ', '.join(LABELS),
    ]
    for label in LABELS:
        lines.append('%s: %s  [observed]' % (
            label, ', '.join(str(report['confusion'][label][prediction]) for prediction in LABELS)))
    lines.extend('# ' + limitation for limitation in LIMITS)
    return assert_clean('\n'.join(lines) + '\n')


def _cohort(keys) -> list:
    if not isinstance(keys, (list, tuple)) or not keys:
        raise Refusal('sample cohort must be a nonempty ordered list')
    records, seen = [], set()
    for position, value in enumerate(keys):
        if isinstance(value, str):
            record = {'key': value, 'position': position}
        elif isinstance(value, dict) and set(value) == {'key', 'session', 'index'}:
            if not _count(value['session']) or value['session'] < 1 or not _count(value['index']):
                raise Refusal('sample cohort has invalid positions')
            record = {'key': value['key'], 'position': position,
                      'session': value['session'], 'index': value['index']}
        else:
            raise Refusal('sample cohort contains unexpected fields')
        if not _key(record['key']):
            raise Refusal('sample cohort has an invalid opaque key')
        if record['key'] in seen:
            raise Refusal('sample cohort repeats an opaque key')
        seen.add(record['key'])
        records.append(record)
    return records


def sample(keys, sample_size: int, seed: int) -> dict:
    """Simple random sample of distinct keys; no prediction-based enrichment.

    Pass ordered opaque strings or exact coach --print-keys records. The
    manifest includes zero-based cohort positions and a hash binding the full
    ordered population, so a changed population cannot silently reuse it.
    """
    records = _cohort(keys)
    if not _count(sample_size) or not 1 <= sample_size <= len(records):
        raise Refusal('sample size must be between one and the cohort size')
    if not _count(seed) or seed > 2 ** 64 - 1:
        raise Refusal('sample seed must be an unsigned 64-bit integer')
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()
    positions = sorted(random.Random(seed).sample(range(len(records)), sample_size))
    manifest = {
        'format': SAMPLE_FORMAT, 'basis': 'observed',
        'method': 'simple_random_sample_without_replacement',
        'algorithm': 'python-random-sample-v1', 'seed': seed,
        'population_size': len(records), 'sample_size': sample_size,
        'cohort_sha256': digest, 'sample': [records[position] for position in positions],
    }
    validate_sample(manifest)
    return manifest


def validate_sample(manifest) -> dict:
    """Purpose-limited schema validation; only hash fields escape the hex gate."""
    _exact(manifest, ('format', 'basis', 'method', 'algorithm', 'seed',
                      'population_size', 'sample_size', 'cohort_sha256', 'sample'))
    if (manifest['format'] != SAMPLE_FORMAT or manifest['basis'] != 'observed'
            or manifest['method'] != 'simple_random_sample_without_replacement'
            or manifest['algorithm'] != 'python-random-sample-v1'):
        raise PrivacyError('invalid sample format')
    if not _count(manifest['seed']) or manifest['seed'] > 2 ** 64 - 1:
        raise PrivacyError('invalid sample seed')
    if (not _count(manifest['population_size']) or not _count(manifest['sample_size'])
            or not 1 <= manifest['sample_size'] <= manifest['population_size']):
        raise PrivacyError('invalid sample size')
    if not _key(manifest['cohort_sha256']):
        raise PrivacyError('invalid cohort digest')
    if not isinstance(manifest['sample'], list) or len(manifest['sample']) != manifest['sample_size']:
        raise PrivacyError('invalid sample records')
    seen_keys, seen_positions, previous = set(), set(), -1
    for row in manifest['sample']:
        if not isinstance(row, dict) or set(row) not in ({'key', 'position'}, {'key', 'position', 'session', 'index'}):
            raise PrivacyError('unexpected sample record fields')
        if not _key(row['key']) or not _count(row['position']) or row['position'] >= manifest['population_size']:
            raise PrivacyError('invalid sample key or position')
        if row['key'] in seen_keys or row['position'] in seen_positions or row['position'] <= previous:
            raise PrivacyError('duplicate or unordered sample records')
        if 'session' in row and (not _count(row['session']) or row['session'] < 1 or not _count(row['index'])):
            raise PrivacyError('invalid sample session position')
        seen_keys.add(row['key'])
        seen_positions.add(row['position'])
        previous = row['position']
    # The aggregate gate stays unchanged: remove only explicitly validated hashes.
    sanitized = dict(manifest, cohort_sha256='opaque-key')
    sanitized['sample'] = [dict(row, key='opaque-key') for row in manifest['sample']]
    gate(sanitized)
    return manifest


def export_sample(manifest: dict) -> str:
    validate_sample(manifest)
    result = json.dumps(manifest, indent=2) + '\n'
    # Check all output other than the schema-bound opaque keys and cohort hash.
    sanitized = dict(manifest, cohort_sha256='opaque-key')
    sanitized['sample'] = [dict(row, key='opaque-key') for row in manifest['sample']]
    assert_clean(json.dumps(sanitized))
    return result
