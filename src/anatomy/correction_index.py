"""Observed Claude Code prompt labels joined to subsequent served-call usage.

This is an association, not an invoice, waste estimate, or causal saving. Raw
transcript text, paths, and record identifiers are used locally and never enter
the aggregate report. Physical record order, not timestamps, establishes the
owner of a call. A later streaming update cannot move that call to a new user
prompt. Queued human prompts and conflicting copies have ambiguous ownership.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import subprocess

from . import coach
from .ingest import claude
from .ledger import TIERS, claude_attempt_cost
from .privacy import PrivacyError, assert_clean, gate, model_label

LABELS = (*coach.LABELS, 'unknown')
OWNER_LABELS = (*LABELS, 'unattributed', 'ambiguous')
FORMAT = 'anatomy-correction-index-v1'
NONCONVERSATIONAL_METADATA = frozenset({
    'last-prompt', 'custom-title', 'agent-name', 'mode', 'atis-latch',
    'bridge-session', 'file-history-snapshot', 'file-history-delta',
    'cost-state', 'artifact-comment-monitor', 'artifact-autoreact-ledger',
    'frame-link', 'pr-link',
})
TOKEN_FIELDS = {'input_tokens': 'in', 'output_tokens': 'out',
                'cache_creation_input_tokens': 'cc', 'cache_read_input_tokens': 'cr'}


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _empty_usage():
    return {'in': 0, 'cc': 0, 'cc5': 0, 'cc1': 0, 'cr': 0, 'out': 0,
            '_fields': set(), 'invalid_usage': False, 'split': False}


def _usage_into(call, usage):
    if not isinstance(usage, dict):
        if usage is not None:
            call['invalid_usage'] = True
        return
    for field, short in TOKEN_FIELDS.items():
        if field not in usage:
            continue
        value = usage[field]
        if not _integer(value):
            call['invalid_usage'] = True
            continue
        call['_fields'].add(short)
        call[short] = max(call[short], value) if short == 'out' else value
    split = usage.get('cache_creation')
    if split is not None:
        if not isinstance(split, dict):
            call['invalid_usage'] = True
        elif any(k in split for k in ('ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens')):
            call['split'] = True
            for field, short in (('ephemeral_5m_input_tokens', 'cc5'), ('ephemeral_1h_input_tokens', 'cc1')):
                value = split.get(field, 0)
                if not _integer(value):
                    call['invalid_usage'] = True
                else:
                    call['_fields'].add(short)
                    call[short] = value
    for field, short, permitted, special in (
            ('speed', 'speed', ('standard', 'fast'), 'fast'),
            ('inference_geo', 'geo', ('global', 'us'), 'us')):
        value = usage.get(field)
        if field == 'inference_geo' and value in (None, '', 'not_available'):
            # Missing routing metadata is not an explicit global location.
            # Pricing applies a documented global-rate assumption until an
            # explicit global/us value supplies actual recorded evidence.
            continue
        if value is not None:
            seen = '_' + short + '_value'
            if value not in permitted or (seen in call and call[seen] != value):
                call['modifier_conflict'] = True
            call[seen] = value
            call[short] = special if value == special else None


def _usage_status(call):
    if call['invalid_usage']:
        return 'invalid_usage'
    if not {'in', 'out'} <= call['_fields']:
        return 'missing_usage'
    if call['split'] and 'cc' in call['_fields'] and call['cc5'] + call['cc1'] != call['cc']:
        return 'invalid_usage'
    return 'known'


def _repo_resolver():
    cache = {}

    def resolve(cwd):
        if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd):
            return None
        cwd = os.path.realpath(cwd)
        if cwd not in cache:
            try:
                result = subprocess.run(['git', '-C', cwd, 'rev-parse', '--git-common-dir'],
                                        capture_output=True, text=True, timeout=2, check=False)
                common = result.stdout.strip() if result.returncode == 0 else ''
                cache[cwd] = os.path.realpath(common if os.path.isabs(common) else os.path.join(cwd, common)) if common else None
            except (OSError, subprocess.SubprocessError):
                cache[cwd] = None
        return cache[cwd]
    return resolve


def _human_boundary(record):
    """Return text, nontext or ambiguous; None means harness/tool material."""
    if record.get('type') != 'user' or record.get('isMeta') or record.get('isCompactSummary') or record.get('isSidechain'):
        return None
    origin = record.get('origin')
    if isinstance(origin, dict) and origin.get('kind') not in (None, 'human'):
        return None
    if record.get('turnOrigin') not in (None, 'human'):
        return None
    message = record.get('message')
    if not isinstance(message, dict):
        return 'ambiguous'
    content = message.get('content')
    if isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if any(b.get('type') == 'tool_result' for b in blocks):
            return 'ambiguous' if any(b.get('type') in ('text', 'image', 'document') for b in blocks) else None
    raw = content if isinstance(content, str) else '\n'.join(
        b.get('text', '') for b in content or [] if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str)) if isinstance(content, list) else ''
    if any(marker in raw for marker in coach.ECHO_MARKERS):
        return None
    if coach.prompt_text(record) is not None:
        return 'text'
    if isinstance(content, list) and any(isinstance(b, dict) and b.get('type') in ('image', 'document') for b in content):
        return 'nontext'
    return 'ambiguous'


def _read_file(path, until, resolve, coverage):
    events, calls, by_id, excluded_first_ids = [], [], {}, set()
    tail, current_repo = '', None
    first_ts = last_ts = None
    stopped = False
    with open(path, 'rb') as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                coverage['malformed_records'] += 1
                events.append(('barrier', None))
                continue
            if not isinstance(record, dict):
                coverage['malformed_records'] += 1
                events.append(('barrier', None))
                continue
            ts = claude.parse_ts(record.get('timestamp'))
            if ts is None:
                coverage['untimed_records'] += 1
            if record.get('type') in NONCONVERSATIONAL_METADATA and not isinstance(record.get('message'), dict) and 'content' not in record:
                # Verified Claude Code state mirrors, links and file history
                # are not user actions or assistant calls. Their often absent
                # timestamps must not sever a real conversation's ownership.
                coverage['ignored_metadata_records'] += 1
                coverage['ignored_untimed_metadata_records'] += ts is None
                coverage['ignored_outside_metadata_records'] += until is not None and ts is not None and ts > until
                continue
            outside = until is not None and (ts is None or ts > until)
            if outside:
                stopped = True
                coverage['cutoff_untimed_records' if ts is None else 'cutoff_excluded_records'] += 1
                message = record.get('message')
                if record.get('type') == 'assistant' and isinstance(message, dict):
                    identifier = message.get('id') or record.get('requestId') or record.get('uuid')
                    if isinstance(identifier, str) and identifier:
                        prior = by_id.get(('id', identifier))
                        if prior is None:
                            excluded_first_ids.add(identifier)
                        else:
                            prior['cutoff_truncated'] = True
                # Future or untimed rows can hide a human action. The fixed
                # window never treats them as known in-window observations.
                if not events or events[-1][0] != 'barrier':
                    events.append(('barrier', None))
                continue
            if ts is not None:
                first_ts = ts if first_ts is None else first_ts
                last_ts = ts
            if record.get('isSidechain'):
                coverage['excluded_sidechain_records'] += 1
                continue
            if 'cwd' in record:
                current_repo = resolve(record['cwd'])
            kind = record.get('type')
            if kind == 'user':
                boundary = _human_boundary(record)
                if boundary is None:
                    coverage['excluded_user_records'] += 1
                    continue
                text = coach.prompt_text(record) if boundary == 'text' else None
                uid = record.get('uuid')
                digest_text = text if text is not None else json.dumps(
                    (record.get('message') or {}).get('content') if isinstance(record.get('message'), dict) else None,
                    sort_keys=True, separators=(',', ':'), ensure_ascii=False)
                prompt = {'key': coach.prompt_key(text, tail) if text is not None else None,
                          'text_digest': hashlib.sha256(digest_text.encode('utf-8', 'surrogatepass')).digest(),
                          'uuid': uid if isinstance(uid, str) and uid else None,
                          'kind': boundary, 'repo': current_repo}
                events.append(('prompt', prompt))
            elif kind == 'assistant':
                # Match coach.prompt_key's preceding-text convention, including
                # synthetic/error text; these messages themselves are not calls.
                parts = coach.assistant_text(record)
                if parts:
                    tail = '\n'.join(([tail] if tail else []) + parts)[-coach.TAIL_CHARS:]
                message = record.get('message')
                if not isinstance(message, dict) or message.get('model') == '<synthetic>' or record.get('isApiErrorMessage'):
                    coverage['excluded_assistant_records'] += 1
                    continue
                identifier = message.get('id') or record.get('requestId') or record.get('uuid')
                identified = isinstance(identifier, str) and bool(identifier)
                local_id = ('id', identifier) if identified else ('unidentified', line_number)
                call = by_id.get(local_id)
                if call is None:
                    call = dict(_empty_usage(), identifier=identifier if identified else None,
                                model=model_label(message.get('model')), model_conflict=False,
                                speed=None, geo=None, repo=current_repo, iterations=None,
                                stop_reason=False, stream_updates=0, owner=None,
                                untimed=ts is None, window_unknown=identified and identifier in excluded_first_ids,
                                cutoff_truncated=False)
                    calls.append(call)
                    by_id[local_id] = call
                    events.append(('call', call))
                else:
                    call['stream_updates'] += 1
                    if model_label(message.get('model')) != call['model']:
                        call['model_conflict'] = True
                _usage_into(call, message.get('usage'))
                call['stop_reason'] |= message.get('stop_reason') is not None
                usage = message.get('usage')
                if isinstance(usage, dict) and isinstance(usage.get('iterations'), list):
                    call['iterations'] = usage['iterations']
    return {'events': events, 'calls': calls, 'first_ts': first_ts, 'last_ts': last_ts,
            'stopped': stopped, 'path': path}


def collect(root: str, until: float | None = None) -> dict:
    """Collect bounded metadata locally; no network or classifier calls.

    The returned internal object contains local join identities and must never
    be printed. Use build() for aggregate output or key_rows() for label joins.
    Files use coach's oldest-last-record ordering; records remain physical.
    """
    coverage = collections.Counter()
    sessions = []
    resolve = _repo_resolver()
    for path, kind in claude.discover(root) if os.path.isdir(root) else []:
        coverage['discovered_files'] += 1
        if kind != 'main':
            coverage['excluded_' + kind + '_files'] += 1
            continue
        coverage['main_files'] += 1
        try:
            sessions.append(_read_file(path, until, resolve, coverage))
        except OSError:
            coverage['unreadable_files'] += 1
    sessions.sort(key=lambda s: (s['last_ts'] is None, s['last_ts'] or 0, s['path']))
    canonical, first_uuid, earlier_keys, all_calls = [], {}, {}, {}
    for session_index, session in enumerate(sessions, 1):
        active, responded = None, True
        own_uuids, own_keys, own_canonical, boundaries = set(), {}, set(), []
        for event, value in session.pop('events'):
            if event == 'barrier':
                active, responded = ('barrier',), True
                boundaries.append({'prompt': None, 'copied': False, 'barrier': True})
            elif event == 'prompt':
                uid, key = value['uuid'], value['key']
                if uid is not None and uid in own_uuids:
                    # A duplicated historical row appended late is not a new
                    # human action and must not reset the current owner.
                    coverage['in_file_prompt_copies'] += 1
                    original = canonical[first_uuid[uid]]
                    if value['text_digest'] != original['text_digest'] or value['kind'] != original['kind']:
                        original['conflict'] = True
                        coverage['conflicting_prompt_copies'] += 1
                        # A changed record might be an edit or a new action;
                        # neither the old nor current label safely owns it.
                        active, responded = ('barrier',), True
                        boundaries.append({'prompt': None, 'copied': False, 'barrier': True})
                    continue
                copied = False
                prior = first_uuid.get(uid) if uid is not None else None
                if prior is None and uid is None and key is not None:
                    prior = earlier_keys.get(key)
                    if prior is not None:
                        coverage['key_only_prompt_copies'] += 1
                if prior is not None:
                    prompt = canonical[prior]
                    copied = True
                    coverage['copied_prompts'] += 1
                    if uid is not None and uid in first_uuid and value['text_digest'] != prompt['text_digest']:
                        prompt['conflict'] = True
                        coverage['conflicting_prompt_copies'] += 1
                    if prompt['repo'] is not None and value['repo'] is not None and prompt['repo'] != value['repo']:
                        prompt['repo'] = None
                        coverage['conflicting_prompt_repositories'] += 1
                else:
                    prompt = dict(value, index=len(canonical), session=session_index,
                                  position=len(boundaries) + 1, conflict=False)
                    canonical.append(prompt)
                    coverage['unique_prompts'] += 1
                    coverage[prompt['kind'] + '_prompts'] += 1
                index = prompt['index']
                if copied and index in own_canonical:
                    coverage['in_file_prompt_copies'] += 1
                    continue
                own_canonical.add(index)
                if uid is not None:
                    first_uuid.setdefault(uid, index)
                    own_uuids.add(uid)
                if key is not None:
                    own_keys.setdefault(key, index)
                boundaries.append({'prompt': index, 'copied': copied, 'barrier': False})
                if not responded and active is not None:
                    active = ('queued',)
                    coverage['queued_prompt_boundaries'] += 1
                else:
                    active = index
                responded = False
            else:
                call = value
                call['owner'] = active
                identifier = call['identifier']
                if identifier is None or identifier not in all_calls or all_calls[identifier]['owner'] == active:
                    responded = True
                if identifier is None:
                    coverage['unidentified_calls'] += 1
                    # Cannot establish global uniqueness: leave it out of the
                    # primary priced population, with explicit coverage.
                    call['owner'] = ('unidentified',)
                    all_calls[('unidentified', session_index, len(all_calls))] = call
                elif identifier not in all_calls:
                    all_calls[identifier] = call
                    call['copy_conflict'] = False
                else:
                    first = all_calls[identifier]
                    coverage['copied_calls'] += 1
                    if call['owner'] != first['owner']:
                        first['copy_conflict'] = True
                    if call['repo'] != first['repo']:
                        first['repo'] = None
                        coverage['conflicting_call_repositories'] += 1
                    if call['model'] != first['model'] or call['model_conflict']:
                        first['model_conflict'] = True
                    for modifier in ('speed', 'geo'):
                        if call[modifier] is not None:
                            if first[modifier] not in (None, call[modifier]):
                                first['model_conflict'] = True
                            first[modifier] = call[modifier]
                        seen = '_' + modifier + '_value'
                        if seen in call:
                            if seen in first and first[seen] != call[seen]:
                                first['modifier_conflict'] = True
                            first[seen] = call[seen]
                    first['modifier_conflict'] = bool(first.get('modifier_conflict') or call.get('modifier_conflict'))
                    # Streaming output can grow in copied transcripts. Input
                    # disagreement between copies is not a free price choice.
                    for field in ('in', 'cc', 'cc5', 'cc1', 'cr'):
                        if field in call['_fields'] and field in first['_fields'] and call[field] != first[field]:
                            first['invalid_usage'] = True
                        elif field in call['_fields']:
                            first[field] = call[field]
                    first['out'] = max(first['out'], call['out'])
                    first['_fields'] |= call['_fields']
                    first['split'] |= call['split']
                    first['invalid_usage'] |= call['invalid_usage']
                    first['stop_reason'] |= call['stop_reason']
                    first['stream_updates'] += call['stream_updates']
                    first['window_unknown'] |= call['window_unknown']
                    first['cutoff_truncated'] |= call['cutoff_truncated']
                    if call['iterations'] is not None:
                        if first['iterations'] is not None and call['iterations'] != first['iterations']:
                            first['invalid_iterations'] = True
                        else:
                            first['iterations'] = call['iterations']
        earlier_keys.update({k: earlier_keys.get(k, v) for k, v in own_keys.items()})
        session['boundaries'] = boundaries
        session['trailing_unanswered'] = not responded
        session.pop('path')
        session.pop('calls')
    return {'sessions': sessions, 'prompts': canonical, 'calls': list(all_calls.values()),
            'coverage': dict(coverage)}


def key_rows(collection: dict) -> list[dict]:
    """Hash-and-integer-only join rows. No prompt text or local identities.

    Hashes deliberately do not pass the general aggregate privacy gate. The
    caller must use an explicit opt-in and this narrow validation for export.
    """
    rows, seen = [], set()
    for prompt in collection['prompts']:
        if prompt['key'] is None or prompt['conflict'] or prompt['key'] in seen:
            continue
        seen.add(prompt['key'])
        row = {'session': prompt['session'], 'index': prompt['position'] - 1, 'key': prompt['key']}
        if not coach.KEY.fullmatch(row['key']) or not _integer(row['session']) or row['session'] < 1 or not _integer(row['index']):
            raise PrivacyError('invalid correction index key row')
        rows.append(row)
    return rows


def _bucket():
    return {'calls': 0, 'priced_calls': 0, 'unpriced_calls': 0, 'missing_usage_calls': 0,
            'invalid_usage_calls': 0, 'unidentified_calls': 0, 'window_unknown_calls': 0, 'known_usd': 0.0,
            'known_usd_by_tier': dict.fromkeys(TIERS, 0.0)}


def _add(bucket, status, costs):
    bucket['calls'] += 1
    if status != 'known':
        bucket[status + '_calls'] += 1
    else:
        bucket['priced_calls'] += 1
        bucket['known_usd'] += sum(costs)
        for tier, amount in zip(TIERS, costs):
            bucket['known_usd_by_tier'][tier] += amount


def _finish(bucket):
    if bucket['priced_calls'] == 0:
        bucket['known_usd'] = None
        bucket['known_usd_by_tier'] = dict.fromkeys(TIERS, None)
    return bucket


def _priced(call, prices):
    if call.get('window_unknown'):
        return 'window_unknown', None
    status = _usage_status(call)
    if status != 'known':
        return status, None
    if call.get('identifier') is None and not call.get('attempt'):
        return 'unidentified', None
    base, _ = prices.base_row(call['model'])
    modifier_unknown = call.get('modifier_conflict') or (call.get('speed') == 'fast' and base not in prices.fast)
    rates = prices.rates(call['model'], call.get('speed'), call.get('geo')) if not call.get('model_conflict') and not modifier_unknown else None
    if rates is None:
        return 'unpriced', None
    return 'known', claude_attempt_cost(rates, call['in'], call['cc5'], call['cc1'], call['cc'], call['cr'], call['out'])


def _fallback_attempts(call):
    """A verified served fallback followed only earlier message attempts.

    A single message iteration usually describes the already served call. It
    is never automatically a declined charge. Unmarked/mixed arrays remain
    unresolved rather than contributing invented attempted costs.
    """
    iterations = call.get('iterations')
    if not isinstance(iterations, list) or len(iterations) < 2 or call.get('invalid_iterations'):
        return None
    if not all(isinstance(attempt, dict) for attempt in iterations):
        return None
    if iterations[-1].get('type') != 'fallback_message' or any(a.get('type') != 'message' for a in iterations[:-1]):
        return None
    served = iterations[-1]
    candidate = _empty_usage()
    _usage_into(candidate, served)
    if _usage_status(candidate) != 'known' or call['invalid_usage'] or call.get('model_conflict'):
        return None
    served_model = model_label(served.get('model'))
    if served_model in ('unknown', 'other-model') or served_model != call['model']:
        return None
    if not {'in', 'out'} <= call['_fields'] or any(candidate[field] != call[field] for field in ('in', 'out', 'cc', 'cr')):
        return None
    return iterations[:-1], iterations[-1]


def _served_usage(call):
    """Enrich served tiers/modifiers only from a matching final served arm."""
    attempts = _fallback_attempts(call)
    if attempts is None:
        return call, False
    raw = attempts[1]
    if model_label(raw.get('model')) != call['model']:
        return call, False
    if not all(raw.get(field, 0) == call[short] for field, short in TOKEN_FIELDS.items()):
        return call, False
    candidate = _empty_usage()
    _usage_into(candidate, raw)
    if _usage_status(candidate) != 'known':
        return call, False
    # Aggregate inputs/output and served model independently agree; only the
    # recorded served attempt's internally consistent tier split replaces the
    # top-level split that came from a different attempt.
    effective = dict(call)
    split_resolved = candidate['split'] and (
        not call['split'] or call['cc5'] != candidate['cc5'] or call['cc1'] != candidate['cc1'])
    if split_resolved:
        effective.update(cc5=candidate['cc5'], cc1=candidate['cc1'], split=True)
    for modifier in ('speed', 'geo'):
        seen = '_' + modifier + '_value'
        if seen in candidate:
            if seen in effective and effective[seen] != candidate[seen]:
                effective['modifier_conflict'] = True
            else:
                effective[seen] = candidate[seen]
                effective[modifier] = candidate[modifier]
    effective['modifier_conflict'] = bool(effective.get('modifier_conflict') or candidate.get('modifier_conflict'))
    return effective, bool(split_resolved)


def build(collection: dict, labels: dict, prices, classifier: str = 'labels-file') -> dict:
    """Build aggregate served API-list-price equivalents, with visible gaps.

    Only the five Anatomy classes plus unknown are accepted. Retry and nudge
    are actual boundaries and known noncorrections, never transparent windows.
    Declined attempts are separately estimated; never added to served totals.
    """
    if not isinstance(labels, dict) or any(not isinstance(k, str) or not coach.KEY.fullmatch(k) or v not in LABELS for k, v in labels.items()):
        raise ValueError('invalid correction index labels')
    if not isinstance(classifier, str) or not coach.CLASSIFIER_ID.fullmatch(classifier):
        raise ValueError('invalid correction index classifier')
    prompts = collection['prompts']
    prompt_labels = [labels.get(p['key'], 'unknown') if p['key'] is not None and not p['conflict'] else 'unknown' for p in prompts]
    coverage = collections.Counter(collection['coverage'])
    coverage['sessions'] = len(collection['sessions'])
    coverage['trailing_unanswered_sessions'] = sum(s['trailing_unanswered'] for s in collection['sessions'])
    coverage['cutoff_filtered_sessions'] = sum(s['stopped'] for s in collection['sessions'])
    coverage['served_calls'] = len(collection['calls'])
    coverage['calls_without_stop_reason'] = sum(not c['stop_reason'] for c in collection['calls'])
    coverage['untimed_calls'] = sum(c['untimed'] for c in collection['calls'])
    coverage['cutoff_truncated_calls'] = sum(c['cutoff_truncated'] for c in collection['calls'])
    coverage['stream_updates'] = sum(c['stream_updates'] for c in collection['calls'])
    coverage['unused_label_keys'] = len(set(labels) - {p['key'] for p in prompts if p['key'] is not None})
    for counter in ('assumed_5m_cache_write_calls', 'assumed_5m_cache_write_tokens', 'assumed_global_geo_calls'):
        coverage[counter] = 0
    repos = {}
    def alias(identity):
        if identity is None:
            return 'unknown-repo'
        if identity not in repos:
            repos[identity] = 'repo-%d' % (len(repos) + 1)
        return repos[identity]
    for prompt in prompts:
        alias(prompt['repo'])
    total = _bucket()
    by_label = {name: _bucket() for name in OWNER_LABELS}
    by_repo, by_model, repo_labels, model_labels = {}, {}, {}, {}
    declined = {name: _bucket() for name in ('streamed_output_estimate', 'pre_output_uncertain')}
    declined_labels = {name: {label: _bucket() for label in OWNER_LABELS} for name in declined}
    owned_prompts = set()
    for call in collection['calls']:
        owner = call['owner']
        if call.get('copy_conflict') or owner == ('queued',):
            label = 'ambiguous'
            coverage['ambiguous_owner_calls'] += 1
        elif isinstance(owner, int):
            label = prompt_labels[owner]
            owned_prompts.add(owner)
        elif owner == ('barrier',):
            label = 'unknown'
            coverage['malformed_boundary_owner_calls'] += 1
        else:
            label = 'unattributed'
            coverage['unattributed_calls'] += 1
        # A job's repo is the working directory at its owning human prompt.
        # The assistant can later report a parent directory or change cwd.
        # Queues and unowned calls have no single identifiable job repository.
        repo_identity = prompts[owner]['repo'] if isinstance(owner, int) and not call.get('copy_conflict') else None
        if isinstance(owner, int) and prompts[owner]['repo'] != call['repo']:
            coverage['owned_calls_changed_cwd'] += 1
        repo, model = alias(repo_identity), call['model']
        effective, split_resolved = _served_usage(call)
        coverage['served_cache_split_from_fallback_calls'] += split_resolved
        if effective['cc'] > 0 and not effective['split']:
            coverage['assumed_5m_cache_write_calls'] += 1
            coverage['assumed_5m_cache_write_tokens'] += effective['cc']
        if effective.get('_geo_value') not in ('global', 'us') and not effective.get('modifier_conflict'):
            coverage['assumed_global_geo_calls'] += 1
        status, costs = _priced(effective, prices)
        if status == 'known' and effective.get('_geo_value') not in ('global', 'us'):
            coverage['priced_assumed_global_geo_calls'] += 1
        for bucket in (total, by_label[label], by_repo.setdefault(repo, _bucket()), by_model.setdefault(model, _bucket())):
            _add(bucket, status, costs)
        for grouping, identity in ((repo_labels, repo), (model_labels, model)):
            _add(grouping.setdefault(identity, {name: _bucket() for name in OWNER_LABELS})[label], status, costs)
        if call.get('iterations'):
            coverage['calls_with_iterations'] += 1
            if call['window_unknown'] or call['identifier'] is None:
                coverage['declined_excluded_unidentified_or_window_calls'] += 1
                continue
            if call.get('invalid_iterations'):
                coverage['conflicting_iteration_calls'] += 1
                continue
            attempts = _fallback_attempts(call)
            if attempts is None:
                coverage['single_iteration_calls' if len(call['iterations']) == 1 else 'unresolved_iteration_calls'] += 1
                continue
            coverage['verified_fallback_calls'] += 1
            for raw in attempts[0]:
                attempt = dict(_empty_usage(), attempt=True, model=model_label(raw.get('model')),
                               speed=None, geo=None, model_conflict=False)
                _usage_into(attempt, raw)
                if attempt.get('_geo_value') not in ('global', 'us') and not attempt.get('modifier_conflict'):
                    coverage['declined_assumed_global_geo_calls'] += 1
                if attempt['cc'] > 0 and not attempt['split']:
                    coverage['declined_assumed_5m_cache_write_calls'] += 1
                    coverage['declined_assumed_5m_cache_write_tokens'] += attempt['cc']
                category = 'streamed_output_estimate' if attempt['out'] > 0 else 'pre_output_uncertain'
                a_status, a_costs = _priced(attempt, prices)
                _add(declined[category], a_status, a_costs)
                _add(declined_labels[category][label], a_status, a_costs)
    coverage['prompts_without_unambiguous_owned_calls'] = len(prompts) - len(owned_prompts)
    coverage['conflicting_call_owners'] = sum(bool(c.get('copy_conflict')) for c in collection['calls'])
    coverage['conflicting_call_models'] = sum(bool(c['model_conflict']) for c in collection['calls'])
    # Literal adjacency: unknown/nontext/malformed boundaries censor outcomes;
    # retry/nudge are known noncorrections and end runs. CC anchors overlap.
    transitions = {name: {'next_corrections': 0, 'next_known': 0, 'next_unknown': 0, 'end_or_boundary': 0}
                   for name in ('after_correction', 'after_two_adjacent_corrections', 'after_noncorrection')}
    initial_two = 0
    for session in collection['sessions']:
        seq = session['boundaries']
        run = 0
        for position, boundary in enumerate(seq):
            index = boundary['prompt']
            label = prompt_labels[index] if index is not None else 'unknown'
            run = run + 1 if label == 'correction' else 0
            if label == 'correction' and run == 2 and not boundary['copied']:
                initial_two += 1
            if label == 'unknown' or boundary['copied']:
                continue
            names = ['after_correction'] if label == 'correction' else ['after_noncorrection']
            if label == 'correction' and run >= 2:
                names.append('after_two_adjacent_corrections')
            nxt = seq[position + 1] if position + 1 < len(seq) else None
            for name in names:
                bucket = transitions[name]
                if nxt is None or nxt['copied']:
                    bucket['end_or_boundary'] += 1
                else:
                    next_label = prompt_labels[nxt['prompt']] if nxt['prompt'] is not None else 'unknown'
                    if next_label == 'unknown':
                        bucket['next_unknown'] += 1
                    else:
                        bucket['next_known'] += 1
                        bucket['next_corrections'] += next_label == 'correction'
    for bucket in transitions.values():
        bucket['rate'] = bucket['next_corrections'] / bucket['next_known'] if bucket['next_known'] else None
    result = {'format': FORMAT, 'provider': 'claude-code', 'classifier': classifier,
              'scope': 'main transcripts only', 'association': 'observed calls following prompt labels',
              'repo_grouping': 'owning prompt git common-dir',
              'price_snapshot': {'basis': 'estimated', 'date': prices.snapshot_date or 'unknown',
                                 'source_url': prices.source_url or 'unknown',
                                 'geo_assumption': 'global rates when geographic metadata unknown'},
              'coverage': {'basis': 'observed', **dict(coverage)},
              'prompt_labels': {'basis': 'estimated', 'counts': dict(collections.Counter(prompt_labels)),
                                'known': sum(label != 'unknown' for label in prompt_labels),
                                'unknown': sum(label == 'unknown' for label in prompt_labels)},
              'served_api_list_price_equivalent': {'basis': 'estimated', 'currency': 'USD',
                  'total': _finish(total), 'by_label': {k: _finish(v) for k, v in by_label.items()},
                  'by_repo': {k: dict(_finish(v), by_label={name: _finish(bucket) for name, bucket in repo_labels[k].items()}) for k, v in by_repo.items()},
                  'by_model': {k: dict(_finish(v), by_label={name: _finish(bucket) for name, bucket in model_labels[k].items()}) for k, v in by_model.items()}},
              'declined_api_list_price_equivalent': {'basis': 'estimated',
                  'categories': {k: _finish(v) for k, v in declined.items()},
                  'by_label': {category: {k: _finish(v) for k, v in buckets.items()} for category, buckets in declined_labels.items()}},
              'literal_adjacent_outcomes': {'basis': 'estimated', 'buckets': transitions,
                                           'initial_two_correction_episodes': initial_two,
                                           'two_correction_anchors_overlap': True},
              'invoiced': {'basis': 'invoiced', 'status': 'not available'}}
    return gate(result)


def render_text(report: dict) -> str:
    """Small aggregate-only display; every number carries its evidence basis."""
    gate(report)
    def money(value):
        return 'not available' if value is None else '$%.4f' % value
    served = report['served_api_list_price_equivalent']
    total, coverage = served['total'], report['coverage']
    lines = ['Anatomy Correction Index',
             'Observed calls following estimated prompt labels; main Claude Code transcripts.',
             'API list-price equivalent: %s [estimated]' % money(total['known_usd']),
             'Served calls: %d; priced: %d; unknown price: %d; missing usage: %d; invalid usage: %d [observed]' % (
                 total['calls'], total['priced_calls'], total['unpriced_calls'], total['missing_usage_calls'], total['invalid_usage_calls'])]
    for name, bucket in served['by_label'].items():
        if bucket['calls']:
            lines.append('%s: %s; calls %d; priced %d [estimated]' % (name, money(bucket['known_usd']), bucket['calls'], bucket['priced_calls']))
    lines.append('Prompts: %d; unknown labels: %d; ambiguous-owner calls: %d [observed]' % (
        coverage.get('unique_prompts', 0), report['prompt_labels']['unknown'], coverage.get('ambiguous_owner_calls', 0)))
    known = report['prompt_labels']['known']
    corrections = report['prompt_labels']['counts'].get('correction', 0)
    frequency = 'not available' if not known else '%.1f%%' % (100 * corrections / known)
    lines.append('Correction frequency among known labels: %s (%d of %d); unknown %d [estimated]' % (
        frequency, corrections, known, report['prompt_labels']['unknown']))
    correction_dollars = served['by_label']['correction']['known_usd']
    share = 'not available' if correction_dollars is None or total['known_usd'] in (None, 0) else '%.1f%%' % (
        100 * correction_dollars / total['known_usd'])
    lines.append('Correction share of known priced served dollars: %s; unknown-label calls included, unpriced usage excluded [estimated]' % share)
    labeled_dollars = sum(served['by_label'][label]['known_usd'] or 0 for label in coach.LABELS)
    labeled_share = 'not available' if total['known_usd'] in (None, 0) else '%.1f%%' % (
        100 * labeled_dollars / total['known_usd'])
    lines.append('Known-label share of priced served dollars: %s; remaining ownership or labels unresolved [estimated]' % labeled_share)
    lines.append('Literal next-prompt label associations; unknown and terminal outcomes are separate:')
    for name, bucket in report['literal_adjacent_outcomes']['buckets'].items():
        rate = 'not available' if bucket['rate'] is None else '%.1f%%' % (100 * bucket['rate'])
        lines.append('  %s: %s (%d of %d known); next unknown %d; end or boundary %d [estimated]' % (
            name, rate, bucket['next_corrections'], bucket['next_known'],
            bucket['next_unknown'], bucket['end_or_boundary']))
    lines.append('Initial two-correction episodes: %d; after-two anchors may overlap [estimated]' %
                 report['literal_adjacent_outcomes']['initial_two_correction_episodes'])
    for field, title in (('by_repo', 'Repository aliases (specific to this scan)'),
                         ('by_model', 'Response models (not the models that caused a correction)')):
        lines.append(title + ':')
        for identity, bucket in served[field].items():
            correction = bucket['by_label']['correction']['known_usd']
            lines.append('  %s: total %s; following corrections %s; priced %d of %d calls [estimated]' % (
                identity, money(bucket['known_usd']), money(correction), bucket['priced_calls'], bucket['calls']))
    lines.append('Coverage: excluded subagent files %d; excluded workflow files %d; unreadable files %d [observed]' % (
        coverage.get('excluded_subagent_files', 0), coverage.get('excluded_workflow_agent_files', 0),
        coverage.get('unreadable_files', 0)))
    lines.append('Coverage: unanswered sessions %d; responses without stop reason %d; cutoff-truncated responses %d [observed]' % (
        coverage.get('trailing_unanswered_sessions', 0), coverage.get('calls_without_stop_reason', 0),
        coverage.get('cutoff_truncated_calls', 0)))
    lines.append('Unsplit cache writes priced at assumed 5m tier: %d calls; %d tokens [estimated]' % (
        coverage.get('assumed_5m_cache_write_calls', 0), coverage.get('assumed_5m_cache_write_tokens', 0)))
    lines.append('Priced calls with unknown geography assumed at global rates: %d [estimated]' %
                 coverage.get('priced_assumed_global_geo_calls', 0))
    lines.append('Iteration coverage: verified fallbacks %d; single iterations %d; unresolved arrays %d [observed]' % (
        coverage.get('verified_fallback_calls', 0), coverage.get('single_iteration_calls', 0),
        coverage.get('unresolved_iteration_calls', 0)))
    lines.append('Declined attempts are separate estimates; no invoiced total available [invoiced]')
    snapshot = report['price_snapshot']
    lines.append('Price snapshot %s: %s [estimated]' % (snapshot['date'], snapshot['source_url']))
    if 'label_audit' not in report:
        lines.append('Classifier accuracy: not assessed; supply independently reviewed labels.')
    lines.append('This report does not measure avoidable waste, recovery benefit or model quality.')
    return assert_clean('\n'.join(lines) + '\n')
