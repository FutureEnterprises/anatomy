"""Hand-computed physical-order joins, coverage gaps, copies and privacy."""
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tests import helpers
from anatomy import coach, correction_index as index
from anatomy.prices import AnthropicPrices
from anatomy.privacy import PrivacyError, assert_clean, gate

AP = AnthropicPrices(helpers.ANTHROPIC_PRICES)


def user(text='prompt', uid='u1', second=0, **extra):
    return dict(type='user', uuid=uid, timestamp='2026-09-01T00:00:%02dZ' % second,
                message={'content': text}, **extra)


def assistant(mid='a1', second=1, model='claude-opus-5-5', usage=None, text='', **extra):
    message = {'id': mid, 'model': model, 'content': [{'type': 'text', 'text': text}], 'stop_reason': 'end_turn'}
    if usage is not None:
        message['usage'] = usage
    return dict(type='assistant', timestamp='2026-09-01T00:00:%02dZ' % second, message=message, **extra)


def usage(inp=0, out=0, **extra):
    return dict(input_tokens=inp, output_tokens=out, **extra)


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, 'projects')
        self.prices = AP

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, records, kind='main'):
        directory = os.path.join(self.root, 'project')
        if kind != 'main':
            directory = os.path.join(directory, kind)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name + '.jsonl')
        with open(path, 'w') as output:
            for record in records:
                output.write(json.dumps(record) + '\n' if not isinstance(record, bytes) else record.decode())
        return path

    def collect(self, until=None):
        return index.collect(self.root, until)

    def build(self, collection, values):
        rows = index.key_rows(collection)
        labels = {row['key']: value for row, value in zip(rows, values)}
        return index.build(collection, labels, self.prices)

    def test_price_hand_calculation_all_cache_tiers_and_model_switch(self):
        self.write('a', [user('first'), assistant(usage=usage(1000000, 1000000, cache_creation_input_tokens=3000000,
                    cache_creation={'ephemeral_5m_input_tokens': 1000000, 'ephemeral_1h_input_tokens': 2000000},
                    cache_read_input_tokens=1000000)), user('fix', 'u2', 2),
                    assistant('a2', 3, model='claude-opus-5', usage=usage(1000000, 1000000))])
        report = self.build(self.collect(), ['new', 'correction'])
        served = report['served_api_list_price_equivalent']
        # Invented fixture prices: 10 input +12.5 write5m +40 write1h +1 read +50 output; model2=5+25.
        self.assertAlmostEqual(served['total']['known_usd'], 143.5)
        self.assertAlmostEqual(served['by_label']['correction']['known_usd'], 30)
        self.assertAlmostEqual(served['by_label']['new']['known_usd_by_tier']['cache_write_1h'], 40)
        self.assertEqual(served['by_model']['claude-opus-5']['by_label']['correction']['calls'], 1)

    def test_stream_update_after_prompt_keeps_original_owner_and_explicit_zero(self):
        self.write('a', [user(), assistant(usage=usage(1000000, 100000)), user('fix', 'u2', 2),
                        assistant('a1', 3, usage=usage(0, 200000)), assistant('a2', 4, usage=usage(0, 300000))])
        report = self.build(self.collect(), ['new', 'correction'])
        served = report['served_api_list_price_equivalent']
        self.assertEqual(served['total']['calls'], 2)
        self.assertAlmostEqual(served['by_label']['new']['known_usd'], 10)
        self.assertAlmostEqual(served['by_label']['correction']['known_usd'], 15)
        self.assertEqual(report['coverage']['stream_updates'], 1)

    def test_resumed_copies_not_rebilled_and_new_live_call_owned(self):
        original = [user('first'), assistant(usage=usage(0, 100000)), user('fix', 'u2', 2), assistant('a2', 3, usage=usage(0, 200000))]
        self.write('a', original)
        self.write('b', original + [user('next', 'u3', 4), assistant('a3', 5, usage=usage(0, 300000))])
        collection = self.collect()
        report = self.build(collection, ['new', 'correction', 'new'])
        self.assertEqual(report['coverage']['unique_prompts'], 3)
        self.assertEqual(report['coverage']['copied_calls'], 2)
        self.assertEqual(report['coverage']['conflicting_call_owners'], 0)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['total']['known_usd'], 30)

    def test_cross_file_input_disagreement_not_authoritatively_priced(self):
        original = [user(), assistant(usage=usage(100, 10))]
        self.write('a', original)
        self.write('b', [user(), assistant(second=3, usage=usage(200, 20))])
        report = self.build(self.collect(), ['new'])
        self.assertEqual(report['served_api_list_price_equivalent']['total']['invalid_usage_calls'], 1)
        self.assertIsNone(report['served_api_list_price_equivalent']['total']['known_usd'])

    def test_cross_file_cache_tier_disagreement_visible(self):
        def split(five, one):
            return usage(0, 10, cache_creation_input_tokens=100, cache_creation={
                'ephemeral_5m_input_tokens': five, 'ephemeral_1h_input_tokens': one})
        self.write('a', [user(), assistant(usage=split(100, 0))])
        self.write('b', [user(), assistant(second=3, usage=split(0, 100))])
        report = self.build(self.collect(), ['new'])
        self.assertEqual(report['served_api_list_price_equivalent']['total']['invalid_usage_calls'], 1)

    def test_cross_file_missing_usage_can_recover_full_known_snapshot(self):
        self.write('a', [user(), assistant()])
        self.write('b', [user(), assistant(second=3, usage=usage(0, 100000, cache_creation_input_tokens=200,
                    cache_creation={'ephemeral_5m_input_tokens': 100, 'ephemeral_1h_input_tokens': 100}))])
        report = self.build(self.collect(), ['new'])
        self.assertEqual(report['served_api_list_price_equivalent']['total']['priced_calls'], 1)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['total']['known_usd'], 5.00325)

    def test_cross_file_conflicting_owner_is_ambiguous(self):
        self.write('a', [user('first'), assistant(usage=usage(0, 100000))])
        self.write('b', [user('other', 'u2', 2), assistant(second=3, usage=usage(0, 100000))])
        report = self.build(self.collect(), ['new', 'correction'])
        self.assertEqual(report['coverage']['conflicting_call_owners'], 1)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['by_label']['ambiguous']['known_usd'], 5)

    def test_queued_human_prompts_have_ambiguous_cost(self):
        self.write('a', [user('first'), user('fix', 'u2', 2), assistant('a1', 3, usage=usage(0, 100000)),
                        user('third', 'u3', 4), assistant('a2', 5, usage=usage(0, 200000))])
        report = self.build(self.collect(), ['new', 'correction', 'new'])
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['by_label']['ambiguous']['known_usd'], 5)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['by_label']['new']['known_usd'], 10)
        self.assertIsNone(report['served_api_list_price_equivalent']['by_label']['correction']['known_usd'])

    def test_image_and_interruption_unknown_boundaries(self):
        self.write('a', [user('fix'), assistant(usage=usage(0, 100000)),
            user([{'type': 'image', 'source': {'data': 'PLANTED'}}], 'u2', 2), assistant('a2', 3, usage=usage(0, 100000)),
            user('[Request interrupted by user]', 'u3', 4), assistant('a3', 5, usage=usage(0, 100000))])
        report = self.build(self.collect(), ['correction'])
        self.assertEqual(report['prompt_labels']['unknown'], 2)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['by_label']['unknown']['known_usd'], 10)
        self.assertEqual(report['literal_adjacent_outcomes']['buckets']['after_correction']['next_unknown'], 1)

    def test_tool_results_and_meta_do_not_break_owner(self):
        self.write('a', [user(), assistant(usage=usage(0, 100000)),
            user([{'type': 'tool_result', 'content': 'secret'}], 'tool', 2),
            user('meta', 'meta', 3, isMeta=True), user('summary', 'summary', 4, isCompactSummary=True),
            user('<command-name>/clear</command-name>', 'cmd', 5), assistant('a2', 6, usage=usage(0, 100000))])
        report = self.build(self.collect(), ['correction'])
        self.assertEqual(report['coverage']['unique_prompts'], 1)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['by_label']['correction']['known_usd'], 10)

    def test_retry_nudge_are_literal_boundaries_and_end_streaks(self):
        self.write('a', [user('fix'), assistant(usage=usage(0, 10)), user('?', 'u2', 2),
            assistant('a2', 3, usage=usage(0, 10)), user('fix again', 'u3', 4), assistant('a3', 5, usage=usage(0, 10)),
            user('retry', 'u4', 6), assistant('a4', 7, usage=usage(0, 10))])
        report = self.build(self.collect(), ['correction', 'nudge', 'correction', 'retry'])
        outcomes = report['literal_adjacent_outcomes']['buckets']
        self.assertEqual(outcomes['after_correction']['next_known'], 2)
        self.assertEqual(outcomes['after_correction']['next_corrections'], 0)
        self.assertEqual(outcomes['after_two_adjacent_corrections']['next_known'], 0)
        self.assertEqual(report['served_api_list_price_equivalent']['by_label']['nudge']['calls'], 1)

    def test_same_key_same_file_distinct_occurrences_one_classifier_key(self):
        self.write('a', [user('same', 'u1'), assistant(text='', usage=usage(0, 10)),
                        user('same', 'u2', 2), assistant('a2', 3, text='', usage=usage(0, 10))])
        collection = self.collect()
        self.assertEqual(len(collection['prompts']), 2)
        self.assertEqual(len(index.key_rows(collection)), 1)
        self.assertEqual(self.build(collection, ['correction'])['prompt_labels']['counts']['correction'], 2)

    def test_distinct_uuids_same_key_across_sessions_are_independent_prompts(self):
        self.write('a', [user('same', 'u1'), assistant('a1', 1, usage=usage(0, 100000))])
        self.write('b', [user('same', 'u2', 2), assistant('a2', 3, usage=usage(0, 100000))])
        collection = self.collect()
        report = self.build(collection, ['correction'])
        self.assertEqual(report['coverage']['unique_prompts'], 2)
        self.assertEqual(len(index.key_rows(collection)), 1)
        self.assertEqual(report['prompt_labels']['counts']['correction'], 2)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['by_label']['correction']['known_usd'], 10)

    def test_late_duplicate_uuid_does_not_reset_owner_or_sequence(self):
        first = user('first', 'u1')
        self.write('a', [first, assistant(usage=usage(0, 10)), user('fix', 'u2', 2),
                        first, assistant('a2', 3, usage=usage(0, 100000))])
        report = self.build(self.collect(), ['new', 'correction'])
        self.assertEqual(report['coverage']['in_file_prompt_copies'], 1)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['by_label']['correction']['known_usd'], 5)

    def test_zero_usage_unknown_price_missing_usage_and_invalid_usage_are_distinct(self):
        self.write('a', [user(), assistant('a0', usage=usage()), assistant('a1', 2),
                        assistant('a2', 3, model='claude-unknown-99', usage=usage(10, 10)),
                        assistant('a3', 4, usage=usage(True, 10))])
        report = self.build(self.collect(), ['new'])
        total = report['served_api_list_price_equivalent']['total']
        self.assertEqual(total['known_usd'], 0)
        self.assertEqual(total['priced_calls'], 1)
        self.assertEqual(total['missing_usage_calls'], 1)
        self.assertEqual(total['unpriced_calls'], 1)
        self.assertEqual(total['invalid_usage_calls'], 1)

    def test_calls_before_prompt_and_unidentified_visible(self):
        no_id = assistant(usage=usage(0, 10))
        no_id['message'].pop('id')
        self.write('a', [assistant('a0', usage=usage(0, 100000)), user('first', second=2), no_id])
        report = self.build(self.collect(), ['new'])
        total = report['served_api_list_price_equivalent']['total']
        self.assertEqual(total['unidentified_calls'], 1)
        self.assertAlmostEqual(total['known_usd'], 5)
        self.assertEqual(report['coverage']['unattributed_calls'], 2)

    def test_declined_attempts_separate_unknown_model_not_inherited(self):
        iterations = [dict(type='message', model='claude-opus-5', **usage(1000000, 1000000)),
                      dict(type='message', **usage(1000000, 1)),
                      dict(type='message', model='claude-opus-5', **usage(1000000, 0)),
                      dict(type='fallback_message', model='claude-opus-5-5', **usage(0, 1000000))]
        self.write('a', [user(), assistant(usage=usage(0, 1000000, iterations=iterations))])
        report = self.build(self.collect(), ['correction'])
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['total']['known_usd'], 50)
        declined = report['declined_api_list_price_equivalent']['categories']
        self.assertAlmostEqual(declined['streamed_output_estimate']['known_usd'], 30)
        self.assertEqual(declined['streamed_output_estimate']['unpriced_calls'], 1)
        self.assertAlmostEqual(declined['pre_output_uncertain']['known_usd'], 5)

    def test_malformed_record_censors_owner_until_unambiguous_human(self):
        self.write('a', [user('first'), assistant(usage=usage(0, 10)), b'{bad json}\n',
                        assistant('a2', 2, usage=usage(0, 100000)), user('fix', 'u2', 3), assistant('a3', 4, usage=usage(0, 200000))])
        report = self.build(self.collect(), ['new', 'correction'])
        self.assertEqual(report['coverage']['malformed_records'], 1)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['by_label']['unknown']['known_usd'], 5)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['by_label']['correction']['known_usd'], 10)

    def test_cutoff_keeps_later_in_window_records_after_timestamp_reversal(self):
        self.write('a', [user(), assistant('future', 50, usage=usage(0, 1000000)),
                        user('fix', 'u2', 2), assistant('past', 3, usage=usage(0, 100000))])
        collection = self.collect(claude_ts('2026-09-01T00:00:10Z'))
        report = self.build(collection, ['new', 'correction'])
        self.assertEqual(report['coverage']['cutoff_excluded_records'], 1)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['total']['known_usd'], 5)
        self.assertEqual(report['served_api_list_price_equivalent']['by_label']['correction']['calls'], 1)

    def test_subagents_sidechain_and_missing_stop_reason_visible(self):
        self.write('agent', [user(), assistant(usage=usage(0, 1000000))], 'subagents')
        final = assistant(usage=usage(0, 100000))
        final['message'].pop('stop_reason')
        self.write('main', [user(), assistant('side', usage=usage(0, 1000000), isSidechain=True), final])
        report = self.build(self.collect(), ['new'])
        self.assertEqual(report['coverage']['excluded_subagent_files'], 1)
        self.assertEqual(report['coverage']['excluded_sidechain_records'], 1)
        self.assertEqual(report['coverage']['calls_without_stop_reason'], 1)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['total']['known_usd'], 5)

    def test_repo_aliases_git_worktrees_and_per_call_changed_cwd(self):
        first_repo = os.path.join(self.tmp.name, 'secret-first-repo')
        worktree = os.path.join(self.tmp.name, 'secret-worktree')
        second_repo = os.path.join(self.tmp.name, 'secret-second-repo')
        for directory in (first_repo, worktree, second_repo):
            os.makedirs(directory)
        first_common = os.path.join(first_repo, '.git')
        def git(args, **kw):
            common = first_common if args[2] in (os.path.realpath(first_repo), os.path.realpath(worktree)) else os.path.join(second_repo, '.git')
            return subprocess.CompletedProcess(args, 0, common + '\n', '')
        self.write('a', [user(cwd=first_repo), assistant(usage=usage(0, 100000), cwd=worktree),
                        assistant('a2', 2, usage=usage(0, 100000), cwd=second_repo),
                        user('fix', 'u2', 3, cwd=worktree), assistant('a3', 4, usage=usage(0, 100000), cwd=worktree),
                        user('next job', 'u3', 5, cwd=second_repo), assistant('a4', 6, usage=usage(0, 100000), cwd=second_repo)])
        with mock.patch.object(subprocess, 'run', side_effect=git) as runner:
            collection = self.collect()
        report = self.build(collection, ['new', 'correction', 'new'])
        repos = report['served_api_list_price_equivalent']['by_repo']
        self.assertEqual(set(repos), {'repo-1', 'repo-2'})
        self.assertAlmostEqual(repos['repo-1']['known_usd'], 15)
        self.assertAlmostEqual(repos['repo-1']['by_label']['correction']['known_usd'], 5)
        self.assertAlmostEqual(repos['repo-2']['known_usd'], 5)
        self.assertEqual(report['coverage']['owned_calls_changed_cwd'], 1)
        self.assertEqual(runner.call_count, 3)

    def test_same_file_uuid_with_changed_text_is_unknown(self):
        self.write('a', [user('first'), assistant(usage=usage(0, 100000)),
                        user('changed contents', 'u1', 2), assistant('a2', 3, usage=usage(0, 100000))])
        report = index.build(self.collect(), {coach.prompt_key('first', ''): 'new'}, self.prices)
        self.assertEqual(report['coverage']['conflicting_prompt_copies'], 1)
        self.assertEqual(report['prompt_labels']['unknown'], 1)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['by_label']['unknown']['known_usd'], 10)

    def test_changed_late_uuid_never_inherits_a_more_recent_prompts_label(self):
        self.write('a', [user('first', 'u1'), assistant(usage=usage(0, 100000)), user('fix', 'u2', 2),
                        user('edited older row', 'u1', 3), assistant('a2', 4, usage=usage(0, 100000))])
        report = index.build(self.collect(), {coach.prompt_key('first', ''): 'new', coach.prompt_key('fix', ''): 'correction'}, self.prices)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['by_label']['unknown']['known_usd'], 10)
        self.assertIsNone(report['served_api_list_price_equivalent']['by_label']['correction']['known_usd'])

    def test_unsplit_cache_tier_assumption_visible(self):
        self.write('a', [user(), assistant(usage=usage(0, 0, cache_creation_input_tokens=1000000))])
        report = self.build(self.collect(), ['new'])
        self.assertEqual(report['coverage']['assumed_5m_cache_write_calls'], 1)
        self.assertEqual(report['coverage']['assumed_5m_cache_write_tokens'], 1000000)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['total']['known_usd'], 12.5)
        self.assertIn('assumed 5m tier', index.render_text(report))

    def test_untimed_record_excluded_from_cutoff_and_later_update_not_new_in_window_call(self):
        untimed = assistant('a1', usage=usage(0, 1000000))
        untimed.pop('timestamp')
        self.write('a', [user(), untimed, user('fix', 'u2', 2),
                        assistant('a1', 3, usage=usage(0, 2000000)), assistant('a2', 4, usage=usage(0, 100000))])
        report = self.build(self.collect(claude_ts('2026-09-01T00:00:10Z')), ['new', 'correction'])
        self.assertEqual(report['coverage']['cutoff_untimed_records'], 1)
        self.assertEqual(report['served_api_list_price_equivalent']['total']['window_unknown_calls'], 1)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['total']['known_usd'], 5)

    def test_future_stream_update_excluded_but_in_window_partial_usage_visible(self):
        self.write('a', [user(), assistant('a1', 1, usage=usage(0, 100000)),
                        assistant('a1', 50, usage=usage(0, 1000000))])
        report = self.build(self.collect(claude_ts('2026-09-01T00:00:10Z')), ['new'])
        self.assertEqual(report['coverage']['cutoff_truncated_calls'], 1)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['total']['known_usd'], 5)

    def test_verified_untimed_metadata_never_breaks_prompt_owner_under_cutoff(self):
        records = [user(), assistant('a1', usage=usage(0, 100000))]
        records.extend({'type': kind, 'title': 'PRIVATE_METADATA'} for kind in index.NONCONVERSATIONAL_METADATA)
        records.append(assistant('a2', 2, usage=usage(0, 100000)))
        self.write('a', records)
        report = self.build(self.collect(claude_ts('2026-09-01T00:00:10Z')), ['correction'])
        self.assertEqual(report['coverage']['ignored_untimed_metadata_records'], len(index.NONCONVERSATIONAL_METADATA))
        self.assertEqual(report['coverage'].get('malformed_boundary_owner_calls', 0), 0)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['by_label']['correction']['known_usd'], 10)

    def test_unknown_untimed_type_or_metadata_with_message_still_breaks_owner(self):
        self.write('a', [user(), {'type': 'future-unrecognized-kind'}, assistant('a1', 1, usage=usage(0, 100000)),
                        user('fix', 'u2', 2), {'type': 'mode', 'message': {'content': 'possible user input'}},
                        assistant('a2', 3, usage=usage(0, 100000))])
        report = self.build(self.collect(claude_ts('2026-09-01T00:00:10Z')), ['new', 'correction'])
        self.assertEqual(report['coverage']['cutoff_untimed_records'], 2)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['by_label']['unknown']['known_usd'], 10)

    def test_geo_placeholders_assume_global_and_explicit_geo_enriches(self):
        for before, after, expected, assumed in (
                ('not_available', '', 5, 1), ('not_available', 'us', 5.5, 0),
                ('us', 'not_available', 5.5, 0), (None, 'global', 5, 0)):
            with self.subTest(before=before, after=after):
                self.prices = AnthropicPrices(helpers.ANTHROPIC_PRICES)
                self.prices.geo_us = 1.1
                self.write('a', [user(), assistant('a1', 1, usage=usage(0, 100000, inference_geo=before)),
                                assistant('a1', 2, usage=usage(0, 100000, inference_geo=after))])
                report = self.build(self.collect(), ['new'])
                self.assertAlmostEqual(report['served_api_list_price_equivalent']['total']['known_usd'], expected)
                self.assertEqual(report['coverage']['assumed_global_geo_calls'], assumed)
                self.assertEqual(report['coverage'].get('priced_assumed_global_geo_calls', 0), assumed)
                self.assertEqual(report['price_snapshot']['geo_assumption'], 'global rates when geographic metadata unknown')

    def test_geo_explicit_conflicts_and_unsupported_values_unpriced(self):
        for before, after in (('us', 'global'), ('global', 'us'), ('unsupported-geo', 'not_available')):
            with self.subTest(before=before, after=after):
                self.write('a', [user(), assistant('a1', 1, usage=usage(0, 100000, inference_geo=before)),
                                assistant('a1', 2, usage=usage(0, 100000, inference_geo=after))])
                report = self.build(self.collect(), ['new'])
                self.assertEqual(report['served_api_list_price_equivalent']['total']['unpriced_calls'], 1)
                self.assertIsNone(report['served_api_list_price_equivalent']['total']['known_usd'])

    def test_single_and_unmarked_multiple_iterations_are_never_declined_estimates(self):
        for attempts in (1, 2):
            with self.subTest(attempts=attempts):
                iterations = [dict(type='message', model='claude-opus-5-5', **usage(0, 100000))] * attempts
                self.write('a', [user(), assistant(usage=usage(0, 100000, iterations=iterations))])
                report = self.build(self.collect(), ['new'])
                self.assertAlmostEqual(report['served_api_list_price_equivalent']['total']['known_usd'], 5)
                self.assertEqual(report['declined_api_list_price_equivalent']['categories']['streamed_output_estimate']['calls'], 0)
                self.assertEqual(report['coverage']['single_iteration_calls' if attempts == 1 else 'unresolved_iteration_calls'], 1)

    def test_mixed_top_cache_split_resolved_only_from_matching_served_fallback(self):
        served = usage(0, 100000, cache_creation_input_tokens=0, cache_read_input_tokens=1000000,
                       cache_creation={'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 0})
        declined = usage(0, 100000, cache_creation_input_tokens=1000000, cache_read_input_tokens=1000000,
                         cache_creation={'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 1000000})
        iterations = [dict(type='message', model='claude-opus-5-5', **declined),
                      dict(type='fallback_message', model='claude-opus-5-5', **served)]
        top = dict(served, cache_creation={'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 1000000}, iterations=iterations)
        self.write('a', [user(), assistant(usage=top)])
        report = self.build(self.collect(), ['correction'])
        self.assertEqual(report['coverage']['served_cache_split_from_fallback_calls'], 1)
        self.assertAlmostEqual(report['served_api_list_price_equivalent']['total']['known_usd'], 6)
        self.assertAlmostEqual(report['declined_api_list_price_equivalent']['categories']['streamed_output_estimate']['known_usd'], 26)
        self.assertEqual(report['coverage']['declined_assumed_global_geo_calls'], 1)

    def test_incomplete_mismatched_or_misordered_fallback_remains_uncertain(self):
        declined = dict(type='message', model='claude-opus-5-5', **usage(0, 100000))
        served = dict(type='fallback_message', model='claude-opus-5-5', **usage(0, 100000))
        for iterations in ([served, declined], [declined, dict(served, output_tokens=200000)],
                           [declined, {k: v for k, v in served.items() if k != 'input_tokens'}]):
            with self.subTest(iteration_count=len(iterations)):
                self.write('a', [user(), assistant(usage=usage(0, 100000, iterations=iterations))])
                report = self.build(self.collect(), ['new'])
                self.assertEqual(report['coverage']['unresolved_iteration_calls'], 1)
                self.assertEqual(report['declined_api_list_price_equivalent']['categories']['streamed_output_estimate']['calls'], 0)

    def test_matching_fallback_geo_enriches_placeholder_or_exposes_explicit_conflict(self):
        self.prices = AnthropicPrices(helpers.ANTHROPIC_PRICES)
        self.prices.geo_us = 1.1
        served = usage(0, 100000, cache_creation_input_tokens=1000000, cache_read_input_tokens=0,
                       cache_creation={'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 1000000}, inference_geo='us')
        iterations = [dict(type='message', model='claude-opus-5-5', **usage(0, 100000)),
                      dict(type='fallback_message', model='claude-opus-5-5', **served)]
        for top_geo, expected in (('not_available', 27.5), ('global', None)):
            with self.subTest(top_geo=top_geo):
                top = dict(served, inference_geo=top_geo, iterations=iterations,
                           cache_creation={'ephemeral_5m_input_tokens': 1000000, 'ephemeral_1h_input_tokens': 0})
                self.write('a', [user(), assistant(usage=top)])
                report = self.build(self.collect(), ['new'])
                if expected is not None:
                    self.assertAlmostEqual(report['served_api_list_price_equivalent']['total']['known_usd'], expected)
                    self.assertEqual(report['coverage']['assumed_global_geo_calls'], 0)
                else:
                    self.assertIsNone(report['served_api_list_price_equivalent']['total']['known_usd'])
                    self.assertEqual(report['served_api_list_price_equivalent']['total']['unpriced_calls'], 1)

    def test_unknown_and_conflicting_pricing_modifiers_stay_unpriced(self):
        self.write('a', [user(), assistant(usage=usage(0, 100000, speed='fast', inference_geo='us'))])
        self.write('b', [user(), assistant(second=3, usage=usage(0, 100000, speed='standard', inference_geo='global'))])
        report = self.build(self.collect(), ['new'])
        self.assertEqual(report['served_api_list_price_equivalent']['total']['unpriced_calls'], 1)
        self.assertIsNone(report['served_api_list_price_equivalent']['total']['known_usd'])

    def test_initial_episode_anchors_not_all_overlapping_pairs(self):
        records = []
        for i in range(4):
            records.extend([user('fix%d' % i, 'u%d' % i, i * 2), assistant('a%d' % i, i * 2 + 1, usage=usage())])
        self.write('a', records)
        report = self.build(self.collect(), ['correction'] * 4)
        adjacent = report['literal_adjacent_outcomes']
        self.assertEqual(adjacent['initial_two_correction_episodes'], 1)
        self.assertEqual(adjacent['buckets']['after_two_adjacent_corrections']['next_known'], 2)
        self.assertEqual(adjacent['buckets']['after_two_adjacent_corrections']['next_corrections'], 2)

    def test_only_git_local_metadata_subprocess_no_classifier(self):
        self.write('a', [user(), assistant(usage=usage(0, 10))])
        with mock.patch.object(subprocess, 'run', side_effect=AssertionError('unexpected subprocess')):
            report = self.build(self.collect(), ['new'])
        self.assertEqual(report['classifier'], 'labels-file')

    def test_keys_match_coach_prompt_hash_and_narrow_positional_schema(self):
        self.write('a', [user(' first '), assistant(text='answer', usage=usage()),
                        user([{'type': 'image'}], 'image', 2), assistant('a2', 3, text='more', usage=usage()),
                        user('fix', 'u3', 4), assistant('a3', 5, usage=usage())])
        collection = self.collect()
        rows = index.key_rows(collection)
        self.assertEqual(rows[0], {'key': coach.prompt_key('first', ''), 'session': 1, 'index': 0})
        self.assertEqual(rows[1], {'key': coach.prompt_key('fix', 'answer\nmore'), 'session': 1, 'index': 2})
        with self.assertRaises(PrivacyError):
            gate(rows)

    def test_privacy_aggregate_and_renderer_no_raw_text_paths_ids_or_keys(self):
        secret = 'sk-ant-api03-PLANTEDsecretPLANTEDsecret'
        self.write('a', [user(secret, 'private-uuid-123'), assistant('private-message-id', usage=usage(0, 10), text=secret)])
        collection = self.collect()
        report = self.build(collection, ['correction'])
        rendered = json.dumps(report) + index.render_text(report)
        assert_clean(rendered)
        self.assertNotIn(secret, rendered)
        self.assertNotIn('private-uuid', rendered)
        self.assertNotIn('private-message-id', rendered)
        self.assertNotIn(collection['prompts'][0]['key'], rendered)

    def test_invalid_labels_classifier_refused_without_echo(self):
        collection = self.collect()
        for labels in ({'bad': 'new'}, {'0' * 64: 'made-up'}):
            with self.assertRaisesRegex(ValueError, '^invalid correction index labels$'):
                index.build(collection, labels, self.prices)
        with self.assertRaisesRegex(ValueError, '^invalid correction index classifier$'):
            index.build(collection, {}, self.prices, '/Users/private')


def claude_ts(value):
    from anatomy.ingest.claude import parse_ts
    return parse_ts(value)


if __name__ == '__main__':
    unittest.main()
