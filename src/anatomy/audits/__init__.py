"""Audits: each one turns the ledger into labeled numbers and at most one plain-language fix.

Per-file work (claude_thread, codex_session) adds plain counters to the scan's
aggregate, so audits merge across worker processes and follow the global
dedupe: calls copied into resumed or forked files are never scored twice.
report() turns the merged counters into one section per audit. Every section
carries a basis (observed, estimated, modeled); dollars are USD API list-price
equivalent at the price snapshots.

A fix "clears break-even" when its modeled net saving at the user's own prices
is positive under its stated assumptions. Fixes whose net is an upper bound
(zero re-fetch) are reported but never ranked among the top fixes.
"""
from __future__ import annotations

from .. import breakeven as be
from . import boot_scope, clearing, keepalive, oversized, polls

LIST_PRICE = 'USD API list-price equivalent'
MIN_THREADS, MIN_SESSIONS = 20, 5   # privacy rule 2: no shared aggregate below these
LEASE = {'K_calls': 3, 'batch_tokens': 30_000, 'min_output_tokens': 500}

FIX_TEXT = {
    'boot_listings_on_demand': 'Start subagents without the listings they never use (skills, deferred tools, MCP '
                               'instructions) and load those on demand.',
    'keepalive_capped': 'Keep the cache warm through idle gaps only up to the break-even cap{cap}, then let it expire.',
    'codex_blocking_waits': 'Let Codex wait for a running command to print or exit instead of polling it on a short timer.',
    'cap_large_tool_outputs': 'Cap tool outputs{where} at about {cap_k}K tokens, keep the head and the tail, and read the rest on demand.',
    'batched_clearing': 'Clear old tool outputs only in batches of {batch_k}K tokens or more; this pays only while fewer '
                        'than {pstar} of the cleared outputs are ever fetched back.',
}
NOT_RECOMMENDED_TEXT = {
    'batched_clearing': 'Batched clearing ({batch_k}K batches) stops paying once {pstar} of the cleared outputs are '
                        'fetched back, and a lexical proxy put that share at {ref} on the corpus behind this project.',
}
TRICK_TEXT = {
    'evict_old_tool_results': 'Pruning old tool outputs as you go (3 calls old, 30K-token batches)',
    'clear_tool_outputs_small_batches': 'Clearing old tool outputs in small batches (20K tokens, 100K trigger)',
    'keepalive_always_on': 'Uncapped keep-alive through every idle gap',
}


def claude_thread(th: dict, kind: str, prices, costs: list, agg, ratios: tuple) -> None:
    tl = be.claude_timeline(th, prices)
    be.record(agg, 'claude:lease', be.replay(tl, be.Lease(**_lease_args())), ratios)
    boot_scope.claude(th, kind, costs, agg)
    keepalive.claude(th, kind, prices, agg)
    oversized.run(tl, agg, 'claude')
    clearing.run(tl, agg, 'claude', ratios)


def codex_session(s: dict, ev: list, prices, costs: list, agg, ratios: tuple) -> None:
    tl = be.codex_timeline(s, prices)
    be.record(agg, 'codex:lease', be.replay(tl, be.Lease(**_lease_args())), ratios)
    boot_scope.codex(s, prices, agg)
    polls.codex(s, ev, prices, costs, agg)
    oversized.run(tl, agg, 'codex')
    clearing.run(tl, agg, 'codex', ratios)


def _lease_args():
    return {'K': LEASE['K_calls'], 'E': LEASE['batch_tokens'], 'min_tok': LEASE['min_output_tokens']}


def _dominant_claude_model(agg):
    best, usd = None, 0.0
    for k, v in agg.items():
        if k.startswith('claude_usd_model:'):
            t = sum(v.values())
            if t > usd:
                best, usd = k.split(':', 1)[1], t
    return best


