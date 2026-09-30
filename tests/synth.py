"""Synthetic in-memory threads and sessions for the audit tests. Every value is invented.

Claude threads have the shape ingest/claude.read_thread() returns; Codex sessions
are built by running ingest/codex.fold() on a synthetic event list, so the audits
are tested against the real fold.
"""
from tests.helpers import ANTHROPIC_PRICES, OPENAI_PRICES  # noqa: F401  (also puts src on the path)
from anatomy.ingest import codex as codex_ingest
from anatomy.prices import AnthropicPrices, OpenAIPrices

AP = AnthropicPrices(ANTHROPIC_PRICES)
OP = OpenAIPrices(OPENAI_PRICES)
M = 1e-6
CPT = 2.40   # chars per token for claude-opus-5-5 (new tokenizer)


def call(ctx, model='claude-opus-5-5', ts=None, pre=None, write='5m', read=0, out=10, copied=False, iters=None):
    """A Claude call whose prompt is ctx tokens: `read` of them from cache, the rest written."""
    w = ctx - read
    cc5, cc1 = (w, 0) if write == '5m' else (0, w)
    return {'model': model, 'ts': ts, 'in': 0, 'cc5': cc5, 'cc1': cc1, 'cc': w, 'cr': read, 'out': out,
            'think_tok': None, 'iters': iters, 'text_chars': 0, 'think_blocks': 0, 'speed': None, 'geo': None,
            'tu': [], 'pre': pre or [], 'copied': copied}


def tool_result(tid, tokens):
    return {'kind': 'tool_result', 'tid': tid, 'chars': int(tokens * CPT), 'img_tok': 0}


def prompt(tokens):
    return {'kind': 'user_prompt', 'chars': int(tokens * CPT)}


def attachment(sub, tokens):
    return {'kind': 'attachment', 'sub': sub, 'chars': int(tokens * CPT)}


def thread(calls, tools=None):
    """tools: {tid: category}"""
    return {'calls': calls, 'tool_uses': {tid: {'cat': cat, 'bash_kind': None, 'in_chars': 0, 'call': 0}
                                          for tid, cat in (tools or {}).items()}}


def two_batch_thread(copied_upto=-1):
    """Boot 10K; a 40K tool result enters at call 1 and another at call 5; 1K of prompt per other call.
    Lease K=3, E=30K evicts the first at call 4 and the second at call 8."""
    ctxs = [10_000, 50_000, 51_000, 52_000, 53_000, 93_000, 94_000, 95_000, 96_000, 97_000]
    calls = []
    for j, cx in enumerate(ctxs):
        pre = []
        if j == 1:
            pre = [tool_result('t1', 40_000)]
        elif j == 5:
            pre = [tool_result('t2', 40_000)]
        elif j > 0:
            pre = [prompt(1_000)]
        calls.append(call(cx, pre=pre, read=ctxs[j - 1] if j else 0, copied=j <= copied_upto))
    return thread(calls, {'t1': 'Read', 't2': 'Read'})


def ts(sec):
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return '2026-09-01T%02d:%02d:%02d.000Z' % (10 + h, m, s)


def poll_session():
    """Three polls: two return nothing new (the calls they trigger are blocking-wait candidates), the
    third returns output. The wait that would replace the chain lasts 405 s, past the cache window."""
    ev = [('task_started',), ('ctx', 'gpt-5.6-sol'), ('ctxmsg', 'user', 400),
          ('reasoning',), ('call', 'c1', 'fn:write_stdin', None, True), ('tc', 1000, 0, 0, 10, 0, ts(0)),
          ('out', 'c1', 60, True),
          ('call', 'c2', 'fn:write_stdin', None, True), ('tc', 1100, 1000, 0, 10, 0, ts(5)),
          ('out', 'c2', 60, True),
          ('call', 'c3', 'fn:write_stdin', None, True), ('tc', 1200, 1100, 0, 10, 0, ts(400)),
          ('out', 'c3', 90, False),
          ('asst',), ('tc', 1300, 1200, 0, 10, 0, ts(405))]
    s = codex_ingest.fold(ev)
    s['meta'] = {'source': 'root'}
    return s, ev
