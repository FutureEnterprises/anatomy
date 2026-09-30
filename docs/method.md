# Method

How Anatomy turns local transcripts into a cost ledger, and how it decides whether a context edit pays. Every number Anatomy prints carries one of four bases, and every dollar is a USD API list-price equivalent at a dated price snapshot.

## Bases

| Basis | Meaning |
|---|---|
| observed | Read from transcript usage fields (tokens, calls), or those fields times a published list price. |
| estimated | Attribution of observed tokens to a cause (a tool result, a boot prefix, an idle gap), and token sizes estimated from characters. |
| modeled | A counterfactual replay: what a change would have saved on the recorded calls. Not a live result. |
| invoiced | Vendor invoice or usage-report data. Anatomy has none unless you supply it, and says so. |

Subscription plans do not bill per token. On a subscription, read the dollars as a consistent cost weight, not as money spent.

## The ledger

**Claude Code** (`~/.claude/projects/**/*.jsonl`). One line per message id, carrying the served attempt's top-level usage: uncached input, 5-minute and 1-hour cache writes, cache reads and output, each at its own price. A declined fallback attempt that had already streamed output is billed from its per-attempt usage record; one declined before any output is reported separately and kept out of the total.

**Codex** (`~/.codex/sessions` and `~/.codex/archived_sessions`). One line per deduplicated `token_count` event, at long-context rates when the request is over the model's threshold, plus compaction requests that appear only as usage records.

**Each response is billed once.** A resumed or forked session copies earlier responses into its own transcript file. By default (`--dedupe global`) Anatomy bills every message or response id once, in the file whose last record is oldest, and marks the copies in every other file. Copies stay in their thread's timeline, so the context they carry is still seen by attribution and replays, but they are never billed or scored twice. `--dedupe file` reproduces the older per-file behavior for comparison.

`--until` ignores records stamped after a given time, so a report can be pinned to a corpus snapshot while transcripts keep growing.

## The break-even rule

Deleting b tokens from a cached prompt saves their cache read r on each of the L later calls they would have stayed for. It forces the S tokens after them to be written again, once, at the write price w instead of the read price r. So:

    a deletion pays only if  b × L × r  >  S × (w - r)  (+ any re-fetch cost)

Divide by b × r: the deletion pays back after (S / b) × R calls, where R = (w - r) / r is the model's write/read ratio. With the tail as large as the deletion (S = b), the rule is L > R. Examples from the price snapshots in this repository (modeled, from list prices dated 2026-09-30):

| Model | R, 5-minute cache | R, 1-hour cache |
|---|---:|---:|
| Claude Opus 5, Opus 4.8, Sonnet 5 | 11.5 | 19 |
| Claude Opus 5.5 | 24 | 39 |
| Claude Fable 5.1 | 49 | 79 |
| GPT-5.6-sol, rewrite billed as uncached input | 9 | n/a |
| GPT-5.6-sol, rewrite at the listed cache-write price | 11.5 | n/a |

Codex rollouts report no cache-write tokens, so a rewritten Codex suffix is priced as uncached input.

Three details matter when the rule is applied to real transcripts:

1. **S is measured on the pruned context.** The tail to rewrite is everything still in context after the earliest removed token, net of anything an earlier edit in the same replay already removed. Measuring it on the historical context counts earlier removals twice and overstates the rewrite.
2. **Re-fetch is an assumption.** Nothing in a transcript says whether a removed output would have been needed again. Anatomy reports the zero re-fetch case as an upper bound and the re-fetch rate at which the edit stops paying.
3. **Prices are per call.** Each call is priced at its own model and cache tier, so the same edit can pay on one model and lose on another.

The same ratio decides keep-alive in the other direction: a ping re-reads the prefix at r, a rebuild writes it at w, so pinging pays while the gap needs fewer than R pings.

## Prices

`src/anatomy/prices/anthropic.toml` and `openai.toml` are dated snapshots of the official pricing pages, each with its source URL. Nothing else in the code holds a price. To update, re-open the pages, edit the snapshot and its `snapshot_date`, and regenerate the page data with `python3 site/make_prices.py`.

## What this is not

- Not live savings. Replays run on recorded calls; removing context in a live agent can change what it does next, how often it retries and whether it finishes.
- Not invoices. List prices, standard speed, no negotiated discounts.
- Not a statement about anyone else's usage. A report describes the transcripts it read.
