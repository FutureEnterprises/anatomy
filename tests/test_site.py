"""The published pages: the calculator runs from any subpath with local files only, and its prices match the snapshots."""
import importlib.util
import os
import re
import unittest
from html.parser import HTMLParser

from tests.helpers import ROOT

CALC = os.path.join(ROOT, 'docs', 'calculator')


def _make_prices():
    spec = importlib.util.spec_from_file_location('make_prices', os.path.join(ROOT, 'scripts', 'make_prices.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Refs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for k in ('src', 'href'):
            if a.get(k) and (tag in ('script', 'link', 'img', 'iframe') or (tag == 'a' and k == 'href')):
                self.refs.append((tag, a[k]))


class Calculator(unittest.TestCase):
    def test_prices_match_the_snapshots(self):
        text, _, _ = _make_prices().build()
        with open(os.path.join(CALC, 'prices.json'), encoding='utf-8') as fh:
            self.assertEqual(fh.read(), text, 'run python3 scripts/make_prices.py')

    def test_local_relative_assets_only(self):
        with open(os.path.join(CALC, 'index.html'), encoding='utf-8') as fh:
            html = fh.read()
        p = _Refs()
        p.feed(html)
        assets = [r for t, r in p.refs if t != 'a']
        self.assertEqual(assets, ['breakeven.js'])
        for tag, ref in p.refs:
            if tag == 'a' and ref.startswith('https://'):
                continue            # outbound links are fine; nothing loads from them
            self.assertFalse(ref.startswith('/') or '://' in ref, ref)   # relative, so it works under /anatomy/calculator/
            if tag != 'a':
                self.assertTrue(os.path.exists(os.path.join(CALC, ref)), ref)
        self.assertNotRegex(html, r'<script>[^<]')       # no inline script; the CSP allows only 'self'
        with open(os.path.join(CALC, 'breakeven.js'), encoding='utf-8') as fh:
            js = fh.read()
        self.assertEqual(re.findall(r"fetch\('([^']+)'", js), ['prices.json'])
        self.assertNotIn('://', js)

    def test_no_em_dashes_in_published_text(self):
        for d, _, files in os.walk(os.path.join(ROOT, 'docs')):
            for f in files:
                if f.endswith(('.md', '.html', '.js', '.yml')):
                    with open(os.path.join(d, f), encoding='utf-8') as fh:
                        self.assertNotIn('—', fh.read(), f)
        with open(os.path.join(ROOT, 'README.md'), encoding='utf-8') as fh:
            self.assertNotIn('—', fh.read())


if __name__ == '__main__':
    unittest.main()
