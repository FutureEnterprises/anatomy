"""Boot scope: what a subagent or workflow agent pays to start, against what its task adds.

Claude Code: the boot prefix is the whole prompt of the thread's first call. The
visible part of it (the task prompt and attachments recorded in the transcript)
is sized from characters; the rest is the harness (system prompt and in-context
tool schemas), which transcripts do not record item by item. The skill listing
and the deferred-tool listing are recorded as attachments, so their size is
known, and whether the thread ever used them is known (a Skill or ToolSearch
call). A listing the thread never used is the measurable part of "unused
schemas" at boot.

Codex: the first call's input of each session, by session source, against the
growth that came after it. Codex does not record the boot items, so there is
no listing split and no fix.

Dollars are each call's input cost times the share of its prompt the boot part
occupies (estimated). The fix is modeled: dropping listings a thread never used
changes nothing else in its prompt, so there is no rewrite to pay back.
"""
from __future__ import annotations

from ..attribute import cpt
from ..ingest.claude import ctx

LISTINGS = {'skill_listing': 'Skill', 'deferred_tools_delta': 'ToolSearch', 'mcp_instructions_delta': 'mcp'}
AGENT_KINDS = ('subagent', 'workflow_agent')


def claude(th: dict, kind: str, costs: list, agg) -> None:
    if kind not in AGENT_KINDS:
        return
    calls = th['calls']
    c0 = calls[0]
    if c0.get('copied'):
        return
    a = agg['aud_boot']
    m = c0['model']
    boot = ctx(c0)
    listing = {k: 0.0 for k in LISTINGS}
    task = 0.0
    for it in c0['pre']:
        k = it['kind']
        if k in ('assistant_out', 'compact_marker'):
            continue
        est = it['chars'] / cpt(m) + it.get('img_tok', 0)
        if k == 'attachment' and it.get('sub') in LISTINGS:
            listing[it['sub']] += est
        else:
            task += est
    lst = sum(listing.values())
    harness = max(boot - task - lst, 0.0)
    shared = harness + lst
    growth = 0.0
    for j in range(1, len(calls)):
        growth += max(ctx(calls[j]) - ctx(calls[j - 1]), 0)
    task_unique = task + growth
    used = {tu['cat'] for tu in th['tool_uses'].values()}
    a['threads'] += 1
    a['threads:' + kind] += 1
    a['boot_tokens'] += boot
    a['harness_tokens'] += harness
    a['listing_tokens'] += lst
    a['task_prompt_tokens'] += task
    a['task_unique_tokens'] += task_unique
    if shared > task_unique:
        a['threads_boot_prefix_over_task_unique'] += 1
    input_usd = shared_usd = 0.0
    lusd = {k: 0.0 for k in LISTINGS}
    for j, c in enumerate(calls):
        if c.get('copied'):
            continue
        cin = sum(costs[j][:4])
        cx = ctx(c)
        if cx <= 0:
            continue
        input_usd += cin
        shared_usd += cin * min(1.0, shared / cx)
        for k, v in listing.items():
            if v:
                lusd[k] += cin * min(1.0, v / cx)
    a['input_usd'] += input_usd
    a['boot_prefix_usd'] += shared_usd
    for k, tool in LISTINGS.items():
        if not listing[k]:
            continue
        a['threads_with:' + k] += 1
        a['tokens:' + k] += listing[k]
        a['usd:' + k] += lusd[k]
        if tool not in used:
            a['threads_unused:' + k] += 1
            a['unused_tokens:' + k] += listing[k]
            a['unused_usd:' + k] += lusd[k]
    agg['aud_boot_tools_used'][str(min(len(used), 10))] += 1