def breakeven_report(agg, ap, op, ratios: tuple) -> dict:
    used_c = set(agg.get('claude_calls_by_model') or {})
    used_o = set(agg.get('codex_calls_by_model') or {})
    cr = be.claude_ratios(ap)
    in_use = {'basis': 'observed'}
    for m in sorted(used_c):
        base, _ = ap.base_row(m)
        if base in cr:
            in_use[m] = {'ratio_5m': cr[base]['5m'], 'ratio_1h': cr[base]['1h']}
    for m in sorted(used_o):
        row = op.row(m)
        if row:
            in_use[m] = {'ratio_rewrite_as_input': round(be.ratio(row['input'], row['cached_input']), 4)}
    out = {'write_read_ratios_in_use': in_use}
    net = 0.0
    evaluated = False
    for vendor in ('claude', 'codex'):
        k = vendor + ':lease'
        if ('be:' + k) not in agg:
            continue
        sec = {'basis': 'modeled', **LEASE, 'refetch_rate': 0.0, 'upper_bound': True, **be.share_paying(agg, k, ratios)}
        out[vendor + '_eviction_opportunities'] = sec
        net += sec['net_usd']
        evaluated = evaluated or sec['batches'] > 0
    out['trick'] = {'basis': 'modeled', 'id': 'evict_old_tool_results', 'net_usd': round(net, 6), 'evaluated': evaluated,
                    'zero_refetch': True}
    return out


def report(agg, ap, op) -> dict:
    ratios = be.ratio_set(ap, op)
    dom = _dominant_claude_model(agg)
    r5 = r1 = None
    if dom:
        base, _ = ap.base_row(dom)
        if base:
            rr = be.claude_ratios(ap)[base]
            r5, r1 = rr['5m'], rr['1h']
    cf, xf = agg.get('claude_files') or {}, agg.get('codex_files') or {}
    threads = (agg.get('claude_threads') or {}).get('with_calls', 0) + xf.get('all', 0)
    sessions = cf.get('main', 0) + xf.get('source:root', 0)
    return {
        'unit': LIST_PRICE,
        'sample': {'basis': 'observed', 'threads': int(threads), 'sessions': int(sessions),
                   'meets_sharing_minimum': threads >= MIN_THREADS and sessions >= MIN_SESSIONS,
                   'minimum_threads': MIN_THREADS, 'minimum_sessions': MIN_SESSIONS},
        'breakeven': breakeven_report(agg, ap, op, ratios),
        'boot_scope': boot_scope.report(agg),
        'keepalive': keepalive.report(agg, r5, r1),
        'polls': polls.report(agg),
        'oversized': oversized.report(agg),
        'clearing': clearing.report(agg, ratios),
    }


def fixes(audits: dict) -> list:
    """Every fix any audit proposed, best net first."""
    out = [dict(sec['fix'], audit=name) for name, sec in audits.items()
           if isinstance(sec, dict) and isinstance(sec.get('fix'), dict)]
    return sorted(out, key=lambda f: -f['net_usd'])


def top_fixes(audits: dict, n: int = 3) -> list:
    return [f for f in fixes(audits) if f['clears_breakeven'] and not f['upper_bound'] and f['net_usd'] > 0][:n]


def costly_trick(audits: dict):
    """The evaluated trick that loses the most money at the user's prices, or None."""
    tr = [sec['trick'] for sec in audits.values() if isinstance(sec, dict) and isinstance(sec.get('trick'), dict)]
    tr = [t for t in tr if t['evaluated'] and t['net_usd'] < 0]
    return min(tr, key=lambda t: t['net_usd']) if tr else None


def fix_text(f: dict) -> str:
    cap = ''
    if f['id'] == 'keepalive_capped':
        if f.get('cap_minutes_5m') is not None:
            cap = ' (about %d minutes on the 5-minute tier at your main model\'s prices)' % f['cap_minutes_5m']
    names = {'claude': 'Claude Code', 'codex': 'Codex'}
    v = f.get('vendors') or []
    where = (' in ' + ' and '.join(names[x] for x in v)) if 0 < len(v) < 2 else ''
    pstar = '{:.0f}%'.format(100 * f.get('breakeven_refetch_rate', 0.0))
    return FIX_TEXT[f['id']].format(cap=cap, cap_k=f.get('cap_tokens', 0) // 1000, batch_k=f.get('batch_tokens', 0) // 1000,
                                    where=where, pstar=pstar)


def trick_text(t: dict) -> str:
    return TRICK_TEXT[t['id']]


def not_recommended_text(d: dict) -> str:
    return NOT_RECOMMENDED_TEXT[d['id']].format(batch_k=d.get('batch_tokens', 0) // 1000,
                                                pstar='{:.0f}%'.format(100 * d['breakeven_refetch_rate']),
                                                ref='{:.0f}%'.format(100 * d['reference_refetch_rate']))
