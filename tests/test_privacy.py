"""The output gate: only aggregates leave Anatomy, whatever the transcripts contain."""
import json
import unittest
from unittest import mock

from tests.fixtures.make_fixtures import PLANTED
from tests.helpers import CLAUDE_DIR, CODEX_DIR, FIX, run_cli
from anatomy import cli
from anatomy.privacy import PrivacyError, assert_clean, gate, label, model_label


class OutputNeverLeaks(unittest.TestCase):
    def assert_no_planted(self, text):
        for s in PLANTED + [FIX, CLAUDE_DIR, CODEX_DIR, 'fixture-session', 'call_fixture']:
            self.assertNotIn(s, text)

    def test_text_output(self):
        code, out, err = run_cli()
        self.assertEqual(code, 0, err)
        self.assertIn('CLAUDE CODE', out)
        self.assert_no_planted(out + err)

    def test_json_output(self):
        code, out, err = run_cli('--json')
        self.assertEqual(code, 0, err)
        self.assert_no_planted(out + err)
        rep = json.loads(out)
        # MCP servers collapse to one bucket, odd attachment types to 'other'
        self.assertNotIn('acme', json.dumps(rep))

    def test_gate_withholds_output(self):
        leaky = {'corpus': {'basis': 'observed', 'alice@example.com': 1}}
        with mock.patch.object(cli, 'claude_report', return_value=leaky):
            code, out, err = run_cli('--no-codex')
        self.assertEqual(code, 3)
        self.assertEqual(out, '')
        self.assertIn('privacy gate', err)
        self.assertNotIn('alice', err)


class Gate(unittest.TestCase):
    def test_accepts_aggregates(self):
        gate({'claude': {'calls': 3, 'list_price_usd': {'basis': 'observed', 'unit': 'USD API list-price equivalent', 'total': 1.5},
                         'calls_by_model': {'claude-opus-5-5': 2, 'claude-haiku-4-5-20251001': 1}},
              'prices': {'source_url': 'https://platform.claude.com/docs/en/about-claude/pricing'}})

    def test_rejects(self):
        bad = [
            {'x': '/Users/alice/acme/file.txt'},
            {'alice@example.com': 1},
            {'k': 'sk-ant-api03-abcdefghijklmnop'},
            {'k': 'ghp_abcdefghijklmnopqrstuvwxyz0123'},
            {'k': 'AKIAABCDEFGHIJKLMNOP'},
            {'k': 'https://example.com/private'},
            {'key with/slash': 1},
            {'k': 'a' * 90},
            {'k': 'deadbeef' * 4},
            {'k': float('nan')},
            {'k': 'multi\nline'},
            {'k': object()},
        ]
        for b in bad:
            with self.assertRaises(PrivacyError, msg=repr(b)[:40]):
                gate(b)

    def test_error_never_echoes_value(self):
        try:
            gate({'k': 'alice@example.com'})
        except PrivacyError as e:
            self.assertNotIn('alice', str(e))

    def test_assert_clean(self):
        assert_clean('total $1.00 [observed]\nhttps://developers.openai.com/api/docs/pricing\n')
        for t in ('mail bob@example.org', 'see /home/bob/x', 'token ghp_abcdefghijklmnopqrstuvwxyz0123'):
            with self.assertRaises(PrivacyError):
                assert_clean(t)

    def test_label_sanitizers(self):
        self.assertEqual(label('task_reminder'), 'task_reminder')
        self.assertEqual(label('Secret Project Plan'), 'other')
        self.assertEqual(label('../../etc'), 'other')
        self.assertEqual(label(None), 'other')
        self.assertEqual(model_label('claude-opus-5-5'), 'claude-opus-5-5')
        self.assertEqual(model_label('gpt-5.6-sol'), 'gpt-5.6-sol')
        self.assertEqual(model_label('my-private-finetune'), 'other-model')
        self.assertEqual(model_label('claude-' + 'x' * 80), 'other-model')
        self.assertEqual(model_label(None), 'unknown')


if __name__ == '__main__':
    unittest.main()
