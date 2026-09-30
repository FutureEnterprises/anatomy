"""Where the input dollars went.

Claude Code (content carry): every item that enters context (tool result,
thinking, text, attachment, prompt, boot prefix) is charged its entry cost on
the call where it first appears and a carry cost on every later call while it
stays in context, at that call's per-token price of the carried prefix. Cache
rewrites of an already-seen prefix are split out as rebuild excess and
classified by cause (the rebuild taxonomy):

  after_refusal_fallback            the previous call fell back to another model
  model_switch                      the model changed between calls
  ttl_expired_idle_gap              the gap exceeded the thread's cache TTL
  idle_gap_5m_segment_on_1h_thread  a >5 min gap on a thread that writes 1h entries
  context_shrink_or_edit            the context got smaller (edits, rewinds)
  prefix_mutation_unexplained       none of the above

Codex (context rent): each category's tokens times the calls they stay
resident for, until the window ends at compaction; reasoning stays resident
only to the end of its turn.

Token sizes of new items are estimated from characters (measured chars per
token) and scaled so that they sum to the observed context growth.
"""
from __future__ import annotations

from .ingest.claude import ctx
from .prices import AnthropicPrices, OpenAIPrices

# chars per token, measured on 10,278 clean call pairs (new tokenizer); old tokenizer derived as 1.3x
NEW_TOKENIZER = ('claude-opus-4-7', 'claude-opus-4-8', 'claude-opus-5', 'claude-fable', 'claude-sonnet-5')
CPT_NEW = 2.40
CPT_OLD = 3.10
CODEX_CPT = 4.0

REBUILD_CAUSES = ('after_refusal_fallback', 'model_switch', 'ttl_expired_idle_gap',
                  'idle_gap_5m_segment_on_1h_thread', 'context_shrink_or_edit', 'prefix_mutation_unexplained')


def cpt(model) -> float:
    return CPT_NEW if model and model.startswith(NEW_TOKENIZER) else CPT_OLD


def tr_cat(tu) -> str:
    if tu is None:
        return 'tool_result:unknown'
    if tu['bash_kind'] is not None:
        return 'tool_result:Bash:' + tu['bash_kind']
    return 'tool_result:' + tu['cat']


def claude_group(cat: str) -> str:
    """Roll a fine category up to the reporting group."""
    if cat.startswith('cache_rebuild_excess:'):
        return 'cache_rebuild:' + cat.split(':', 1)[1]
    if cat.startswith('tool_result'):
        return 'tool_result'
    if cat.startswith('assistant:tool_input'):
        return 'assistant:tool_input'
    if cat.startswith('attachment:'):
        return 'attachment'
    return cat


