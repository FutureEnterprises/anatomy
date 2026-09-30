"""The break-even rule for context edits, per model and cache tier.

Removing b tokens from a cached prompt saves their cache read on every later call
they would have stayed in context for (L calls at read price r). It costs a
rewrite of everything after the earliest removed token (the suffix, S tokens) at
the write price w of the tier the suffix is written to, instead of a read:

    evicting b tokens pays only if  b x L x r  >  S x (w - r)  +  expected re-fetch cost

Divided through by b x r: the batch pays back after (S / b) x R calls, where
R = (w - r) / r is the model's write/read ratio. R is 11.5 on most 5-minute
Claude rows and 49 (5 minutes) or 79 (1 hour) on Fable 5.1, so the same edit
can pay on one model and lose money on another.

This module holds the rule, the ratio table read from the price snapshots, and
a replay that finds the user's own eviction opportunities in their transcripts
and scores each one at the prices that call actually paid. The replay is the
method of the launch analysis (lease eviction of tool outputs of 500 or more
tokens once they are 3 calls old, in batches of 30K or more tokens), ported to
the deduplicated ledger: calls copied into resumed or forked files keep their
place in the timeline but are never scored twice. One correction over that
analysis: the suffix is measured on the counterfactual context, net of tokens
the same replay already evicted.

Everything here is modeled: it is a counterfactual on observed usage, and
re-fetch cost is an explicit assumption (zero by default, an upper bound). Each
result also carries its break-even re-fetch rate: the share of evicted tokens
that could come back, at worst-case timing, before the policy stops paying.
"""
from __future__ import annotations

import heapq
import math

from .attribute import cpt, tr_cat
from .ingest.claude import ctx

LOADED_SCHEMAS = 'tool_result:ToolSearch:loaded_tool_schemas'


# ---------------------------------------------------------------- the rule
def ratio(w: float, r: float) -> float:
    """R = (w - r) / r: calls of read savings needed per token rewritten."""
    return (w - r) / r if r > 0 else math.inf


def margin(b: float, L: float, S: float, w: float, r: float, refetch_usd: float = 0.0) -> float:
    """b x L x r - S x (w - r) - refetch, in the currency of w and r (USD per token gives USD)."""
    return b * L * r - S * (w - r) - refetch_usd


def pays(b: float, L: float, S: float, w: float, r: float, refetch_usd: float = 0.0) -> bool:
    return margin(b, L, S, w, r, refetch_usd) > 0


def payback_calls(b: float, S: float, w: float, r: float) -> float:
    """Later calls the evicted tokens must have stayed for, for the rewrite to pay back."""
    if b <= 0:
        return math.inf
    return (S / b) * ratio(w, r)


def claude_ratios(prices) -> dict:
    """{model: {'5m': R, '1h': R}} from the Anthropic snapshot (standard speed, global routing)."""
    out = {}
    for m, row in prices.models.items():
        rd = row['cache_read']
        out[m] = {'5m': round(ratio(row['cache_write_5m'], rd), 4), '1h': round(ratio(row['cache_write_1h'], rd), 4)}
    return out


def openai_ratios(prices) -> dict:
    """{model: {'rewrite_as_input': R, 'rewrite_as_cache_write': R or None}} from the OpenAI snapshot.

    Codex rollouts report no cache-write tokens, so a rewritten suffix bills as
    uncached input; the cache-write row is shown where the page lists one.
    """
    out = {}
    for m, row in prices.models.items():
        ci = row['cached_input']
        out[m] = {'rewrite_as_input': round(ratio(row['input'], ci), 4),
                  'rewrite_as_cache_write': round(ratio(row['cache_write'], ci), 4) if 'cache_write' in row else None}
    return out


def ratio_set(ap, op) -> tuple:
    """Distinct write/read ratios across both snapshots, sorted."""
    s = set()
    for v in claude_ratios(ap).values():
        s.update(v.values())
    for v in openai_ratios(op).values():
        s.update(x for x in v.values() if x is not None)
    return tuple(sorted(x for x in s if math.isfinite(x)))


def ratio_key(R: float) -> str:
    return 'R' + ('%g' % R)


