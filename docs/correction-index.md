# Personal Correction Index

The Correction Index describes recorded human corrections and the API-list-price
equivalent of responses following them. It reads main Claude Code transcripts
locally. It performs no classification or network requests. Codex cost joining,
team accounts and recovery-effectiveness claims are outside this version.

```sh
PYTHONPATH=src python3 -m anatomy correction-index --labels labels.jsonl
PYTHONPATH=src python3 -m anatomy correction-index --labels labels.jsonl --json --out index.json
```

Without `--labels`, every prompt remains unknown. This still checks usage and
attribution coverage; it cannot tell you how frequently you correct the agent.
Labels use the same opaque keys as `coach-baseline --print-keys` and the same five
classes: `correction`, `bug_report`, `retry`, `nudge`, `new`. An explicit `unknown`
label is also accepted. Label files must contain only `key` and `label` fields.
Unmatched labels are counted. No classifier accuracy is assumed.

## Cost attribution

An actual human prompt starts a cost span. Physical source order establishes the
owner of each response at its first appearance; later streaming updates refine
its usage without moving it to a newer prompt. Every served response is priced
at its own recorded model, cache-write tier, cache-read tier and supported
modifiers, using the dated Anatomy pricing snapshot.

Missing, empty and `not_available` geography fields do not establish where
inference ran. Their API-price equivalent assumes standard global routing;
the report counts these assumptions. An explicit `us` value uses the snapshot's
US multiplier. Known geography may be recovered from another snapshot of the
same call; conflicting explicit values and unsupported modifiers remain
unpriced. This is a pricing assumption, not a routing or billing observation.

Retries and nudges are real boundaries. They are not removed from cost windows
or literal transition sequences. Queued prompts whose ownership cannot be
resolved, conflicting copied histories, malformed records and ambiguous human
messages have visible coverage buckets. Nontext human messages have unknown
labels. Tool results and known injected messages do not become human prompts.

Main-thread response IDs are deduplicated across resumed and forked files.
Missing or invalid usage and unknown model prices remain visible. A missing
price does not become a measured zero. A genuine zero-usage call can have a
known zero cost. Declined fallback attempts are estimated separately only when
an explicit final fallback marker establishes the earlier attempts. A lone
`message` iteration is not counted as a declined attempt. Ambiguous or unmarked
iteration arrays remain excluded with coverage counts. Declined estimates are
excluded from primary served totals.
Subagents and workflow-agent files are excluded and counted. Calls without an
established human owner remain in unattributed or ambiguous buckets.

Repository grouping resolves each prompt's local working directory to its Git
common directory; different worktrees of the same repository share an identity.
Exports contain ordinal aliases such as `repo-1`, with `unknown-repo` when no
identity can be established. These aliases belong to this scan; they are not
stable public IDs. No remote Git request is made and no directory or repository
name is exported.

A model breakdown identifies the model that served the response after a
prompt. It does not identify which model caused the user to correct something.
Differences across models or repositories can reflect task difficulty, user
review habits, changed scope or differing coverage.

## Interpreting the numbers

Correction frequency, dollars following corrections and avoidable waste are
different quantities. A bug report does not necessarily implicate the agent.
Even an agent-caused correction can lead to useful repair work. The report's
known-dollar shares exclude unpriced usage. The correction-dollar denominator
includes calls with unknown labels or owners, so sparse labeling can make this
share small. Consult the known-label dollar coverage before comparing shares.
These are API-price equivalents, not subscription invoices, human
labor costs or attainable savings.

Literal transition rates are label associations. Unknowns break known history;
terminal, missing and unknown outcomes do not count as recovery. If after-two
rates count overlapping adjacent pairs, they are not the baseline for a coach
episode triggered at its first pair. This report does not measure verified
task completion, intervention efficacy or a universal AI-performance score.

Use `--until TIME` to pin a cutoff. The Index excludes future-stamped records
and inserts sequence/ownership barriers for conversational or unrecognized
records; known nonconversational harness metadata does not interrupt the
conversation. A later physical record with an
in-window timestamp remains eligible. Records are not timestamp-sorted.
Filtered records, incomplete responses and unanswered tails are reported.
The older coach collector used by the review tool stops at the first record
beyond its cutoff, so those populations can differ.

## Check the labels locally

First obtain labels from the existing consented `coach-baseline` path or supply
labels produced elsewhere. A successful classifier call checks integration;
it does not establish accuracy. To create a reproducible random sample of
distinct classifier inputs without displaying text:

```sh
PYTHONPATH=src python3 -m anatomy correction-index --sample 40 --seed 20261001 --out sample.json
```

For a blind human review, start the local browser tool:

```sh
PYTHONPATH=src python3 -m anatomy correction-review --sample 40 --seed 20261001 --out reviews.jsonl
```

Open the loopback URL printed by the command. It shows the same bounded
excerpts available to the classifier: the first 1,500 user characters and the
last 500 preceding assistant characters. Machine predictions stay hidden.
Choose `unknown` when those excerpts do not provide enough context. The review
population follows the coach collector; it can differ from the Index's wider
human-boundary population. The review manifest records its own ordered cohort.

Excerpts remain in memory and the local browser, with no external scripts,
requests, persistence or HTTP access logs. Only the opaque key and selected
enum are saved in a private JSONL file, alongside a sample manifest. The server
binds to `127.0.0.1`, requires its random URL token, checks Host and POST Origin,
and sends `no-store` responses. Stop it with Ctrl-C when finished. A partly
completed file contains only completed answers. Protect the local URL as you
would the excerpts it displays. This command deliberately displays private
text locally; the aggregate commands never display text. A write or sync failure
stops further answers, since a partial append cannot safely be retried.

Then compare machine labels with the saved reviews:

```sh
PYTHONPATH=src python3 -m anatomy correction-audit --labels labels.jsonl --reviews reviews.jsonl
PYTHONPATH=src python3 -m anatomy correction-index --labels labels.jsonl --reviews reviews.jsonl
```

The audit reports a confusion matrix, correction precision and recall, and
unreviewed/unmatched/unknown coverage. Conditional known-label accuracy is
separate from end-to-end agreement. Unknown or missing predictions remain
misses in recall; they cannot improve the score by disappearing. Unknown human
reviews are unresolved cases, not evidence of correctness. A small random
sample may contain too few corrections to estimate precision or recall well.

Review-file authorship, correctness, independence and representativeness are
not authenticated. An untouched test set must be kept separate from classifier
development. Do not call model-generated review labels human gold. Comparing a
file with itself verifies wiring only. Episode-level accuracy needs separate
validation because serial classifier errors can create apparent streaks.

`--out` creates a new private file and refuses to overwrite an existing file.
The commands print only aggregates or explicitly requested opaque-key records.
No feedback is submitted publicly. Recovery benefit requires a prospective
comparison with a holdout and verified outcomes; it is not inferred here.
