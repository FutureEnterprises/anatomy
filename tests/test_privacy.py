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


class PrivateToolNames(unittest.TestCase):
    """Tool names are printed only from a fixed allowlist of built-in tools."""
    PRIVATE = ('acme_payroll', 'sync_client_ledger', 'globex_dispatch', 'AcmePayrollSync')

    def test_labels(self):
        from anatomy.ingest.claude import tool_category
        from anatomy.ingest.codex import custom_label, fn_label
        self.assertEqual(fn_label('acme_payroll', 'sync_client_ledger'), 'fn:other')
        self.assertEqual(fn_label('', 'sync_client_ledger'), 'fn:other')
        self.assertEqual(fn_label('collaboration', 'sync_client_ledger'), 'fn:other')   # known namespace, unknown name
        self.assertEqual(fn_label('acme_payroll', 'spawn_agent'), 'fn:other')          # known name, unknown namespace
        self.assertEqual(fn_label('', 'write_stdin'), 'fn:write_stdin')
        self.assertEqual(fn_label('collaboration', 'spawn_agent'), 'fn:collaboration.spawn_agent')
        self.assertEqual(fn_label('', 'mcp__acme__read'), 'fn:mcp')
        self.assertEqual(custom_label('globex_dispatch'), 'custom-other')
        self.assertEqual(custom_label('exec'), 'exec')
        self.assertEqual(tool_category('AcmePayrollSync'), 'other')
        self.assertEqual(tool_category('Bash'), 'Bash')
        self.assertEqual(tool_category('Task'), 'Agent')

    def test_end_to_end(self):
        import os
        import tempfile
        from tests.fixtures.make_fixtures import assistant, cx, tc, usage, user, write
        from tests.helpers import ANTHROPIC_PRICES, OPENAI_PRICES
        with tempfile.TemporaryDirectory() as d:
            cdir, xdir = os.path.join(d, 'claude'), os.path.join(d, 'codex')
            write(os.path.join(cdir, 'p', 's.jsonl'), [
                user(0, 'go'),
                assistant(1, 'msg_p1', 'claude-opus-5-5', usage(3, cc5=1000, out=10),
                          [{'type': 'tool_use', 'id': 'toolu_p1', 'name': 'AcmePayrollSync', 'input': {}}]),
                user(2, [{'type': 'tool_result', 'tool_use_id': 'toolu_p1', 'content': 'x' * 400}]),
                assistant(3, 'msg_p2', 'claude-opus-5-5', usage(3, cc5=200, cr=1000, out=10), [{'type': 'text', 'text': 'ok'}])])
            write(os.path.join(xdir, 'sessions', '2026', '09', '01', 'rollout-p.jsonl'), [
                cx(0, 'session_meta', {'id': 's', 'source': 'cli'}),
                cx(1, 'turn_context', {'model': 'gpt-5.6-sol'}),
                cx(2, 'event_msg', {'type': 'task_started'}),
                cx(3, 'response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'go'}]}),
                cx(4, 'response_item', {'type': 'function_call', 'namespace': 'acme_payroll', 'name': 'sync_client_ledger',
                                        'call_id': 'c1', 'arguments': '{}'}),
                cx(4, 'response_item', {'type': 'custom_tool_call', 'name': 'globex_dispatch', 'call_id': 'c2', 'input': 'go'}),
                tc(5, 1050, 1000, 0, 50, 0),
                cx(6, 'response_item', {'type': 'function_call_output', 'call_id': 'c1', 'output': 'y' * 4000}),
                cx(6, 'response_item', {'type': 'custom_tool_call_output', 'call_id': 'c2', 'output': 'z' * 4000}),
                cx(7, 'response_item', {'type': 'reasoning', 'summary': []}),
                tc(8, 3100, 3000, 1000, 50, 0)])
            for extra in (('--json',), ()):
                argv = ['audit', '--claude-dir', cdir, '--codex-dir', xdir, '--workers', '1',
                        '--anthropic-prices', ANTHROPIC_PRICES, '--openai-prices', OPENAI_PRICES, *extra]
                import contextlib
                import io
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cli.main(argv)
                self.assertEqual(code, 0, err.getvalue())
                for name in self.PRIVATE:
                    self.assertNotIn(name, out.getvalue() + err.getvalue())
                if extra:
                    kinds = json.loads(out.getvalue())['codex']['context_rent']['tool_output_by_kind']
                    self.assertIn('fn:other', kinds)
                    self.assertIn('custom-other', kinds)


if __name__ == '__main__':
    unittest.main()