def claude_thread(th: dict, prices: AnthropicPrices, call_cost: list, agg) -> None:
    """Attribute one thread's served input cost (list price). Adds to agg['claude_attr_*'] and agg['claude_rebuild_*']."""
    calls = th['calls']
    n = len(calls)
    if not n:
        return
    tool_uses = th['tool_uses']
    Qss = [0.0] * n
    tail_price_list = [0.0] * n
    items = []    # [cat, entry_call, tokens, F_at_entry, death, entry_usd_override]
    alive = []
    Fcur = 1.0
    prev_ctx = 0
    ttl1h = sum(c['cc1'] for c in calls) > sum(c['cc5'] for c in calls)
    ttl = 3600 if ttl1h else 300
    entry = agg['claude_attr_entry_usd']
    # calls copied from another file carry weight 0: their context still flows into later
    # calls, but their own cost was billed and attributed in the file they came from
    w = [0.0 if c.get('copied') else 1.0 for c in calls]
    for j, c in enumerate(calls):
        m = c['model']
        r = prices.rates(m, c.get('speed'), c.get('geo'))
        base, w5, w1, rd = (r.input * 1e6, r.cache_write_5m * 1e6, r.cache_write_1h * 1e6, r.cache_read * 1e6) if r else (0, 0, 0, 0)
        cx = ctx(c)
        pre = c['pre']
        marker = any(i['kind'] == 'compact_marker' for i in pre)
        compact = marker
        new = []
        by_call: dict = {}
        for it in pre:
            k = it['kind']
            if k == 'compact_marker':
                continue
            if k == 'assistant_out':
                by_call.setdefault(it['call'], []).append(it)
                continue
            est = it['chars'] / cpt(m) + it.get('img_tok', 0)
            if k == 'tool_result':
                new.append([tr_cat(tool_uses.get(it['tid'])), est])
            elif k == 'attachment':
                new.append(['attachment:' + str(it.get('sub')), est])
            else:
                new.append([k, est])
        for ci, its in by_call.items():
            pc = calls[ci]
            vis = [i for i in its if i['sub'] != 'thinking']
            thk = [i for i in its if i['sub'] == 'thinking']
            vis_est = sum(i['chars'] for i in vis) / cpt(pc['model'])
            if pc['think_tok'] is not None:
                think_total = pc['think_tok']
                vis_total = max(pc['out'] - think_total, 0)
            else:
                vis_total = min(vis_est, pc['out']) if thk else pc['out']
                think_total = pc['out'] - vis_total
            if think_total > 0:
                new.append(['assistant:thinking', float(think_total)])
            if vis_total > 0:
                tot_chars = sum(i['chars'] for i in vis)
                for i in vis:
                    share = (i['chars'] / tot_chars) if tot_chars else 1.0 / max(len(vis), 1)
                    cat = 'assistant:text' if i['sub'] == 'text' else 'assistant:tool_input:' + str(i.get('tool'))
                    new.append([cat, vis_total * share])
                if not vis:
                    new.append(['assistant:text', float(vis_total)])
        S = sum(x[1] for x in new)
        in_cost = sum(call_cost[j][:4])
        cc = c['cc']
        pw = ((c['cc5'] * w5 + c['cc1'] * w1) / (c['cc5'] + c['cc1'])) if (c['cc5'] + c['cc1']) else w5
        if j == 0 or compact:
            tail = cx
        else:
            delta = cx - prev_ctx
            tail = delta if delta >= 0 else min(S, cx)
        tail_cost, old_tok, old_cost, rewrite, tail_price = _split(tail, cx, cc, c, pw, base, rd, in_cost)
        tail_price_list[j] = tail_price
        # shrink handling (context edits, dropped thinking after a model switch, rewinds)
        if j > 0 and not compact and cx - prev_ctx < 0:
            f = (old_tok / prev_ctx) if prev_ctx > 0 else 0.0
            agg['claude_shrink']['events'] += w[j]
            if f < 0.05:
                # near-total replacement: a reset, so carry is not lost to underflow
                compact = True
                tail = cx
                tail_cost, _, _, _, tail_price = _split(tail, cx, cc, c, pw, base, rd, in_cost)
                old_tok, old_cost, rewrite = 0, 0.0, 0
                tail_price_list[j] = tail_price
                agg['claude_shrink']['near_total_resets'] += w[j]
            else:
                Fcur *= f
        rebuild_excess = 0.0
        is_rebuild = j > 0 and not compact and rewrite > 2000 and rewrite > 0.1 * max(old_tok, 1)
        if is_rebuild:
            rebuild_excess = min(rewrite * max(pw - rd, 0.0) / 1e6, old_cost)
        opss = ((old_cost - rebuild_excess) / old_tok) if old_tok > 0 else 0.0
        Qss[j] = (Qss[j - 1] if j else 0.0) + opss * Fcur * w[j]
        agg['claude_attr_check']['input_usd'] += in_cost
        if compact and j > 0:
            for itm in alive:
                itm[4] = j
            alive = []
        if j == 0:
            itm = ['boot_prefix', 0, float(cx), Fcur, None, in_cost]
            items.append(itm)
            alive.append(itm)
        elif compact:
            itm = ['post_compact_context' if marker else 'post_reset_context', j, float(cx), Fcur, None, in_cost]
            items.append(itm)
            alive.append(itm)
        else:
            scale = min(1.0, tail / S) if S > 0 else 0.0
            for cat, est in new:
                itm = [cat, j, est * scale, Fcur, None, None]
                items.append(itm)
                alive.append(itm)
            un = tail - S * scale
            if un > 0:
                ucat = 'unattributed_growth'
                if any(x[0] == 'tool_result:ToolSearch' for x in new):
                    ucat = 'tool_result:ToolSearch:loaded_tool_schemas'
                itm = [ucat, j, float(un), Fcur, None, None]
                items.append(itm)
                alive.append(itm)
        if is_rebuild and w[j]:
            gap = (c['ts'] - calls[j - 1]['ts']) if (c['ts'] and calls[j - 1]['ts']) else 0
            if calls[j - 1]['iters']:
                cause = 'after_refusal_fallback'
            elif m != calls[j - 1]['model']:
                cause = 'model_switch'
            elif gap > ttl:
                cause = 'ttl_expired_idle_gap'
            elif ttl == 3600 and gap > 300:
                cause = 'idle_gap_5m_segment_on_1h_thread'
            elif cx - prev_ctx < 0:
                cause = 'context_shrink_or_edit'
            else:
                cause = 'prefix_mutation_unexplained'
            entry['cache_rebuild_excess:' + cause] += rebuild_excess
            agg['claude_rebuild_events'][cause] += 1
            agg['claude_rebuild_tokens'][cause] += rewrite
            agg['claude_rebuild_usd'][cause] += rebuild_excess
        prev_ctx = cx
    for itm in alive:
        itm[4] = n
    carry = agg['claude_attr_carry_usd']
    tokens = agg['claude_attr_tokens']
    count = agg['claude_attr_items']
    for cat, e, T, Fe, d, eo in items:
        entry[cat] += eo if eo is not None else T * tail_price_list[e] * w[e]
        carry[cat] += (T / Fe) * (Qss[d - 1] - Qss[e]) if (Fe > 0 and d - 1 > e) else 0.0
        tokens[cat] += T
        count[cat] += 1


