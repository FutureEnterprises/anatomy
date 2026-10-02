# Anatomy

A local, cache-correct cost audit for Claude Code and Codex transcripts.

**Read the essay:** [The anatomy of 745,771 agent calls](https://futureenterprises.github.io/anatomy/essay.html). **Try the numbers:** the [break-even calculator](https://futureenterprises.github.io/anatomy/calculator/). The pre-registered [benchmark design](https://futureenterprises.github.io/anatomy/benchmark.html) is published; nothing has run yet.

Both tools already keep a transcript of every session on your machine. Anatomy reads them, rebuilds what each API call would cost at list prices (cache writes and cache reads priced separately, each response billed once), attributes the cost to what caused it, and prices candidate fixes against your own model's cache prices. Removing context from a cached prompt forces everything after it to be cached again, so a trick that saves money on one model can lose it on another. Anatomy does that arithmetic per call.

## What it is not

- **Not live savings.** Fix estimates are replays on your recorded calls, labeled `modeled`. In a live agent, removing context can change what it does next, how often it retries and whether it finishes.
- **Not invoices.** Dollars are USD API list-price equivalents at a dated price snapshot: standard speed, no negotiated discounts. On a subscription plan, read them as a cost weight.
- **Not anyone else's usage.** A report describes the transcripts it read. The launch analysis behind this project is one heavy user's corpus and makes no claim about how common any pattern is.

## Install and run

Python 3.12 or later. No dependencies.

```
uvx --from git+https://github.com/FutureEnterprises/anatomy anatomy scan
```

From a clone:

```
PYTHONPATH=src python3 -m anatomy scan
```

## Commands

| Command | What it prints |
|---|---|
| `anatomy scan` | The ledger: calls, tokens and list-price cost by tier, model and thread kind; input cost attributed by cause; cache rebuilds by cause; Codex process polls. |
| `anatomy audit` | The scan plus every audit (boot scope, keep-alive, polls, oversized outputs, context clearing, break-even), each with at most one fix and whether it clears break-even at your prices. |
| `anatomy card` | A numbers-only card: your top three fixes that clear break-even and the one evaluated trick that would have cost you money in replay. `--svg FILE` also writes it as an image, and `--shares-only` replaces every dollar amount with a share of the spend read. Below 20 threads or 5 sessions the card is marked do-not-share. |
| `anatomy coach-baseline` | A personal historical correction baseline for the EMILIA Session Coach, exported as `emilia.anatomy.coach.v1`. Labels come from a file keyed by `--print-keys` (join keys and positions, never text), or, with explicit consent, from an unmanaged first-party Claude Pro or Max subscription through the local `claude` CLI. The baseline uses a filtered prompt sequence, explained below. Labels are estimates; prompt and session counts are observed. |
| `anatomy correction-index` | Personal correction patterns and individually priced main Claude Code responses following each prompt, with literal boundaries, copied-history deduplication, opaque repository groups and visible missing data. Uses saved labels; without them, labels stay unknown. Dollars are API-price equivalents. |
| `anatomy correction-review` | A blind human review of a random sample in a private loopback browser. Excerpts stay in RAM and the local browser; saved answers contain only opaque keys and label enums. |
| `anatomy correction-audit` | Compares saved machine labels with separately supplied reviews: confusion counts, precision, recall and unresolved coverage. It does not authenticate human authorship or validate recovery benefit. |

Scan, audit and card options: `--json`, `--until TIME` (ignore records after a UTC time, to pin a snapshot), `--no-claude`, `--no-codex`, `--claude-dir`, `--codex-dir`, `--workers`, and `--dedupe {global,file}` (default `global`: bill each message or response id once across all files; `file` reproduces per-file deduplication for comparison). Correction commands have their own options; run their `--help` or read the [Correction Index method and review workflow](docs/correction-index.md).

Every number in the output ends with its basis: `[observed]` (transcript usage fields, times list price, and the published prices themselves), `[estimated]` (attribution, token-size estimates, and the cost of declined Claude Code fallback attempts, whose billing the pricing page does not state), `[modeled]` (counterfactual replays) or `[invoiced]` (vendor data, which Anatomy only has if you supply it). See [docs/method.md](docs/method.md).

### Coach baseline

This is a filtered historical association in Claude Code, not a control group or evidence that a recovery action works. `retry` and `nudge` labels are skipped: a correction, a retry, then another correction are adjacent in this baseline. `afterTwo` is a subset of `afterOne`. Unknown labels break the known history and never count as non-corrections. Copies preserve history but are not counted again. These definitions differ from the coach experiment's literal next-user-prompt sequence, where every prompt needs a label; do not treat the two rates as interchangeable.

The text report includes unknown/skipped labels, read errors and failed classifier batches. The strict coach JSON envelope includes only the baseline's counts, denominators and classifier identifier; it omits that coverage detail. Retain the text report when evaluating an import. Truncation and unvalidated classifier labels can affect the rates; a successful live smoke proves integration, not classifier accuracy.

The optional classifier supports unmanaged macOS or Linux profiles signed into a first-party Claude Pro or Max subscription. It refuses API credentials, custom provider/routing/runtime environment, provider helpers or settings, managed/cached policy files and unsupported CLI isolation flags before sending prompts. It does not silently remove a billing credential and switch accounts. Native auth status is checked without printing identity fields. Unsupported profiles can use `--labels`; no live model is used for that path.

All classifier calls require safe mode, disabled tools and slash commands, an explicit empty MCP configuration, no saved session, empty user/project setting sources, and a replacement system prompt. `--bare` is not used because subscription OAuth must remain available. The child receives a small environment allowlist; unrelated API and GitHub credentials are excluded. Anatomy requests disabled nonessential client traffic and automatic attachments. This is not a guarantee about the CLI's inherent runtime traffic, provider storage or system/administrator policies.

After signing in, a bounded smoke can use a directory containing only hand-written synthetic main-thread fixtures. Set `COACH_SMOKE_ROOT` to that directory and `COACH_PANEL_SESSION` to the session ID shown in the coach panel, then run from the clone:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m anatomy coach-baseline \
  --claude-dir "$COACH_SMOKE_ROOT" --workers 1 \
  --coach-session "$COACH_PANEL_SESSION" --coach-provider claude \
  --classify-with-claude --i-consent-to-send-prompts-to-my-claude --json
```

This sends only the synthetic fixture's selected prompt excerpts and preceding reply tails as classifier input. It does not scan the default private transcript directory. A fixture result is test data, not a personal baseline. Before merging the live path, verify that it returns known labels, the expected public classifier identity, a valid envelope and a successful coach import; retain the fixture's text report for coverage.

## The break-even calculator

[The calculator](https://futureenterprises.github.io/anatomy/calculator/) ([source](docs/calculator/index.html)) is a static page: pick a model and cache tier, or enter b, L, S, w and r, and it shows whether a deletion pays, the write/read ratio and the calls needed to pay back. It uses one local script and the price snapshot in `docs/calculator/prices.json`, and sends nothing anywhere. To run it locally, serve the folder:

```
python3 -m http.server --directory docs/calculator 8000
```

Regenerate `docs/calculator/prices.json` after a price update with `python3 scripts/make_prices.py`. A test fails if the page data and the snapshots disagree.

## Privacy

- Standard library only. Anatomy makes no network calls itself. Its one model path is `coach-baseline --classify-with-claude`: after explicit consent and local profile/account checks, it provides each selected prompt (cut to 1,500 characters) and the preceding 500-character reply tail to the local `claude` CLI for first-party subscription classification. Tools, MCP, slash commands and session saving are disabled. The CLI and provider still govern their own runtime traffic, policies and storage.
- Aggregate commands print numbers and labels only. Explicit key/sample exports contain opaque hashes and positions. `correction-review` deliberately shows selected private excerpts in a token-protected loopback browser for human review, without saving the excerpts or sending them to a provider. Prompt text, file contents, paths and commands never become report labels. A tool name is printed only when it is on a fixed list of built-in Claude Code and Codex tools; any other tool name prints as `other` and every MCP tool as `mcp`, so your own tool, plugin and MCP server names stay out. Model ids print only when they look like public model names. Other labels are harness-defined words (attachment and message kinds) that pass a shape filter and a deny list.
- Before printing, a gate walks the whole result and refuses any string that fails that shape filter or deny list, then scans the rendered text for secret, email, path and URL shapes (the two vendor pricing pages are the only URLs allowed). The gate is a shape check, not a vocabulary check; the allowlists above are what keep private names out.
- Reads transcripts read-only and writes nothing unless you ask for `--svg FILE`, `--out FILE` or `--save-labels FILE`. Correction report/sample/review files are private and refuse overwrites. A human review also saves its opaque sample manifest.

## Prices

`src/anatomy/prices/anthropic.toml` and `openai.toml` are dated snapshots of the official pricing pages, each with its source URL. The scan and audit reports print each snapshot's date and source URL, and the card prints the dates, because prices change and the break-even moves with them.

## Credits

This stands on other people's work:

- Anthropic's guide, [Optimizing for cost and intelligence](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence), states the rewrite cost plainly: a context-editing pass invalidates the cached prefix from the point it clears.
- Yan Song, [Cache-Aware Prompt Compression](https://arxiv.org/abs/2607.15516) (arXiv 2607.15516), shows the same tension for prompt compression.
- Anthropic's [`/claude-api cost-optimize`](https://claude.com/blog/reducing-cost-and-improving-performance-with-claude-platform) profiles where tokens go for apps built on the Claude API. Start there if that is what you build; Anatomy covers coding-agent harness transcripts.
- [ccusage](https://github.com/ccusage/ccusage) made local cost reports for coding agents normal.
- [ccledger](https://github.com/zkm00323/ccledger) dedupes Claude Code records on the stable message id, the rule this ledger applies across files.
- [claude-thermos](https://github.com/izeigerman/claude-thermos) and [cachekeeper](https://github.com/grapefruit0205/cachekeeper) keep the prompt cache warm and audit cache rebuilds.
- [rtk](https://github.com/rtk-ai/rtk) compresses command output before it reaches the model, and [JetBrains' paired test of it](https://blog.jetbrains.com/ai/2026/07/rtk-claude-code-token-savings/) is the kind of measurement token savers need.
- [The Complexity Trap](https://arxiv.org/abs/2508.21433) (JetBrains Research) set the observation-masking baseline, and [TokenPilot](https://arxiv.org/abs/2606.17016) models cache-aware eviction.

## License

Apache-2.0. See [LICENSE](https://github.com/FutureEnterprises/anatomy/blob/main/LICENSE).
