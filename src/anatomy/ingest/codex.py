"""Codex rollout ingest (~/.codex/{sessions,archived_sessions}/**/rollout-*.jsonl).

Pass 1 routes every line by a regex on its first 240 bytes. 'compacted' lines
(full replacement histories, most of the bytes) are counted, never parsed.
Pass 2 folds the event list into API calls (one per deduplicated token_count
event), the tool outputs and messages that entered context before each call,
and compaction windows. Only sizes, counts and fixed-vocabulary labels leave
this module.
"""
from __future__ import annotations

import collections
import glob
import hashlib
import json
import os
import re

from ..privacy import model_label

ROUTE = re.compile(rb'"type":"([a-z_]+)","payload":\{(?:"type":"([a-z_]+)")?')
TS_HEAD = re.compile(rb'^\{"timestamp":"([0-9T:.\-]+)')


def default_root() -> str:
    return os.path.expanduser('~/.codex')


def discover(root: str) -> list[str]:
    """rollout files under root/sessions and root/archived_sessions, or under root itself."""
    subs = [os.path.join(root, d) for d in ('sessions', 'archived_sessions') if os.path.isdir(os.path.join(root, d))]
    files: list[str] = []
    for d in subs or [root]:
        files += glob.glob(os.path.join(d, '**', 'rollout-*.jsonl'), recursive=True)
    return sorted(files)


# ---------------------------------------------------------------- command and tool labels
KIND_RULES = [
    ('git-write', re.compile(r'\bgit\s+(?:-C\s+\S+\s+)?(add|commit|push|pull|merge|rebase|checkout|switch|stash|reset|cherry-pick|tag|worktree)\b')),
    ('git-inspect', re.compile(r'\bgit\s+(?:-C\s+\S+\s+)?(status|diff|log|show|blame|branch|rev-parse|fetch|ls-files|ls-remote|remote|grep)\b')),
    ('test', re.compile(r'\b(vitest|jest|pytest|mocha|playwright\s+test|go\s+test|cargo\s+test|(npm|pnpm|yarn|bun)\s+(run\s+)?test[\w:-]*|node\s+--test|xcodebuild\s+test|swift\s+test)\b')),
    ('build-typecheck-lint', re.compile(r'\b(tsc|next\s+build|(npm|pnpm|yarn|bun)\s+(run\s+)?(build|lint|typecheck|check)[\w:-]*|eslint|xcodebuild|cargo\s+build|swift\s+build|make)\b')),
    ('install', re.compile(r'\b((npm|pnpm|yarn|bun)\s+(install|ci|add|i)\b|pip3?\s+install|brew\s+install)')),
    ('network-gh', re.compile(r'(^|[;&|]\s*)(curl|wget|gh|vercel|http)\b')),
    ('db', re.compile(r'\b(psql|supabase|sqlite3)\b')),
    ('inline-script', re.compile(r'\b(python3?|node|ruby|perl|bash|sh|tsx|deno|bun)\s+(-c|-e|--eval|<<|-\s*<<)|<<\s*[\'"]?\w+')),
    ('run-script', re.compile(r'\b(python3?|node|tsx|deno|bun|npx)\s+\S+')),
    ('sleep-wait', re.compile(r'(^|[;&|]\s*)(sleep|wait)\b')),
    ('read', re.compile(r'(^|[;&|]\s*)(cat|sed|head|tail|nl|less|bat|wc|awk|jq|diff|cmp|stat|file)\b')),
    ('search', re.compile(r'(^|[;&|]\s*)(rg|grep|ag|ack|egrep)\b')),
    ('list', re.compile(r'(^|[;&|]\s*)(ls|find|tree|fd|du)\b')),
    ('file-ops', re.compile(r'(^|[;&|]\s*)(mkdir|cp|mv|rm|touch|chmod|ln|tar|unzip)\b')),
    ('process', re.compile(r'(^|[;&|]\s*)(ps|kill|pkill|lsof|pgrep|top)\b')),
]
SHELL_FNS = ('shell', 'exec_command', 'local_shell', 'container.exec', 'shell_command')
POLL_TOOLS = {'exec~poll-process-stdin', 'fn:write_stdin'}


