"""Claude Code transcript ingest (~/.claude/projects/**/*.jsonl).

Streams one thread file and returns its API calls, deduplicated by message id,
with the items appended to the conversation before each call. Only sizes,
counts and fixed-vocabulary labels leave this module: message text, file
contents, paths, commands and tool arguments are measured and dropped.
"""
from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
import re
import struct
from datetime import datetime

from ..privacy import label, model_label

KINDS = ('main', 'subagent', 'workflow_agent', 'other')


def default_root() -> str:
    return os.path.expanduser('~/.claude/projects')


def thread_kind(path: str, root: str) -> str:
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    if 'workflows' in parts:
        return 'workflow_agent'
    if 'subagents' in parts:
        return 'subagent'
    if len(parts) == 2:
        return 'main'
    return 'other'


def discover(root: str) -> list[tuple[str, str]]:
    """(path, kind) for every transcript under root. Hidden files are skipped, as glob does."""
    files = glob.glob(os.path.join(root, '**', '*.jsonl'), recursive=True)
    return [(p, thread_kind(p, root)) for p in sorted(files)]


# ---------------------------------------------------------------- labels
TASK_TOOLS = {'TaskCreate', 'TaskUpdate', 'TaskOutput', 'TaskStop', 'TaskList', 'TaskGet'}
_BUILTIN = re.compile(r'^[A-Z][A-Za-z]{1,40}$')


def tool_category(name) -> str:
    """Built-in tool names are kept; every MCP server and connector collapses to 'mcp'."""
    if not name or not isinstance(name, str):
        return 'unknown'
    if name.startswith('mcp__'):
        return 'mcp'
    if name in ('Task', 'Agent'):
        return 'Agent'
    if name in TASK_TOOLS:
        return 'TaskTools'
    return name if _BUILTIN.match(name) else 'other'


BASH_RULES = [
    ('test', re.compile(r'\b(vitest|jest|pytest|mocha|playwright\s+test|npm\s+(run\s+)?test|pnpm\s+(run\s+)?test|yarn\s+test|go\s+test|cargo\s+test|node\s+--test|bun\s+test|deno\s+test|xcodebuild\s+[^|;&]*\btest\b|swift\s+test|test:[a-z]+|npm\s+run\s+[a-z:-]*test|conformance|tlc|tla2tools)', re.I)),
    ('build_typecheck_lint', re.compile(r'\b(next\s+build|npm\s+run\s+build|pnpm\s+(run\s+)?build|yarn\s+build|tsc\b|webpack|vite\s+build|xcodebuild|swift\s+build|cargo\s+(build|check)|eslint|npm\s+run\s+(lint|typecheck|check)|expo\s+(export|prebuild)|gradle|make\b|vercel\s+build|turbo\s+run)', re.I)),
    ('install', re.compile(r'\b(npm\s+(i|install|ci)\b|pnpm\s+(i|install|add)\b|yarn\s+(add|install)\b|pip3?\s+install|brew\s+install|bun\s+(install|add)|cargo\s+install|pod\s+install|npx\s+skills\s+add)', re.I)),
    ('git_diff_log_show', re.compile(r'\bgit\s+(-C\s+\S+\s+)?(diff|log|show|blame|reflog)\b', re.I)),
    ('git_other', re.compile(r'\bgit\s+(-C\s+\S+\s+)?(status|branch|fetch|pull|push|commit|add|checkout|merge|rebase|stash|worktree|ls-files|rev-parse|remote|clone|cherry-pick)\b', re.I)),
    ('gh_cli', re.compile(r'\bgh\s+(pr|run|api|issue|repo|release|search|workflow)\b', re.I)),
    ('logs', re.compile(r'(\bvercel\s+logs|\bdocker\s+logs|journalctl|\blog\s+show\b|\.log\b|\blogs\b|\btail\s+-f)', re.I)),
    ('search_list', re.compile(r'(^|[\s|;&(])(grep|rg|ag|find|fd|ls|tree|git\s+grep|mdfind|du|wc)\b', re.I)),
    ('file_read', re.compile(r'(^|[\s|;&(])(cat|sed\s+-n|head|tail|nl|less|awk|jq|xxd|strings|plutil|pdftotext)\b', re.I)),
    ('network_http', re.compile(r'\b(curl|wget|http|httpie|dig|nslookup|openssl\s+s_client)\b', re.I)),
    ('db_sql', re.compile(r'\b(psql|supabase|sqlite3|mysql|pg_dump)\b', re.I)),
    ('script_exec', re.compile(r'\b(python3?|node|npx|tsx|ts-node|bun|deno|ruby|bash\s+\S+\.sh|sh\s+\S+\.sh|osascript)\b', re.I)),
]


