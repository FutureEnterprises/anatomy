# Anatomy

A local, cache-correct cost audit for Claude Code and Codex transcripts.

Both tools already keep a transcript of every session on your machine. Anatomy reads them, rebuilds what each API call would cost at list prices (cache writes and cache reads priced separately, each response billed once), attributes the cost to what caused it, and prices candidate fixes against your own model's cache prices. Removing context from a cached prompt forces everything after it to be cached again, so a trick that saves money on one model can lose it on another. Anatomy does that arithmetic per call.

## What it is not

- **Not live savings.** Fix estimates are replays on your recorded calls, labeled `modeled`. In a live agent, removing context can change what it does next, how often it retries and whether it finishes.
- **Not invoices.** Dollars are USD API list-price equivalents at a dated price snapshot: standard speed, no negotiated discounts. On a subscription plan, read them as a cost weight.
- **Not anyone else's usage.** A report describes the transcripts it read. The launch analysis behind this project is one heavy user's corpus and makes no claim about how common any pattern is.

## Install and run

Python 3.12 or later. No dependencies.

```
uvx --from git+https://github.com/<OWNER>/anatomy anatomy scan
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
| `anatomy card` | A numbers-only card: your top three fixes that clear break-even and the one evaluated trick that would have cost you money in replay. `--svg FILE` also writes it as an image. Below 20 threads or 5 sessions the card is marked do-not-share. |

Common options: `--json`, `--until TIME` (ignore records after a UTC time, to pin a snapshot), `--no-claude`, `--no-codex`, `--claude-dir`, `--codex-dir`, `--workers`, and `--dedupe {global,file}` (default `global`: bill each message or response id once across all files; `file` reproduces per-file deduplication for comparison).

Every number in the output ends with its basis: `[observed]` (transcript usage fields, times list price, and the published prices themselves), `[estimated]` (attribution, token-size estimates, and the cost of declined Claude Code fallback attempts, whose billing the pricing page does not state), `[modeled]` (counterfactual replays) or `[invoiced]` (vendor data, which Anatomy only has if you supply it). See [docs/method.md](docs/method.md).

## The break-even page

[site/breakeven.html](site/breakeven.html) is a static page: pick a model and cache tier, or enter b, L, S, w and r, and it shows whether a deletion pays, the write/read ratio and the calls needed to pay back. It uses one local script and the price snapshot in `site/prices.json`, and sends nothing anywhere. Serve the folder to use the model picker:

```
python3 -m http.server --directory site 8000
```

Regenerate `site/prices.json` after a price update with `python3 site/make_prices.py`.

## Privacy

- Runs entirely on your machine. Standard library only; no network calls.
- Prints numbers and labels only. Prompt text, file contents, paths and commands never become labels. A tool name is printed only when it is on a fixed list of built-in Claude Code and Codex tools; any other tool name prints as `other` and every MCP tool as `mcp`, so your own tool, plugin and MCP server names stay out. Model ids print only when they look like public model names. Other labels are harness-defined words (attachment and message kinds) that pass a shape filter and a deny list.
- Before printing, a gate walks the whole result and refuses any string that fails that shape filter or deny list, then scans the rendered text for secret, email, path and URL shapes (the two vendor pricing pages are the only URLs allowed). The gate is a shape check, not a vocabulary check; the allowlists above are what keep private names out.
- Reads transcripts read-only and writes nothing unless you ask for `--svg FILE`.

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

Apache-2.0. See [LICENSE](LICENSE).
