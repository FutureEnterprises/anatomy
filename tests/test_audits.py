"""Each audit on synthetic threads, the audit command on the fixtures, and its labels."""
import contextlib
import io
import json
import re
import unittest
from unittest import mock

from tests.fixtures.make_fixtures import PLANTED
from tests.helpers import ANTHROPIC_PRICES, CLAUDE_DIR, CODEX_DIR, FIX, OPENAI_PRICES
from tests.synth import AP, M, OP, attachment, call, poll_session, prompt, thread, tool_result, two_batch_thread
from tests.test_labels import numeric_leaves
from anatomy import audits, breakeven as be, cli, ledger
from anatomy.audits import boot_scope, clearing, keepalive, oversized, polls
from anatomy.cli import BASES
from anatomy.ingest import codex as codex_ingest
from anatomy.ledger import new_agg
from anatomy.privacy import gate

TAG = re.compile(r'\[(%s)\]$' % '|'.join(BASES))


def run(cmd, *extra):
    argv = [cmd, '--claude-dir', CLAUDE_DIR, '--codex-dir', CODEX_DIR, '--workers', '1',
            '--anthropic-prices', ANTHROPIC_PRICES, '--openai-prices', OPENAI_PRICES, *extra]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class BootScope(unittest.TestCase):
    def setUp(self):
        pre = [prompt(1_000), attachment('skill_listing', 10_000), attachment('deferred_tools_delta', 5_000)]
        c0 = call(30_000, pre=pre)
        c1 = call(32_000, pre=[prompt(2_000)], read=30_000)
        self.th = thread([c0, c1], {'x': 'ToolSearch'})
        self.costs = [(0.0, 0.3, 0.0, 0.0, 0.01), (0.0, 0.02, 0.0, 0.03, 0.01)]

    def test_listings_and_harness(self):
        agg = new_agg()
        boot_scope.claude(self.th, 'subagent', self.costs, agg)
        boot_scope.claude(self.th, 'main', self.costs, agg)        # main threads are not agents
        rep = boot_scope.report(agg)
        a = rep['claude_agents']
        self.assertEqual(a['threads'], 1)
        self.assertEqual(a['boot_tokens'], 30_000)
        self.assertAlmostEqual(a['harness_tokens'], 14_000, delta=2)
        self.assertAlmostEqual(a['task_unique_tokens'], 3_000, delta=2)
        self.assertEqual(a['threads_boot_prefix_over_task_unique'], 1)
        skill = a['listings']['skill_listing']
        usd = 0.3 * 10_000 / 30_000 + 0.05 * 10_000 / 32_000
        self.assertAlmostEqual(skill['usd'], usd, places=4)
        self.assertEqual(skill['threads_never_used'], 1)             # no Skill call
        self.assertEqual(a['listings']['deferred_tools_delta']['threads_never_used'], 0)   # ToolSearch was used
        self.assertEqual(rep['fix']['id'], 'boot_listings_on_demand')
        self.assertAlmostEqual(rep['fix']['net_usd'], usd, places=4)
        self.assertFalse(rep['fix']['upper_bound'])

    def test_no_fix_when_listings_are_used(self):
        self.th['tool_uses']['y'] = {'cat': 'Skill', 'bash_kind': None, 'in_chars': 0, 'call': 0}
        agg = new_agg()
        boot_scope.claude(self.th, 'workflow_agent', self.costs, agg)
        self.assertIsNone(boot_scope.report(agg)['fix'])


