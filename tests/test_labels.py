"""Every printed number carries a basis: observed, estimated, modeled or invoiced."""
import json
import re
import unittest

from tests.helpers import run_cli
from anatomy.cli import BASES

TAG = re.compile(r'\[(%s)\]' % '|'.join(BASES))


def numeric_leaves(obj, path='$', basis=None):
    if isinstance(obj, dict):
        b = obj.get('basis', basis)
        for k, v in obj.items():
            yield from numeric_leaves(v, path + '.' + k, b)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from numeric_leaves(v, '%s[%d]' % (path, i), basis)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        yield path, basis


class Labels(unittest.TestCase):
    def test_every_json_number_has_a_basis(self):
        code, out, err = run_cli('--json')
        self.assertEqual(code, 0, err)
        rep = json.loads(out)
        leaves = list(numeric_leaves(rep))
        self.assertGreater(len(leaves), 50)
        for path, basis in leaves:
            self.assertIn(basis, BASES, path)
        self.assertEqual(rep['invoiced'], {'basis': 'invoiced', 'status': 'not available'})

    def test_every_text_line_with_a_number_has_a_basis(self):
        code, out, err = run_cli()
        self.assertEqual(code, 0, err)
        lines = [ln for ln in out.splitlines() if re.search(r'\d', ln) and not ln.startswith('#')]
        self.assertGreater(len(lines), 20)
        for ln in lines:
            self.assertRegex(ln, TAG, ln)
        self.assertIn('invoiced totals: not available', out)
        self.assertIn('https://platform.claude.com/docs/en/about-claude/pricing', out)   # snapshot source, not only its date
        self.assertIn('https://developers.openai.com/api/docs/pricing', out)
        self.assertRegex(out, r'declined fallbacks .*billing assumed  \[estimated\]')

    def test_dollars_are_list_price_equivalent(self):
        for extra in ((), ('--json',)):
            code, out, err = run_cli(*extra)
            self.assertIn('API list-price equivalent', out)
            self.assertNotRegex(out.lower(), r'\bspen[dt]\b|\bspend_|wasted')


if __name__ == '__main__':
    unittest.main()
