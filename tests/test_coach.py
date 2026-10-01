"""coach-baseline: the coach import contract, the bucket math, copy dedupe, privacy and the consent gate.

No test here can start the real claude CLI: setUpModule replaces subprocess.run for this module's run,
and every classifier test injects its own runner.
"""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tests import helpers  # noqa: F401  (puts src on the path)
from anatomy import cli, coach
from anatomy.privacy import PrivacyError

SECRET = 'sk-ant-api03-PLANTEDsecretPLANTEDsecret'
SECRET_PATH = '/Users/alice/private-repo/notes.txt'
TRANSCRIPT_SID = '7c1d9a52-0000-4000-8000-planted0sid'
COACH_SID = 'coach_session-01'

_patch = None


def _no_subprocess(*a, **k):
    raise AssertionError('a test tried to run a subprocess')


def setUpModule():
    global _patch
    _patch = mock.patch.object(subprocess, 'run', _no_subprocess)
    _patch.start()


def tearDownModule():
    _patch.stop()


# ---------------------------------------------------------------- transcript builder
class T:
    """Write main-thread transcripts: root/<project>/<name>.jsonl."""

    def __init__(self, root):
        self.root = root
        self.n = 0

    def write(self, name, records, project='-proj'):
        d = os.path.join(self.root, project)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name + '.jsonl'), 'w', encoding='utf-8') as fh:
            for r in records:
                fh.write(json.dumps(r) + '\n')

    def ts(self, sec):
        return '2026-09-01T%02d:%02d:%02d.000Z' % (10 + sec // 3600, sec // 60 % 60, sec % 60)


def user(text, sec, uuid=None, **extra):
    return {'type': 'user', 'uuid': uuid or 'u-%s-%d' % (abs(hash(text)) % 10**8, sec), 'sessionId': TRANSCRIPT_SID,
            'cwd': SECRET_PATH, 'timestamp': T.ts(None, sec), 'message': {'role': 'user', 'content': text}, **extra}


def asst(text, sec):
    return {'type': 'assistant', 'sessionId': TRANSCRIPT_SID, 'timestamp': T.ts(None, sec),
            'message': {'role': 'assistant', 'model': 'claude-opus-4-5', 'content': [{'type': 'text', 'text': text}]}}


def conversation(prompts, start=0, uuids=None):
    """Alternate user prompts and assistant replies; reply i is unique text."""
    out = []
    for i, p in enumerate(prompts):
        out.append(user(p, start + 2 * i, uuid=(uuids[i] if uuids else None)))
        out.append(asst('reply %d to %s %s' % (i, p[:6], SECRET), start + 2 * i + 1))
    return out


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(['coach-baseline', '--workers', '1', *argv])
    return code, out.getvalue(), err.getvalue()


def label_all(sessions, fn):
    """labels by key from fn(prompt text)."""
    return {p['key']: fn(p['text']) for s in sessions for p in s['prompts']}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, 'projects')
        self.t = T(self.root)
        self.environment = mock.patch.dict(os.environ, {'HOME': self.tmp.name, 'PATH': os.defpath}, clear=True)
        self.environment.start()
        self.real_policy_paths = coach._managed_policy_paths
        self.policies = mock.patch.object(coach, '_managed_policy_paths', return_value=[])
        self.policies.start()

    def tearDown(self):
        self.policies.stop()
        self.environment.stop()
        self.tmp.cleanup()

    def labels_file(self, mapping):
        sessions, _ = coach.collect(self.root)
        path = os.path.join(self.tmp.name, 'labels.jsonl')
        with open(path, 'w') as fh:
            for s in sessions:
                for p in s['prompts']:
                    lab = mapping(p['text'])
                    if lab is not None:
                        fh.write(json.dumps({'key': p['key'], 'label': lab}) + '\n')
        return path


def by_prefix(text):
    for lab in coach.LABELS + (coach.UNKNOWN,):
        if text.startswith(lab):
            return lab
    return 'new'