def codex(s: dict, prices, agg) -> None:
    live = s['live']
    if not live or live[0].get('copied'):
        return
    src = str(s['meta'].get('source'))
    a = agg['aud_boot_codex:' + src]
    boot = live[0]['u'][0]
    growth = 0.0
    for i in range(1, len(live)):
        growth += max(live[i]['u'][0] - live[i - 1]['u'][0], 0)
    a['sessions'] += 1
    a['boot_tokens'] += boot
    a['task_unique_tokens'] += growth
    if boot > growth:
        a['sessions_boot_over_task_unique'] += 1
    for c in live:
        if c.get('copied'):
            continue
        inp, ca = c['u'][0], c['u'][1]
        r = prices.rates(c['model'], inp)
        if r is None or inp <= 0:
            continue
        cin = max(inp - ca, 0) * r.input + ca * r.cached_input
        a['input_usd'] += cin
        a['boot_prefix_usd'] += cin * min(1.0, boot / inp)


def report(agg) -> dict:
    a = agg.get('aud_boot') or {}
    listings = {}
    unused_usd = 0.0
    unused_tok = 0.0
    for k in LISTINGS:
        if not a.get('threads_with:' + k):
            continue
        listings[k] = {'threads_with': int(a['threads_with:' + k]), 'tokens': round(a['tokens:' + k]),
                       'usd': round(a['usd:' + k], 6),
                       'threads_never_used': int(a.get('threads_unused:' + k, 0)),
                       'unused_tokens': round(a.get('unused_tokens:' + k, 0.0)),
                       'unused_usd': round(a.get('unused_usd:' + k, 0.0), 6)}
        unused_usd += a.get('unused_usd:' + k, 0.0)
        unused_tok += a.get('unused_tokens:' + k, 0.0)
    th = int(a.get('threads', 0))
    out = {
        'claude_agents': {
            'basis': 'estimated',
            'threads': th,
            'threads_by_kind': {k: int(a.get('threads:' + k, 0)) for k in AGENT_KINDS},
            'boot_tokens': round(a.get('boot_tokens', 0.0)),
            'harness_tokens': round(a.get('harness_tokens', 0.0)),
            'listing_tokens': round(a.get('listing_tokens', 0.0)),
            'task_prompt_tokens': round(a.get('task_prompt_tokens', 0.0)),
            'task_unique_tokens': round(a.get('task_unique_tokens', 0.0)),
            'threads_boot_prefix_over_task_unique': int(a.get('threads_boot_prefix_over_task_unique', 0)),
            'input_usd': round(a.get('input_usd', 0.0), 6),
            'boot_prefix_usd': round(a.get('boot_prefix_usd', 0.0), 6),
            'boot_prefix_share_of_input': round(a['boot_prefix_usd'] / a['input_usd'], 4) if a.get('input_usd') else None,
            'listings': listings,
        },
        'claude_agents_distinct_tools_used': {
            'basis': 'observed',
            'threads_by_count': {k: int(v) for k, v in sorted((agg.get('aud_boot_tools_used') or {}).items(), key=lambda kv: int(kv[0]))},
        },
    }
    for key in sorted(k for k in agg if k.startswith('aud_boot_codex:')):
        c = agg[key]
        out['codex_' + key.split(':', 1)[1] + '_sessions'] = {
            'basis': 'estimated',
            'sessions': int(c.get('sessions', 0)),
            'boot_tokens': round(c.get('boot_tokens', 0.0)),
            'task_unique_tokens': round(c.get('task_unique_tokens', 0.0)),
            'sessions_boot_over_task_unique': int(c.get('sessions_boot_over_task_unique', 0)),
            'input_usd': round(c.get('input_usd', 0.0), 6),
            'boot_prefix_usd': round(c.get('boot_prefix_usd', 0.0), 6),
        }
    out['fix'] = None
    if unused_usd > 0:
        out['fix'] = {'basis': 'modeled', 'id': 'boot_listings_on_demand', 'net_usd': round(unused_usd, 6),
                      'tokens': round(unused_tok), 'clears_breakeven': True, 'upper_bound': False}
    return out
