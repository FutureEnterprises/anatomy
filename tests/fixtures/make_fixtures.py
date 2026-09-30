"""Writes the synthetic transcripts used by the tests. Every value here is invented.

Run from the repo root: python3 tests/fixtures/make_fixtures.py
The planted "secrets", emails and paths are fake and exist so the privacy tests
can prove none of them ever reaches Anatomy's output.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# Planted strings. None of them may appear in any output.
FAKE_ANTHROPIC_KEY = 'sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKEFAKE0000'
FAKE_AWS_KEY = 'AKIAFAKEFAKEFAKE1234'
FAKE_EMAIL = 'alice@example.com'
FAKE_EMAIL_2 = 'bob@example.org'
FAKE_PATH = '/Users/alice/acme-private-repo/secrets/.env'
FAKE_MCP = 'mcp__acme-private-repo__read_file'
FAKE_ATTACHMENT_TYPE = 'Secret Project Plan'
FAKE_GH_TOKEN = 'ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKE000000'
PLANTED = [FAKE_ANTHROPIC_KEY, FAKE_AWS_KEY, FAKE_EMAIL, FAKE_EMAIL_2, FAKE_PATH, 'acme-private-repo',
           FAKE_ATTACHMENT_TYPE, FAKE_GH_TOKEN, 'msg_fixture_', 'resp_fixture_', 'toolu_fixture_', '/Users/alice']

def ts(sec):
    """ISO timestamp sec seconds after 2026-09-01T10:00:00Z."""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return '2026-09-01T%02d:%02d:%02d.000Z' % (10 + h, m, s)


def usage(inp, cc5=0, cc1=0, cr=0, out=0, iterations=None):
    u = {'input_tokens': inp, 'cache_creation_input_tokens': cc5 + cc1, 'cache_read_input_tokens': cr,
         'cache_creation': {'ephemeral_5m_input_tokens': cc5, 'ephemeral_1h_input_tokens': cc1},
         'output_tokens': out, 'service_tier': 'standard', 'speed': 'standard'}
    if iterations is not None:
        u['iterations'] = iterations
    return u


def attempt(kind, inp, cc5=0, cc1=0, cr=0, out=0, model=None):
    a = {'type': kind, 'input_tokens': inp, 'cache_creation_input_tokens': cc5 + cc1, 'cache_read_input_tokens': cr,
         'cache_creation': {'ephemeral_5m_input_tokens': cc5, 'ephemeral_1h_input_tokens': cc1}, 'output_tokens': out}
    if model:
        a['model'] = model
    return a


def assistant(sec, mid, model, u, content):
    return {'type': 'assistant', 'timestamp': ts(sec), 'cwd': '/Users/alice/acme-private-repo',
            'message': {'id': mid, 'model': model, 'role': 'assistant', 'usage': u, 'content': content}}


def user(sec, content, **kw):
    return {'type': 'user', 'timestamp': ts(sec), 'cwd': '/Users/alice/acme-private-repo',
            'message': {'role': 'user', 'content': content}, **kw}


def claude_main():
    """One main thread (1h TTL class) with both cache tiers, two fallbacks and an idle-gap rebuild."""
    R = []
    R.append(user(0, 'Deploy with key %s and mail %s' % (FAKE_ANTHROPIC_KEY, FAKE_EMAIL)))
    # call 1: 1h cache write; streamed as two records of one message id (output grows 50 -> 100)
    R.append(assistant(1, 'msg_fixture_A1', 'claude-opus-5-5', usage(10, cc1=20000, out=50),
                       [{'type': 'text', 'text': 'Reading %s now.' % FAKE_PATH}]))
    R.append(assistant(1, 'msg_fixture_A1', 'claude-opus-5-5', usage(10, cc1=20000, out=100),
                       [{'type': 'tool_use', 'id': 'toolu_fixture_1', 'name': 'Bash', 'input': {'command': 'cat ' + FAKE_PATH}}]))
    R.append(user(5, [{'type': 'tool_result', 'tool_use_id': 'toolu_fixture_1',
                       'content': 'AWS_ACCESS_KEY_ID=%s\nGITHUB_TOKEN=%s\n' % (FAKE_AWS_KEY, FAKE_GH_TOKEN)}]))
    R.append({'type': 'attachment', 'timestamp': ts(6), 'attachment': {'type': FAKE_ATTACHMENT_TYPE, 'content': 'owner ' + FAKE_EMAIL}})
    # call 2: 5m cache write plus a cache read of the whole prefix
    R.append(assistant(10, 'msg_fixture_A2', 'claude-opus-5-5', usage(5, cc5=3000, cr=20010, out=200),
                       [{'type': 'thinking', 'thinking': 'The key %s must not leak.' % FAKE_ANTHROPIC_KEY},
                        {'type': 'tool_use', 'id': 'toolu_fixture_2', 'name': FAKE_MCP, 'input': {'path': FAKE_PATH}}]))
    R.append(user(12, [{'type': 'tool_result', 'tool_use_id': 'toolu_fixture_2', 'content': [{'type': 'text', 'text': 'contents of ' + FAKE_PATH}]}]))
    # ignored records: synthetic model and API error
    R.append(assistant(13, 'msg_fixture_syn', '<synthetic>', usage(0, out=0), [{'type': 'text', 'text': 'x'}]))
    R.append({**assistant(14, 'msg_fixture_err', 'claude-opus-5-5', usage(0), [{'type': 'text', 'text': FAKE_EMAIL}]), 'isApiErrorMessage': True})
    # call 3: fallback. The declined attempt on claude-fable-5 streamed 40 output tokens (billed);
    # the served attempt is claude-opus-5. The model change forces a full 1h rewrite.
    R.append(user(20, 'next step, cc ' + FAKE_EMAIL_2))
    R.append(assistant(21, 'msg_fixture_A3', 'claude-opus-5', usage(5, cc1=23100, out=80, iterations=[
        attempt('message', 5, cc1=23100, out=40, model='claude-fable-5'),
        attempt('fallback_message', 5, cc1=23100, out=80)]),
        [{'type': 'text', 'text': 'Done.'}]))
    # call 4: another fallback; this declined attempt stopped before any output (maybe billed)
    R.append(user(30, 'and again'))
    R.append(assistant(31, 'msg_fixture_A4', 'claude-opus-5', usage(5, cc1=23300, out=60, iterations=[
        attempt('message', 5, cc1=23300, out=0, model='claude-fable-5'),
        attempt('fallback_message', 5, cc1=23300, out=60)]),
        [{'type': 'text', 'text': 'Done again.'}]))
    # call 5: warm, reads the prefix
    R.append(user(40, 'status?'))
    R.append(assistant(41, 'msg_fixture_A5', 'claude-opus-5', usage(5, cc1=100, cr=23305, out=30),
                       [{'type': 'text', 'text': 'All good.'}]))
    # call 6: two hours idle, past the 1h TTL: full rewrite (ttl_expired_idle_gap)
    R.append(user(41 + 7200, 'back after lunch'))
    R.append(assistant(42 + 7200, 'msg_fixture_A6', 'claude-opus-5', usage(5, cc1=23600, out=40),
                       [{'type': 'text', 'text': 'Welcome back.'}]))
    return R


def claude_resumed(main):
    """A resumed session: copies calls 1-2 of the main thread verbatim (ids and timestamps), then adds one call."""
    R = [r for r in main[:7]]
    R.append(user(3 * 3600, 'resume: continue from where we were'))
    R.append(assistant(3 * 3600 + 5, 'msg_fixture_B1', 'claude-opus-5-5', usage(5, cc1=23100, out=20),
                       [{'type': 'text', 'text': 'Continuing.'}]))
    return R


def claude_subagent():
    """A subagent thread on the 5m tier."""
    return [user(100, 'Explore ' + FAKE_PATH),
            assistant(101, 'msg_fixture_S1', 'claude-opus-5-5', usage(3, cc5=1000, out=10), [{'type': 'text', 'text': 'ok'}])]


def cx(sec, typ, payload):
    return {'timestamp': ts(sec), 'type': typ, 'payload': payload}


def tc(sec, total, inp, cached, out, reas):
    return cx(sec, 'event_msg', {'type': 'token_count', 'info': {
        'total_token_usage': {'input_tokens': 0, 'total_tokens': total},
        'last_token_usage': {'input_tokens': inp, 'cached_input_tokens': cached, 'output_tokens': out,
                             'reasoning_output_tokens': reas, 'total_tokens': inp + out}}})


def codex_rollout():
    """One Codex session: shell read, an empty poll (no input, no new output), a poll with no input
    that returned output, a compaction, a compaction request billed outside token_count, and a
    long-context call."""
    E = []
    E.append(cx(0, 'session_meta', {'id': 'fixture-session-1', 'cwd': '/Users/alice/acme-private-repo', 'source': 'cli',
                                    'base_instructions': {'text': 'Never print ' + FAKE_GH_TOKEN}}))
    E.append(cx(1, 'turn_context', {'model': 'gpt-5.6-sol', 'effort': 'high', 'cwd': '/Users/alice/acme-private-repo'}))
    E.append(cx(2, 'event_msg', {'type': 'task_started'}))
    E.append(cx(3, 'response_item', {'type': 'message', 'role': 'user', 'content': [
        {'type': 'input_text', 'text': 'use %s and mail %s' % (FAKE_ANTHROPIC_KEY, FAKE_EMAIL)}]}))
    # call 1: reasoning + a shell read
    E.append(cx(4, 'response_item', {'type': 'reasoning', 'summary': []}))
    E.append(cx(5, 'response_item', {'type': 'function_call', 'name': 'exec_command', 'call_id': 'call_fixture_1',
                                     'arguments': json.dumps({'cmd': 'cat ' + FAKE_PATH})}))
    E.append(tc(6, 1050, 1000, 0, 50, 20))
    E.append(tc(6, 1050, 1000, 0, 50, 20))   # duplicate event, same running total: dropped
    E.append(cx(7, 'event_msg', {'type': 'item_completed', 'item': {
        'type': 'CommandExecution', 'command': ['bash', '-lc', 'cat ' + FAKE_PATH],
        'parsed_cmd': [{'type': 'read', 'cmd': 'cat ' + FAKE_PATH, 'path': FAKE_PATH}],
        'aggregated_output': 'AWS_ACCESS_KEY_ID=' + FAKE_AWS_KEY, 'exit_code': 0, 'cwd': '/Users/alice/acme-private-repo'}}))
    E.append(cx(8, 'response_item', {'type': 'function_call_output', 'call_id': 'call_fixture_1',
                                     'output': 'AWS_ACCESS_KEY_ID=' + FAKE_AWS_KEY + '\n' + 'x' * 3980}))
    # call 2: an empty poll (sends no input, gets no new output)
    E.append(cx(9, 'response_item', {'type': 'function_call', 'name': 'write_stdin', 'call_id': 'call_fixture_2',
                                     'arguments': json.dumps({'session_id': 7, 'chars': ''})}))
    # ... and a code-mode poll in the same response that prints the tool result as JSON with empty output
    E.append(cx(9, 'response_item', {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'call_fixture_2b',
                                     'input': 'const r = await tools.write_stdin({session_id: 7, chars: ""}); text(JSON.stringify(r));'}))
    E.append(tc(10, 2350, 2200, 1000, 100, 0))
    E.append(cx(11, 'response_item', {'type': 'function_call_output', 'call_id': 'call_fixture_2',
                                      'output': 'Chunk ID: 1\nWall time: 5.0 seconds\nProcess running with session ID 7\nOriginal token count: 0\nOutput:\n'}))
    E.append(cx(11, 'response_item', {'type': 'custom_tool_call_output', 'call_id': 'call_fixture_2b', 'output': [
        {'type': 'input_text', 'text': 'Script ran.\nOutput:\n' + json.dumps({'chunk_id': 'a1', 'original_token_count': 0, 'output': '',
                                                                           'session_id': 7, 'wall_time_seconds': 5.0})}]}))
    # call 3: a poll with no input that did return new output (not wasted)
    E.append(cx(12, 'response_item', {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Checking.'}]}))
    E.append(cx(12, 'response_item', {'type': 'function_call', 'name': 'write_stdin', 'call_id': 'call_fixture_3',
                                      'arguments': json.dumps({'session_id': 7, 'chars': ''})}))
    E.append(tc(13, 2690, 2300, 2200, 40, 0))
    E.append(cx(14, 'response_item', {'type': 'function_call_output', 'call_id': 'call_fixture_3',
                                      'output': 'Chunk ID: 2\nWall time: 1.0 seconds\nOriginal token count: 3\nOutput:\nbuild ok\n'}))
    # compaction request, billed as its own response outside token_count
    E.append(cx(16, 'token_usage_record', {'response_id': 'resp_fixture_compact', 'usage': {
        'input_tokens': 5000, 'cached_input_tokens': 0, 'output_tokens': 300, 'reasoning_output_tokens': 0}}))
    E.append(cx(17, 'compacted', {'message': 'summary mentioning ' + FAKE_EMAIL, 'replacement_history': []}))
    # call 4: first call of the new window
    E.append(cx(18, 'event_msg', {'type': 'task_started'}))
    E.append(cx(19, 'response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'go on'}]}))
    E.append(cx(20, 'response_item', {'type': 'reasoning', 'summary': []}))
    E.append(tc(21, 2700, 800, 0, 10, 0))
    E.append(cx(22, 'token_usage_record', {'response_id': 'resp_fixture_4', 'usage': {
        'input_tokens': 800, 'cached_input_tokens': 0, 'output_tokens': 10, 'reasoning_output_tokens': 0}}))
    # call 5: a long-context request (over 272K input tokens)
    E.append(cx(23, 'response_item', {'type': 'reasoning', 'summary': []}))
    E.append(tc(24, 3000, 300000, 290000, 100, 0))
    return E


def write(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as fh:
        for r in records:
            fh.write(json.dumps(r, separators=(',', ':'), ensure_ascii=False) + '\n')


def main():
    proj = os.path.join(HERE, 'claude', 'projects', '-Users-alice-acme-private-repo')
    main_thread = claude_main()
    write(os.path.join(proj, 'session-main.jsonl'), main_thread)
    write(os.path.join(proj, 'session-resumed.jsonl'), claude_resumed(main_thread))
    write(os.path.join(proj, 'session-main', 'subagents', 'agent-explore.jsonl'), claude_subagent())
    write(os.path.join(HERE, 'codex', 'sessions', '2026', '09', '01', 'rollout-2026-09-01T10-00-00-fixture.jsonl'), codex_rollout())


if __name__ == '__main__':
    main()
