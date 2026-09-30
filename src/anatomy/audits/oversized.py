"""Oversized tool outputs: results over fixed token thresholds, and what they cost to carry.

Estimated: each tool output's tokens (from characters) and its cost, the write
on the call it entered plus a cache read on every later billed call it stayed
in context for (a warm-cache lower bound: rebuilds would make it higher).

Modeled fix: cap outputs at CAP tokens (keep the head and the tail) and let the
agent read the rest on demand. The saving is the cost of the excess tokens. With
probability NEED_RATE the agent needs the dropped part and fetches it again,
which gives the saving back and adds one extra model call over the context.
NEED_RATE is an assumption carried from the launch replay of large-output
admission, where 64% to 66% of large outputs were referenced again.
"""
from __future__ import annotations

THRESHOLDS = (10_000, 25_000, 50_000)
CAP = 10_000
NEED_RATE = 0.65


def run(tl, agg, vendor: str) -> None:
    a = agg['aud_big:' + vendor]
    a['threads'] += 1
    for (e, tok, d, ev) in tl.items:
        if not ev or tok < THRESHOLDS[0]:
            continue
        entry = tok * tl.write[e] * tl.wt[e]
        carry = tok * (tl.cr[d] - tl.cr[min(e + 1, d)])
        for t in THRESHOLDS:
            if tok >= t:
                a['outputs>=%d' % t] += 1
                a['tokens>=%d' % t] += tok
                a['usd>=%d' % t] += entry + carry
        if tok > CAP and tl.wt[e]:
            per_tok = (entry + carry) / tok
            excess = tok - CAP
            saving = excess * per_tok
            extra_call = tl.ctx[e] * tl.read[e]
            a['capped_outputs'] += 1
            a['excess_tokens'] += excess
            a['excess_usd'] += saving
            a['net_usd'] += (1 - NEED_RATE) * saving - NEED_RATE * extra_call


def report(agg) -> dict:
    out = {}
    net = 0.0
    n_out = 0
    where = []
    for vendor in ('claude', 'codex'):
        a = agg.get('aud_big:' + vendor)
        if not a:
            continue
        sec = {'basis': 'estimated', 'threads': int(a.get('threads', 0))}
        for t in THRESHOLDS:
            sec['outputs_over_%dk' % (t // 1000)] = int(a.get('outputs>=%d' % t, 0))
            sec['tokens_over_%dk' % (t // 1000)] = round(a.get('tokens>=%d' % t, 0.0))
            sec['usd_over_%dk' % (t // 1000)] = round(a.get('usd>=%d' % t, 0.0), 6)
        out[vendor + '_tool_outputs'] = sec
        out[vendor + '_cap_at_%dk' % (CAP // 1000)] = {
            'basis': 'modeled', 'outputs': int(a.get('capped_outputs', 0)),
            'excess_tokens': round(a.get('excess_tokens', 0.0)),
            'excess_usd': round(a.get('excess_usd', 0.0), 6),
            'need_rate': NEED_RATE,
            'net_usd': round(a.get('net_usd', 0.0), 6)}
        if a.get('net_usd', 0.0) > 0:     # the cap is applied only where it pays
            net += a['net_usd']
            n_out += int(a.get('capped_outputs', 0))
            where.append(vendor)
    out['fix'] = None
    if net > 0:
        out['fix'] = {'basis': 'modeled', 'id': 'cap_large_tool_outputs', 'net_usd': round(net, 6),
                      'outputs': n_out, 'cap_tokens': CAP, 'need_rate': NEED_RATE, 'vendors': where,
                      'clears_breakeven': True, 'upper_bound': False}
    return out
