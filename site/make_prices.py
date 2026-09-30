"""Copy the repo's dated price snapshots into site/prices.json for the break-even page.

Run from anywhere: python3 site/make_prices.py
Only list prices, snapshot dates and source URLs are copied. Standard library only.
"""
import json
import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / 'src' / 'anatomy' / 'prices'


def load(name):
    with open(SRC / (name + '.toml'), 'rb') as fh:
        return tomllib.load(fh)


def main():
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
    dest = ROOT / 'site' / 'prices.json'
    dest.write_text(json.dumps(out, indent=1) + '\n')
    print('wrote', dest.relative_to(ROOT), len(anthropic), 'anthropic rows,', len(openai), 'openai rows')


if __name__ == '__main__':
    main()
