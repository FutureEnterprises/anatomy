"""Local Correction Index commands. No model calls or raw text output."""
from __future__ import annotations

import json
import os
import sys

from . import coach, correction_index, label_audit
from .ingest import claude
from .prices import AnthropicPrices
from .privacy import PrivacyError, assert_clean, gate


def _classifier(args, default):
    value = args.classifier_id or default
    if not isinstance(value, str) or not coach.CLASSIFIER_ID.fullmatch(value):
        raise coach.Refusal('classifier identifier must use letters, digits, . _ : - and at most 80 characters')
    # The existing aggregate gate also refuses secret-shaped identifiers.
    gate({'classifier': value})
    return value


def _keys_text(rows):
    for row in rows:
        if (set(row) != {'key', 'session', 'index'} or
                not isinstance(row['key'], str) or not coach.KEY.fullmatch(row['key']) or
                type(row['session']) is not int or row['session'] < 1 or
                type(row['index']) is not int or row['index'] < 0):
            raise PrivacyError('unexpected key record')
    return ''.join(json.dumps(row, allow_nan=False) + '\n' for row in rows)


def _save(path, text):
    # Private aggregate/opaque-key files must not become world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)


def _main(args):
    if args.cmd == 'correction-audit':
        classifier = _classifier(args, 'labels-file')
        labels = label_audit.load_reviews(args.labels)
        reviews = label_audit.load_reviews(args.reviews)
        rep = label_audit.evaluate(labels, reviews, classifier=classifier)
        encoded = label_audit.export_json(rep)
        text = encoded if args.json else assert_clean(label_audit.render_text(rep))
    else:
        if args.classifier_id and not args.labels:
            raise coach.Refusal('--classifier-id requires --labels')
        if args.reviews and not args.labels:
            raise coach.Refusal('--reviews requires --labels')
        if args.reviews and (args.print_keys or args.sample is not None):
            raise coach.Refusal('--reviews belongs to the report, not a key or sample export')
        if args.sample is not None and args.sample < 1:
            raise coach.Refusal('--sample must be a positive integer')
        classifier = _classifier(args, 'labels-file' if args.labels else 'unlabeled')
        from .cli import _parse_until
        try:
            until, until_iso = _parse_until(args.until)
        except ValueError:
            raise coach.Refusal('--until must be an ISO time') from None
        labels = label_audit.load_reviews(args.labels) if args.labels else {}
        reviews = label_audit.load_reviews(args.reviews) if args.reviews else None
        root = os.path.expanduser(args.claude_dir or claude.default_root())
        collection = correction_index.collect(root, until=until)
        if not collection['prompts']:
            raise coach.Refusal('no human prompt boundaries found in main Claude Code transcripts')
        rows = correction_index.key_rows(collection)
        if args.print_keys:
            text = encoded = _keys_text(rows)
        elif args.sample is not None:
            manifest = label_audit.sample(rows, args.sample, args.seed)
            text = encoded = label_audit.export_sample(manifest)
        else:
            rep = correction_index.build(collection, labels, AnthropicPrices(args.anthropic_prices), classifier=classifier)
            rep['until'] = until_iso
            if reviews is not None:
                keys = {row['key'] for row in rows}
                actual_labels = {key: labels.get(key, 'unknown') for key in keys}
                actual_reviews = {key: value for key, value in reviews.items() if key in keys}
                if actual_reviews:
                    rep['label_audit'] = label_audit.evaluate(actual_labels, actual_reviews, classifier=classifier)
                rep['label_audit_coverage'] = {'reviews_inside_corpus': len(actual_reviews),
                    'reviews_outside_corpus': len(reviews) - len(actual_reviews)}
            gate(rep)
            encoded = assert_clean(json.dumps(rep, indent=2, allow_nan=False) + '\n')
            text = encoded if args.json else assert_clean(correction_index.render_text(rep))
            if not args.json and reviews is not None:
                text += '# Supplied review comparison\n'
                text += 'Review keys outside this corpus: %d  [observed]\n' % rep['label_audit_coverage']['reviews_outside_corpus']
                if 'label_audit' in rep:
                    text += label_audit.render_text(rep['label_audit'])
                else:
                    text += '# No supplied reviews match this corpus; accuracy is not assessed.\n'
                assert_clean(text)
    if args.out:
        _save(args.out, encoded)
    sys.stdout.write(text)
    return 0


def main(args):
    try:
        return _main(args)
    except coach.Refusal as error:
        sys.stderr.write('anatomy: %s\n' % error)
        return 2
    except PrivacyError as error:
        sys.stderr.write('anatomy: output withheld by the privacy gate (%s)\n' % error)
        return 3
    except OSError as error:
        sys.stderr.write('anatomy: could not read or write a file (%s)\n' % type(error).__name__)
        return 2
    except Exception as error:
        # A traceback can contain local paths or transcript data.
        sys.stderr.write('anatomy: correction command failed (%s)\n' % type(error).__name__)
        return 1