# ---------------------------------------------------------------- timelines
class Timeline:
    """One thread's calls as the replay sees them.

    ctx[j]     input tokens of call j (the whole prompt)
    wt[j]      1 for a call billed in this file, 0 for one copied from another file
    read[j]    cache read price of call j, USD per token
    write[j]   price of rewriting a token on call j at its tier, USD per token
    items      (entry call, tokens, death call, evictable) in context order; an item is
               in context on calls entry .. death-1
    """
    __slots__ = ('n', 'ctx', 'wt', 'read', 'write', 'items', 'cw', 'cr')

    def __init__(self, ctx_list, wt, read, write, items):
        self.n = len(ctx_list)
        self.ctx, self.wt, self.read, self.write, self.items = ctx_list, wt, read, write, items
        cw, cr = [0.0], [0.0]
        for j in range(self.n):
            cw.append(cw[-1] + wt[j])
            cr.append(cr[-1] + wt[j] * read[j])
        self.cw, self.cr = cw, cr     # prefix sums: billed calls, billed read price


def claude_timeline(th: dict, prices) -> Timeline:
    """Items entering each call with token sizes estimated from characters and scaled to the
    observed context growth (as attribute.py does). Tool results are evictable; the boot prefix,
    post-compaction context, prompts and model outputs are not. A compaction marker or a
    near-total context reset kills every item in context."""
    calls = th['calls']
    tool_uses = th['tool_uses']
    n = len(calls)
    ttl1h = sum(c['cc1'] for c in calls) > sum(c['cc5'] for c in calls)
    ctx_list, wt, read, write = [], [], [], []
    items: list = []
    alive: list = []
    prev_ctx = 0
    for j, c in enumerate(calls):
        r = prices.rates(c['model'], c.get('speed'), c.get('geo'))
        cx = ctx(c)
        ctx_list.append(cx)
        wt.append(0 if c.get('copied') else 1)
        if r is None:
            read.append(0.0)
            write.append(0.0)
        else:
            one_h = c['cc1'] > c['cc5'] or (c['cc1'] == c['cc5'] == 0 and ttl1h)
            read.append(r.cache_read)
            write.append(r.cache_write_1h if one_h else r.cache_write_5m)
        pre = c['pre']
        marker = any(i['kind'] == 'compact_marker' for i in pre)
        new = []          # (tokens estimate, evictable)
        outs = set()
        for it in pre:
            k = it['kind']
            if k == 'compact_marker':
                continue
            if k == 'assistant_out':
                outs.add(it['call'])
                continue
            est = it['chars'] / cpt(c['model']) + it.get('img_tok', 0)
            ev = k == 'tool_result' and tr_cat(tool_uses.get(it['tid'])) != LOADED_SCHEMAS
            new.append((est, ev))
        for ci in sorted(outs):
            new.append((float(calls[ci]['out']), False))
        S = sum(x[0] for x in new)
        reset = marker
        if j > 0 and not marker and cx < prev_ctx:
            old = cx - min(S, cx)
            if prev_ctx > 0 and old / prev_ctx < 0.05:
                reset = True
        if j == 0 or reset:
            for itm in alive:
                itm[2] = j
            alive = []
            itm = [j, float(cx), None, False]
            items.append(itm)
            alive.append(itm)
        else:
            delta = cx - prev_ctx
            tail = delta if delta >= 0 else min(S, cx)
            scale = min(1.0, tail / S) if S > 0 else 0.0
            for est, ev in new:
                itm = [j, est * scale, None, ev]
                items.append(itm)
                alive.append(itm)
            un = tail - S * scale
            if un > 0:
                itm = [j, float(un), None, False]
                items.append(itm)
                alive.append(itm)
        prev_ctx = cx
    for itm in alive:
        itm[2] = n
    return Timeline(ctx_list, wt, read, write, [tuple(i) for i in items])


def codex_timeline(s: dict, prices) -> Timeline:
    """Codex calls: tool outputs (4 chars per token) are evictable until their window ends.
    A rewritten suffix bills as uncached input (rollouts report no cache writes)."""
    live = s['live']
    n = len(live)
    ctx_list, wt, read, write = [], [], [], []
    win_start, win_end = {}, {}
    for i, c in enumerate(live):
        win_start.setdefault(c['window'], i)
        win_end[c['window']] = i + 1
        inp = c['u'][0]
        ctx_list.append(inp)
        wt.append(0 if c.get('copied') else 1)
        r = prices.rates(c['model'], inp)
        read.append(r.cached_input if r else 0.0)
        write.append(r.input if r else 0.0)
    raw = []   # (entry, order, tokens, death, evictable)
    for w, st in win_start.items():
        raw.append((st, 0, float(live[st]['u'][0]), win_end[w], False))
    for i, c in enumerate(live):
        vis = c['u'][3] - c['u'][4]
        if vis > 0 and i + 1 < win_end[c['window']]:
            raw.append((i + 1, 1, float(vis), win_end[c['window']], False))
    for (pl, nch, _kind) in s['inj']:
        if pl < n and pl != win_start.get(live[pl]['window']):
            raw.append((pl, 2, nch / 4.0, win_end[live[pl]['window']], False))
    for r in s['tools']:
        pl = r['pl']
        if pl < n and pl != win_start.get(live[pl]['window']):
            raw.append((pl, 3, r['out_chars'] / 4.0, win_end[live[pl]['window']], True))
    raw.sort(key=lambda x: (x[0], x[1]))
    return Timeline(ctx_list, wt, read, write, [(e, t, d, ev) for e, _, t, d, ev in raw])