class KeepAlive(unittest.TestCase):
    def test_short_gap_pays_long_gap_does_not(self):
        # 5m tier: read 1, write 12.5, input 10, output 50 per MTok
        calls = [call(20_000, ts=0.0), call(20_500, ts=7200.0), call(21_000, ts=7800.0)]
        agg = new_agg()
        keepalive.claude(thread(calls), 'subagent', AP, agg)
        rep = keepalive.report(agg, 11.5, 19.0)
        ping1 = 20_000 * M + 10 * 10 * M + 50 * M                 # prefix read plus the ping's own tokens
        ping2 = 20_500 * M + 10 * 10 * M + 50 * M
        ex1, ex2 = 20_000 * 11.5 * M, 20_500 * 11.5 * M
        self.assertEqual(rep['idle_gaps']['gaps_over_ttl_5m'], 2)
        self.assertEqual(rep['rebuilds_after_idle_gaps']['rebuilds_5m'], 2)
        self.assertAlmostEqual(rep['rebuilds_after_idle_gaps']['rebuild_excess_usd_5m'], ex1 + ex2, places=5)
        # the 2 h gap needs 26 pings, more than a rebuild costs; the 10 min gap needs 2
        self.assertEqual(rep['rebuilds_after_idle_gaps']['hindsight_paying_gaps_5m'], 1)
        self.assertAlmostEqual(rep['hindsight']['net_usd'], ex2 - 2 * ping2, places=5)
        cap = rep['capped_keepalive']
        self.assertAlmostEqual(cap['ping_usd'], 11 * ping1 + 2 * ping2, places=5)    # capped at 11 pings
        self.assertAlmostEqual(cap['saved_usd'], ex2, places=5)
        self.assertEqual(cap['ping_usd_after_last_call'], 0.0)                        # not a main thread
        on = rep['always_on_keepalive']
        self.assertAlmostEqual(on['ping_usd'], 26 * ping1 + 2 * ping2, places=5)
        self.assertAlmostEqual(on['net_usd'], ex1 + ex2 - 26 * ping1 - 2 * ping2, places=5)
        self.assertLess(rep['trick']['net_usd'], 0)
        self.assertIsNone(rep['fix'])                                                 # capped net is negative here

    def test_capped_fix_and_main_thread_tail(self):
        calls = [call(20_000, ts=0.0), call(20_500, ts=600.0)]
        agg = new_agg()
        keepalive.claude(thread(calls), 'subagent', AP, agg)
        rep = keepalive.report(agg, 11.5, 19.0)
        self.assertEqual(rep['fix']['id'], 'keepalive_capped')
        self.assertEqual(rep['fix']['cap_minutes_5m'], round(11 * 270 / 60))
        agg2 = new_agg()
        keepalive.claude(thread(calls), 'main', AP, agg2)
        tail = keepalive.report(agg2)['capped_keepalive']['ping_usd_after_last_call']
        self.assertAlmostEqual(tail, 11 * (20_500 * M + 150 * M), places=5)

    def test_model_switch_and_fallback_are_skipped(self):
        calls = [call(20_000, ts=0.0), call(20_500, ts=600.0, model='claude-opus-5')]
        agg = new_agg()
        keepalive.claude(thread(calls), 'subagent', AP, agg)
        self.assertEqual(keepalive.report(agg)['idle_gaps']['gaps_over_ttl_5m'], 0)


