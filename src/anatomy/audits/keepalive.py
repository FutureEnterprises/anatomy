"""Keep-alive: cache rebuilds after idle gaps, and which gaps a keep-alive would have paid for.

Claude Code only (Codex rollouts do not show a cache TTL to keep alive). For each
idle gap between two billed calls of a thread with the same model and no
fallback, a keep-alive pings the cached prefix every (TTL - 30 s): each ping
reads the prefix (P tokens at the read price) plus a few tokens of its own. A
gap longer than the TTL that ended in a rewrite of the prefix is a rebuild; its
excess over a warm read is P x (w - r).

Three views, all modeled:
- hindsight: gaps where the pings needed to bridge the gap cost less than the
  rebuild (an upper bound, since nobody knows a gap's length in advance);
- capped: ping until the pings would have cost as much as one rebuild, then
  stop (the break-even cap). Gaps that outlast the cap pay the pings and the
  rebuild. Main threads also pay the full cap after their last call, since the
  pinger cannot know the session is over;
- always on: ping through every gap, however long, with no cap (no pings
  after the last call are charged, which flatters it).

The thread's TTL class follows attribute.py: 1 hour when the thread writes more
1-hour than 5-minute cache tokens.
"""
from __future__ import annotations

import math

from ..ingest.claude import ctx

PING_INPUT_TOKENS = 10
PING_OUTPUT_TOKENS = 1
MARGIN_S = 30


def claude(th: dict, kind: str, prices, agg) -> None:
    calls = th['calls']
    n = len(calls)
    if n == 0:
        return
    a = agg['aud_keep']
    ttl1h = sum(c['cc1'] for c in calls) > sum(c['cc5'] for c in calls)
    ttl = 3600 if ttl1h else 300
    tp = ttl - MARGIN_S
    tier = '1h' if ttl1h else '5m'

    def unit(c, P):
        r = prices.rates(c['model'], c.get('speed'), c.get('geo'))
        if r is None:
            return None
        w = r.cache_write_1h if ttl1h else r.cache_write_5m
        ping = P * r.cache_read + PING_INPUT_TOKENS * r.input + PING_OUTPUT_TOKENS * r.output
        cap = int((P * (w - r.cache_read)) // ping) if ping > 0 else 0
        return w, r.cache_read, ping, cap

    for j in range(1, n):
        c, p = calls[j], calls[j - 1]
        if c.get('copied') or c['ts'] is None or p['ts'] is None:
            continue
        if p['iters'] or c['model'] != p['model']:
            continue    # a fallback or model switch rebuilds regardless of keep-alive
        g = c['ts'] - p['ts']
        if g <= tp:
            continue
        P = ctx(p)
        u = unit(c, P)
        if u is None:
            continue
        w, r, ping, cap = u
        fired = int(g // tp)
        need = math.ceil((g - ttl) / tp) if g > ttl else 0
        growth = max(ctx(c) - P, 0)
        rebuilt = min(max(c['cc'] - growth, 0), P)
        rebuild = g > ttl and rebuilt > 2000 and rebuilt > 0.1 * max(P, 1)
        excess = rebuilt * (w - r) if rebuild else 0.0
        a['gaps_over_ping_interval:' + tier] += 1
        if g > ttl:
            a['gaps_over_ttl:' + tier] += 1
        if rebuild:
            a['rebuilds:' + tier] += 1
            a['rebuild_excess_usd:' + tier] += excess
            if need * ping < excess:
                a['hindsight_paying_gaps:' + tier] += 1
                a['hindsight_net_usd'] += excess - need * ping
        # capped policy
        fc = min(cap, fired)
        a['capped_ping_usd'] += fc * ping
        if rebuild and need <= cap:
            a['capped_saved_usd'] += excess
            a['capped_bridged_rebuilds'] += 1
        # always on
        a['always_ping_usd'] += fired * ping
        if rebuild:
            a['always_saved_usd'] += excess
    if kind == 'main' and not calls[-1].get('copied'):
        u = unit(calls[-1], ctx(calls[-1]))
        if u is not None:
            a['capped_ping_usd_after_last_call'] += u[3] * u[2]
            a['main_threads_after_last_call'] += 1


def report(agg, dominant_ratio_5m=None, dominant_ratio_1h=None) -> dict:
    a = agg.get('aud_keep') or {}
    capped_net = a.get('capped_saved_usd', 0.0) - a.get('capped_ping_usd', 0.0) - a.get('capped_ping_usd_after_last_call', 0.0)
    always_net = a.get('always_saved_usd', 0.0) - a.get('always_ping_usd', 0.0)
    gaps = {'basis': 'observed'}
    rebuilds = {'basis': 'estimated'}
    for t in ('5m', '1h'):
        gaps['gaps_over_ping_interval_' + t] = int(a.get('gaps_over_ping_interval:' + t, 0))
        gaps['gaps_over_ttl_' + t] = int(a.get('gaps_over_ttl:' + t, 0))
        rebuilds['rebuilds_' + t] = int(a.get('rebuilds:' + t, 0))
        rebuilds['rebuild_excess_usd_' + t] = round(a.get('rebuild_excess_usd:' + t, 0.0), 6)
        rebuilds['hindsight_paying_gaps_' + t] = int(a.get('hindsight_paying_gaps:' + t, 0))
    out = {
        'idle_gaps': gaps,
        'rebuilds_after_idle_gaps': rebuilds,
        'hindsight': {'basis': 'modeled', 'net_usd': round(a.get('hindsight_net_usd', 0.0), 6), 'upper_bound': True},
        'capped_keepalive': {'basis': 'modeled',
                             'ping_usd': round(a.get('capped_ping_usd', 0.0), 6),
                             'ping_usd_after_last_call': round(a.get('capped_ping_usd_after_last_call', 0.0), 6),
                             'saved_usd': round(a.get('capped_saved_usd', 0.0), 6),
                             'bridged_rebuilds': int(a.get('capped_bridged_rebuilds', 0)),
                             'net_usd': round(capped_net, 6)},
        'always_on_keepalive': {'basis': 'modeled',
                                'ping_usd': round(a.get('always_ping_usd', 0.0), 6),
                                'saved_usd': round(a.get('always_saved_usd', 0.0), 6),
                                'net_usd': round(always_net, 6)},
        'fix': None,
        'trick': {'basis': 'modeled', 'id': 'keepalive_always_on', 'net_usd': round(always_net, 6),
                  'evaluated': bool(a.get('gaps_over_ping_interval:5m') or a.get('gaps_over_ping_interval:1h'))},
    }
    if capped_net > 0:
        fix = {'basis': 'modeled', 'id': 'keepalive_capped', 'net_usd': round(capped_net, 6),
               'clears_breakeven': True, 'upper_bound': False}
        if dominant_ratio_5m:
            fix['cap_minutes_5m'] = round(math.floor(dominant_ratio_5m) * (300 - MARGIN_S) / 60)
        if dominant_ratio_1h:
            fix['cap_hours_1h'] = round(math.floor(dominant_ratio_1h) * (3600 - MARGIN_S) / 3600)
        out['fix'] = fix
    return out
