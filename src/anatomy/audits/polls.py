"""Polls: Codex calls to a running process, and the blocking-wait opportunity.

Observed: polls sent, polls that sent no input (the normal way to check on a
process) and polls that returned no new output. Neither kind is waste by
itself: a poll is how the agent learns the process is still running.

Modeled: the blocking-wait opportunity. A model call whose only new input was
polls that returned no new output exists because the agent checked on a timer.
Had the poll blocked until the process printed or exited, that call would not
have happened. The saving is the cost of those calls, less a cache penalty: when
the wait that replaces a chain of them is longer than the cache idle window, the
call that resumes pays uncached input for what it would have read from cache.
"""
from __future__ import annotations

from ..ingest.claude import parse_ts

CACHE_IDLE_S = 300   # modeled: a prompt cache idle for longer than this is assumed cold


def output_flags(ev: list) -> list:
    """The no-new-output flag of every tool output, in the order fold() lists s['tools'].

    Mirrors fold(): history before the live turn is skipped, and outputs count only
    once the first response of the live turn has started."""
    F = next((i for i, e in enumerate(ev) if e[0] == 'tc'), None)
    T = 0
    if F is not None:
        for i in range(F, -1, -1):
            if ev[i][0] == 'task_started':
                T = i
                break
    started = False
    flags = []
    for i in range(T, len(ev)):
        k = ev[i][0]
        if k in ('reasoning', 'call', 'asst', 'tc'):
            started = True
        elif k == 'out' and started:
            flags.append(bool(ev[i][3]))
    return flags


def codex(s: dict, ev: list, prices, costs: list, agg) -> None:
    a = agg['aud_polls']
    a['polls'] += s['polls']
    a['polls_no_input'] += s['polls_no_input']
    a['polls_no_new_output'] += s['polls_no_new_output']
    live = s['live']
    n = len(live)
    if not n or not s['polls_no_new_output']:
        return
    flags = output_flags(ev)
    if len(flags) != len(s['tools']):
        a['sessions_not_linked'] += 1
        return
    empty = [0] * (n + 1)
    other = [0] * (n + 1)
    for r, f in zip(s['tools'], flags):
        p = min(r['pl'], n)
        if f:
            empty[p] += 1
        else:
            other[p] += 1
    for (pl, _n, _k) in s['inj']:
        other[min(pl, n)] += 1
    cand = [0 < p and empty[p] > 0 and other[p] == 0 for p in range(n)]
    p = 1
    while p < n:
        if not cand[p]:
            p += 1
            continue
        q = p
        chain_usd = 0.0
        while q < n and cand[q]:
            c = live[q]
            if not c.get('copied'):
                if costs[q] is None:
                    a['unpriced_calls'] += 1
                else:
                    chain_usd += costs[q]
                    a['calls_after_empty_polls_only'] += 1
            q += 1
        a['chains'] += 1
        a['calls_avoidable_usd'] += chain_usd
        # the call that resumes after the wait
        if q < n and not live[q].get('copied'):
            t0, t1 = parse_ts(live[p - 1].get('ts')), parse_ts(live[q].get('ts'))
            if t0 is not None and t1 is not None and t1 - t0 > CACHE_IDLE_S:
                inp, ca = live[q]['u'][0], live[q]['u'][1]
                r = prices.rates(live[q]['model'], inp)
                if r is not None:
                    a['chains_over_cache_window'] += 1
                    a['cache_penalty_usd'] += ca * (r.input - r.cached_input)
        p = q


def report(agg) -> dict:
    a = agg.get('aud_polls') or {}
    net = a.get('calls_avoidable_usd', 0.0) - a.get('cache_penalty_usd', 0.0)
    out = {
        'codex_polls': {'basis': 'observed', 'polls': int(a.get('polls', 0)),
                        'polls_no_input': int(a.get('polls_no_input', 0)),
                        'polls_no_new_output': int(a.get('polls_no_new_output', 0)),
                        'sessions_not_linked': int(a.get('sessions_not_linked', 0))},
        'blocking_wait_opportunity': {'basis': 'modeled',
                                      'calls_after_empty_polls_only': int(a.get('calls_after_empty_polls_only', 0)),
                                      'chains': int(a.get('chains', 0)),
                                      'calls_avoidable_usd': round(a.get('calls_avoidable_usd', 0.0), 6),
                                      'chains_over_cache_window': int(a.get('chains_over_cache_window', 0)),
                                      'cache_penalty_usd': round(a.get('cache_penalty_usd', 0.0), 6),
                                      'cache_idle_window_s': CACHE_IDLE_S,
                                      'net_usd': round(net, 6)},
        'fix': None,
    }
    if net > 0:
        out['fix'] = {'basis': 'modeled', 'id': 'codex_blocking_waits', 'net_usd': round(net, 6),
                      'calls': int(a.get('calls_after_empty_polls_only', 0)), 'clears_breakeven': True, 'upper_bound': False}
    return out