# ---------------------------------------------------------------- replay
class _Fenwick:
    __slots__ = ('t', 'm')

    def __init__(self, m):
        self.m = m
        self.t = [0.0] * (m + 1)

    def add(self, i, v):
        i += 1
        while i <= self.m:
            self.t[i] += v
            i += i & -i

    def psum(self, i):
        """Sum of entries [0, i)."""
        s = 0.0
        while i > 0:
            s += self.t[i]
            i -= i & -i
        return s


class Lease:
    """Evict tool outputs of min_tok+ tokens once they are K calls old, in batches of E+ tokens."""
    name = 'lease'

    def __init__(self, K=3, E=30_000, min_tok=500):
        self.K, self.E, self.min_tok = K, E, min_tok
        self.h, self.pool, self.pool_tok = [], {}, 0.0

    def enter(self, o, entry, tok, evictable):
        if evictable and tok >= self.min_tok:
            heapq.heappush(self.h, (entry + self.K, o, tok))

    def died(self, o, tok):
        if o in self.pool:
            self.pool_tok -= self.pool.pop(o)

    def choose(self, j, ctx_cf, dead):
        while self.h and self.h[0][0] <= j:
            _, o, tok = heapq.heappop(self.h)
            if not dead[o] and o not in self.pool:
                self.pool[o] = tok
                self.pool_tok += tok
        if self.pool_tok >= self.E:
            out = list(self.pool)
            self.pool, self.pool_tok = {}, 0.0
            return out
        return None


class Clearing:
    """Once the context reaches `trigger` tokens, clear every tool output except the `keep` most
    recent, but only when that frees at least `at_least` tokens."""
    name = 'clearing'

    def __init__(self, trigger=100_000, keep=3, at_least=20_000):
        self.trigger, self.keep, self.at_least = trigger, keep, at_least
        self.alive: list = []       # [o, tok] in entry order
        self.tot = 0.0
        self.dead_set: set = set()

    def enter(self, o, entry, tok, evictable):
        if evictable:
            self.alive.append([o, tok])
            self.tot += tok

    def died(self, o, tok):
        self.dead_set.add(o)

    def choose(self, j, ctx_cf, dead):
        if self.dead_set:
            self.alive = [x for x in self.alive if x[0] not in self.dead_set]
            self.tot = sum(x[1] for x in self.alive)
            self.dead_set = set()
        if ctx_cf < self.trigger or len(self.alive) <= self.keep:
            return None
        keep_tok = sum(x[1] for x in self.alive[-self.keep:]) if self.keep else 0.0
        if self.tot - keep_tok < self.at_least:
            return None
        out = [x[0] for x in (self.alive[:-self.keep] if self.keep else self.alive)]
        self.alive = self.alive[-self.keep:] if self.keep else []
        self.tot = keep_tok
        return out


