"""Real denominators and privacy boundaries of local supplied-label audits."""
import copy
import hashlib
import json
import os
import tempfile
import unittest

from tests import helpers  # adds src to path
from anatomy import label_audit as audit
from anatomy.coach import Refusal, LABELS
from anatomy.privacy import PrivacyError, assert_clean


def key(number):
    return hashlib.sha256(('synthetic-%d' % number).encode()).hexdigest()


class ReviewFiles(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'reviews.jsonl')
            with open(path, 'wb') as handle:
                handle.write(text.encode() if isinstance(text, str) else text)
            return audit.load_reviews(path)

    def rows(self, rows):
        return self.load('\n'.join(json.dumps(row) for row in rows))

    def test_all_six_labels_and_unknown_preserved(self):
        rows = [{'key': key(i), 'label': label} for i, label in enumerate(audit.ALL_LABELS)]
        self.assertEqual(self.rows(rows), {row['key']: row['label'] for row in rows})

    def test_identical_duplicates_collapse(self):
        row = {'key': key(1), 'label': 'correction'}
        self.assertEqual(self.rows([row, row]), {key(1): 'correction'})

    def test_conflicting_duplicates_refused_including_unknown(self):
        for label in ('new', 'unknown'):
            with self.subTest(label=label), self.assertRaisesRegex(Refusal, 'conflicts'):
                self.rows([{'key': key(1), 'label': 'correction'}, {'key': key(1), 'label': label}])

    def test_duplicate_json_fields_refused(self):
        text = '{"key":"%s","label":"new","label":"correction"}' % key(1)
        with self.assertRaisesRegex(Refusal, 'strict JSON'):
            self.load(text)

    def test_raw_fields_and_missing_fields_refused(self):
        for row in ({'key': key(1), 'label': 'new', 'text': 'PRIVATE'},
                    {'key': key(1)}, {'label': 'new'}, [], 'PRIVATE'):
            with self.subTest(row=row), self.assertRaises(Refusal) as caught:
                self.rows([row])
            self.assertNotIn('PRIVATE', str(caught.exception))

    def test_bad_key_label_and_unhashable_label_refused(self):
        for row in ({'key': '/Users/private', 'label': 'new'},
                    {'key': key(1) + '\n', 'label': 'new'},
                    {'key': key(1).upper(), 'label': 'new'},
                    {'key': key(1), 'label': 'failure_report'},
                    {'key': key(1), 'label': ['new']},
                    {'key': key(1), 'label': None}):
            with self.subTest(row=row), self.assertRaises(Refusal):
                self.rows([row])

    def test_empty_whitespace_malformed_and_utf8_refused(self):
        for text in ('', '\n \n', '{PRIVATE', b'\xff'):
            with self.subTest(text=text), self.assertRaises(Refusal) as caught:
                self.load(text)
            self.assertNotIn('PRIVATE', str(caught.exception))
            self.assertNotIn('/Users/', str(caught.exception))

    def test_no_io_or_classifier_called_during_audit(self):
        # The model boundary is absent: these opaque dictionaries suffice.
        report = audit.evaluate({key(1): 'correction'}, {key(1): 'correction'})
        self.assertEqual(report['correction']['recall'], 1)