def _split(tail, cx, cc, c, pw, base, rd, in_cost):
    """Split a call's input into the new tail (priced write-first, then uncached, then read) and the carried prefix."""
    tail_cc = min(tail, cc)
    rem = tail - tail_cc
    tail_in = min(rem, c['in'])
    rem -= tail_in
    tail_cr = min(rem, c['cr'])
    tail_cost = (tail_cc * pw + tail_in * base + tail_cr * rd) / 1e6
    old_tok = cx - tail
    old_cost = max(in_cost - tail_cost, 0.0)
    rewrite = cc - tail_cc
    tail_price = tail_cost / tail if tail > 0 else 0.0
    return tail_cost, old_tok, old_cost, rewrite, tail_price


# ---------------------------------------------------------------- Codex
def codex_group(cat: str) -> str:
    return 'tool_output' if cat.startswith('tool_output:') else cat


def codex_session(sess: dict, prices: OpenAIPrices, agg) -> None:
    """Context rent for one session. Adds to agg['codex_rent_*'].

    Calls copied from another file count as residency slots of weight 0: content they
    carried is still resident in later calls, but the copied calls themselves were
    billed in the file they came from.
    """
    live = sess['live']
    ncalls = len(live)
    w = [0 if c.get('copied') else 1 for c in live]
    agg['codex_rent_check']['input_token_calls'] += sum(c['u'][0] for c, wi in zip(live, w) if wi)
    if not ncalls:
        return
    cum = [0]
    for x in w:
        cum.append(cum[-1] + x)

    def slots(a, b):
        """Billed calls in [a, b)."""
        return cum[min(b, ncalls)] - cum[min(a, ncalls)]

    rent = agg['codex_rent_token_calls']
    rent_usd = agg['codex_rent_usd']
    rent_cwt = agg['codex_rent_cwt']
    win_end = {}
    win_start = {}
    turn_end = {}
    for i, c in enumerate(live):
        win_end[c['window']] = i + 1
        win_start.setdefault(c['window'], i)
        turn_end[(c['window'], c['turn'])] = i + 1

    def pr(pl):
        # carried tokens are priced as warm cache hits at the standard (short-context) rates
        r = prices.rates(live[pl]['model']) if pl < ncalls else None
        return (r.input * 1e6, r.cached_input * 1e6) if r else (1.0, 0.1)

    def add(cat, T, a, b):
        """T tokens resident from call a (their entry) to call b (exclusive)."""
        R = slots(a, b)
        if T <= 0 or R <= 0:
            return
        pin, pc = pr(a)
        first = w[a] if a < ncalls else 0
        rent[cat] += T * R
        rent_usd[cat] += (T * pin * first + T * (R - first) * pc) / 1e6
        rent_cwt[cat] += T * (first + (pc / pin) * (R - first))

    for win, s in win_start.items():
        R = slots(s, win_end[win])
        inp, ca = live[s]['u'][0], live[s]['u'][1]
        cat = 'prefix_first_window' if s == 0 else 'post_compaction_baseline'
        pin, pc = pr(s)
        rent[cat] += inp * R
        rent_usd[cat] += (((inp - ca) * pin + ca * pc) * w[s] + inp * (R - w[s]) * pc) / 1e6
        rent_cwt[cat] += ((inp - ca) + ca * pc / pin) * w[s] + inp * (R - w[s]) * pc / pin
    for i, c in enumerate(live):
        inp, ca, wr, ou, re_ = c['u']
        add('model_visible_output', ou - re_, i + 1, win_end[c['window']])
        # reasoning is carried within a turn and largely dropped at turn boundaries
        add('reasoning_carried', re_, i + 1, turn_end[(c['window'], c['turn'])])
    for r in sess['tools']:
        pl = r['pl']
        if pl >= ncalls or pl == win_start.get(live[pl]['window']):
            continue
        end = win_end[live[pl]['window']]
        if r['cmds']:
            for c in r['cmds']:
                add('tool_output:' + c['kind'], c.get('vis', 0) / CODEX_CPT, pl, end)
        else:
            add('tool_output:' + r['tool'], r['out_chars'] / CODEX_CPT, pl, end)
    for (pl, n, kind) in sess['inj']:
        if pl >= ncalls or pl == win_start.get(live[pl]['window']):
            continue
        add('message:' + kind, n / CODEX_CPT, pl, win_end[live[pl]['window']])