def replay(tl: Timeline, policy) -> list:
    """Run a policy over one timeline. Returns one tuple per scored batch:
    (b, S, L_calls, read_life, w, r) where read_life is the sum of read prices over the billed
    calls the evicted tokens would have stayed for (token-weighted), and w, r are the write
    and read prices of the next call, which rewrites the suffix. Batches whose rewrite lands
    on a copied call are applied (so the timeline matches its original) but not scored."""
    items = tl.items
    m = len(items)
    fen = _Fenwick(m)
    by_entry: dict = {}
    by_death: dict = {}
    for o, (e, tok, d, ev) in enumerate(items):
        by_entry.setdefault(e, []).append(o)
        by_death.setdefault(d, []).append(o)
    dead = [False] * m
    evicted = [False] * m
    evicted_alive = 0.0
    out = []
    n = tl.n
    for j in range(n):
        for o in by_death.get(j, ()):
            tok = items[o][1]
            if evicted[o]:
                evicted_alive -= tok
            elif not dead[o]:
                fen.add(o, -tok)
            dead[o] = True
            policy.died(o, tok)
        for o in by_entry.get(j, ()):
            e, tok, d, ev = items[o]
            fen.add(o, tok)
            policy.enter(o, e, tok, ev)
        ctx_cf = tl.ctx[j] - evicted_alive
        chosen = policy.choose(j, ctx_cf, dead)
        if not chosen or j >= n - 1:
            continue
        chosen = [o for o in chosen if not dead[o] and not evicted[o]]
        if not chosen:
            continue
        first = min(chosen)
        prefix = fen.psum(first)
        b = sum(items[o][1] for o in chosen)
        S = max(0.0, ctx_cf - prefix - b)
        if tl.wt[j + 1] and b > 0:
            L = sum(items[o][1] * (tl.cw[items[o][2]] - tl.cw[j + 1]) for o in chosen) / b
            RL = sum(items[o][1] * (tl.cr[items[o][2]] - tl.cr[j + 1]) for o in chosen) / b
            out.append((b, S, L, RL, tl.write[j + 1], tl.read[j + 1]))
        for o in chosen:
            fen.add(o, -items[o][1])
            evicted[o] = True
            dead[o] = True
        evicted_alive += b
    return out


# ---------------------------------------------------------------- aggregation
def record(agg, key: str, batches: list, ratios: tuple = (), refetch_rate: float = 0.0) -> None:
    """Add scored batches to agg['be:' + key] (plain counters, so they merge by addition).

    At the user's prices each batch pays iff b x read_life > S x (w - r) + refetch. A
    re-fetched token is taken to come back on the very next call: its read savings are
    lost and it is written again, so refetch = refetch_rate x b x (read_life + w). That
    timing is the worst case, so a net at a given rate is a floor for that rate. For each
    reference ratio R the batch pays iff L x b / S > R (the launch analysis' test)."""
    a = agg['be:' + key]
    for b, S, L, RL, w, r in batches:
        refetch = refetch_rate * b * (RL + w)
        mg = b * RL - S * (w - r) - refetch
        a['batches'] += 1
        a['tokens'] += b
        a['saved_read_usd'] += b * RL
        a['rewrite_usd'] += S * (w - r)
        a['refetch_usd'] += refetch
        a['refetch_unit_usd'] += b * (RL + w)
        a['net_usd'] += mg
        if mg > 0:
            a['pay_batches'] += 1
            a['pay_tokens'] += b
            a['net_usd_paying_only'] += mg
        x = (L * b / S) if S > 0 else math.inf
        for R in ratios:
            if x > R:
                k = ratio_key(R)
                a['pay_batches@' + k] += 1
                a['pay_tokens@' + k] += b


def breakeven_refetch_rate(a) -> float:
    """The re-fetch rate at which the policy's net reaches zero, with every re-fetch on the next
    call (its savings lost and a write paid): p x sum(b x (read_life + w)). 0 when the policy
    loses even with no re-fetch."""
    unit = a.get('refetch_unit_usd', 0.0)
    net0 = a.get('net_usd', 0.0) + a.get('refetch_usd', 0.0)
    if unit <= 0 or net0 <= 0:
        return 0.0
    return round(net0 / unit, 4)


def share_paying(agg, key: str, ratios: tuple = ()) -> dict:
    """Share of the user's own eviction opportunities that pay back at their prices."""
    a = agg.get('be:' + key) or {}
    nb, tk = a.get('batches', 0), a.get('tokens', 0.0)
    out = {'batches': int(nb), 'tokens': round(tk),
           'share_batches_that_pay': round(a.get('pay_batches', 0) / nb, 4) if nb else None,
           'share_tokens_in_paying_batches': round(a.get('pay_tokens', 0.0) / tk, 4) if tk else None,
           'saved_read_usd': round(a.get('saved_read_usd', 0.0), 6),
           'rewrite_usd': round(a.get('rewrite_usd', 0.0), 6),
           'refetch_usd': round(a.get('refetch_usd', 0.0), 6),
           'net_usd': round(a.get('net_usd', 0.0), 6),
           'net_usd_if_only_paying_batches': round(a.get('net_usd_paying_only', 0.0), 6),
           'breakeven_refetch_rate': breakeven_refetch_rate(a)}
    if ratios:
        out['share_batches_that_pay_by_ratio'] = {
            ratio_key(R): (round(a.get('pay_batches@' + ratio_key(R), 0) / nb, 4) if nb else None) for R in ratios}
    return out
