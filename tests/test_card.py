"""The share card: numbers only, labeled, gated, and honest about small samples."""
import json
import os
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET

from tests.fixtures.make_fixtures import PLANTED
from tests.helpers import CLAUDE_DIR, CODEX_DIR, FIX
from tests.test_audits import TAG, run
from tests.test_labels import numeric_leaves
from anatomy import card
from anatomy.cli import BASES
from anatomy.privacy import PrivacyError, gate

PRIVATE = PLANTED + [FIX, CLAUDE_DIR, CODEX_DIR, 'fixture-session', 'call_fixture']


def synthetic_report(threads=40, sessions=8):
    """A report with fixed audit results, enough to fill every slot of the card."""
    fix = lambda i, n, **kw: {'basis': 'modeled', 'id': i, 'net_usd': n, 'clears_breakeven': True, 'upper_bound': False, **kw}
    return {
        'prices': {'anthropic': {'snapshot_date': '2026-09-30'}, 'openai': {'snapshot_date': '2026-09-30'}},
        'claude': {'list_price_usd': {'served': 900.0}, 'declined_fallbacks_usd': {'declined_billed': 100.0}},
        'codex': {'list_price_usd': {'total': 500.0}},
        'audits': {
            'unit': 'USD API list-price equivalent',
            'sample': {'basis': 'observed', 'threads': threads, 'sessions': sessions,
                       'meets_sharing_minimum': threads >= 20 and sessions >= 5, 'minimum_threads': 20, 'minimum_sessions': 5},
            'breakeven': {'claude_eviction_opportunities': {'batches': 10, 'share_batches_that_pay': 0.3},
                          'trick': {'basis': 'modeled', 'id': 'evict_old_tool_results', 'net_usd': -12.0,
                                    'evaluated': True, 'zero_refetch': True}},
            'boot_scope': {'fix': fix('boot_listings_on_demand', 40.0)},
            'keepalive': {'fix': fix('keepalive_capped', 30.0, cap_minutes_5m=50),
                          'trick': {'basis': 'modeled', 'id': 'keepalive_always_on', 'net_usd': -5.0, 'evaluated': True}},
            'polls': {'fix': fix('codex_blocking_waits', 20.0)},
            'oversized': {'fix': fix('cap_large_tool_outputs', 10.0, cap_tokens=10_000, vendors=['claude'])},
            'clearing': {'fix': dict(fix('batched_clearing', 500.0, batch_tokens=150_000, breakeven_refetch_rate=0.2),
                                     upper_bound=True)},
        },
    }


class Build(unittest.TestCase):
    def setUp(self):
        self.c = card.build(synthetic_report())

    def test_top_three_and_the_costly_trick(self):
        self.assertEqual([f['id'] for f in self.c['fixes']], ['boot_listings_on_demand', 'keepalive_capped', 'codex_blocking_waits'])
        self.assertEqual(self.c['costly_trick']['id'], 'evict_old_tool_results')
        self.assertAlmostEqual(self.c['fixes'][0]['share_of_spend'], 40 / 1500, places=4)
        text = card.render_text(self.c)
        self.assertIn('about 50 minutes', text)
        self.assertIn('even with no re-fetches', text)
        self.assertIn('would have saved $40 in replay', text)   # a replay, never a realized saving
        self.assertNotRegex(text, r'\bsaves\b|POPULAR|popular')
        self.assertIn('Claude Code $900 served', text)
        self.assertRegex(text, r'Plus \$100 of declined .*billing assumed.*\[estimated\]')
        self.assertEqual(self.c['spend_declined']['basis'], 'estimated')
        self.assertNotIn('batches of 150K', text)          # upper-bound fixes are never ranked

    def test_no_label_is_missing(self):
        gate(self.c)
        for path, basis in numeric_leaves(self.c):
            self.assertIn(basis, BASES, path)
        for kind, text in card.lines(self.c):
            if kind in ('title', 'meta') or not re.search(r'\d', text):
                continue
            self.assertRegex(text.rstrip(), TAG, text)

    def test_svg_is_well_formed_and_clean(self):
        svg = card.render_svg(self.c)
        root = ET.fromstring(svg)
        self.assertTrue(root.tag.endswith('svg'))
        self.assertEqual(svg.count('://'), 1)             # the SVG namespace, nothing else
        self.assertNotIn('—', svg)
        texts = ''.join(t.text or '' for t in root.iter() if t.tag.endswith('text'))
        self.assertIn('TOP FIXES', texts)

    def test_small_sample_says_do_not_share(self):
        c = card.build(synthetic_report(threads=4, sessions=1))
        self.assertFalse(c['sample']['meets_sharing_minimum'])
        self.assertIn('do not share', card.render_text(c))
        self.assertIn('do not share', card.render_svg(c))

    def test_gate_refuses_a_private_string(self):
        rep = synthetic_report()
        rep['prices']['anthropic']['snapshot_date'] = '/Users/alice/secret'
        with self.assertRaises(PrivacyError):
            card.build(rep)

    def test_no_fix_no_trick(self):
        rep = synthetic_report()
        for k in ('boot_scope', 'keepalive', 'polls', 'oversized', 'clearing'):
            rep['audits'][k] = {'fix': None}
        rep['audits']['breakeven']['trick']['net_usd'] = 3.0
        c = card.build(rep)
        text = card.render_text(c)
        self.assertIn('None of the audited fixes clears break-even', text)
        self.assertIn('None of the evaluated tricks loses money', text)