class Polls(unittest.TestCase):
    def test_blocking_wait_opportunity(self):
        s, ev = poll_session()
        self.assertEqual((s['polls'], s['polls_no_new_output']), (3, 2))
        self.assertEqual(polls.output_flags(ev), [True, True, False])
        agg = new_agg()
        polls.codex(s, ev, OP, [0.01, 0.02, 0.03, 0.04], agg)
        rep = polls.report(agg)
        self.assertEqual(rep['codex_polls']['polls_no_new_output'], 2)
        bw = rep['blocking_wait_opportunity']
        self.assertEqual(bw['calls_after_empty_polls_only'], 2)
        self.assertEqual(bw['chains'], 1)
        self.assertAlmostEqual(bw['calls_avoidable_usd'], 0.05)
        self.assertEqual(bw['chains_over_cache_window'], 1)          # 405 s wait
        pen = 1200 * (4.0 - 0.4) * M
        self.assertAlmostEqual(bw['cache_penalty_usd'], pen)
        self.assertAlmostEqual(rep['fix']['net_usd'], 0.05 - pen)

    def test_polls_emitted_by_copied_calls_are_not_counted(self):
        s, ev = poll_session()
        s = codex_ingest.fold(ev + [('recu', 1000, 0, 0, 10, 0, 'h1')])   # ties to call 0, which emitted poll c1
        self.assertEqual((s['polls'], s['polls_no_input'], s['polls_no_new_output']), (3, 3, 2))
        codex_ingest.mark_copied(s, frozenset(['h1']))
        self.assertTrue(s['live'][0]['copied'])
        self.assertEqual((s['polls'], s['polls_no_input'], s['polls_no_new_output']), (2, 2, 1))
        agg = new_agg()
        ledger.codex_session(s, OP, agg)
        polls.codex(s, ev, OP, [None, 0.02, 0.03, 0.04], agg)
        self.assertEqual((agg['codex_polls']['calls'], agg['codex_polls']['polls_no_input'],
                          agg['codex_polls']['polls_no_new_output']), (2, 2, 1))
        self.assertEqual(polls.report(agg)['codex_polls']['polls'], 2)

    def test_unlinked_session_is_counted_not_guessed(self):
        s, ev = poll_session()
        ev = [e for e in ev if not (e[0] == 'out' and e[1] == 'c3')]
        agg = new_agg()
        polls.codex(s, ev, OP, [0.01] * 4, agg)
        rep = polls.report(agg)
        self.assertEqual(rep['codex_polls']['sessions_not_linked'], 1)
        self.assertIsNone(rep['fix'])


class Oversized(unittest.TestCase):
    def test_thresholds_and_modeled_cap(self):
        calls = [call(10_000), call(40_000, pre=[tool_result('t', 30_000)], read=10_000)]
        calls += [call(41_000 + 1000 * i, pre=[prompt(1_000)], read=40_000 + 1000 * i) for i in range(4)]
        tl = be.claude_timeline(thread(calls, {'t': 'Read'}), AP)
        agg = new_agg()
        oversized.run(tl, agg, 'claude')
        rep = oversized.report(agg)
        sec = rep['claude_tool_outputs']
        self.assertEqual((sec['outputs_over_10k'], sec['outputs_over_25k'], sec['outputs_over_50k']), (1, 1, 0))
        per_tok = 12.5 * M + 4 * 1.0 * M        # written once, read on 4 later calls
        self.assertAlmostEqual(sec['usd_over_25k'], 30_000 * per_tok, places=5)
        saving = 20_000 * per_tok
        net = (1 - oversized.NEED_RATE) * saving - oversized.NEED_RATE * 40_000 * 1.0 * M
        self.assertAlmostEqual(rep['claude_cap_at_10k']['net_usd'], net, places=5)
        self.assertEqual(rep['fix']['vendors'], ['claude'])
        self.assertIn('in Claude Code', audits.fix_text(rep['fix']))


