"""Command integration: local defaults, coherent exports and no private leakage."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from anatomy import cli, coach
from tests.helpers import CLAUDE_DIR, ANTHROPIC_PRICES
from tests.fixtures.make_fixtures import PLANTED


def invoke(command, *args):
    output, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        status = cli.main([command, *args])
    return status, output.getvalue(), errors.getvalue()


def index(*args):
    return invoke('correction-index', '--claude-dir', CLAUDE_DIR,
                  '--anthropic-prices', ANTHROPIC_PRICES, *args)


class IndexCommands(unittest.TestCase):
    def assert_private(self, output):
        for value in PLANTED + [CLAUDE_DIR, 'fixture-session', 'call_fixture']:
            self.assertNotIn(value, output)

    def test_default_unknown_report_does_not_invoke_classifier(self):
        with mock.patch.object(coach, 'classify', side_effect=AssertionError('must stay local')):
            code, text, error = index('--json')
        self.assertEqual(code, 0, error)
        report = json.loads(text)
        self.assertEqual(report['classifier'], 'unlabeled')
        self.assert_private(text + error)
        code, text, error = index()
        self.assertEqual(code, 0, error)
        self.assertIn('Correction share of known priced served dollars: not available', text)
        self.assertNotIn('Correction frequency among known labels: 0.0%', text)

    def test_keys_are_unique_and_coach_compatible(self):
        code, text, error = index('--print-keys')
        self.assertEqual(code, 0, error)
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertTrue(rows)
        self.assertEqual(len(rows), len({row['key'] for row in rows}))
        for row in rows:
            self.assertEqual(set(row), {'key', 'session', 'index'})
            self.assertIsNotNone(coach.KEY.fullmatch(row['key']))
        sessions, _ = coach.collect(CLAUDE_DIR)
        expected = {p['key'] for session in sessions for p in session['prompts']}
        self.assertTrue({row['key'] for row in rows} <= expected)
        self.assert_private(text + error)

    def test_sample_is_reproducible_and_contains_no_text(self):
        first = index('--sample', '2', '--seed', '19')
        self.assertEqual(first, index('--sample', '2', '--seed', '19'))
        self.assertEqual(first[0], 0, first[2])
        self.assertEqual(json.loads(first[1])['sample_size'], 2)
        self.assert_private(first[1] + first[2])

    def test_partial_labels_keep_unknown_spend_visible_in_summary(self):
        _, keys, _ = index('--print-keys')
        row = json.loads(keys.splitlines()[0])
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'labels.jsonl')
            with open(path, 'w') as output:
                output.write(json.dumps({'key': row['key'], 'label': 'correction'}) + '\n')
            code, text, error = index('--labels', path)
            self.assertEqual(code, 0, error)
            self.assertIn('unknown-label calls included', text)
            self.assertIn('Known-label share of priced served dollars:', text)
            self.assertIn('remaining ownership or labels unresolved', text)
            self.assert_private(text + error)

    def test_labels_and_reviews_attach_local_audit(self):
        _, keys, _ = index('--print-keys')
        rows = [json.loads(line) for line in keys.splitlines()]
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'labels.jsonl')
            with open(path, 'w') as output:
                for i, row in enumerate(rows):
                    output.write(json.dumps({'key': row['key'], 'label': 'correction' if i % 2 else 'new'}) + '\n')
            code, text, error = index('--labels', path, '--reviews', path, '--json')
            self.assertEqual(code, 0, error)
            report = json.loads(text)
            self.assertEqual(report['label_audit']['accuracy']['end_to_end_known_reviews'], 1.0)
            self.assert_private(text + error)
            # Equal files are a synthetic pipeline check, never evidence of an
            # independent human review; the result retains that limitation.
            self.assertIn('not verified', report['label_audit']['limitations'][0])

    def test_saved_report_is_private_and_exclusive(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'report.json')
            code, text, error = index('--out', path, '--json')
            self.assertEqual(code, 0, error)
            with open(path) as saved:
                self.assertEqual(saved.read(), text)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            second, new_text, new_error = index('--out', path)
            self.assertEqual(second, 2)
            self.assertEqual(new_text, '')
            self.assertNotIn(path, new_error)
            with open(path) as saved:
                self.assertEqual(saved.read(), text)

    def test_invalid_options_and_empty_corpus_refuse(self):
        for args in [('--sample', '0'), ('--until', 'bad'), ('--classifier-id', 'x'), ('--reviews', 'missing')]:
            code, text, error = index(*args)
            self.assertEqual(code, 2, (args, error))
            self.assertEqual(text, '')
        with tempfile.TemporaryDirectory() as root:
            code, text, error = invoke('correction-index', '--claude-dir', root)
            self.assertEqual(code, 2)
            self.assertEqual(text, '')

    def test_audit_recall_keeps_unresolved_predictions(self):
        with tempfile.TemporaryDirectory() as root:
            labels, reviews = [os.path.join(root, name) for name in ('labels.jsonl', 'reviews.jsonl')]
            with open(labels, 'w') as output:
                output.write(json.dumps({'key': 'a' * 64, 'label': 'unknown'}) + '\n')
            with open(reviews, 'w') as output:
                output.write(json.dumps({'key': 'a' * 64, 'label': 'correction'}) + '\n')
            code, text, error = invoke('correction-audit', '--labels', labels, '--reviews', reviews, '--json')
            self.assertEqual(code, 0, error)
            report = json.loads(text)
            self.assertEqual(report['correction']['recall'], 0.0)
            self.assertIsNone(report['accuracy']['conditional_known_labels'])
            self.assertNotIn(root, text + error)


if __name__ == '__main__':
    unittest.main()