def classify_cmd(cmd, parsed_types) -> str:
    if parsed_types:
        s = set(parsed_types)
        if s <= {'read'}:
            return 'read'
        if s <= {'read', 'search', 'list_files'} and 'search' in s:
            return 'search'
        if s <= {'list_files'}:
            return 'list'
        if s <= {'read', 'list_files'}:
            return 'read'
    c = re.sub(r'^\s*cd\s+[^;&]+(&&|;)\s*', '', cmd or '')
    for k, rx in KIND_RULES:
        if rx.search(c):
            return k
    return 'other'


_FN_NAME = re.compile(r'^[a-z][a-z0-9_]{0,39}$')
_NS_NAME = re.compile(r'^[a-z][a-z0-9_]{0,39}$')


def fn_label(ns: str, name: str) -> str:
    """'fn:<namespace>.<name>' for built-in function tools; every MCP tool collapses to 'fn:mcp'."""
    if 'mcp__' in name or 'mcp__' in ns or name.startswith('mcp'):
        return 'fn:mcp'
    n = name[:40]
    if not _FN_NAME.match(n) or (ns and not _NS_NAME.match(ns)):
        return 'fn:other'
    return ('fn:' + ns + '.' if ns else 'fn:') + n


def custom_label(name: str) -> str:
    n = (name or 'custom')[:40]
    return n if _FN_NAME.match(n) else 'custom-other'


TAG_RX = re.compile(r'\s*<([a-z_ -]{2,40})>')


def is_user_prompt(role, text: str) -> bool:
    """True for a human turn: a user-role message that is not AGENTS.md or a <tagged> injection."""
    if role != 'user':
        return False
    if text.lstrip().startswith('# AGENTS.md'):
        return False
    return TAG_RX.match(text) is None


JS_CMD = re.compile(r'\bcmd\s*:\s*(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|`((?:[^`\\]|\\.)*)`)', re.S)
READ_SEG = re.compile(r'^\s*(sed|cat|head|tail|nl|bat|less)\b(.*)$')


def js_cmd_kinds(raw: str) -> list[str]:
    """Kinds of the shell commands inside a code-mode exec script (older rollouts log no CommandExecution)."""
    out = []
    for m in JS_CMD.finditer(raw):
        cmd = next(g for g in m.groups() if g is not None)
        try:
            cmd = json.loads('"' + cmd.replace('"', '\\"') + '"') if '\\' in cmd else cmd
        except Exception:
            pass
        reads = 0
        for seg in re.split(r'&&|;|\|\|', cmd):
            mm = READ_SEG.match(seg)
            if not mm:
                continue
            toks = [t.strip('\'"') for t in mm.group(2).split() if t and not t.startswith('-')]
            toks = [t for t in toks if not re.fullmatch(r'\d+(,\d+)?p?', t) and '/' in t or '.' in t]
            if toks:
                reads += 1
        simple = reads and all(READ_SEG.match(x) for x in re.split(r'&&|;', cmd) if x.strip())
        out.append(classify_cmd(cmd, ['read'] * reads if simple else []))
    return out[:20]


def _text(out) -> str:
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        return ''.join((c.get('text') or '') for c in out if isinstance(c, dict))
    if isinstance(out, dict):
        return json.dumps(out)
    return ''


OUTPUT_MARK = re.compile(r'(?:^|\n)Output:\n')


def no_new_output(txt: str) -> bool:
    """A poll result carried no new bytes: nothing but whitespace after its 'Output:' line, or,
    for code-mode polls that print the tool result as JSON, an empty 'output' field."""
    m = OUTPUT_MARK.search(txt)
    rest = (txt[m.end():] if m else txt).strip()
    if not rest:
        return True
    if rest[0] == '{':
        try:
            j = json.loads(rest)
        except ValueError:
            return False
        if isinstance(j, dict) and 'output' in j:
            return not str(j['output'] or '').strip()
    return False


