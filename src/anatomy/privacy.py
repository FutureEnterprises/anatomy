"""Output gate: only aggregates leave Anatomy.

Two layers:

1. Labels are sanitized where they are created (ingest). Tool names are
   printed only when they are on a fixed allowlist of built-in Claude Code and
   Codex tools (ingest/claude.BUILTIN_TOOLS, ingest/codex.CODEX_FUNCTIONS);
   every other tool name becomes 'other', and every MCP tool 'mcp'. Model ids
   must look like public model names. Harness-defined words such as attachment
   types pass a shape filter (`label()`) plus the deny list below; command
   kinds and message kinds come from fixed lists. Message text, file contents,
   paths, commands, ids and MCP server names never become labels.
2. Before anything is printed, `gate()` walks the whole result and refuses
   (raises PrivacyError) if any key or string fails the shape filter or the
   deny list, and `assert_clean()` scans the rendered text for secret, email,
   path and URL shapes. The gate is a shape filter, not a vocabulary check: it
   is the second layer, and the allowlists in layer 1 are what keep private
   names out. The error names the rule, never the offending value.
"""
from __future__ import annotations

import math
import re


class PrivacyError(Exception):
    pass


# ---------------------------------------------------------------- label sanitizers
_LABEL = re.compile(r'^[a-z][a-z0-9_\-]{0,40}$')
_MODEL = re.compile(r'^(claude|gpt|codex|o[1-9])(-[a-z0-9][a-z0-9.]*){1,8}$')


def label(s, bucket: str = 'other') -> str:
    """A lower-case vocabulary word from a transcript (attachment type, tag), or the bucket."""
    if isinstance(s, str) and _LABEL.match(s) and _unsafe_reason(s) is None:
        return s
    return bucket


def model_label(m):
    """A public-looking model id, 'unknown' for none, or 'other-model'."""
    if m is None:
        return 'unknown'
    if isinstance(m, str) and len(m) <= 64 and _MODEL.match(m) and _unsafe_reason(m) is None:
        return m
    return 'other-model'


# ---------------------------------------------------------------- deny patterns
_DENY = [
    ('email', re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')),
    ('home path', re.compile(r'(/Users/|/home/|/private/|/var/folders/|~/|[A-Za-z]:\\)')),
    ('url', re.compile(r'\b[a-z][a-z0-9+.\-]*://', re.I)),
    ('api key', re.compile(r'(sk-[A-Za-z0-9_\-]{8,}|sk_(live|test)_[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}'
                           r'|AKIA[0-9A-Z]{12,}|xox[abprs]-[A-Za-z0-9\-]{8,}|AIza[0-9A-Za-z_\-]{20,}|-----BEGIN [A-Z ]+-----'
                           r'|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})')),
    ('long hex', re.compile(r'[0-9a-fA-F]{24,}')),
    ('long token', re.compile(r'[A-Za-z0-9_\-+/=]{48,}')),
    ('uuid', re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-')),
]


def _unsafe_reason(s: str):
    for name, rx in _DENY:
        if rx.search(s):
            return name
    return None


# Strings the gate accepts as values (not keys): vendor pricing pages, which are
# public and printed next to every dollar figure.
PUBLIC_URLS = {
    'https://platform.claude.com/docs/en/about-claude/pricing',
    'https://developers.openai.com/api/docs/pricing',
}

_SAFE_KEY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:+~*\-]{0,79}$')
_SAFE_VALUE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:+~*\- ]{0,79}$')


def gate(obj, _path='$'):
    """Raise PrivacyError unless obj holds only numbers, booleans, None and safe labels."""
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        if isinstance(obj, float) and not math.isfinite(obj):
            raise PrivacyError('non-finite number at ' + _path)
        return obj
    if isinstance(obj, str):
        if obj in PUBLIC_URLS:
            return obj
        if not _SAFE_VALUE.match(obj):
            raise PrivacyError('unsafe string value at ' + _path)
        r = _unsafe_reason(obj)
        if r:
            raise PrivacyError(r + ' shape in value at ' + _path)
        return obj
    if isinstance(obj, (list, tuple)):
        for i, x in enumerate(obj):
            gate(x, '%s[%d]' % (_path, i))
        return obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str) or not _SAFE_KEY.match(k):
                raise PrivacyError('unsafe key under ' + _path)
            r = _unsafe_reason(k)
            if r:
                raise PrivacyError(r + ' shape in key under ' + _path)
            gate(v, _path + '.' + k)
        return obj
    raise PrivacyError('unsupported type at ' + _path)


def assert_clean(text: str) -> str:
    """Final scan of rendered output. Vendor pricing URLs are the only URLs allowed."""
    t = text
    for u in PUBLIC_URLS:
        t = t.replace(u, '')
    r = _unsafe_reason(t)
    if r:
        raise PrivacyError(r + ' shape in rendered output')
    return text
