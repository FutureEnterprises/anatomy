"""The break-even rule, the ratio table and the eviction replay."""
import collections
import unittest

from tests.synth import AP, M, OP, thread, two_batch_thread, call, tool_result, prompt
from anatomy import breakeven as be
from anatomy.ledger import new_agg
from anatomy.prices import AnthropicPrices, OpenAIPrices


class Rule(unittest.TestCase):
    def test_ratio_and_payback(self):
        self.assertAlmostEqual(be.ratio(12.5, 1.0), 11.5)
        self.assertAlmostEqual(be.payback_calls(1000, 1000, 12.5, 1.0), 11.5)
        self.assertAlmostEqual(be.payback_calls(2000, 1000, 12.5, 1.0), 5.75)

    def test_pays_only_above_the_line(self):
        # b x L x r against S x (w - r)
        self.assertTrue(be.pays(1000, 12, 1000, 12.5, 1.0))
        self.assertFalse(be.pays(1000, 11, 1000, 12.5, 1.0))
        self.assertFalse(be.pays(1000, 12, 1000, 12.5, 1.0, refetch_usd=1000))
        self.assertAlmostEqual(be.margin(1000, 12, 1000, 12.5, 1.0), 12000 - 11500)

    def test_ratios_from_the_published_snapshots(self):
        c = be.claude_ratios(AnthropicPrices())
        self.assertEqual(c['claude-opus-5-5'], {'5m': 24.0, '1h': 39.0})
        self.assertEqual(c['claude-fable-5-1'], {'5m': 49.0, '1h': 79.0})
        self.assertEqual(c['claude-sonnet-5'], {'5m': 11.5, '1h': 19.0})
        o = be.openai_ratios(OpenAIPrices())
        self.assertEqual(o['gpt-5.6-sol'], {'rewrite_as_input': 9.0, 'rewrite_as_cache_write': 11.5})
        self.assertIsNone(o['gpt-5.5']['rewrite_as_cache_write'])
        rs = be.ratio_set(AnthropicPrices(), OpenAIPrices())
        for R in (9.0, 11.5, 19.0, 24.0, 39.0, 49.0, 79.0):
            self.assertIn(R, rs)