# ---------------------------------------------------------------- pass 1: events
def scan(path: str, until: str | None = None) -> tuple[list, dict]:
    """Stream one rollout into a compact event list. `until` is 'YYYY-MM-DDTHH:MM:SS' (UTC)."""
    ev: list = []
    meta = {'lines': 0, 'bad_lines': 0, 'compacted_lines': 0, 'tc_events': 0, 'tc_dups': 0,
            'source': None, 'first_ts': None, 'truncated_at_until': False}
    last_total = None
    poll_ids: set = set()
    ub = until.encode() if until else None
    with open(path, 'rb') as fh:
        for line in fh:
            if ub is not None:
                mt = TS_HEAD.match(line)
                if mt and mt.group(1)[:19] > ub:
                    meta['truncated_at_until'] = True
                    break
            meta['lines'] += 1
            m = ROUTE.search(line, 0, 240)
            if not m:
                meta['bad_lines'] += 1
                continue
            t = m.group(1).decode()
            pt = (m.group(2) or b'').decode()
            if t == 'compacted':
                meta['compacted_lines'] += 1
                ev.append(('comp',))
                continue
            if t == 'world_state':
                continue
            try:
                o = json.loads(line)
            except Exception:
                meta['bad_lines'] += 1
                continue
            p = o.get('payload') or {}
            if not isinstance(p, dict):
                continue
            if meta['first_ts'] is None and isinstance(o.get('timestamp'), str):
                meta['first_ts'] = o['timestamp'][:7]
            if t == 'session_meta':
                if meta['source'] is None:
                    meta['source'] = _session_source(p)
                continue
            if t == 'turn_context':
                ev.append(('ctx', p.get('model') if isinstance(p.get('model'), str) else None))
                continue
            if t == 'token_usage_record':
                u = p.get('usage') or {}
                rid = p.get('response_id')
                ev.append(('recu', u.get('input_tokens') or 0, u.get('cached_input_tokens') or 0,
                           u.get('cache_write_input_tokens') or 0, u.get('output_tokens') or 0,
                           u.get('reasoning_output_tokens') or 0, response_id_hash(rid) if isinstance(rid, str) and rid else None))
                continue
            if t == 'event_msg':
                if pt == 'token_count':
                    info = p.get('info') or {}
                    tu = info.get('total_token_usage') or {}
                    lu = info.get('last_token_usage') or {}
                    meta['tc_events'] += 1
                    tot = tu.get('total_tokens')
                    if not lu or tot is None or tot == last_total:
                        meta['tc_dups'] += 1
                        continue
                    last_total = tot
                    ev.append(('tc', lu.get('input_tokens') or 0, lu.get('cached_input_tokens') or 0,
                               lu.get('cache_write_input_tokens') or 0, lu.get('output_tokens') or 0,
                               lu.get('reasoning_output_tokens') or 0, o.get('timestamp')))
                    continue
                if pt == 'task_started':
                    ev.append(('task_started',))
                    continue
                if pt == 'item_completed':
                    it = p.get('item') or {}
                    if it.get('type') == 'CommandExecution':
                        cmdl = it.get('command')
                        cmd = cmdl[-1] if isinstance(cmdl, list) and cmdl else (cmdl if isinstance(cmdl, str) else '')
                        cmd = cmd if isinstance(cmd, str) else ''
                        parsed = it.get('parsed_cmd') or []
                        ptypes = [q.get('type') for q in parsed if isinstance(q, dict)]
                        ev.append(('cmd', classify_cmd(cmd, ptypes), len(it.get('aggregated_output') or '')))
                continue
            if t == 'response_item':
                if pt in ('custom_tool_call', 'function_call'):
                    name = p.get('name') if isinstance(p.get('name'), str) else ''
                    ns = p.get('namespace') if isinstance(p.get('namespace'), str) else ''
                    raw = p.get('input') if pt == 'custom_tool_call' else p.get('arguments')
                    raw = raw if isinstance(raw, str) else json.dumps(raw or '')
                    shell = None
                    no_input = False
                    if pt == 'custom_tool_call' and '*** Begin Patch' in raw:
                        tool = 'apply_patch'
                    elif pt == 'custom_tool_call':
                        tool = custom_label(name)
                        if tool == 'exec':
                            nm = set(re.findall(r'\btools\.([A-Za-z_]+)\s*\(', raw))
                            if 'write_stdin' in nm:
                                tool = 'exec~poll-process-stdin'
                                no_input = bool(re.search(r'chars\s*:\s*(""|\'\'|``)', raw) or not re.search(r'chars\s*:', raw))
                            elif any(x.startswith('web') for x in nm):
                                tool = 'exec~web-search'
                            elif any(x.startswith('mcp__') for x in nm):
                                tool = 'exec~mcp-app'
                            elif 'view_image' in nm:
                                tool = 'exec~view-image'
                            elif 'exec_command' in nm:
                                tool = 'exec'
                                shell = js_cmd_kinds(raw)
                            elif re.search(r'readFileSync|readFile\s*\(|\bfs\.|readdir', raw):
                                tool = 'exec~js-file-io'
                            else:
                                tool = 'exec~js-other'
                    else:
                        tool = fn_label(ns, name)
                        if name in SHELL_FNS:
                            try:
                                a = json.loads(raw)
                                cmd = a.get('cmd') or a.get('command') or ''
                                if isinstance(cmd, list):
                                    cmd = cmd[-1] if cmd else ''
                            except Exception:
                                cmd = ''
                            shell = classify_cmd(cmd if isinstance(cmd, str) else '', [])
                        elif name == 'write_stdin':
                            try:
                                a = json.loads(raw)
                                no_input = not (isinstance(a, dict) and a.get('chars'))
                            except Exception:
                                no_input = False
                    if tool in POLL_TOOLS:
                        poll_ids.add(p.get('call_id') or '')
                    ev.append(('call', p.get('call_id') or '', tool, shell, no_input))
                    continue
                if pt in ('custom_tool_call_output', 'function_call_output'):
                    cid = p.get('call_id') or ''
                    txt = _text(p.get('output'))
                    if cid in poll_ids:
                        poll_ids.discard(cid)
                        ev.append(('out', cid, len(txt), no_new_output(txt)))
                    else:
                        ev.append(('out', cid, len(txt), False))
                    continue
                if pt == 'reasoning':
                    ev.append(('reasoning',))
                    continue
                if pt == 'message':
                    role = p.get('role')
                    if role == 'assistant':
                        ev.append(('asst',))
                    elif role in ('developer', 'user', 'system'):
                        for c in p.get('content') or []:
                            if isinstance(c, dict):
                                tx = c.get('text') or ''
                                ev.append(('ctxmsg', 'user' if is_user_prompt(role, tx) else 'injected', len(tx)))
                    continue
                if pt == 'agent_message':
                    ev.append(('agentmsg', len(json.dumps(p.get('content') or ''))))
                    continue
    return ev, meta


