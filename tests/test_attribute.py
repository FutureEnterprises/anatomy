"""Content carry and the rebuild taxonomy (Claude), context rent (Codex)."""
import unittest

from tests.helpers import scan_json


class ClaudeAttribution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = scan_json('--no-codex')['claude']

    def test_rebuild_taxonomy(self):
        r = self.c['cache_rebuilds']
        self.assertEqual(r['model_switch']['events'], 1)            # fallback served on another model
        self.assertEqual(r['after_refusal_fallback']['events'], 1)  # the call after a fallback rewrites
        self.assertEqual(r['ttl_expired_idle_gap']['events'], 2)    # two-hour gap; resume after three hours
        for cause in ('idle_gap_5m_segment_on_1h_thread', 'context_shrink_or_edit', 'prefix_mutation_unexplained'):
            self.assertEqual(r[cause]['events'], 0, cause)

    def test_idle_gap_rebuild_excess(self):
        # rewrite of the old prefix priced at (1h write - read): main call 6 on the opus-5 test prices,
        # resumed call B1 on the opus-5-5 test prices
        main = (23600 - 195) * (10 - 0.5) * 1e-6
        resumed = (23100 - 90) * (20 - 1) * 1e-6
        self.assertAlmostEqual(self.c['cache_rebuilds']['ttl_expired_idle_gap']['excess_usd'], main + resumed, places=6)

    def test_attribution_closes(self):
        ia = self.c['input_attribution']
        served_input = self.c['list_price_usd']['served_input']
        self.assertAlmostEqual(ia['attributed_usd'] + ia['unattributed_usd'], served_input, places=4)
        self.assertLess(abs(ia['unattributed_usd']), 0.05 * served_input)
        shares = sum(v['share'] for v in ia['by_group'].values())
        self.assertAlmostEqual(shares, 1.0, places=4)
        groups = set(ia['by_group'])
        for g in ('boot_prefix', 'tool_result', 'assistant:thinking', 'cache_rebuild:ttl_expired_idle_gap'):
            self.assertIn(g, groups)

    def test_copied_calls_not_attributed_twice(self):
        g = scan_json('--no-codex')['claude']['list_price_usd']['served_input']
        f = scan_json('--no-codex', '--dedupe', 'file')['claude']['list_price_usd']['served_input']
        a1_a2_input = (10 * 10 + 20000 * 20) * 1e-6 + (5 * 10 + 3000 * 12.5 + 20010 * 1) * 1e-6
        self.assertAlmostEqual(f - g, a1_a2_input, places=6)


class CodexRent(unittest.TestCase):
    def test_rent(self):
        cr = scan_json('--no-claude')['codex']['context_rent']
        self.assertEqual(cr['input_token_calls'], 1000 + 2200 + 2300 + 800 + 300000)
        # the shell read's output (4,019 chars at 4 chars/token) stays resident for 2 calls of window 0
        read = cr['tool_output_by_kind']['read']['token_calls_share'] * cr['input_token_calls']
        self.assertAlmostEqual(read, 4019 / 4 * 2, places=0)
        by = cr['by_group']
        self.assertAlmostEqual(by['prefix_first_window']['token_calls_share'] * cr['input_token_calls'], 1000 * 3, places=0)
        self.assertAlmostEqual(by['post_compaction_baseline']['token_calls_share'] * cr['input_token_calls'], 800 * 2, places=0)
        self.assertIn('fn:write_stdin', cr['tool_output_by_kind'])
        self.assertIn('exec~poll-process-stdin', cr['tool_output_by_kind'])


if __name__ == '__main__':
    unittest.main()