def bash_kind(cmd) -> str:
    if not isinstance(cmd, str):
        return 'unknown'
    for k, rx in BASH_RULES:
        if rx.search(cmd):
            return k
    return 'other'


# ---------------------------------------------------------------- sizes
ATT_EXCLUDE = {'prompt_snapshot', 'remote_session_change', 'structured_output', 'deferred_tools_record',
               'queue-operation', 'credential_org', 'hook_success', 'hook_non_blocking_error',
               'hook_cancelled', 'async_hook_response', 'auto_mode', 'command_permissions',
               'read_truncation_notice', 'thinking_drop'}
META_KEYS = {'type', 'hookName', 'toolUseID', 'hookEvent', 'command', 'exitCode', 'durationMs',
             'displayPath', 'filename', 'path', 'timestamp', 'source_uuid', 'origin', 'commandMode',
             'isInitial', 'skillCount', 'itemCount', 'names', 'removedNames', 'addedNames', 'readdedNames',
             'pendingMcpServers', 'needsAuthMcpServers', 'failedMcpServers', 'wireHiddenNames', 'surfacedNames',
             'removedTypes', 'addedTypes', 'showConcurrencyNote', 'model', 'clearAt', 'identity', 'uuid'}


def _strlen(v) -> int:
    if isinstance(v, str):
        return len(v)
    if isinstance(v, list):
        return sum(_strlen(x) for x in v)
    if isinstance(v, dict):
        return sum(_strlen(x) for k, x in v.items() if k not in META_KEYS)
    return 0


def attachment_chars(a: dict) -> int:
    """Approximate characters an attachment contributes to model context."""
    t = a.get('type')
    if t == 'hook_success':
        # only SessionStart hook content is injected; tool hooks carry empty content
        c = a.get('content')
        return len(c) if isinstance(c, str) else 0
    if t in ATT_EXCLUDE:
        return 0
    return sum(_strlen(v) for k, v in a.items() if k not in META_KEYS)


def image_tokens_from_b64(data: str) -> int:
    """Image tokens from PNG/JPEG header dimensions, resized to a 1568px long edge; tokens = w*h/750."""
    try:
        raw = base64.b64decode(data[:65536] + '=' * (-len(data[:65536]) % 4), validate=False)
    except Exception:
        return 1600
    w = h = None
    if raw[:8] == b'\x89PNG\r\n\x1a\n' and len(raw) >= 24:
        w, h = struct.unpack('>II', raw[16:24])
    elif raw[:2] == b'\xff\xd8':
        i = 2
        while i + 9 < len(raw):
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack('>HH', raw[i + 5:i + 9])
                break
            seglen = struct.unpack('>H', raw[i + 2:i + 4])[0]
            i += 2 + seglen
    if not w or not h:
        return 1600
    s = min(1.0, 1568.0 / max(w, h))
    return int(w * s * h * s / 750) + 1