class AuditMetrics(unittest.TestCase):
    def test_confusion_matrix_orientation_and_exact_counts(self):
        predictions = {key(1): 'correction', key(2): 'correction', key(3): 'new',
                       key(4): 'unknown', key(6): 'bug_report', key(7): 'correction',
                       key(8): 'correction', key(9): 'unknown'}
        reviews = {key(1): 'correction', key(2): 'new', key(3): 'correction',
                   key(4): 'correction', key(5): 'correction', key(6): 'bug_report',
                   key(7): 'unknown', key(10): 'unknown'}
        result = audit.evaluate(predictions, reviews, 'test-model:prompt-v1')
        counts = result['counts']
        self.assertEqual(counts, {
            'prediction_keys': 8, 'review_keys': 8, 'matched_keys': 6,
            'unreviewed_prediction_keys': 2, 'unmatched_review_keys': 2,
            'known_predictions': 6, 'unknown_predictions': 2,
            'known_reviews': 6, 'unknown_reviews': 2,
            'matched_known_reviews': 5, 'paired_known_labels': 4,
            'unknown_predictions_on_known_reviews': 1,
            'missing_predictions_on_known_reviews': 1, 'correct_known_pairs': 2,
        })
        self.assertEqual(result['confusion']['correction']['new'], 1)
        self.assertEqual(result['confusion']['new']['correction'], 1)
        self.assertEqual(sum(sum(row.values()) for row in result['confusion'].values()), 4)
        self.assertEqual(result['accuracy']['conditional_known_labels'], 1 / 2)
        self.assertEqual(result['accuracy']['end_to_end_known_reviews'], 1 / 3)
        self.assertEqual(result['coverage']['known_prediction_fraction_on_known_reviews'], 2 / 3)
        correction = result['correction']
        self.assertEqual(correction, {
            'true_positives': 1, 'false_positives': 1, 'false_negatives': 3,
            'false_negatives_known_predictions': 1, 'unknown_prediction_corrections': 1,
            'missing_prediction_corrections': 1, 'reviewed_corrections': 4,
            'unassessed_correction_predictions': 2, 'precision': 1 / 2,
            'recall': 1 / 4, 'recall_on_known_predictions': 1 / 2,
        })

    def test_unknown_prediction_cannot_inflate_recall_or_agreement(self):
        result = audit.evaluate({key(1): 'correction', key(2): 'unknown'},
                                {key(1): 'correction', key(2): 'correction'})
        self.assertEqual(result['accuracy']['conditional_known_labels'], 1)
        self.assertEqual(result['accuracy']['end_to_end_known_reviews'], 1 / 2)
        self.assertEqual(result['correction']['recall'], 1 / 2)
        self.assertEqual(result['correction']['false_negatives'], 1)

    def test_missing_predictions_are_misses_not_dropped_reviews(self):
        result = audit.evaluate({}, {key(1): 'correction', key(2): 'new'})
        self.assertIsNone(result['accuracy']['conditional_known_labels'])
        self.assertEqual(result['accuracy']['end_to_end_known_reviews'], 0)
        self.assertEqual(result['correction']['recall'], 0)
        self.assertIsNone(result['correction']['precision'])
        self.assertEqual(result['correction']['missing_prediction_corrections'], 1)

    def test_only_unknown_reviews_have_no_accuracy_or_precision(self):
        result = audit.evaluate({key(1): 'correction'}, {key(1): 'unknown'})
        self.assertEqual(result['counts']['unknown_reviews'], 1)
        self.assertIsNone(result['accuracy']['conditional_known_labels'])
        self.assertIsNone(result['accuracy']['end_to_end_known_reviews'])
        self.assertIsNone(result['correction']['recall'])
        self.assertIsNone(result['correction']['precision'])
        self.assertEqual(result['correction']['unassessed_correction_predictions'], 1)

    def test_no_positive_predictions_precision_null_recall_zero(self):
        result = audit.evaluate({key(1): 'new'}, {key(1): 'correction'})
        self.assertIsNone(result['correction']['precision'])
        self.assertEqual(result['correction']['recall'], 0)

    def test_no_positive_reviews_recall_null(self):
        result = audit.evaluate({key(1): 'correction'}, {key(1): 'new'})
        self.assertEqual(result['correction']['precision'], 0)
        self.assertIsNone(result['correction']['recall'])

    def test_all_known_classes_are_present_in_confusion(self):
        labels = {key(i): label for i, label in enumerate(LABELS)}
        result = audit.evaluate(labels, labels)
        self.assertEqual(set(result['confusion']), set(LABELS))
        for label in LABELS:
            self.assertEqual(result['confusion'][label][label], 1)
            self.assertEqual(sum(result['confusion'][label].values()), 1)

    def test_inputs_and_classifier_validated_without_private_error_values(self):
        bad = '/Users/private/file'
        for labels, reviews, classifier in (({bad: 'new'}, {key(1): 'new'}, 'labels-file'),
                                            ({key(1): []}, {key(1): 'new'}, 'labels-file'),
                                            ({key(1): 'new'}, {}, 'labels-file'),
                                            ({key(1): 'new'}, {key(1): 'new'}, bad),
                                            ({key(1): 'new'}, {key(1): 'new'}, 'labels-file\n')):
            with self.subTest(classifier=classifier), self.assertRaises(Refusal) as caught:
                audit.evaluate(labels, reviews, classifier)
            self.assertNotIn(bad, str(caught.exception))
        with self.assertRaises(PrivacyError):
            audit.evaluate({key(1): 'new'}, {key(1): 'new'}, 'sk-abcdefghijklm')

    def test_aggregate_outputs_have_no_keys_or_text_and_keep_limitations(self):
        result = audit.evaluate({key(1): 'correction'}, {key(1): 'correction'})
        for rendered in (audit.export_json(result), audit.render_text(result)):
            self.assertNotIn(key(1), rendered)
            self.assertIn('not verified', rendered)
            assert_clean(rendered)
        self.assertIn('including unresolved', audit.render_text(result))
        self.assertEqual(json.loads(audit.export_json(result)), result)

    def test_mutated_report_schema_and_private_values_refused(self):
        base = audit.evaluate({key(1): 'new'}, {key(1): 'new'})
        for field, value in (('extra', 'PRIVATE'), ('classifier', '/Users/private'),
                             ('limitations', ['PRIVATE']), ('counts', {'private': key(1)}),
                             ('accuracy', {'conditional_known_labels': float('nan'),
                                           'end_to_end_known_reviews': 1})):
            result = copy.deepcopy(base)
            result[field] = value
            with self.subTest(field=field), self.assertRaises(PrivacyError) as caught:
                audit.export_json(result)
            self.assertNotIn('PRIVATE', str(caught.exception))