class Clearing(unittest.TestCase):
    def test_report_picks_the_most_refetch_tolerant_variant(self):
        agg = new_agg()
        with mock.patch.object(clearing, 'TRIGGER', 90_000), mock.patch.object(clearing, 'KEEP', 1):
            clearing.run(be.claude_timeline(two_batch_thread(), AP), agg, 'claude', ())
        rep = clearing.report(agg, ())
        self.assertTrue(rep['policy']['upper_bound'])
        self.assertEqual(rep['claude_batches_20k']['batches'], 1)     # the 40K result at call 5, keeping the newest
        self.assertEqual(rep['claude_batches_60k']['batches'], 0)
        self.assertNotIn('codex_batches_20k', rep)
        # b x L x r = 40K x 4 reads against a 43K suffix rewrite at 11.5: loses money even with no re-fetch
        net = 40_000 * 4 * M - 43_000 * 11.5 * M
        self.assertAlmostEqual(rep['claude_batches_20k']['net_usd'], net, places=5)
        self.assertEqual(rep['claude_batches_20k']['breakeven_refetch_rate'], 0.0)
        self.assertIsNone(rep['fix'])
        self.assertTrue(rep['trick']['evaluated'])
        self.assertAlmostEqual(rep['trick']['net_usd'], net, places=5)
        self.assertEqual(audits.costly_trick({'clearing': rep})['id'], 'clear_tool_outputs_small_batches')

    def _agg(self, net, unit):
        agg = new_agg()
        a = agg['be:' + clearing.key('claude', 60_000)]
        a['batches'], a['tokens'], a['net_usd'], a['refetch_unit_usd'] = 1, 60_000.0, net, unit
        return agg

    def test_fix_only_when_breakeven_clears_the_reference_rate(self):
        # break-even 90%: well past the 60% lexical reference plus the margin, so it is a fix
        rep = clearing.report(self._agg(9.0, 10.0), ())
        self.assertEqual(rep['fix']['breakeven_refetch_rate'], 0.9)
        self.assertNotIn('not_recommended', rep)
        # break-even 30%: positive before re-fetches, but a re-fetch rate the corpus exceeds erases it
        rep = clearing.report(self._agg(3.0, 10.0), ())
        self.assertIsNone(rep['fix'])
        nr = rep['not_recommended']
        self.assertEqual((nr['breakeven_refetch_rate'], nr['reference_refetch_rate'], nr['batch_tokens']), (0.3, 0.6, 60_000))
        text = audits.not_recommended_text(nr)
        self.assertIn('30%', text)
        self.assertIn('60%', text)
        self.assertNotIn('—', text)
        self.assertEqual(audits.top_fixes({'clearing': rep}), [])

    def test_audit_text_prints_the_verdict(self):
        rep = {'anatomy_version': 'x', 'prices': {'anthropic': {'snapshot_date': '2026-09-30', 'source_url': 'u1'},
                                                  'openai': {'snapshot_date': '2026-09-30', 'source_url': 'u2'}},
               'audits': {'sample': {'basis': 'observed', 'threads': 1}}}
        for k, _ in cli.AUDIT_TITLES:
            rep['audits'][k] = {'fix': None}
        rep['audits']['clearing'] = clearing.report(self._agg(3.0, 10.0), ())
        out = cli.render_audit_text(rep)
        self.assertIn('fix: none recommended. Batched clearing (60K batches) stops paying once 30%', out)
        self.assertNotIn('popular', out)
        self.assertIn('trick evaluated: Clearing old tool outputs', out)


class Ranking(unittest.TestCase):
    def audits_dict(self):
        f = lambda i, n, ub=False: {'basis': 'modeled', 'id': i, 'net_usd': n, 'clears_breakeven': True, 'upper_bound': ub}
        return {'unit': 'x', 'sample': {'basis': 'observed'},
                'boot_scope': {'fix': f('boot_listings_on_demand', 5.0)},
                'keepalive': {'fix': f('keepalive_capped', 7.0),
                              'trick': {'basis': 'modeled', 'id': 'keepalive_always_on', 'net_usd': -2.0, 'evaluated': True}},
                'polls': {'fix': f('codex_blocking_waits', 1.0)},
                'oversized': {'fix': f('cap_large_tool_outputs', 3.0)},
                'clearing': {'fix': f('batched_clearing', 99.0, True),
                             'trick': {'basis': 'modeled', 'id': 'clear_tool_outputs_small_batches', 'net_usd': -9.0, 'evaluated': False}},
                'breakeven': {'trick': {'basis': 'modeled', 'id': 'evict_old_tool_results', 'net_usd': -1.0, 'evaluated': True}}}

    def test_top_three_exclude_upper_bounds(self):
        top = audits.top_fixes(self.audits_dict())
        self.assertEqual([x['id'] for x in top], ['keepalive_capped', 'boot_listings_on_demand', 'cap_large_tool_outputs'])

    def test_costly_trick_is_the_worst_evaluated_loss(self):
        self.assertEqual(audits.costly_trick(self.audits_dict())['id'], 'keepalive_always_on')
        d = self.audits_dict()
        for k in ('keepalive', 'breakeven'):
            d[k]['trick']['net_usd'] = 1.0
        self.assertIsNone(audits.costly_trick(d))

    def test_every_fix_and_trick_has_text_without_dashes(self):
        for i in audits.FIX_TEXT:
            t = audits.fix_text({'id': i, 'cap_minutes_5m': 50, 'cap_tokens': 10_000, 'batch_tokens': 60_000,
                                 'breakeven_refetch_rate': 0.25})
            self.assertNotIn('—', t)
            self.assertNotIn('{', t)
        for i in audits.TRICK_TEXT:
            self.assertNotIn('—', audits.trick_text({'id': i}))


