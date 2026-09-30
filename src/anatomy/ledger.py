"""The bill, rebuilt from every billed attempt.

Claude Code: one line per message id (the served attempt, from top-level usage)
plus one line per declined fallback attempt found in `usage.iterations`. Cache
writes are priced per tier (5-minute and 1-hour). A declined attempt that had
streamed output before the decline is billed; one declined before any output is
reported separately as "maybe billed" and kept out of the total.

Codex: one line per deduplicated token_count event, at long-context rates when
the request is over the threshold, plus compaction requests that appear only as
token_usage_record lines.

All results are plain dicts of numbers keyed by fixed labels, so they merge by
addition across worker processes.
"""
from __future__ import annotations

import collections

from .prices import AnthropicPrices, OpenAIPrices

TIERS = ('input', 'cache_write_5m', 'cache_write_1h', 'cache_read', 'output')


def new_agg():
    return collections.defaultdict(collections.Counter)


def merge(into, other):
    for k, v in other.items():
        into[k].update(v)
    return into


# ---------------------------------------------------------------- Claude
def claude_attempt_cost(rates, inp, cc5, cc1, cc, cr, out):
    """(input, cache_write_5m, cache_write_1h, cache_read, output) in USD.

    When a record reports cache_creation_input_tokens without a tier split, the
    writes are priced as 5-minute writes.
    """
    if rates is None:
        return (0.0, 0.0, 0.0, 0.0, 0.0)
    if cc5 + cc1 == 0 and cc > 0:
        cc5 = cc
    return (inp * rates.input, cc5 * rates.cache_write_5m, cc1 * rates.cache_write_1h,
            cr * rates.cache_read, out * rates.output)


def claude_thread(thread: dict, kind: str, prices: AnthropicPrices, agg) -> list:
    """Add one thread's calls to agg. Returns the served-attempt cost tuple per call."""
    calls = thread['calls']
    costs = []
    if any(not c.get('copied') for c in calls):
        agg['claude_threads']['with_calls'] += 1
    for c in calls:
        if c.get('copied'):
            costs.append((0.0, 0.0, 0.0, 0.0, 0.0))   # billed in the file it was copied from
            continue
        m = c['model']
        rates = prices.rates(m, c.get('speed'), c.get('geo'))
        cs = claude_attempt_cost(rates, c['in'], c['cc5'], c['cc1'], c['cc'], c['cr'], c['out'])
        costs.append(cs)
        mk = m or 'unknown'
        agg['claude_calls_by_model'][mk] += 1
        agg['claude_calls_by_kind'][kind] += 1
        if rates is None:
            agg['claude_unpriced']['calls'] += 1
            agg['claude_unpriced']['tokens'] += c['in'] + c['cc'] + c['cr'] + c['out']
        if c.get('speed') == 'fast':
            agg['claude_modifiers']['fast_calls'] += 1
        if c.get('geo') == 'us':
            agg['claude_modifiers']['us_geo_calls'] += 1
        toks = (c['in'], c['cc5'] if (c['cc5'] + c['cc1']) else c['cc'], c['cc1'], c['cr'], c['out'])
        for t, v, usd in zip(TIERS, toks, cs):
            agg['claude_tok_tier'][t] += v
            agg['claude_usd_tier'][t] += usd
            agg['claude_usd_model:' + mk][t] += usd
            agg['claude_usd_kind:' + kind][t] += usd
        agg['claude_usd']['served'] += sum(cs)
        if c['iters']:
            agg['claude_fallback']['calls_with_fallback'] += 1
            for it in c['iters']:
                if it['type'] != 'message':
                    continue   # 'fallback_message' is the served attempt, already billed above
                im = it['model'] or m
                dc = sum(claude_attempt_cost(prices.rates(im), it['in'], it['cc5'], it['cc1'], it['cc'], it['cr'], it['out']))
                if it['out'] > 0:
                    agg['claude_usd']['declined_billed'] += dc
                    agg['claude_fallback']['declined_attempts_billed'] += 1
                else:
                    agg['claude_usd']['declined_maybe_billed'] += dc
                    agg['claude_fallback']['declined_attempts_pre_output'] += 1
                agg['claude_fallback']['declined_cache_write_tokens'] += it['cc']
                agg['claude_fallback_declined_model'][im or 'unknown'] += 1
    return costs


# ---------------------------------------------------------------- Codex
def codex_call_cost(prices: OpenAIPrices, model, inp, ca, wr, out):
    r = prices.rates(model, inp)
    if r is None:
        return None
    unc = max(0, inp - ca - wr)
    return unc * r.input + ca * r.cached_input + wr * r.cache_write + out * r.output


def codex_session(sess: dict, prices: OpenAIPrices, agg) -> list:
    """Add one session's calls to agg. Returns the cost per live call (None when unpriced)."""
    live = sess['live']
    costs = []
    for c in live:
        if c.get('copied'):
            costs.append(None)   # billed in the file it was copied from
            continue
        inp, ca, wr, ou, re_ = c['u']
        m = c['model']
        cc = codex_call_cost(prices, m, inp, ca, wr, ou)
        costs.append(cc)
        mk = m or 'unknown'
        agg['codex_calls_by_model'][mk] += 1
        agg['codex_tok']['input'] += inp
        agg['codex_tok']['cached_input'] += ca
        agg['codex_tok']['cache_write'] += wr
        agg['codex_tok']['output'] += ou
        agg['codex_tok']['reasoning'] += re_
        if inp > prices.long_threshold:
            agg['codex_long_context']['calls_over_threshold'] += 1
            if prices.is_long_context(m, inp):
                agg['codex_long_context']['calls_at_long_rates'] += 1
        if cc is None:
            agg['codex_unpriced']['calls'] += 1
            agg['codex_unpriced']['tokens'] += inp + ou
            continue
        r = prices.rates(m, inp)
        agg['codex_usd']['token_count_calls'] += cc
        agg['codex_usd_model'][mk] += cc
        agg['codex_usd_part']['cached_in'] += ca * r.cached_input
        agg['codex_usd_part']['uncached_in'] += max(0, inp - ca - wr) * r.input + wr * r.cache_write
        agg['codex_usd_part']['out_reasoning'] += re_ * r.output
        agg['codex_usd_part']['out_visible'] += (ou - re_) * r.output
    # token_usage_record responses with no matching token_count call (compaction requests)
    for r in sess['recs']:
        if r['match'] is not None:
            continue
        ri, rc, ro, rre, rw = r['u']
        rm = r['model']
        agg['codex_compaction']['requests'] += 1
        agg['codex_compaction']['input'] += ri
        agg['codex_compaction']['output'] += ro
        agg['codex_usd']['compaction_requests'] += codex_call_cost(prices, rm, ri, rc, rw, ro) or 0.0
    agg['codex_calls']['live'] += sum(1 for c in live if not c.get('copied'))
    agg['codex_calls']['synthetic_without_response_items'] += sess['synthetic']
    agg['codex_calls']['responses_without_usage'] += sess['responses_without_usage']
    agg['codex_polls']['calls'] += sess['polls']
    agg['codex_polls']['polls_no_input'] += sess['polls_no_input']
    agg['codex_polls']['polls_no_new_output'] += sess['polls_no_new_output']
    return costs
