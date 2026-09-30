"""The shipped price snapshots load, carry their source and date, and resolve model ids."""
import unittest

import tests.helpers  # noqa: F401  (puts src on the path)
from anatomy.prices import AnthropicPrices, OpenAIPrices


class Snapshots(unittest.TestCase):
    def test_anthropic(self):
        p = AnthropicPrices()
        self.assertTrue(p.source_url.startswith('https://platform.claude.com/'))
        self.assertRegex(p.snapshot_date, r'^\d{4}-\d{2}-\d{2}$')
        r = p.rates('claude-opus-5-5')
        self.assertAlmostEqual(r.cache_read * 1e6, 0.20)
        self.assertAlmostEqual(r.cache_write_1h * 1e6, 8.0)
        self.assertEqual(p.rates('claude-haiku-4-5-20251001'), p.rates('claude-haiku-4-5'))
        self.assertIsNone(p.rates('claude-opus-4-9'))      # never falls back to a different model's row
        self.assertIsNone(p.rates(None))
        fast = p.rates('claude-opus-5-5', speed='fast')
        self.assertAlmostEqual(fast.input * 1e6, 8.0)
        self.assertAlmostEqual(fast.cache_read * 1e6, 8.0 * 0.05)
        self.assertAlmostEqual(p.rates('claude-opus-5', geo='us').output * 1e6, 25.0 * 1.1)

    def test_openai(self):
        p = OpenAIPrices()
        self.assertTrue(p.source_url.startswith('https://developers.openai.com/'))
        self.assertAlmostEqual(p.rates('gpt-5.6-sol', 1000).input * 1e6, 4.0)
        self.assertAlmostEqual(p.rates('gpt-5.6-sol', 300_000).input * 1e6, 8.0)
        self.assertAlmostEqual(p.rates('gpt-5.6-sol', 300_000).output * 1e6, 30.0)
        self.assertAlmostEqual(p.rates('gpt-5.2', 300_000).input * 1e6, 1.75)   # no long-context row
        self.assertIsNone(p.rates('codex-auto-review', 10))


if __name__ == '__main__':
    unittest.main()