# ---------------------------------------------------------------- contract
class Contract(Base):
    def test_exact_keys_and_invariants(self):
        self.t.write('a', conversation(['new start', 'correction one', 'correction two', 'new ask', 'correction three']))
        self.t.write('b', conversation(['new other', 'bug_report it fails', 'correction x', 'new y'], start=100))
        code, out, err = run('--claude-dir', self.root, '--coach-session', COACH_SID, '--coach-provider', 'claude',
                             '--labels', self.labels_file(by_prefix), '--json')
        self.assertEqual(code, 0, err)
        self.assertLessEqual(len(out), coach.MAX_JSON_CHARS)
        env = json.loads(out)
        self.assertEqual(set(env), {'format', 'sessionId', 'provider', 'prompt', 'baseline'})
        self.assertEqual(env['format'], 'emilia.anatomy.coach.v1')
        self.assertEqual(env['sessionId'], COACH_SID)
        self.assertIn(env['provider'], ('chatgpt', 'claude', 'native'))
        self.assertIsNone(env['prompt'])
        b = env['baseline']
        self.assertEqual(set(b), {'provider', 'promptCount', 'sessionCount', 'classifier', 'afterOne', 'afterTwo', 'otherwise'})
        self.assertEqual(b['provider'], 'claude-code')
        self.assertEqual(b['classifier'], 'labels-file')
        self.assertEqual((b['promptCount'], b['sessionCount']), (9, 2))
        self.assertTrue(1 <= b['sessionCount'] <= b['promptCount'])
        for k in ('afterOne', 'afterTwo', 'otherwise'):
            self.assertEqual(set(b[k]), {'corrections', 'observed'})
            self.assertTrue(0 <= b[k]['corrections'] <= b[k]['observed'] <= b['promptCount'])
        # a: idx1 corr after new (otherwise 1/1), idx2 corr after corr (afterOne 1/1), idx3 new after corr,corr
        # (afterOne, afterTwo 0/1), idx4 corr after new (otherwise). b: idx1 bug after new, idx2 corr after bug,
        # idx3 new after corr.
        self.assertEqual(b['afterOne'], {'corrections': 1, 'observed': 3})
        self.assertEqual(b['afterTwo'], {'corrections': 0, 'observed': 1})
        self.assertEqual(b['otherwise'], {'corrections': 3, 'observed': 4})
        coach.validate(env)

    def test_out_file_matches_json_and_text_summary_has_no_session_id(self):
        self.t.write('a', conversation(['new a', 'correction b', 'new c']))
        out_path = os.path.join(self.tmp.name, 'coach.json')
        code, out, err = run('--claude-dir', self.root, '--coach-session', COACH_SID, '--coach-provider', 'native',
                             '--labels', self.labels_file(by_prefix), '--out', out_path)
        self.assertEqual(code, 0, err)
        with open(out_path) as fh:
            env = json.load(fh)
        coach.validate(env)
        self.assertEqual(env['provider'], 'native')
        self.assertNotIn(COACH_SID, out)
        self.assertIn('[estimated]', out)

    def test_validate_refuses_contract_breaks(self):
        good = coach.envelope('s1', 'claude', {'provider': 'claude-code', 'promptCount': 3, 'sessionCount': 1,
                                               'classifier': 'x', 'afterOne': {'corrections': 1, 'observed': 2},
                                               'afterTwo': {'corrections': 0, 'observed': 1},
                                               'otherwise': {'corrections': 0, 'observed': 1}})
        coach.validate(good)
        bad = [
            lambda e: e.update(extra=1),
            lambda e: e.update(provider='codex'),
            lambda e: e.update(prompt={}),
            lambda e: e.update(sessionId='has space'),
            lambda e: e['baseline'].update(promptCount=0, sessionCount=0),
            lambda e: e['baseline'].update(sessionCount=4),
            lambda e: e['baseline'].update(classifier='bad id!'),
            lambda e: e['baseline']['afterOne'].update(corrections=3),
            lambda e: e['baseline']['otherwise'].update(observed=4),
            lambda e: e['baseline']['afterTwo'].update(rate=0.5),
            lambda e: e['baseline'].update(promptCount=True),
        ]
        for f in bad:
            e = json.loads(json.dumps(good))
            f(e)
            with self.assertRaises(ValueError):
                coach.validate(e)

    def test_refusals_before_reading(self):
        for argv in (['--coach-provider', 'claude', '--labels', 'x'],
                     ['--coach-session', 'bad id', '--coach-provider', 'claude', '--labels', 'x'],
                     ['--coach-session', COACH_SID, '--labels', 'x'],
                     ['--coach-session', COACH_SID, '--coach-provider', 'claude']):
            code, out, err = run('--claude-dir', self.root, *argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(out, '')

    def test_no_prompts_is_a_refusal(self):
        lab = os.path.join(self.tmp.name, 'empty.jsonl')
        open(lab, 'w').close()
        code, out, err = run('--claude-dir', self.root, '--coach-session', COACH_SID, '--coach-provider', 'claude',
                             '--labels', lab)
        self.assertEqual(code, 2)
        self.assertEqual(out, '')


# ---------------------------------------------------------------- bucket math
def seq(*labels, copied=()):
    """One session whose prompts carry these labels; keys are the label plus the index."""
    prompts = [{'key': '%s-%d' % (lab, i), 'text': '', 'tail': '', 'copied': i in copied} for i, lab in enumerate(labels)]
    return {'prompts': prompts}, {p['key']: lab for p, lab in zip(prompts, labels) if lab != coach.UNKNOWN}


def tally(*labels, copied=()):
    s, lab = seq(*labels, copied=copied)
    return coach.tally([s], lab)


class Buckets(Base):
    def test_index_zero_is_never_counted(self):
        t = tally('correction')
        self.assertEqual(t['buckets'], {k: {'corrections': 0, 'observed': 0} for k in coach.BUCKETS})
        self.assertEqual(t['skipped']['first_prompt'], 1)
        self.assertEqual((t['promptCount'], t['sessionCount']), (1, 1))

    def test_after_two_needs_two_corrections_in_a_row(self):
        t = tally('new', 'correction', 'new', 'correction', 'correction')
        b = t['buckets']
        # idx2 after one correction (new), idx3 otherwise, idx4 after one correction (prev two: corr, new)
        self.assertEqual(b['afterOne'], {'corrections': 1, 'observed': 2})
        self.assertEqual(b['afterTwo'], {'corrections': 0, 'observed': 0})
        t = tally('new', 'correction', 'correction', 'correction', 'new')
        b = t['buckets']
        self.assertEqual(b['afterTwo'], {'corrections': 1, 'observed': 2})
        self.assertEqual(b['afterOne'], {'corrections': 2, 'observed': 3})

    def test_first_prompt_can_be_the_earlier_of_two(self):
        b = tally('correction', 'correction', 'new')['buckets']
        self.assertEqual(b['afterTwo'], {'corrections': 0, 'observed': 1})

    def test_unknown_breaks_the_streak_and_is_never_a_non_correction(self):
        t = tally('new', 'correction', 'unknown', 'correction', 'new')
        b = t['buckets']
        # idx2 unknown: not counted. idx3 after unknown: not counted. idx4 after one correction, two back unknown
        self.assertEqual(b['afterOne'], {'corrections': 0, 'observed': 1})
        self.assertEqual(b['afterTwo'], {'corrections': 0, 'observed': 0})
        self.assertEqual(b['otherwise'], {'corrections': 1, 'observed': 1})   # idx1 only
        self.assertEqual(t['skipped']['unknown_label'], 1)
        self.assertEqual(t['skipped']['after_unknown'], 1)
        self.assertEqual(t['skipped']['after_two_unknown'], 1)
        # an unknown in the middle of a run never lets the run continue into afterTwo
        b = tally('new', 'correction', 'unknown', 'correction', 'correction')['buckets']
        self.assertEqual(b['afterTwo'], {'corrections': 0, 'observed': 0})

    def test_retry_and_nudge_are_transparent_and_not_counted(self):
        t = tally('new', 'correction', 'retry', 'nudge', 'correction', 'new')
        b = t['buckets']
        self.assertEqual(b['afterOne'], {'corrections': 1, 'observed': 2})
        self.assertEqual(b['afterTwo'], {'corrections': 0, 'observed': 1})
        self.assertEqual(t['skipped']['retry_or_nudge'], 2)
        self.assertEqual(t['promptCount'], 6)

    def test_copies_are_history_not_counts(self):
        # a resumed session: the first three prompts are copies of an earlier session
        t = tally('new', 'correction', 'correction', 'correction', 'new', copied=(0, 1, 2))
        b = t['buckets']
        self.assertEqual(t['promptCount'], 2)
        self.assertEqual(t['copied'], 3)
        self.assertEqual(b['afterTwo'], {'corrections': 1, 'observed': 2})
        self.assertEqual(sum(v['observed'] for v in b.values()) - b['afterTwo']['observed'], 2)

    def test_all_copied_session_is_not_a_session(self):
        t = coach.tally([seq('new', 'correction')[0], seq('new', copied=(0,))[0]],
                        {'new-0': 'new', 'correction-1': 'correction'})
        self.assertEqual(t['sessionCount'], 1)


# ---------------------------------------------------------------- copies
class Copies(Base):
    def test_resumed_session_counted_once(self):
        first = conversation(['new a', 'correction b', 'new c'], uuids=['u1', 'u2', 'u3'])
        self.t.write('orig', first)
        # the resumed file repeats the history with the same records, then goes on
        self.t.write('resumed', first + conversation(['correction d', 'new e'], start=200))
        sessions, _ = coach.collect(self.root)
        self.assertEqual([sum(p['copied'] for p in s['prompts']) for s in sessions], [0, 3])
        t = coach.tally(sessions, label_all(sessions, by_prefix))
        self.assertEqual((t['promptCount'], t['sessionCount'], t['copied']), (5, 2, 3))

    def test_copy_with_a_different_context_is_caught_by_uuid_and_keeps_its_label(self):
        self.t.write('orig', conversation(['new a', 'correction b'], uuids=['u1', 'u2']))
        # the copy lost the assistant text before it, so its key differs; its record uuid does not
        self.t.write('resumed', [user('correction b', 300, uuid='u2'), asst('later', 301), user('correction c', 302, uuid='u9'),
                                 asst('x', 303), user('new d', 304, uuid='u10')])
        sessions, _ = coach.collect(self.root)
        resumed = sessions[1]['prompts']
        self.assertTrue(resumed[0]['copied'])
        self.assertEqual(resumed[0]['key'], sessions[0]['prompts'][1]['key'])
        self.assertTrue(all('uuid' not in p for s in sessions for p in s['prompts']))
        labels = {p['key']: by_prefix(p['text']) for s in sessions for p in s['prompts'] if not p['copied']}
        t = coach.tally(sessions, labels)
        self.assertEqual(t['promptCount'], 4)
        # correction c follows the copied correction b: after one; new d follows two corrections
        self.assertEqual(t['buckets']['afterOne'], {'corrections': 1, 'observed': 2})
        self.assertEqual(t['buckets']['afterTwo'], {'corrections': 0, 'observed': 1})

    def test_history_replayed_into_the_same_file_counted_once(self):
        conv = conversation(['new a', 'correction b', 'new c'], uuids=['u1', 'u2', 'u3'])
        self.t.write('one', conv + conv + conversation(['new d'], start=50, uuids=['u4']))
        sessions, _ = coach.collect(self.root)
        t = coach.tally(sessions, label_all(sessions, by_prefix))
        self.assertEqual((t['promptCount'], t['copied']), (4, 3))

    def test_older_file_wins_even_when_listed_later(self):
        conv = conversation(['new a', 'correction b'], uuids=['u1', 'u2'])
        self.t.write('a-newer', conv + conversation(['new z'], start=900, uuids=['u3']))
        self.t.write('z-older', conv)
        sessions, _ = coach.collect(self.root)
        self.assertEqual([len(s['prompts']) for s in sessions], [2, 3])
        self.assertEqual([p['copied'] for p in sessions[1]['prompts']], [True, True, False])

    def test_unique_prompts_skip_copies_and_repeats(self):
        conv = conversation(['new a', 'correction b'], uuids=['u1', 'u2'])
        self.t.write('orig', conv)
        self.t.write('resumed', conv + conversation(['new c'], start=99, uuids=['u3']))
        sessions, _ = coach.collect(self.root)
        self.assertEqual(sorted(p['text'] for p in coach.unique_prompts(sessions)), ['correction b', 'new a', 'new c'])

    def test_print_keys_lists_each_key_once(self):
        # the same words after the same reply, twice in one session: one key, listed once
        same = 'same reply ' * 60   # longer than the tail, so the tail before both is the same
        self.t.write('s', [asst(same, 0), user('new again', 1, uuid='k1'), asst('x', 2), user('new b', 3, uuid='k2'),
                           asst(same, 4), user('new again', 5, uuid='k3')])
        sessions, _ = coach.collect(self.root)
        keys = [p['key'] for p in sessions[0]['prompts']]
        self.assertEqual(keys[0], keys[2])
        lines = coach.keys_lines(sessions).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(coach.tally(sessions, label_all(sessions, by_prefix))['promptCount'], 3)

    def test_what_is_not_a_prompt(self):
        recs = [
            user('new real', 0),
            {'type': 'user', 'timestamp': T.ts(None, 1), 'message': {'content': [{'type': 'tool_result', 'content': 'x'}]}},
            user('meta', 2, isMeta=True),
            user('summary', 3, isCompactSummary=True),
            user('<command-name>/clear</command-name>', 4),
            user('<system-reminder>only a reminder</system-reminder>', 5),
            user('[Request interrupted by user]', 6),
            user('notification', 7, origin={'kind': 'task-notification'}),
            user('side', 8, isSidechain=True),
            user('<system-reminder>r</system-reminder> new kept', 9),
        ]
        self.t.write('s', recs)
        sessions, _ = coach.collect(self.root)
        self.assertEqual([p['text'] for p in sessions[0]['prompts']], ['new real', 'new kept'])

    def test_until_stops_reading(self):
        self.t.write('s', conversation(['new a', 'correction b', 'new c']))
        sessions, _ = coach.collect(self.root, until=coach.claude_ingest.parse_ts(T.ts(None, 2)))
        self.assertEqual(len(sessions[0]['prompts']), 2)


# ---------------------------------------------------------------- privacy
class Privacy(Base):
    def planted(self):
        return [SECRET, SECRET_PATH, TRANSCRIPT_SID, 'PLANTED', self.root, self.tmp.name, 'private-repo']

    def assert_clean(self, *texts):
        for text in texts:
            for s in self.planted():
                self.assertNotIn(s, text)

    def write_secret_sessions(self):
        self.t.write('s', conversation(['new %s at %s' % (SECRET, SECRET_PATH), 'correction %s' % SECRET, 'new c']),
                     project='-Users-alice-private-repo')

    def test_text_json_keys_and_saved_labels_carry_no_transcript_content(self):
        self.write_secret_sessions()
        lab = self.labels_file(by_prefix)
        for extra in ([], ['--json']):
            code, out, err = run('--claude-dir', self.root, '--coach-session', COACH_SID, '--coach-provider', 'claude',
                                 '--labels', lab, *extra)
            self.assertEqual(code, 0, err)
            self.assert_clean(out, err)
        code, out, err = run('--claude-dir', self.root, '--print-keys')
        self.assertEqual(code, 0, err)
        self.assert_clean(out, err)
        for ln in out.splitlines():
            self.assertEqual(set(json.loads(ln)), {'key', 'session', 'index'})
        saved = os.path.join(self.tmp.name, 'saved.jsonl')
        with mock.patch.object(coach, 'run_process', FakeClaude(label='correction')):
            code, out, err = run('--claude-dir', self.root, '--coach-session', COACH_SID, '--coach-provider', 'claude',
                                 '--classify-with-claude', '--i-consent-to-send-prompts-to-my-claude', '--save-labels', saved)
        self.assertEqual(code, 0, err)
        with open(saved) as fh:
            body = fh.read()
        self.assert_clean(out, err, body)
        for ln in body.splitlines():
            self.assertEqual(set(json.loads(ln)), {'key', 'label'})

    def test_file_errors_never_echo_a_path(self):
        missing = os.path.join(self.tmp.name, 'Users-alice-private-repo-labels.jsonl')
        self.write_secret_sessions()
        code, out, err = run('--claude-dir', self.root, '--coach-session', COACH_SID, '--coach-provider', 'claude',
                             '--labels', missing)
        self.assertEqual(code, 2)
        self.assertNotIn(missing, err)
        self.assertNotIn('private-repo', err)
        bad_out = os.path.join(self.tmp.name, 'no', 'such', 'dir', 'out.json')
        code, out, err = run('--claude-dir', self.root, '--coach-session', COACH_SID, '--coach-provider', 'claude',
                             '--labels', self.labels_file(by_prefix), '--out', bad_out)
        self.assertEqual(code, 2)
        self.assertNotIn(self.tmp.name, err)

    def test_bad_labels_file_refusals_name_the_line_only(self):
        self.write_secret_sessions()
        for body in ('{"key": "%s", "label": "new"}\n' % SECRET, 'not json %s\n' % SECRET,
                     '{"key": "%s", "label": "%s"}\n' % ('a' * 64, SECRET)):
            path = os.path.join(self.tmp.name, 'bad.jsonl')
            with open(path, 'w') as fh:
                fh.write(body)
            code, out, err = run('--claude-dir', self.root, '--coach-session', COACH_SID, '--coach-provider', 'claude',
                                 '--labels', path)
            self.assertEqual(code, 2)
            self.assert_clean(out, err)
        with open(path, 'wb') as fh:
            fh.write(b'\xff\xfe' + SECRET.encode())
        code, out, err = run('--claude-dir', self.root, '--coach-session', COACH_SID, '--coach-provider', 'claude',
                             '--labels', path)
        self.assertEqual(code, 2)
        self.assert_clean(out, err)

    def test_unexpected_error_prints_its_type_only(self):
        self.write_secret_sessions()
        with mock.patch.object(coach, 'tally', side_effect=RuntimeError(SECRET_PATH)):
            code, out, err = run('--claude-dir', self.root, '--coach-session', COACH_SID, '--coach-provider', 'claude',
                                 '--labels', self.labels_file(by_prefix))
        self.assertEqual(code, 1)
        self.assertIn('RuntimeError', err)
        self.assert_clean(out, err)

    def test_export_gate_refuses_a_private_classifier_name(self):
        env = coach.envelope(COACH_SID, 'claude', {'provider': 'claude-code', 'promptCount': 1, 'sessionCount': 1,
                                                   'classifier': 'a' * 30, **{k: {'corrections': 0, 'observed': 0}
                                                                              for k in coach.BUCKETS}})
        with self.assertRaises(PrivacyError):
            coach.export_json(env)


# ---------------------------------------------------------------- classifier
HELP = '''Usage: claude [options]
  -p, --print   print
  --model <model>
  --output-format <format>
  --no-session-persistence
  --tools <tools...>
  --safe-mode
  --strict-mcp-config
  --mcp-config <config>
  --disable-slash-commands
  --setting-sources <sources>
  --settings <settings>
  --system-prompt <prompt>
'''


def reply(labels, model='claude-haiku-4-5-20251001', **env):
    o = {'type': 'result', 'subtype': 'success', 'is_error': False,
         'result': json.dumps([{'i': i + 1, 'label': lab} for i, lab in enumerate(labels)]),
         'modelUsage': {model: {}}, 'session_id': TRANSCRIPT_SID}
    o.update(env)
    return json.dumps(o)


class FakeClaude:
    """A stand-in for the claude CLI: answers --help, then labels every item with `label` (or per call)."""

    def __init__(self, label='new', help_text=HELP, calls=None, missing=False, auth=None, auth_rc=0, help_rc=0):
        self.label, self.help_text, self.missing = label, help_text, missing
        self.calls = [] if calls is None else calls
        self.auth = auth if auth is not None else {'loggedIn': True, 'authMethod': 'claude.ai',
                                                 'apiProvider': 'firstParty', 'subscriptionType': 'pro'}
        self.auth_rc = auth_rc
        self.help_rc = help_rc

    def __call__(self, argv, stdin_text, cwd=None):
        self.calls.append((list(argv), stdin_text))
        if self.missing:
            raise FileNotFoundError('claude')
        if argv[1:] == ['--help']:
            return self.help_rc, self.help_text
        if argv[-3:] == ['auth', 'status', '--json']:
            return self.auth_rc, json.dumps(self.auth)
        items = json.loads(stdin_text.split('Items:\n', 1)[1])
        lab = self.label(items) if callable(self.label) else [self.label] * len(items)
        if isinstance(lab, tuple):
            return lab
        return 0, reply(lab)


def items(n):
    return [{'key': '%064x' % i, 'text': 'prompt %d' % i, 'tail': 'tail %d' % i, 'copied': False} for i in range(n)]


class Classifier(Base):
    def test_consent_is_checked_before_any_process(self):
        self.t.write('s', conversation(['new a', 'correction b']))
        fake = FakeClaude()
        with mock.patch.object(coach, 'run_process', fake):
            code, out, err = run('--claude-dir', self.root, '--coach-session', COACH_SID, '--coach-provider', 'claude',
                                 '--classify-with-claude')
        self.assertEqual(code, 2)
        self.assertIn('--i-consent-to-send-prompts-to-my-claude', err)
        self.assertEqual(fake.calls, [])
        with self.assertRaises(coach.Refusal):
            coach.classify(items(3), runner=fake)
        with self.assertRaises(coach.Refusal):
            coach.classify(items(3), runner=fake, consent='yes')
        self.assertEqual(fake.calls, [])

    def test_print_keys_never_classifies(self):
        self.t.write('s', conversation(['new a', 'correction b']))
        fake = FakeClaude()
        with mock.patch.object(coach, 'run_process', fake):
            code, out, err = run('--claude-dir', self.root, '--print-keys', '--classify-with-claude',
                                 '--i-consent-to-send-prompts-to-my-claude')
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.calls, [])

    def test_argv_uses_listed_flags_and_disables_tools_and_persistence(self):
        fake = FakeClaude()
        labels, st = coach.classify(items(2), runner=fake, consent=True)
        argv, stdin = next(call for call in fake.calls if 'Items:\n' in call[1])
        self.assertEqual(argv[:9], ['claude', '-p', '--model', 'haiku', '--output-format', 'json',
                                    '--no-session-persistence', '--tools', ''])
        for f in ('--safe-mode', '--strict-mcp-config', '--disable-slash-commands', '--system-prompt', '--setting-sources', '--settings'):
            self.assertIn(f, argv)
        self.assertEqual(json.loads(argv[argv.index('--mcp-config') + 1]), {'mcpServers': {}})
        self.assertEqual(argv[argv.index('--setting-sources') + 1], '')
        self.assertEqual(json.loads(argv[argv.index('--settings') + 1]), {'disableAllHooks': True, 'autoMemoryEnabled': False})
        self.assertNotIn('--bare', argv)
        self.assertNotIn('prompt 0', ' '.join(argv))   # prompts go on stdin, never on the command line
        self.assertIn('prompt 0', stdin)
        self.assertEqual(len(labels), 2)
        self.assertEqual(coach.classifier_name(st['models']), 'claude-haiku-4-5-20251001:prompt-v1')

    def test_old_cli_without_a_required_flag_is_a_refusal(self):
        fake = FakeClaude(help_text=HELP.replace('--no-session-persistence', ''))
        with self.assertRaises(coach.ClassifierUnavailable):
            coach.classify(items(1), runner=fake, consent=True)
        self.assertEqual(len(fake.calls), 1)

    def test_each_missing_isolation_flag_refuses_without_unsafe_fallback_or_prompt_dispatch(self):
        for flag in coach._REQUIRED_CLAUDE_FLAGS:
            with self.subTest(flag=flag):
                fake = FakeClaude(help_text=HELP.replace(flag, 'unsupported'))
                with self.assertRaises(coach.ClassifierUnavailable):
                    coach.classify(items(1), runner=fake, consent=True)
                self.assertEqual(len(fake.calls), 1)
                self.assertEqual(fake.calls[0][1], '')

    def test_missing_cli_is_a_refusal(self):
        with self.assertRaises(coach.ClassifierUnavailable):
            coach.classify(items(1), runner=FakeClaude(missing=True), consent=True)

    def test_failed_batches_degrade_to_unknown(self):
        def per_batch(its):
            first = its[0]['user']
            if first == 'prompt 0':
                return 0, json.dumps({'type': 'result', 'subtype': 'success', 'is_error': True,
                                      'result': 'Failed to authenticate: OAuth session expired'})
            if first == 'prompt 2':
                return 0, 'garbage that is not JSON'
            if first == 'prompt 4':
                return 1, reply(['new', 'new'])
            if first == 'prompt 6':
                raise subprocess.TimeoutExpired('claude', 1)
            # a partial, duplicated and out-of-range reply: only the clean item survives
            return 0, json.dumps({'type': 'result', 'subtype': 'success', 'result': json.dumps(
                [{'i': 1, 'label': 'correction'}, {'i': 2, 'label': 'new'}, {'i': 2, 'label': 'correction'},
                 {'i': 9, 'label': 'new'}, {'i': True, 'label': 'new'}])})
        with mock.patch.object(coach, 'BATCH', 2):
            labels, st = coach.classify(items(10), runner=FakeClaude(label=per_batch), consent=True)
        self.assertEqual(st['batches'], 5)
        self.assertEqual(st['failed_batches'], 4)
        self.assertEqual(set(st['failures']), {'claude CLI not signed in', 'unparseable output', 'exit status 1', 'timeout'})
        self.assertEqual(labels, {'%064x' % 8: 'correction'})

    def test_every_batch_failing_writes_nothing(self):
        self.t.write('s', conversation(['new a', 'correction b']))
        out_path = os.path.join(self.tmp.name, 'coach.json')
        fake = FakeClaude(label=lambda its: (1, 'boom'))
        with mock.patch.object(coach, 'run_process', fake):
            code, out, err = run('--claude-dir', self.root, '--coach-session', COACH_SID, '--coach-provider', 'claude',
                                 '--classify-with-claude', '--i-consent-to-send-prompts-to-my-claude', '--out', out_path)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out_path))

    def test_end_to_end_with_consent(self):
        self.t.write('s', conversation(['new a', 'correction b', 'correction c', 'new d']))
        fake = FakeClaude(label=lambda its: [by_prefix(x['user']) for x in its])
        with mock.patch.object(coach, 'run_process', fake):
            code, out, err = run('--claude-dir', self.root, '--coach-session', COACH_SID, '--coach-provider', 'chatgpt',
                                 '--classify-with-claude', '--i-consent-to-send-prompts-to-my-claude', '--json')
        self.assertEqual(code, 0, err)
        env = coach.validate(json.loads(out))
        b = env['baseline']
        self.assertEqual(b['classifier'], 'claude-haiku-4-5-20251001:prompt-v1')
        self.assertEqual(b['afterTwo'], {'corrections': 0, 'observed': 1})
        self.assertEqual(b['afterOne'], {'corrections': 1, 'observed': 2})
        self.assertEqual(b['otherwise'], {'corrections': 1, 'observed': 1})
        self.assertIn('classification of 4 prompt excerpts', err)

    def test_provider_authentication_and_runtime_env_overrides_refuse_before_any_process(self):
        for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_CUSTOM_HEADERS',
                     'ANTHROPIC_MODEL', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
                     'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CONFIG_DIR', 'CLAUDECODE', 'NODE_OPTIONS',
                     'NODE_EXTRA_CA_CERTS', 'HTTPS_PROXY', 'SSLKEYLOGFILE'):
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: SECRET}):
                fake = FakeClaude()
                with self.assertRaises(coach.ClassifierUnavailable) as error:
                    coach.classify(items(1), runner=fake, consent=True)
                self.assertEqual(fake.calls, [])
                self.assertNotIn(SECRET, str(error.exception))
                self.assertNotIn(name, str(error.exception))

    def test_empty_override_does_not_switch_accounts_and_is_not_forwarded(self):
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': '', 'ANTHROPIC_BASE_URL': ''}):
            safe = coach.classifier_environment()
            self.assertNotIn('ANTHROPIC_API_KEY', safe)
            self.assertNotIn('ANTHROPIC_BASE_URL', safe)
            labels, _ = coach.classify(items(1), runner=FakeClaude(), consent=True)
        self.assertEqual(len(labels), 1)

    def test_unrelated_node_repl_metadata_is_not_forwarded_or_a_provider_override(self):
        with mock.patch.dict(os.environ, {'NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S': SECRET}):
            safe = coach.classifier_environment()
            self.assertNotIn('NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S', safe)
            labels, _ = coach.classify(items(1), runner=FakeClaude(), consent=True)
        self.assertEqual(len(labels), 1)

    def test_default_runner_passes_only_guarded_environment_without_unrelated_credentials(self):
        completed = type('Result', (), {'returncode': 0, 'stdout': 'safe-output'})()
        with mock.patch.dict(os.environ, {'GH_TOKEN': SECRET, 'OPENAI_API_KEY': SECRET, 'AWS_SECRET_ACCESS_KEY': SECRET,
                                         'EDITOR': SECRET_PATH}), mock.patch.object(subprocess, 'run', return_value=completed) as process:
            self.assertEqual(coach.run_process(['claude', '--help'], '', self.tmp.name), (0, 'safe-output'))
        options = process.call_args.kwargs
        safe = options['env']
        self.assertEqual(safe['HOME'], self.tmp.name)
        self.assertEqual(safe['PATH'], os.defpath)
        self.assertEqual(safe['CLAUDE_CODE_DISABLE_ATTACHMENTS'], '1')
        self.assertEqual(safe['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'], '1')
        self.assertNotIn(SECRET, safe.values())
        self.assertNotIn(SECRET_PATH, safe.values())
        self.assertTrue(options['capture_output'])
        self.assertEqual(options['timeout'], coach.TIMEOUT_S)
        self.assertEqual(options['cwd'], self.tmp.name)

    def test_provider_settings_and_api_helpers_refuse_before_any_process(self):
        directory = os.path.join(self.tmp.name, '.claude')
        os.makedirs(directory)
        path = os.path.join(directory, 'settings.json')
        cases = [{'apiKeyHelper': SECRET}, {'env': {'ANTHROPIC_BASE_URL': SECRET}},
                 {'nested': {'awsCredentialExport': SECRET}}, {'forceLoginOrgUUID': SECRET}]
        for value in cases:
            with self.subTest(value=list(value)):
                with open(path, 'w') as fh:
                    json.dump(value, fh)
                fake = FakeClaude()
                with self.assertRaises(coach.ClassifierUnavailable) as error:
                    coach.classify(items(1), runner=fake, consent=True)
                self.assertEqual(fake.calls, [])
                self.assertNotIn(SECRET, str(error.exception))
                self.assertNotIn(path, str(error.exception))

    def test_disabled_mcp_configuration_is_not_a_first_party_provider_override(self):
        directory = os.path.join(self.tmp.name, '.claude')
        os.makedirs(directory)
        with open(os.path.join(directory, 'settings.json'), 'w') as fh:
            json.dump({'mcpServers': {'private-server': {'env': {'ANTHROPIC_API_KEY': SECRET}}}}, fh)
        labels, _ = coach.classify(items(1), runner=FakeClaude(), consent=True)
        self.assertEqual(len(labels), 1)
        self.assertNotIn(SECRET, coach.classifier_environment().values())

    def test_unreadable_profile_settings_fail_closed_without_content_or_path(self):
        directory = os.path.join(self.tmp.name, '.claude')
        os.makedirs(directory)
        path = os.path.join(directory, 'settings.json')
        with open(path, 'w') as fh:
            fh.write('not-json ' + SECRET)
        fake = FakeClaude()
        with self.assertRaises(coach.ClassifierUnavailable) as error:
            coach.classify(items(1), runner=fake, consent=True)
        self.assertEqual(fake.calls, [])
        self.assertNotIn(SECRET, str(error.exception))
        self.assertNotIn(path, str(error.exception))

    def test_managed_policy_refuses_even_before_the_capability_probe(self):
        path = os.path.join(self.tmp.name, 'managed-settings.json')
        with open(path, 'w') as fh:
            json.dump({'env': {'ANTHROPIC_API_KEY': SECRET}}, fh)
        fake = FakeClaude()
        with mock.patch.object(coach, '_managed_policy_paths', return_value=[path]):
            with self.assertRaises(coach.ClassifierUnavailable) as error:
                coach.classify(items(1), runner=fake, consent=True)
        self.assertEqual(fake.calls, [])
        self.assertNotIn(path, str(error.exception))
        self.assertNotIn(SECRET, str(error.exception))

    def test_registry_managed_platform_is_not_silently_treated_as_an_unmanaged_profile(self):
        fake = FakeClaude()
        with mock.patch.object(coach, '_managed_policy_paths', self.real_policy_paths), mock.patch.object(coach.sys, 'platform', 'win32'):
            with self.assertRaises(coach.ClassifierUnavailable):
                coach.classify(items(1), runner=fake, consent=True)
        self.assertEqual(fake.calls, [])

    def test_failed_help_with_flag_names_still_refuses_before_account_or_prompt_dispatch(self):
        fake = FakeClaude(help_rc=1)
        with self.assertRaises(coach.ClassifierUnavailable):
            coach.classify(items(1), runner=fake, consent=True)
        self.assertEqual(fake.calls, [(['claude', '--help'], '')])

    def test_signed_out_api_key_external_and_organization_auth_never_dispatch_prompts(self):
        personal = {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty', 'subscriptionType': 'pro'}
        for changes in ({'loggedIn': False}, {'authMethod': 'apiKey'}, {'apiProvider': 'bedrock'},
                        {'subscriptionType': 'team'}, {'subscriptionType': None}):
            with self.subTest(changes=changes):
                fake = FakeClaude(auth={**personal, **changes, 'email': SECRET, 'token': SECRET})
                with self.assertRaises(coach.ClassifierUnavailable) as error:
                    coach.classify(items(1), runner=fake, consent=True)
                self.assertEqual(len(fake.calls), 2)
                self.assertTrue(all(not stdin for _, stdin in fake.calls))
                self.assertNotIn(SECRET, str(error.exception))
                argv = fake.calls[1][0]
                self.assertEqual(argv[-3:], ['auth', 'status', '--json'])
                self.assertIn('--safe-mode', argv)
                self.assertEqual(argv[argv.index('--setting-sources') + 1], '')

    def test_malformed_auth_metadata_is_not_echoed_or_treated_as_native_subscription(self):
        def fake(argv, stdin, cwd=None):
            return (0, HELP) if argv[1:] == ['--help'] else (0, SECRET)
        with self.assertRaises(coach.ClassifierUnavailable) as error:
            coach.classify(items(1), runner=fake, consent=True)
        self.assertNotIn(SECRET, str(error.exception))

    def test_environment_changed_after_auth_is_checked_again_before_prompt_dispatch(self):
        fake = FakeClaude()
        def changed(argv, stdin, cwd=None):
            result = fake(argv, stdin, cwd)
            if argv[-3:] == ['auth', 'status', '--json']:
                os.environ['ANTHROPIC_API_KEY'] = SECRET
            return result
        with self.assertRaises(coach.ClassifierUnavailable):
            coach.classify(items(1), runner=changed, consent=True)
        self.assertEqual(len(fake.calls), 2)
        self.assertTrue(all(not stdin for _, stdin in fake.calls))


if __name__ == '__main__':
    unittest.main()