class AuditCommand(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code, cls.out, cls.err = run('audit', '--json')
        cls.rep = json.loads(cls.out)
        cls.tcode, cls.text, cls.terr = run('audit')

    def test_runs_and_passes_the_gate(self):
        self.assertEqual(self.code, 0, self.err)
        self.assertEqual(self.tcode, 0, self.terr)
        gate(self.rep)
        for k in ('breakeven', 'boot_scope', 'keepalive', 'polls', 'oversized', 'clearing', 'sample'):
            self.assertIn(k, self.rep['audits'])

    def test_no_label_is_missing_in_json(self):
        leaves = list(numeric_leaves(self.rep['audits']))
        self.assertGreater(len(leaves), 80)
        for path, basis in leaves:
            self.assertIn(basis, BASES, path)

    def test_no_label_is_missing_in_text(self):
        lines = [ln for ln in self.text.splitlines() if re.search(r'\d', ln) and not ln.startswith('#')]
        self.assertGreater(len(lines), 30)
        for ln in lines:
            self.assertRegex(ln.rstrip(), TAG, ln)

    def test_polls_match_the_scan_counters(self):
        p = self.rep['audits']['polls']['codex_polls']
        s = self.rep['codex']['polls']
        self.assertEqual((p['polls'], p['polls_no_input'], p['polls_no_new_output']),
                         (s['calls'], s['polls_no_input'], s['polls_no_new_output']))
        self.assertEqual(self.rep['audits']['polls']['blocking_wait_opportunity']['calls_after_empty_polls_only'], 1)

    def test_nothing_private_leaks(self):
        for s in PLANTED + [FIX, CLAUDE_DIR, CODEX_DIR, 'fixture-session', 'call_fixture']:
            self.assertNotIn(s, self.out + self.err + self.text + self.terr)
        for ln in self.text.splitlines():
            self.assertNotIn('—', ln)

    def test_scan_output_is_unchanged_by_audits(self):
        code, out, err = run('scan', '--json')
        scan = json.loads(out)
        rep = dict(self.rep)
        rep.pop('audits')
        for k in ('claude', 'codex'):
            self.assertEqual(scan[k]['list_price_usd'], rep[k]['list_price_usd'])


class CodexReader(unittest.TestCase):
    def test_read_codex_equals_read_session(self):
        import glob
        import os
        path = glob.glob(os.path.join(CODEX_DIR, '**', 'rollout-*.jsonl'), recursive=True)[0]
        for copied in (None, frozenset([codex_ingest.response_id_hash('resp_fixture_4'),
                                        codex_ingest.response_id_hash('resp_fixture_compact')])):
            s, ev = cli.read_codex(path, None, copied)
            self.assertEqual(s, codex_ingest.read_session(path, None, copied))
            self.assertTrue(ev)


if __name__ == '__main__':
    unittest.main()