def response_id_hash(rid: str) -> str:
    """Short in-memory digest of a response id, used only to find the same response in two files."""
    return hashlib.blake2b(rid.encode('utf-8', 'replace'), digest_size=8).hexdigest()


def _session_source(p: dict) -> str:
    src = p.get('source')
    if isinstance(src, dict):
        sub = src.get('subagent')
        if sub is not None:
            return 'subagent'
        return 'other'
    return 'root' if isinstance(src, str) else 'unknown'


# ---------------------------------------------------------------- pass 2: calls, tools, windows
RESP_ITEMS = {'reasoning', 'call', 'asst'}
INPUT_ITEMS = {'out', 'ctxmsg', 'agentmsg'}


def fold(ev: list) -> dict:
    """Fold events into live calls (those with usage), tool outputs and injected messages.

    Positions are live-call indices: an item at position p entered the input of live call p.
    """
    F = next((i for i, e in enumerate(ev) if e[0] == 'tc'), None)
    T = 0
    if F is not None:
        for i in range(F, -1, -1):
            if ev[i][0] == 'task_started':
                T = i
                break
    forked = any(e[0] == 'comp' for e in ev[:T]) or any(e[0] in RESP_ITEMS for e in ev[:T])
    model = None
    calls: list[dict] = []
    resp_open = False
    window = 0
    comp_pos: list[int] = []
    open_calls: collections.OrderedDict = collections.OrderedDict()
    tools: list[dict] = []
    inj: list[tuple] = []
    recs: list[tuple] = []
    synthetic = 0
    turn = 0
    polls = polls_no_input = polls_no_new_output = 0
    for i, e in enumerate(ev):
        k = e[0]
        if k == 'ctx':
            model = e[1] or model
            continue
        if k == 'recu':
            recs.append({'u': (e[1], e[2], e[4], e[5], e[3]), 'model': model, 'rid': e[6], 'match': None})
            continue
        if i < T:
            continue   # inherited history before the live turn
        if k == 'comp':
            if calls:
                window += 1
                comp_pos.append(len(calls))
            continue
        if k in RESP_ITEMS:
            if not resp_open:
                calls.append({'u': None, 'model': model, 'window': window, 'ts': None, 'turn': turn})
                resp_open = True
            if k == 'call':
                _, cid, tool, shell, no_input = e
                rec = {'tool': tool, 'cmds': [], 'out_chars': 0}
                if isinstance(shell, list):
                    rec['jscmds'] = shell
                elif shell:
                    rec['legacy'] = shell
                if tool in POLL_TOOLS:
                    polls += 1
                    polls_no_input += 1 if no_input else 0
                open_calls[cid or ('anon%d' % i)] = rec
            continue
        if k == 'tc':
            _, inp, ca, wr, ou, re_, ts = e
            if calls and calls[-1]['u'] is None:
                calls[-1]['u'] = (inp, ca, wr, ou, re_)
                calls[-1]['ts'] = ts
            else:
                synthetic += 1
                calls.append({'u': (inp, ca, wr, ou, re_), 'model': model, 'window': window, 'ts': ts, 'turn': turn})
            resp_open = False
            continue
        if k == 'cmd':
            if open_calls:
                open_calls[next(reversed(open_calls))]['cmds'].append({'kind': e[1], 'agg': e[2]})
            continue
        if k == 'task_started':
            turn += 1
            continue
        if k in INPUT_ITEMS:
            resp_open = False
            if not calls:
                continue   # initial context of the first call
            if k == 'ctxmsg':
                inj.append((len(calls), e[2], e[1]))
                continue
            if k == 'agentmsg':
                inj.append((len(calls), e[1], 'inter_agent'))
                continue
            _, cid, n, no_new = e
            rec = open_calls.pop(cid, None)
            if rec is None:
                rec = {'tool': 'unmatched', 'cmds': [], 'out_chars': 0}
            elif rec['tool'] in POLL_TOOLS and no_new:
                polls_no_new_output += 1
            if 'legacy' in rec:
                lg = rec.pop('legacy')
                if not rec['cmds']:
                    rec['cmds'].append({'kind': lg, 'agg': 0})
            if 'jscmds' in rec:
                jc = rec.pop('jscmds')
                if not rec['cmds'] and jc:
                    rec['cmds'] = [{'kind': x, 'agg': 0} for x in jc]
            rec['out_chars'] = n
            rec['pos'] = len(calls)
            tot_agg = sum(max(1, c['agg']) for c in rec['cmds']) or 1
            for c in rec['cmds']:
                c['vis'] = n * max(1, c['agg']) / tot_agg
            tools.append(rec)
            continue

    live = [c for c in calls if c['u'] is not None]
    idx_map = []
    j = 0
    for c in calls:
        idx_map.append(j)
        if c['u'] is not None:
            j += 1
    idx_map.append(j)

    def live_idx(p):
        return idx_map[min(p, len(calls))]

    # a large input drop with no 'compacted' line is an implicit window boundary too
    off = 0
    prev_w = prev_in = None
    for c in live:
        if prev_w is not None and c['window'] + off == prev_w and prev_in is not None \
                and c['u'][0] < prev_in - 20_000 and c['u'][0] < 0.85 * prev_in:
            off += 1
        c['window'] = c['window'] + off
        prev_w = c['window']
        prev_in = c['u'][0]
    for r in tools:
        r['pl'] = live_idx(r['pos'])
    inj_live = [(live_idx(p), n, kind) for (p, n, kind) in inj]
    # token_usage_record lines carry response ids; tie each to the first unmatched call with the same
    # (input, cached, output) usage. Records left unmatched are requests outside token_count (compaction).
    queue = collections.defaultdict(collections.deque)
    for i, c in enumerate(live):
        queue[(c['u'][0], c['u'][1], c['u'][3])].append(i)
    for r in recs:
        q = queue[(r['u'][0], r['u'][1], r['u'][2])]
        if q:
            r['match'] = q.popleft()
    return {'live': live, 'tools': tools, 'inj': inj_live, 'recs': recs, 'forked': forked,
            'synthetic': synthetic, 'responses_without_usage': len(calls) - len(live),
            'polls': polls, 'polls_no_input': polls_no_input, 'polls_no_new_output': polls_no_new_output}


def read_session(path: str, until: str | None = None, copied_rids: frozenset | None = None) -> dict:
    """Read one rollout. `copied_rids` holds response-id digests billed in another file: calls
    tied to them are marked `copied` (kept in the timeline, not billed or attributed again) and
    their unmatched records are dropped."""
    ev, meta = scan(path, until)
    s = fold(ev)
    if copied_rids:
        for r in s['recs']:
            if r['rid'] in copied_rids and r['match'] is not None:
                s['live'][r['match']]['copied'] = True
        s['recs'] = [r for r in s['recs'] if not (r['rid'] in copied_rids and r['match'] is None)]
    for c in s['live']:
        c['model'] = model_label(c['model']) if c['model'] is not None else None
    for r in s['recs']:
        r['model'] = model_label(r['model']) if r['model'] is not None else None
    s['meta'] = meta
    return s
