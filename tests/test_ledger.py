"""The bill: served attempts, declined fallback attempts, both cache tiers, cross-file dedupe."""
import unittest

from tests.helpers import scan_json

M = 1e-6  # per-token factor for USD-per-MTok test prices


class ClaudeLedger(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = scan_json('--no-codex')['claude']
        cls.f = scan_json('--no-codex', '--dedupe', 'file')['claude']

    def test_calls_dedupe_by_message_id(self):
        # main: 6 calls (A1 streamed as two records counts once; synthetic and API-error records ignored),
        # resumed: A1, A2 copied + B1 new, subagent: 1
        self.assertEqual(self.g['corpus']['calls'], 8)
        self.assertEqual(self.f['corpus']['calls'], 10)
        self.assertEqual(self.g['dedupe']['calls_removed'], 2)
        self.assertEqual(self.g['dedupe']['files_with_copied_responses'], 1)

    def test_served_cost_both_tiers(self):
        a1 = (10 * 10 + 20000 * 20 + 100 * 50) * M          # 1h write
        a2 = (5 * 10 + 3000 * 12.5 + 20010 * 1 + 200 * 50) * M  # 5m write + read
        a3 = (5 * 5 + 23100 * 10 + 80 * 25) * M
        a4 = (5 * 5 + 23300 * 10 + 60 * 25) * M
        a5 = (5 * 5 + 100 * 10 + 23305 * 0.5 + 30 * 25) * M
        a6 = (5 * 5 + 23600 * 10 + 40 * 25) * M
        b1 = (5 * 10 + 23100 * 20 + 20 * 50) * M
        s1 = (3 * 10 + 1000 * 12.5 + 10 * 50) * M
        served = a1 + a2 + a3 + a4 + a5 + a6 + b1 + s1
        self.assertAlmostEqual(self.g['list_price_usd']['served'], served, places=4)
        self.assertAlmostEqual(self.f['list_price_usd']['served'], served + a1 + a2, places=4)
        tier = self.g['list_price_usd_by_tier']
        self.assertAlmostEqual(tier['cache_write_5m'], (3000 + 1000) * 12.5 * M, places=6)
        self.assertAlmostEqual(tier['cache_write_1h'], ((20000 + 23100) * 20 + (23100 + 23300 + 100 + 23600) * 10) * M, places=6)
        self.assertAlmostEqual(tier['cache_read'], (20010 * 1 + 23305 * 0.5) * M, places=6)
        toks = self.g['tokens_by_tier']
        self.assertEqual(toks['cache_write_5m'], 4000)
        self.assertEqual(toks['cache_write_1h'], 113200)
        self.assertEqual(toks['output'], 540)   # A1 counted at its final 100, not the partial 50

    def test_declined_fallback_attempts(self):
        billed = (5 * 20 + 23100 * 40 + 40 * 100) * M     # streamed 40 tokens before the decline
        maybe = (5 * 20 + 23300 * 40) * M                 # declined before any output
        s = self.g['list_price_usd']
        self.assertAlmostEqual(s['declined_billed'], billed, places=6)
        self.assertAlmostEqual(s['declined_maybe_billed'], maybe, places=6)
        self.assertAlmostEqual(s['total'], s['served'] + billed, places=4)
        fb = self.g['fallback']
        self.assertEqual((fb['calls_with_fallback'], fb['declined_attempts_billed'], fb['declined_attempts_pre_output']), (2, 1, 1))
        self.assertEqual(fb['declined_by_model'], {'claude-fable-5': 2})

    def test_until_cutoff(self):
        # before the idle-gap call (12:00:42) and the resumed session's new call (13:00:05)
        c = scan_json('--no-codex', '--until', '2026-09-01T11:00:00Z')['claude']
        self.assertEqual(c['corpus']['calls'], 6)   # A1..A5 + subagent S1; resumed copy fully deduped
        self.assertEqual(c['corpus']['files_after_until'], 0)


class CodexLedger(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.x = scan_json('--no-claude')['codex']

    def test_calls_and_dedupe_of_token_count(self):
        self.assertEqual(self.x['corpus']['calls'], 5)
        self.assertEqual(self.x['corpus']['token_count_events_raw'], 6)
        self.assertEqual(self.x['corpus']['token_count_dup_or_empty_dropped'], 1)

    def test_cost_with_long_context_and_compaction(self):
        calls = (1000 * 4 + 50 * 20) + (1200 * 4 + 1000 * 0.4 + 100 * 20) + (100 * 4 + 2200 * 0.4 + 40 * 20) \
            + (800 * 4 + 10 * 20) + (10000 * 8 + 290000 * 0.8 + 100 * 30)   # the last call is over 272K: long rates
        s = self.x['list_price_usd']
        self.assertAlmostEqual(s['token_count_calls'], calls * M, places=6)
        self.assertAlmostEqual(s['compaction_requests'], (5000 * 4 + 300 * 20) * M, places=6)
        self.assertEqual(self.x['compaction_requests']['requests'], 1)
        self.assertEqual(self.x['long_context'], {'basis': 'observed', 'calls_over_threshold': 1, 'calls_at_long_rates': 1})

    def test_polls(self):
        p = self.x['polls']
        self.assertEqual(p['calls'], 3)
        self.assertEqual(p['polls_no_input'], 3)        # no poll sent input
        self.assertEqual(p['polls_no_new_output'], 2)   # one came back with new output, so no-input is not waste

    def test_no_new_output_shapes(self):
        from anatomy.ingest.codex import no_new_output
        self.assertTrue(no_new_output('Chunk ID: 1\nWall time: 5.0 seconds\nOutput:\n'))
        self.assertTrue(no_new_output('Output:\n  \n'))
        self.assertTrue(no_new_output('ran\nOutput:\n{"chunk_id": "a", "output": "", "session_id": 7}'))
        self.assertFalse(no_new_output('ran\nOutput:\n{"chunk_id": "a", "output": "ok", "session_id": 7}'))
        self.assertFalse(no_new_output('Output:\nbuild ok\n'))
        self.assertFalse(no_new_output('Output:\n{not json'))


if __name__ == '__main__':
    unittest.main()