class Sampling(unittest.TestCase):
    def test_same_cohort_seed_size_has_identical_manifest(self):
        cohort = [key(i) for i in range(50)]
        first = audit.sample(cohort, 10, 20261001)
        self.assertEqual(first, audit.sample(cohort, 10, 20261001))
        self.assertNotEqual(first['sample'], audit.sample(cohort, 10, 20261002)['sample'])
        self.assertEqual(first['population_size'], 50)
        self.assertEqual(first['sample_size'], 10)
        self.assertEqual(len(set(row['key'] for row in first['sample'])), 10)
        self.assertEqual([row['position'] for row in first['sample']],
                         sorted(row['position'] for row in first['sample']))
        for row in first['sample']:
            self.assertEqual(row['key'], cohort[row['position']])

    def test_cohort_order_and_values_change_binding(self):
        cohort = [key(i) for i in range(10)]
        first = audit.sample(cohort, 10, 1)
        self.assertNotEqual(first['cohort_sha256'], audit.sample(cohort[::-1], 10, 1)['cohort_sha256'])
        self.assertNotEqual(first['cohort_sha256'], audit.sample(cohort + [key(100)], 10, 1)['cohort_sha256'])

    def test_coach_positions_preserved_without_labels(self):
        cohort = [{'key': key(i), 'session': i // 2 + 1, 'index': i % 2} for i in range(6)]
        manifest = audit.sample(cohort, 6, 1)
        for position, row in enumerate(manifest['sample']):
            self.assertEqual(row, dict(cohort[position], position=position))
        self.assertEqual(json.loads(audit.export_sample(manifest)), manifest)

    def test_invalid_sizes_seeds_and_empty_cohort_refused(self):
        for cohort, size, seed in (([], 1, 0), ([key(1)], 0, 0), ([key(1)], 2, 0),
                                  ([key(1)], True, 0), ([key(1)], 1, -1),
                                  ([key(1)], 1, True), ([key(1)], 1, 2 ** 64),
                                  ([key(1)], 1, 1.1)):
            with self.subTest(size=size, seed=seed), self.assertRaises(Refusal):
                audit.sample(cohort, size, seed)

    def test_duplicate_keys_and_private_extra_fields_refused(self):
        for cohort in ([key(1), key(1)], ['/Users/private'],
                       [{'key': key(1), 'session': 1, 'index': 0, 'text': 'PRIVATE'}],
                       [{'key': key(1), 'session': True, 'index': 0}],
                       [{'key': key(1), 'session': 1, 'index': -1}]):
            with self.subTest(cohort=cohort), self.assertRaises(Refusal) as caught:
                audit.sample(cohort, 1, 1)
            self.assertNotIn('PRIVATE', str(caught.exception))
            self.assertNotIn('/Users/private', str(caught.exception))

    def test_export_only_exempts_exact_schema_hash_fields(self):
        base = audit.sample([key(1), key(2)], 2, 1)
        for mutate in (lambda m: m.update(extra=key(3)),
                       lambda m: m.update(cohort_sha256='/Users/private'),
                       lambda m: m['sample'][0].update(text='PRIVATE'),
                       lambda m: m['sample'][0].update(key='sk-abcdefghijklm'),
                       lambda m: m['sample'][0].update(position=True),
                       lambda m: m['sample'].reverse(),
                       lambda m: m['sample'][1].update(key=m['sample'][0]['key']),
                       lambda m: m.update(seed=-1),
                       lambda m: m.update(sample_size=1)):
            result = copy.deepcopy(base)
            mutate(result)
            with self.assertRaises(PrivacyError) as caught:
                audit.export_sample(result)
            self.assertNotIn('PRIVATE', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
