"""Copy the repo's dated price snapshots into docs/calculator/prices.json for the break-even page.

Run from anywhere: python3 scripts/make_prices.py (--check exits 1 if the page data is stale)
Only list prices, snapshot dates and source URLs are copied. Standard library only.
"""
import json
import pathlib
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / 'src' / 'anatomy' / 'prices'
DEST = ROOT / 'docs' / 'calculator' / 'prices.json'


def load(name):
    with open(SRC / (name + '.toml'), 'rb') as fh:
        return tomllib.load(fh)


def build():
    a, o = load('anthropic'), load('openai')
    anthropic = {m: {k: row[k] for k in ('input', 'cache_write_5m', 'cache_write_1h', 'cache_read')}
                 for m, row in a['models'].items()}
    openai = {}
    for m, row in o['models'].items():
        r = {k: row[k] for k in ('input', 'cached_input', 'cache_write') if k in row}
        if 'long_context' in row:
            r['long_context'] = {k: row['long_context'][k] for k in ('input', 'cached_input', 'cache_write') if k in row['long_context']}
        openai[m] = r
    out = {
        'unit': 'USD per million tokens, list price',
        'copied_from': ['src/anatomy/prices/anthropic.toml', 'src/anatomy/prices/openai.toml'],
        'anthropic': {'snapshot_date': a['snapshot_date'], 'source_url': a['source_url'],
                      'note': 'Standard speed, global routing, no batch discount. ' + a['notes']['cache_read_multiplier'] + '.',
                      'models': anthropic},
        'openai': {'snapshot_date': o['snapshot_date'], 'source_url': o['source_url'],
                   'long_context_threshold': o['long_context_threshold'],
                   'note': 'Standard tier. Codex rollouts report no cache-write tokens, so a rewritten suffix bills as uncached input unless the cache-write row applies.',
                   'models': openai},
    }
    return json.dumps(out, indent=1) + '\n', len(anthropic), len(openai)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    text, na, no = build()
    if '--check' in argv:
        if not DEST.exists() or DEST.read_text() != text:
            print(DEST.relative_to(ROOT), 'is stale: run python3 scripts/make_prices.py')
            return 1
        print(DEST.relative_to(ROOT), 'matches the price snapshots')
        return 0
    DEST.write_text(text)
    print('wrote', DEST.relative_to(ROOT), na, 'anthropic rows,', no, 'openai rows')
    return 0


if __name__ == '__main__':
    sys.exit(main())
