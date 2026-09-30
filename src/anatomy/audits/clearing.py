"""Batched clearing: does clearing old tool outputs in batches pay at the user's prices?

Replays a clearing policy on every thread (breakeven.Clearing): once the
context reaches TRIGGER tokens, clear every tool output except the KEEP most
recent, but only when that frees at least B tokens. Each clear is scored with
the break-even rule at the prices the next call paid: the cache reads it saves
on later calls against the rewrite of the suffix after the earliest cleared
output. Re-fetch cost is taken as zero, so every net here is an upper bound: a
variant that loses money at zero re-fetch loses more once re-fetches count.
Each variant also reports its break-even re-fetch rate: the share of cleared
outputs that could be fetched back before the variant stops paying, each re-fetch
charged its lost savings, a fresh write and one extra round trip
(breakeven.record). The best variant is the one most tolerant of re-fetches.

Nothing in a transcript says which cleared outputs would have been needed again.
The one measurement behind this project is a lexical proxy on the launch corpus:
60% of evicted Claude Code outputs were mentioned again later (it overstates
re-fetches). A variant is proposed as a fix only when its break-even rate clears
that reference by REFERENCE_MARGIN; otherwise it is reported as at break-even at
best and not recommended.
"""
from __future__ import annotations

from .. import breakeven as be

TRIGGER = 100_000
KEEP = 3
BATCHES = (20_000, 60_000, 150_000)
SMALL_BATCH = 20_000   # the small-batch setting evaluated as the trick
REFETCH_REFERENCE = 0.60   # lexical re-fetch proxy measured on the launch corpus (Claude Code)
REFERENCE_MARGIN = 0.15


def key(vendor: str, B: int) -> str:
    return '%s:clear%dk' % (vendor, B // 1000)


def run(tl, agg, vendor: str, ratios: tuple) -> None:
    for B in BATCHES:
        be.record(agg, key(vendor, B), be.replay(tl, be.Clearing(TRIGGER, KEEP, B)), ratios)


def report(agg, ratios: tuple) -> dict:
    out = {'policy': {'basis': 'modeled', 'trigger_tokens': TRIGGER, 'keep_recent_outputs': KEEP,
                      'refetch_rate': 0.0, 'upper_bound': True}}
    best = None
    small = 0.0
    evaluated = False
    for B in BATCHES:
        tot, unit, net0 = 0.0, 0.0, 0.0
        for vendor in ('claude', 'codex'):
            k = key(vendor, B)
            if ('be:' + k) not in agg:
                continue
            sec = {'basis': 'modeled', **be.share_paying(agg, k)}
            out['%s_batches_%dk' % (vendor, B // 1000)] = sec
            tot += sec['net_usd']
            a = agg['be:' + k]
            unit += a.get('refetch_unit_usd', 0.0)
            net0 += a.get('net_usd', 0.0) + a.get('refetch_usd', 0.0)
            evaluated = evaluated or sec['batches'] > 0
        pstar = round(net0 / unit, 4) if unit > 0 and net0 > 0 else 0.0
        if tot > 0 and (best is None or pstar > best[2]):
            best = (B, tot, pstar)
        if B == SMALL_BATCH:
            small = tot
    out['fix'] = None
    if best and best[2] >= REFETCH_REFERENCE + REFERENCE_MARGIN:
        out['fix'] = {'basis': 'modeled', 'id': 'batched_clearing', 'net_usd': round(best[1], 6),
                      'batch_tokens': best[0], 'breakeven_refetch_rate': best[2],
                      'clears_breakeven': True, 'upper_bound': True}
    elif best:
        out['not_recommended'] = {'basis': 'modeled', 'id': 'batched_clearing', 'batch_tokens': best[0],
                                  'net_usd_before_refetch': round(best[1], 6), 'breakeven_refetch_rate': best[2],
                                  'reference_refetch_rate': REFETCH_REFERENCE}
    out['trick'] = {'basis': 'modeled', 'id': 'clear_tool_outputs_small_batches', 'net_usd': round(small, 6),
                    'batch_tokens': SMALL_BATCH, 'evaluated': evaluated, 'zero_refetch': True}
    return out
