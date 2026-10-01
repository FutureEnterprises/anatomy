"""anatomy: stream local Claude Code and Codex transcripts and print aggregates.

  scan    the bill, rebuilt from every billed attempt, and where the input dollars went
  audit   the scan plus the audits: break-even, boot scope, keep-alive, polls, oversized
          outputs and batched clearing, each with at most one fix
  card    a numbers-only card: top three fixes that clear break-even at your prices and
          the one evaluated trick that would have cost you money (text, JSON or SVG)
  coach-baseline
          a personal correction-streak baseline for the EMILIA Session Coach (coach.py)

Nothing is sent anywhere, except that `coach-baseline --classify-with-claude`, given
its consent flag, sends prompts to the user's own Claude account through the local
claude CLI. Output passes the privacy gate (privacy.py) before it is printed or
written: numbers and fixed labels only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from multiprocessing import Pool

from . import __version__, attribute, audits, breakeven, coach, ledger
from . import card as card_mod
from .ingest import claude as claude_ingest
from .ingest import codex as codex_ingest
from .prices import AnthropicPrices, OpenAIPrices
from .privacy import PrivacyError, assert_clean, gate

# ---------------------------------------------------------------- workers
_STATE: dict = {}


def _init(anthropic_path, openai_path, audit=False):
    _STATE['ap'] = AnthropicPrices(anthropic_path)
    _STATE['op'] = OpenAIPrices(openai_path)
    _STATE['audit'] = audit
    _STATE['ratios'] = breakeven.ratio_set(_STATE['ap'], _STATE['op'])


def _claude_file(job):
    """-> (path, agg, (last_ts, message-id digests)) for one transcript."""
    path, kind, until, copied = job
    agg = ledger.new_agg()
    agg['claude_files'][kind] += 1
    try:
        th = claude_ingest.read_thread(path, until, copied)
    except Exception as e:   # never echo content; the type name is enough
        agg['claude_errors'][type(e).__name__[:40]] += 1
        return path, agg, (None, [])
    if th['stopped_at_until'] and th['first_ts'] is None:
        # the file did not exist yet at the cutoff
        agg = ledger.new_agg()
        agg['claude_files_after_until']['files'] += 1
        return path, agg, (None, [])
    info = (th['last_ts'], th['ids'])
    if not th['calls']:
        agg['claude_files'][kind + ':no_calls'] += 1
        return path, agg, info
    ap = _STATE['ap']
    costs = ledger.claude_thread(th, kind, ap, agg)
    attribute.claude_thread(th, ap, costs, agg)
    if _STATE.get('audit'):
        audits.claude_thread(th, kind, ap, costs, agg, _STATE['ratios'])
    return path, agg, info


def read_codex(path, until=None, copied=None):
    """codex ingest read_session(), also returning the event list the polls audit links against.
    Kept equal to read_session() by a test."""
    ev, meta = codex_ingest.scan(path, until)
    s = codex_ingest.fold(ev)
    codex_ingest.mark_copied(s, copied)
    s['meta'] = meta
    return s, ev


def _codex_file(job):
    """-> (path, agg, (last call timestamp, response-id digests)) for one rollout."""
    path, until, copied = job
    agg = ledger.new_agg()
    agg['codex_files']['all'] += 1
    try:
        s, ev = read_codex(path, until, copied)
    except Exception as e:
        agg['codex_errors'][type(e).__name__[:40]] += 1
        return path, agg, (None, [])
    m = s['meta']
    if m['truncated_at_until'] and m['lines'] == 0:
        agg = ledger.new_agg()
        agg['codex_files_after_until']['files'] += 1
        return path, agg, (None, [])
    agg['codex_events']['token_count_raw'] += m['tc_events']
    agg['codex_events']['token_count_dup_or_empty'] += m['tc_dups']
    agg['codex_events']['compacted_lines'] += m['compacted_lines']
    agg['codex_files']['forked_history'] += 1 if s['forked'] else 0
    agg['codex_files']['source:' + str(m['source'])] += 1
    op = _STATE['op']
    costs = ledger.codex_session(s, op, agg)
    attribute.codex_session(s, op, agg)
    if _STATE.get('audit'):
        audits.codex_session(s, ev, op, costs, agg, _STATE['ratios'])
    last = s['live'][-1]['ts'] if s['live'] else None
    return path, agg, (last, [r['rid'] for r in s['recs'] if r['rid']])


def _map(fn, jobs, workers, init_args):
    if workers <= 1:
        _init(*init_args)
        return [fn(j) for j in jobs]
    with Pool(workers, initializer=_init, initargs=init_args) as pool:
        return list(pool.imap_unordered(fn, jobs, chunksize=1))


def copied_ids(infos: dict) -> dict:
    """Ids billed in more than one file. The file whose last record is oldest keeps them
    (a resumed or forked session copies its parent's history, timestamps included);
    every other file gets them as its copied set. -> {path: frozenset(ids)}"""
    where: dict = {}
    for path, (last, hs) in infos.items():
        for h in set(hs):
            where.setdefault(h, []).append(path)
    skip: dict = {}
    for h, paths in where.items():
        if len(paths) < 2:
            continue
        owner = min(paths, key=lambda p: (infos[p][0] is None, infos[p][0] or 0, p))
        for p in paths:
            if p != owner:
                skip.setdefault(p, set()).add(h)
    return {p: frozenset(v) for p, v in skip.items()}


def _scan(fn, jobs, workers, init_args, dedupe, rerun_job, calls_key, usd_of):
    """Pass 1 over every file; with global dedupe, re-read only the files holding copied
    responses, with those responses marked. Returns the merged agg."""
    results = _map(fn, jobs, workers, init_args)
    per = {path: agg for path, agg, _ in results}
    dd = {'files_with_copied_responses': 0, 'calls_removed': 0, 'usd_removed': 0.0}
    if dedupe == 'global':
        skip = copied_ids({path: info for path, _, info in results})
        if skip:
            again = _map(fn, [rerun_job(p, ids) for p, ids in skip.items()], workers, init_args)
            for path, agg, _ in again:
                old = per[path]
                dd['files_with_copied_responses'] += 1
                dd['calls_removed'] += sum(old[calls_key].values()) - sum(agg[calls_key].values())
                dd['usd_removed'] += usd_of(old) - usd_of(agg)
                per[path] = agg
    total = ledger.new_agg()
    for agg in per.values():
        ledger.merge(total, agg)
    total['dedupe'].update(dd)
    return total


# ---------------------------------------------------------------- report
# Every number carries one of four bases:
#   observed   read directly from transcript usage fields (tokens, calls), or those
#              usage fields times a published list price, or the published prices themselves
#   estimated  attribution and token-size estimates
#   modeled    replayed or counterfactual savings (none in `scan`)
#   invoiced   vendor invoice or usage-report data (only when the user supplies it)
BASES = ('observed', 'estimated', 'modeled', 'invoiced')
LIST_PRICE = audits.LIST_PRICE


def _r(x, nd=6):
    return round(float(x), nd)


def _share(d: dict, total: float) -> dict:
    return {k: {'usd': _r(v), 'share': _r(v / total, 6) if total else 0.0} for k, v in sorted(d.items(), key=lambda kv: -kv[1])}


def _dd(a) -> dict:
    return {'basis': 'observed', 'unit': LIST_PRICE,
            **{k: (_r(v) if isinstance(v, float) else v) for k, v in a['dedupe'].items()}}


def claude_report(a) -> dict:
    usd = a['claude_usd']
    served = usd['served']
    billed = usd['declined_billed']
    entry, carry = a['claude_attr_entry_usd'], a['claude_attr_carry_usd']
    fine = {k: entry[k] + carry[k] for k in set(entry) | set(carry)}
    groups: dict = {}
    for k, v in fine.items():
        g = attribute.claude_group(k)
        groups[g] = groups.get(g, 0.0) + v
    attributed = sum(fine.values())
    served_input = a['claude_attr_check']['input_usd']
    by_model = {k.split(':', 1)[1]: _r(sum(v.values())) for k, v in a.items() if k.startswith('claude_usd_model:')}
    by_kind = {k.split(':', 1)[1]: _r(sum(v.values())) for k, v in a.items() if k.startswith('claude_usd_kind:')}
    return {
        'corpus': {
            'basis': 'observed',
            'files': sum(v for k, v in a['claude_files'].items() if ':' not in k),
            'files_after_until': a['claude_files_after_until']['files'],
            'files_failed': sum(a['claude_errors'].values()),
            'files_by_kind': dict(a['claude_files']),
            'threads_with_calls': a['claude_threads']['with_calls'],
            'calls': sum(a['claude_calls_by_model'].values()),
            'calls_by_model': dict(a['claude_calls_by_model'].most_common()),
            'calls_by_kind': dict(a['claude_calls_by_kind'].most_common()),
        },
        'tokens_by_tier': {'basis': 'observed', **{k: a['claude_tok_tier'][k] for k in ledger.TIERS}},
        'list_price_usd': {'basis': 'observed', 'unit': LIST_PRICE, 'served': _r(served), 'served_input': _r(served_input)},
        # Declined fallback attempts: their usage is observed, but whether they are billed is not stated on the
        # pricing page, so the dollars (and every total that includes them) are estimated, billing assumed.
        'declined_fallbacks_usd': {'basis': 'estimated', 'unit': LIST_PRICE, 'billing_assumed': True,
                                   'declined_billed': _r(billed), 'total': _r(served + billed),
                                   'declined_maybe_billed': _r(usd['declined_maybe_billed'])},
        'list_price_usd_by_tier': {'basis': 'observed', 'unit': LIST_PRICE, **{k: _r(a['claude_usd_tier'][k]) for k in ledger.TIERS}},
        'list_price_usd_by_model': {'basis': 'observed', 'unit': LIST_PRICE, **dict(sorted(by_model.items(), key=lambda kv: -kv[1]))},
        'list_price_usd_by_kind': {'basis': 'observed', 'unit': LIST_PRICE, **by_kind},
        'dedupe': _dd(a),
        'fallback': {'basis': 'observed', **dict(a['claude_fallback']),
                     'declined_by_model': dict(a['claude_fallback_declined_model'])},
        'unpriced': {'basis': 'observed', 'calls': a['claude_unpriced']['calls'], 'tokens': a['claude_unpriced']['tokens']},
        'modifiers': {'basis': 'observed', 'fast_calls': a['claude_modifiers']['fast_calls'],
                      'us_geo_calls': a['claude_modifiers']['us_geo_calls']},
        'input_attribution': {
            'basis': 'estimated', 'unit': LIST_PRICE,
            'attributed_usd': _r(attributed),
            'unattributed_usd': _r(served_input - attributed),
            'by_group': _share(groups, attributed),
        },
        'cache_rebuilds': {'basis': 'estimated', 'unit': LIST_PRICE,
                           **{c: {'events': a['claude_rebuild_events'][c], 'tokens': round(a['claude_rebuild_tokens'][c]),
                                  'excess_usd': _r(a['claude_rebuild_usd'][c])} for c in attribute.REBUILD_CAUSES}},
    }


def codex_report(a) -> dict:
    usd = a['codex_usd']
    rent = a['codex_rent_token_calls']
    rti = a['codex_rent_check']['input_token_calls']
    groups: dict = {}
    gusd: dict = {}
    for k, v in rent.items():
        g = attribute.codex_group(k)
        groups[g] = groups.get(g, 0.0) + v
        gusd[g] = gusd.get(g, 0.0) + a['codex_rent_usd'][k]
    tool_kinds = {k.split(':', 1)[1]: {'token_calls_share': _r(v / rti, 6) if rti else 0.0, 'usd': _r(a['codex_rent_usd'][k])}
                  for k, v in sorted(rent.items(), key=lambda kv: -kv[1]) if k.startswith('tool_output:')}
    return {
        'corpus': {
            'basis': 'observed',
            'files': a['codex_files']['all'],
            'files_after_until': a['codex_files_after_until']['files'],
            'files_failed': sum(a['codex_errors'].values()),
            'forked_history_sessions': a['codex_files']['forked_history'],
            'calls': a['codex_calls']['live'],
            'calls_by_model': dict(a['codex_calls_by_model'].most_common()),
            'token_count_events_raw': a['codex_events']['token_count_raw'],
            'token_count_dup_or_empty_dropped': a['codex_events']['token_count_dup_or_empty'],
            'synthetic_calls_without_response_items': a['codex_calls']['synthetic_without_response_items'],
            'responses_without_usage': a['codex_calls']['responses_without_usage'],
        },
        'tokens': {'basis': 'observed', **dict(a['codex_tok'])},
        'list_price_usd': {'basis': 'observed', 'unit': LIST_PRICE,
                           'token_count_calls': _r(usd['token_count_calls']), 'compaction_requests': _r(usd['compaction_requests']),
                           'total': _r(usd['token_count_calls'] + usd['compaction_requests'])},
        'list_price_usd_by_part': {'basis': 'observed', 'unit': LIST_PRICE,
                                   **{k: _r(a['codex_usd_part'][k]) for k in ('cached_in', 'uncached_in', 'out_visible', 'out_reasoning')}},
        'list_price_usd_by_model': {'basis': 'observed', 'unit': LIST_PRICE, **{k: _r(v) for k, v in a['codex_usd_model'].most_common()}},
        'dedupe': _dd(a),
        'compaction_requests': {'basis': 'observed', **dict(a['codex_compaction'])},
        'long_context': {'basis': 'observed', **dict(a['codex_long_context'])},
        'unpriced': {'basis': 'observed', 'calls': a['codex_unpriced']['calls'], 'tokens': a['codex_unpriced']['tokens']},
        'polls': {'basis': 'observed', 'calls': a['codex_polls']['calls'],
                  'polls_no_input': a['codex_polls']['polls_no_input'],
                  'polls_no_new_output': a['codex_polls']['polls_no_new_output']},
        'context_rent': {
            'basis': 'estimated', 'unit': LIST_PRICE,
            'input_token_calls': rti,
            'explained_share': _r(sum(rent.values()) / rti, 6) if rti else 0.0,
            'modeled_input_usd': _r(sum(a['codex_rent_usd'].values())),
            'by_group': {g: {'token_calls_share': _r(v / rti, 6) if rti else 0.0, 'usd': _r(gusd[g])}
                         for g, v in sorted(groups.items(), key=lambda kv: -kv[1])},
            'tool_output_by_kind': tool_kinds,
        },
    }


def _money(x):
    return ('-$' if x < 0 else '$') + '{:,.2f}'.format(abs(x))


def _pct(x):
    return '{:.1f}%'.format(100 * x)


def _n(x):
    return format(x, ',')


def _prices_line(p: dict) -> str:
    return '# Prices: Anthropic snapshot %s (%s), OpenAI snapshot %s (%s).' % (
        p['anthropic']['snapshot_date'], p['anthropic']['source_url'], p['openai']['snapshot_date'], p['openai']['source_url'])


def render_text(rep: dict) -> str:
    """Lines starting with '#' are metadata. Every other line with a number ends with its basis."""
    L = []
    p = rep['prices']
    L.append('# Anatomy %s scan. Dollars are %s at the price snapshots below.' % (rep['anatomy_version'], LIST_PRICE))
    L.append(_prices_line(p))
    if rep.get('until'):
        L.append('# Records after %s UTC are ignored.' % rep['until'])
    L.append('# Bases: [observed] transcript usage fields, [estimated] attribution, [modeled] counterfactual, [invoiced] vendor data.')
    L.append('invoiced totals: not available (no vendor invoice or usage report supplied)  [invoiced]')
    c = rep.get('claude')
    if c:
        co, s, d = c['corpus'], c['list_price_usd'], c['declined_fallbacks_usd']
        L.append('')
        L.append('CLAUDE CODE  %s files, %s threads with calls, %s calls  [observed]' % (_n(co['files']), _n(co['threads_with_calls']), _n(co['calls'])))
        L.append('  served attempts          %14s  [observed]' % _money(s['served']))
        L.append('  declined fallbacks       %14s  output streamed before the decline, billing assumed  [estimated]' % _money(d['declined_billed']))
        L.append('  total                    %14s  served plus declined fallbacks  [estimated]' % _money(d['total']))
        L.append('  declined before output   %14s  may be billed, not in total  [estimated]' % _money(d['declined_maybe_billed']))
        L.append(_dedupe_line(c['dedupe']))
        L.append('  by tier   ' + '  '.join('%s %s' % (k, _money(v)) for k, v in c['list_price_usd_by_tier'].items() if k not in ('basis', 'unit')) + '  [observed]')
        L.append('  by kind   ' + '  '.join('%s %s' % (k, _money(v)) for k, v in c['list_price_usd_by_kind'].items() if k not in ('basis', 'unit')) + '  [observed]')
        for k, v in c['list_price_usd_by_model'].items():
            if k in ('basis', 'unit'):
                continue
            L.append('    %-28s %14s  %s calls  [observed]' % (k, _money(v), _n(co['calls_by_model'].get(k, 0))))
        ia = c['input_attribution']
        L.append('  served input %s, attributed %s; share by cause:  [estimated]' % (_money(s['served_input']), _money(ia['attributed_usd'])))
        for g, v in ia['by_group'].items():
            if v['share'] >= 0.001:
                L.append('    %-46s %7s  %14s  [estimated]' % (g, _pct(v['share']), _money(v['usd'])))
        L.append('  cache rebuilds, excess over a warm read:')
        for cause, v in c['cache_rebuilds'].items():
            if cause in ('basis', 'unit'):
                continue
            L.append('    %-46s %6s events  %14s  [estimated]' % (cause, _n(v['events']), _money(v['excess_usd'])))
    x = rep.get('codex')
    if x:
        co, s = x['corpus'], x['list_price_usd']
        L.append('')
        L.append('CODEX  %s files, %s calls  [observed]' % (_n(co['files']), _n(co['calls'])))
        L.append('  token_count calls        %14s  [observed]' % _money(s['token_count_calls']))
        L.append('  compaction requests      %14s  [observed]' % _money(s['compaction_requests']))
        L.append('  total                    %14s  [observed]' % _money(s['total']))
        L.append(_dedupe_line(x['dedupe']))
        L.append('  by part   ' + '  '.join('%s %s' % (k, _money(v)) for k, v in x['list_price_usd_by_part'].items() if k not in ('basis', 'unit')) + '  [observed]')
        for k, v in x['list_price_usd_by_model'].items():
            if k in ('basis', 'unit'):
                continue
            L.append('    %-28s %14s  %s calls  [observed]' % (k, _money(v), _n(co['calls_by_model'].get(k, 0))))
        cr = x['context_rent']
        L.append('  context rent, share of input token-calls (explained %s):  [estimated]' % _pct(cr['explained_share']))
        for g, v in cr['by_group'].items():
            L.append('    %-46s %7s  %14s  [estimated]' % (g, _pct(v['token_calls_share']), _money(v['usd'])))
        pl = x['polls']
        L.append('  polls %s: sent no input %s, returned no new output %s  [observed]' % (
            _n(pl['calls']), _n(pl['polls_no_input']), _n(pl['polls_no_new_output'])))
    return '\n'.join(L) + '\n'


def _dedupe_line(d: dict) -> str:
    if not d.get('files_with_copied_responses'):
        return '  responses billed in more than one file: 0  [observed]'
    return '  not counted: %s calls copied into %s resumed or forked files, %s already billed in the original  [observed]' % (
        _n(d['calls_removed']), d['files_with_copied_responses'], _money(d['usd_removed']))


# ---------------------------------------------------------------- audit text
AUDIT_TITLES = (('breakeven', 'BREAK-EVEN'), ('boot_scope', 'BOOT SCOPE'), ('keepalive', 'KEEP-ALIVE'),
                ('polls', 'POLLS'), ('oversized', 'OVERSIZED TOOL OUTPUTS'), ('clearing', 'BATCHED CLEARING'))


def _fmt(k, v):
    if isinstance(v, bool) or v is None:
        return '%s %s' % (k, {True: 'yes', False: 'no', None: 'n/a'}[v])
    parts = set(k.replace('.', '_').split('_'))
    if isinstance(v, float) and 'usd' in parts:
        return '%s %s' % (k, _money(v))
    if isinstance(v, float) and (parts & {'share', 'rate'} or '.R' in k):
        return '%s %s' % (k, _pct(v))
    if isinstance(v, (int, float)):
        return '%s %s' % (k, _n(v) if isinstance(v, int) else format(v, ',.4g'))
    return '%s %s' % (k, v)


def _flat(d, pre=''):
    for k, v in d.items():
        if k in ('basis', 'unit', 'id'):
            continue
        if isinstance(v, dict):
            yield from _flat(v, pre + k + '.')
        else:
            yield pre + k, v


def _section_lines(name, d, basis, per_line=4):
    pairs = [_fmt(k, v) for k, v in _flat(d)]
    out = []
    for i in range(0, len(pairs), per_line):
        out.append('  %s%s  [%s]' % ((name + ': ') if i == 0 else '    ', ', '.join(pairs[i:i + per_line]), basis))
    return out


def render_audit_text(rep: dict) -> str:
    """Every line with a number ends with its basis."""
    au = rep['audits']
    p = rep['prices']
    L = ['# Anatomy %s audit. Dollars are %s at the price snapshots below.' % (rep['anatomy_version'], LIST_PRICE),
         _prices_line(p),
         '# Rule: evicting b tokens pays only if b x L x r > S x (w - r) + expected re-fetch cost.',
         '# Bases: [observed] transcript usage and published prices, [estimated] attribution and sizes, '
         '[modeled] counterfactual savings, [invoiced] vendor data.']
    L += _section_lines('sample', au['sample'], 'observed')
    for key, title in AUDIT_TITLES:
        sec = au[key]
        L.append('')
        L.append(title)
        for name, d in sec.items():
            if name in ('fix', 'trick', 'not_recommended') or not isinstance(d, dict):
                continue
            L += _section_lines(name, d, d.get('basis', 'modeled'))
        nr = sec.get('not_recommended')
        f = sec.get('fix')
        if f:
            L.append('  fix: %s  [modeled]' % audits.fix_text(f))
            L.append('    net %s at your prices%s  [modeled]' % (_money(f['net_usd']), ', an upper bound' if f['upper_bound'] else ''))
        elif nr:
            L.append('  fix: none recommended. %s  [modeled]' % audits.not_recommended_text(nr))
            L.append('    net %s at your prices before re-fetches, an upper bound  [modeled]' % _money(nr['net_usd_before_refetch']))
        else:
            L.append('  fix: none clears break-even at your prices')
        t = sec.get('trick')
        if t and t['evaluated']:
            L.append('  trick evaluated: %s, net %s at your prices%s  [modeled]' % (
                audits.trick_text(t), _money(t['net_usd']),
                ', before re-fetches (an upper bound)' if t.get('zero_refetch') else ''))
    return '\n'.join(L) + '\n'


# ---------------------------------------------------------------- entry
def _parse_until(s):
    if not s:
        return None, None
    d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    d = d.astimezone(timezone.utc)
    return d.timestamp(), d.strftime('%Y-%m-%dT%H:%M:%S')


def scan(args) -> dict:
    until_epoch, until_iso = _parse_until(args.until)
    audit = getattr(args, 'audit', False)
    init_args = (args.anthropic_prices, args.openai_prices, audit)
    merged = ledger.new_agg()
    ap, op = AnthropicPrices(args.anthropic_prices), OpenAIPrices(args.openai_prices)
    rep = {'anatomy_version': __version__, 'dedupe': args.dedupe,
           'prices': {'anthropic': {'snapshot_date': ap.snapshot_date, 'source_url': ap.source_url},
                      'openai': {'snapshot_date': op.snapshot_date, 'source_url': op.source_url}},
           'until': until_iso,
           'invoiced': {'basis': 'invoiced', 'status': 'not available'}}
    workers = args.workers or max(1, (os.cpu_count() or 2) - 1)
    t0 = time.time()
    if not args.no_claude:
        root = os.path.expanduser(args.claude_dir or claude_ingest.default_root())
        files = claude_ingest.discover(root) if os.path.isdir(root) else []
        files.sort(key=lambda pk: -os.path.getsize(pk[0]))
        kinds = dict(files)
        a = _scan(_claude_file, [(p, k, until_epoch, None) for p, k in files], workers, init_args, args.dedupe,
                  lambda p, ids: (p, kinds[p], until_epoch, ids), 'claude_calls_by_model',
                  lambda g: g['claude_usd']['served'] + g['claude_usd']['declined_billed'])
        rep['claude'] = claude_report(a)
        ledger.merge(merged, a)
    if not args.no_codex:
        root = os.path.expanduser(args.codex_dir or codex_ingest.default_root())
        files = codex_ingest.discover(root) if os.path.isdir(root) else []
        files.sort(key=lambda p: -os.path.getsize(p))
        a = _scan(_codex_file, [(p, until_iso, None) for p in files], workers, init_args, args.dedupe,
                  lambda p, ids: (p, until_iso, ids), 'codex_calls_by_model',
                  lambda g: g['codex_usd']['token_count_calls'] + g['codex_usd']['compaction_requests'])
        rep['codex'] = codex_report(a)
        ledger.merge(merged, a)
    if audit:
        rep['audits'] = audits.report(merged, ap, op)
    rep['runtime'] = {'basis': 'observed', 'seconds': round(time.time() - t0, 1)}
    return rep


def _corpus_args(s):
    s.add_argument('--claude-dir', help='Claude Code projects directory (default ~/.claude/projects)')
    s.add_argument('--codex-dir', help='Codex home or sessions directory (default ~/.codex: sessions/ and archived_sessions/)')
    s.add_argument('--until', help='ignore records stamped after this ISO time (UTC if no offset)')
    s.add_argument('--no-claude', action='store_true')
    s.add_argument('--no-codex', action='store_true')
    s.add_argument('--workers', type=int, default=0, help='worker processes (default: CPU count - 1)')
    s.add_argument('--dedupe', choices=('global', 'file'), default='global',
                   help='global (default): bill each message/response id once across all files. '
                        'file: dedupe within each file only, as the first analyzers did')
    s.add_argument('--anthropic-prices', help=argparse.SUPPRESS)
    s.add_argument('--openai-prices', help=argparse.SUPPRESS)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='anatomy', description='Cache-correct cost audit of local Claude Code and Codex transcripts.')
    ap.add_argument('--version', action='version', version='anatomy ' + __version__)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('scan', help='stream transcripts and print aggregate totals')
    _corpus_args(s)
    s.add_argument('--json', action='store_true', help='print JSON instead of text')
    a = sub.add_parser('audit', help='the scan plus every audit, each with at most one fix')
    _corpus_args(a)
    a.add_argument('--json', action='store_true', help='print JSON instead of text')
    c = sub.add_parser('card', help='numbers-only card: top three fixes and the one trick that would have cost you money')
    _corpus_args(c)
    c.add_argument('--json', action='store_true', help='print the card as JSON instead of text')
    c.add_argument('--svg', metavar='FILE', help='also write the card as an SVG image to FILE')
    c.add_argument('--shares-only', action='store_true',
                   help='hide every dollar amount: print spend and fixes as shares of the spend read')
    b = sub.add_parser('coach-baseline', help='personal correction-streak baseline for the EMILIA Session Coach',
                       description='Count how often a Claude Code prompt is a correction after one correction, after two '
                                   'in a row, and otherwise, and export the counts as the coach import '
                                   '(%s). Labels are estimates; counts of prompts and sessions are observed.' % coach.FORMAT)
    b.add_argument('--coach-session', metavar='ID', help='the coach session id shown in the panel')
    b.add_argument('--coach-provider', choices=coach.COACH_PROVIDERS, help='the coach client this import is for')
    b.add_argument('--claude-dir', help='Claude Code projects directory (default ~/.claude/projects)')
    b.add_argument('--until', help='ignore records stamped after this ISO time (UTC if no offset)')
    src = b.add_mutually_exclusive_group()
    src.add_argument('--labels', metavar='FILE', help='JSONL of {"key": ..., "label": ...} for the keys --print-keys lists')
    src.add_argument('--classify-with-claude', action='store_true',
                     help='label prompts with your own Claude account through the local claude CLI (model haiku); '
                          'needs --i-consent-to-send-prompts-to-my-claude')
    b.add_argument('--i-consent-to-send-prompts-to-my-claude', action='store_true',
                   help='allow --classify-with-claude to send prompt text and the reply before each to your Claude account')
    b.add_argument('--classifier-id', metavar='ID', help='name of the labeler behind --labels (default labels-file)')
    b.add_argument('--save-labels', metavar='FILE', help='with --classify-with-claude, also write the labels as JSONL for --labels')
    b.add_argument('--print-keys', action='store_true',
                   help='print each prompt\'s join key, session ordinal and index (never text), to label elsewhere')
    b.add_argument('--out', metavar='FILE', help='write the coach import (or, with --print-keys, the keys) to FILE')
    b.add_argument('--json', action='store_true', help='print the coach import instead of the summary')
    b.add_argument('--workers', type=int, default=0, help='worker processes for reading (default: CPU count - 1)')
    args = ap.parse_args(argv)
    if args.cmd == 'coach-baseline':
        try:
            until_epoch, until_iso = _parse_until(args.until)
        except ValueError:
            sys.stderr.write('anatomy: --until must be an ISO time\n')
            return 2
        return coach.main(args, until_epoch, until_iso)
    args.audit = args.cmd in ('audit', 'card')
    rep = scan(args)
    svg = None
    try:
        gate(rep)
        if args.cmd == 'card':
            cd = card_mod.build(rep, shares_only=args.shares_only)
            out = json.dumps(cd, indent=1) + '\n' if args.json else card_mod.render_text(cd)
            if args.svg:
                svg = card_mod.render_svg(cd)
        elif args.cmd == 'audit':
            out = json.dumps(rep, indent=1) + '\n' if args.json else render_audit_text(rep)
        else:
            out = json.dumps(rep, indent=1) + '\n' if args.json else render_text(rep)
        assert_clean(out)
    except PrivacyError as e:
        sys.stderr.write('anatomy: output withheld by the privacy gate (%s)\n' % e)
        return 3
    if svg is not None:
        with open(args.svg, 'w', encoding='utf-8') as fh:
            fh.write(svg)
    sys.stdout.write(out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