class SharesOnly(unittest.TestCase):
    def setUp(self):
        self.c = card.build(synthetic_report(), shares_only=True)

    def test_no_dollar_amount_anywhere(self):
        self.assertFalse([k for k, _ in _keys(self.c) if 'usd' in k])
        self.assertEqual([f['id'] for f in self.c['fixes']], ['boot_listings_on_demand', 'keepalive_capped', 'codex_blocking_waits'])
        text, svg = card.render_text(self.c), card.render_svg(self.c)
        for out in (text, svg, json.dumps(self.c)):
            self.assertNotIn('$', out)
        self.assertIn('Dollar amounts hidden', text)
        self.assertIn('Claude Code 60.0% served, Codex 33.3%', text)
        self.assertRegex(text, r'Plus 6\.7% of declined .*billing assumed.*\[estimated\]')
        self.assertIn('would have saved 2.7% of spend read in replay', text)
        self.assertIn('would have cost 0.8% of spend read in replay, even with no re-fetches', text)
        ET.fromstring(svg)

    def test_every_number_labeled(self):
        gate(self.c)
        for path, basis in numeric_leaves(self.c):
            self.assertIn(basis, BASES, path)
        for kind, text in card.lines(self.c):
            if kind in ('title', 'meta') or not re.search(r'\d', text):
                continue
            self.assertRegex(text.rstrip(), TAG, text)


def _keys(obj, pre=''):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield pre + k, v
            yield from _keys(v, pre + k + '.')
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v, pre)


class CardCommand(unittest.TestCase):
    def test_card_on_fixtures(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'card.svg')
            code, out, err = run('card', '--svg', path)
            self.assertEqual(code, 0, err)
            with open(path, encoding='utf-8') as fh:
                svg = fh.read()
        code2, js, err2 = run('card', '--json')
        self.assertEqual(code2, 0, err2)
        c = json.loads(js)
        gate(c)
        self.assertLessEqual(len(c['fixes']), 3)
        for f in c['fixes']:
            self.assertGreater(f['net_usd'], 0)
        self.assertIn('ANATOMY CARD', out)
        self.assertIn('do not share', out)                 # four fixture threads are below the minimum
        ET.fromstring(svg)
        for s in PRIVATE:
            self.assertNotIn(s, out + err + svg + js + err2)

    def test_shares_only_on_fixtures(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'card.svg')
            code, out, err = run('card', '--shares-only', '--svg', path)
            self.assertEqual(code, 0, err)
            with open(path, encoding='utf-8') as fh:
                svg = fh.read()
        code2, js, err2 = run('card', '--shares-only', '--json')
        self.assertEqual(code2, 0, err2)
        c = json.loads(js)
        self.assertTrue(c['shares_only'])
        self.assertNotIn('$', out + svg + js)
        self.assertNotIn('_usd', js)
        ET.fromstring(svg)


if __name__ == '__main__':
    unittest.main()