class Replay(unittest.TestCase):
    def test_lease_batches_and_suffix_correction(self):
        tl = be.claude_timeline(two_batch_thread(), AP)
        self.assertEqual(tl.ctx[4], 53_000)
        got = be.replay(tl, be.Lease())
        self.assertEqual(len(got), 2)
        (b1, S1, L1, RL1, w1, r1), (b2, S2, L2, RL2, w2, r2) = got
        self.assertAlmostEqual(b1, 40_000, delta=1)
        self.assertAlmostEqual(S1, 3_000, delta=1)       # 53K - 10K boot - 40K evicted
        self.assertEqual(L1, 5)                          # calls 5..9
        self.assertAlmostEqual(RL1, 5 * 1.0 * M)
        self.assertAlmostEqual(w1, 12.5 * M)
        self.assertAlmostEqual(r1, 1.0 * M)
        # the second suffix excludes the first batch, already gone in the counterfactual (43K without the fix)
        self.assertAlmostEqual(S2, 3_000, delta=1)
        self.assertEqual(L2, 1)

    def test_copied_calls_are_replayed_but_not_scored(self):
        tl = be.claude_timeline(two_batch_thread(copied_upto=5), AP)
        got = be.replay(tl, be.Lease())
        self.assertEqual(len(got), 1)                   # the first rewrite lands on copied call 5
        self.assertAlmostEqual(got[0][1], 3_000, delta=1)

    def test_clearing_keeps_recent_and_waits_for_trigger(self):
        tl = be.claude_timeline(two_batch_thread(), AP)
        got = be.replay(tl, be.Clearing(trigger=90_000, keep=1, at_least=20_000))
        self.assertEqual(len(got), 1)
        b, S, L, *_ = got[0]
        self.assertAlmostEqual(b, 40_000, delta=1)
        self.assertAlmostEqual(S, 43_000, delta=1)       # 3K of prompts and the kept 40K result
        self.assertEqual(L, 4)
        self.assertEqual(be.replay(tl, be.Clearing(trigger=200_000)), [])
        self.assertEqual(be.replay(tl, be.Clearing(trigger=90_000, keep=1, at_least=50_000)), [])

    def test_compaction_kills_items(self):
        calls = [call(10_000), call(50_000, pre=[tool_result('t1', 40_000)], read=10_000)]
        calls += [call(51_000 + 1000 * i, pre=[prompt(1_000)], read=50_000 + 1000 * i) for i in range(2)]
        calls.append(call(5_000, pre=[{'kind': 'compact_marker'}, prompt(5_000)]))
        calls += [call(6_000 + 1000 * i, pre=[prompt(1_000)], read=5_000 + 1000 * i) for i in range(3)]
        tl = be.claude_timeline(thread(calls, {'t1': 'Bash'}), AP)
        self.assertEqual(be.replay(tl, be.Lease()), [])  # the result dies at the compaction before it ages
        self.assertEqual([d for (e, t, d, ev) in tl.items if ev], [4])

    def test_codex_timeline(self):
        s = {'live': [{'u': (1000, 0, 0, 10, 0), 'model': 'gpt-5.6-sol', 'window': 0},
                      {'u': (41000, 1000, 0, 10, 0), 'model': 'gpt-5.6-sol', 'window': 0},
                      {'u': (42000, 41000, 0, 10, 0), 'model': 'gpt-5.6-sol', 'window': 0},
                      {'u': (900, 0, 0, 10, 0), 'model': 'gpt-5.6-sol', 'window': 1}],
             'tools': [{'pl': 1, 'out_chars': 160_000, 'tool': 'exec'}], 'inj': [(2, 400, 'user')]}
        tl = be.codex_timeline(s, OP)
        ev = [(e, t, d) for (e, t, d, x) in tl.items if x]
        self.assertEqual(ev, [(1, 40_000.0, 3)])
        self.assertAlmostEqual(tl.write[1], 4.0 * M)     # a rewrite bills as uncached input
        self.assertAlmostEqual(tl.read[1], 0.4 * M)


class Share(unittest.TestCase):
    def test_share_paying_at_user_prices_and_by_ratio(self):
        agg = new_agg()
        batches = be.replay(be.claude_timeline(two_batch_thread(), AP), be.Lease())
        be.record(agg, 'k', batches, (11.5, 19.0))
        sh = be.share_paying(agg, 'k', (11.5, 19.0))
        self.assertEqual(sh['batches'], 2)
        self.assertEqual(sh['share_batches_that_pay'], 1.0)
        # L x b / S is 66.7 for the first batch and 13.3 for the second
        self.assertEqual(sh['share_batches_that_pay_by_ratio'], {'R11.5': 1.0, 'R19': 0.5})
        m1 = 40_000 * 5 * M - 3_000 * 11.5 * M
        m2 = 40_000 * 1 * M - 3_000 * 11.5 * M
        self.assertAlmostEqual(sh['net_usd'], m1 + m2, places=4)
        # every re-fetch on the next call: the savings are lost and the tokens are written again
        unit = 40_000 * (5 * M + 12.5 * M) + 40_000 * (1 * M + 12.5 * M)
        self.assertAlmostEqual(sh['breakeven_refetch_rate'], round((m1 + m2) / unit, 4), places=4)

    def test_refetch_cost_lowers_the_net(self):
        agg = new_agg()
        batches = be.replay(be.claude_timeline(two_batch_thread(), AP), be.Lease())
        be.record(agg, 'k', batches, (), refetch_rate=0.5)
        sh = be.share_paying(agg, 'k')
        self.assertAlmostEqual(sh['refetch_usd'], 0.5 * (40_000 * 17.5 * M + 40_000 * 13.5 * M), places=4)
        self.assertEqual(sh['share_batches_that_pay'], 0.0)

    def test_empty(self):
        sh = be.share_paying(collections.defaultdict(collections.Counter), 'none')
        self.assertEqual(sh['batches'], 0)
        self.assertIsNone(sh['share_batches_that_pay'])
        self.assertEqual(sh['breakeven_refetch_rate'], 0.0)


if __name__ == '__main__':
    unittest.main()