def _tool_result_size(block: dict) -> tuple[int, int]:
    """(text chars, image tokens) of a tool_result block."""
    c = block.get('content')
    if isinstance(c, str):
        return len(c), 0
    n = 0
    parts = 0
    imgtok = 0
    if isinstance(c, list):
        for x in c:
            if not isinstance(x, dict):
                continue
            t = x.get('type')
            if t == 'text':
                n += len(x.get('text') or '')
                parts += 1
            elif t == 'image':
                src = x.get('source') or {}
                imgtok += image_tokens_from_b64(src.get('data') or '') if src.get('type') == 'base64' else 1600
            elif t == 'tool_reference':
                n += len(str(x.get('tool_name') or ''))
                parts += 1
    # texts are joined with newlines
    return n + max(parts - 1, 0), imgtok


def parse_ts(s) -> float | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


# ---------------------------------------------------------------- thread
def message_key_hash(key) -> str:
    """Short in-memory digest of a message id, used only to find the same response in two files."""
    return hashlib.blake2b(str(key).encode('utf-8', 'replace'), digest_size=8).hexdigest()


def read_thread(path: str, until: float | None = None, copied_ids: frozenset | None = None) -> dict:
    """Parse one transcript. With `until` (epoch seconds), stop at the first record stamped later.

    `copied_ids` holds message-id digests billed in another file (history copied
    into a resumed or forked session). Those calls stay in the thread, so the
    context they carry is still seen, but are marked `copied` and are not billed
    or attributed a second time.

    Returns dict(calls, tool_uses, tail, first_ts, last_ts). Each call carries the
    top-level usage of its message id (latest input-side fields, max output), the
    per-attempt `iters` list when a fallback happened, and `pre`: items appended
    since the previous call. Items hold sizes and labels only.
    """
    calls: list[dict] = []
    by_key: dict = {}
    pending: list[dict] = []
    tool_uses: dict = {}
    first_ts = last_ts = None
    stopped = False
    ids: list[str] = []
    with open(path, 'rb') as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            t = o.get('type')
            ts = parse_ts(o.get('timestamp'))
            if ts:
                if until is not None and ts > until:
                    stopped = True
                    break
                first_ts = first_ts or ts
                last_ts = ts
            if t == 'assistant':
                m = o.get('message')
                if not isinstance(m, dict):
                    continue
                model = m.get('model')
                if model == '<synthetic>' or o.get('isApiErrorMessage'):
                    continue
                key = m.get('id') or o.get('requestId') or o.get('uuid')
                hk = message_key_hash(key)
                u = m.get('usage') or {}
                if not isinstance(u, dict):
                    u = {}
                c = by_key.get(key)
                if c is None:
                    c = {'model': model_label(model), 'ts': ts, 'in': 0, 'cc5': 0, 'cc1': 0, 'cc': 0, 'cr': 0, 'out': 0,
                         'think_tok': None, 'iters': None, 'text_chars': 0, 'think_blocks': 0,
                         'speed': None, 'geo': None, 'tu': [], 'pre': pending, 'idx': len(calls),
                         'copied': bool(copied_ids) and hk in copied_ids}
                    pending = []
                    by_key[key] = c
                    calls.append(c)
                    ids.append(hk)
                # usage: latest value for input-side fields, max for output
                c['in'] = u.get('input_tokens') or c['in']
                cc = u.get('cache_creation_input_tokens')
                if cc is not None:
                    c['cc'] = cc
                ccd = u.get('cache_creation') or {}
                if isinstance(ccd, dict) and (ccd.get('ephemeral_5m_input_tokens') is not None or ccd.get('ephemeral_1h_input_tokens') is not None):
                    c['cc5'] = ccd.get('ephemeral_5m_input_tokens') or 0
                    c['cc1'] = ccd.get('ephemeral_1h_input_tokens') or 0
                cr = u.get('cache_read_input_tokens')
                if cr is not None:
                    c['cr'] = cr
                c['out'] = max(c['out'], u.get('output_tokens') or 0)
                od = u.get('output_tokens_details')
                if isinstance(od, dict) and od.get('thinking_tokens') is not None:
                    c['think_tok'] = max(c['think_tok'] or 0, od.get('thinking_tokens') or 0)
                if u.get('speed') == 'fast':
                    c['speed'] = 'fast'
                if u.get('inference_geo') == 'us':
                    c['geo'] = 'us'
                it = u.get('iterations')
                if isinstance(it, list) and len(it) >= 2:
                    # per-attempt billing records: declined attempt(s), then the served one
                    c['iters'] = [_attempt(x) for x in it if isinstance(x, dict)]
                for b in m.get('content') or []:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get('type')
                    if bt == 'text':
                        n = len(b.get('text') or '')
                        c['text_chars'] += n
                        pending.append({'kind': 'assistant_out', 'sub': 'text', 'chars': n, 'call': c['idx']})
                    elif bt in ('thinking', 'redacted_thinking'):
                        n = len(b.get('thinking') or '')
                        c['think_blocks'] += 1
                        pending.append({'kind': 'assistant_out', 'sub': 'thinking', 'chars': n, 'call': c['idx']})
                    elif bt == 'tool_use':
                        inp = b.get('input')
                        n = len(json.dumps(inp, ensure_ascii=False)) if inp is not None else 0
                        name = b.get('name')
                        tu = {'cat': tool_category(name), 'in_chars': n, 'call': c['idx'],
                              'bash_kind': bash_kind((inp or {}).get('command')) if name == 'Bash' and isinstance(inp, dict) else None}
                        if name == 'Bash' and not isinstance(inp, dict):
                            tu['bash_kind'] = 'unknown'
                        tool_uses[b.get('id')] = tu
                        c['tu'].append(tu)
                        pending.append({'kind': 'assistant_out', 'sub': 'tool_use', 'tool': tu['cat'], 'chars': n, 'call': c['idx']})
            elif t == 'user':
                m = o.get('message')
                if not isinstance(m, dict):
                    continue
                content = m.get('content')
                kind = 'compact_summary' if o.get('isCompactSummary') else ('meta_user' if o.get('isMeta') else 'user_prompt')
                if isinstance(content, str):
                    pending.append({'kind': kind, 'chars': len(content)})
                elif isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get('type')
                        if bt == 'tool_result':
                            n, imgtok = _tool_result_size(b)
                            pending.append({'kind': 'tool_result', 'tid': b.get('tool_use_id'), 'chars': n, 'img_tok': imgtok})
                        elif bt == 'text':
                            pending.append({'kind': kind, 'chars': len(b.get('text') or '')})
                        elif bt == 'image':
                            src = b.get('source') or {}
                            pending.append({'kind': kind, 'chars': 0,
                                            'img_tok': image_tokens_from_b64(src.get('data') or '') if src.get('type') == 'base64' else 1600})
                        elif bt == 'document':
                            pending.append({'kind': kind, 'chars': 0})
            elif t == 'attachment':
                a = o.get('attachment')
                if isinstance(a, dict):
                    n = attachment_chars(a)
                    if n:
                        pending.append({'kind': 'attachment', 'sub': label(a.get('type')), 'chars': n})
            elif t == 'system' and o.get('subtype') == 'compact_boundary':
                pending.append({'kind': 'compact_marker'})
    return {'calls': calls, 'tool_uses': tool_uses, 'tail': pending, 'first_ts': first_ts, 'last_ts': last_ts,
            'stopped_at_until': stopped, 'ids': ids}


def _attempt(it: dict) -> dict:
    ccd = it.get('cache_creation') or {}
    if not isinstance(ccd, dict):
        ccd = {}
    return {'type': it.get('type'), 'model': model_label(it['model']) if it.get('model') else None,
            'in': it.get('input_tokens') or 0, 'cc': it.get('cache_creation_input_tokens') or 0,
            'cc5': ccd.get('ephemeral_5m_input_tokens') or 0, 'cc1': ccd.get('ephemeral_1h_input_tokens') or 0,
            'cr': it.get('cache_read_input_tokens') or 0, 'out': it.get('output_tokens') or 0}


def ctx(c: dict) -> int:
    return (c['in'] or 0) + (c['cc'] or 0) + (c['cr'] or 0)
