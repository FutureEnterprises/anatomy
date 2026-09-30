---
title: The anatomy of 745,771 agent calls
description: One heavy user's 745,771 Claude Code and Codex API calls, read to find where the tokens go, and why the same context-pruning rule would have saved 15% on one and 7% on the other in replay.
---

# The anatomy of 745,771 agent calls

I use Claude Code and Codex every day, most days for hours. Both keep transcripts on your machine. Between April and the end of September mine added up to 191,390 Claude Code API calls and 554,381 Codex calls, and I read all of them to find out where the tokens go.

The clearest result is a gap. Replayed on my logs, one context-pruning rule would have cut 15% of my Codex cost and 7% of my Claude Code cost, if nothing it removed were ever needed again. Most of that gap comes from what the two sets of sessions carry, part of it from the price sheet, and on Claude Code the saving is gone once about a third of the removed outputs have to be fetched back.

Four kinds of numbers appear below, and each one is labeled:

- **Observed**: counted straight from the transcripts.
- **Estimated**: attribution of those counts to a cause, which takes some inference.
- **Modeled**: a replay of what a change would have saved. A replay is not a live result. Removing context can change what the agent decides next, how often it retries and whether it finishes.
- **Invoiced**: none. My usage ran on subscriptions, so every cost share here is at API list-price equivalent.

Every response is counted once. A resumed or forked session copies earlier responses into its own transcript file, and those copies are not billed a second time.

Claude Code cost here includes fallback attempts that were declined after they had started streaming output, priced at list rates as if billed. They are 8.9% of it. Every Claude Code share below is of that one total.

## 1. One rule, two different results

*Modeled.* I replayed one pruning rule on both sets of logs:

- A tool output of 500 tokens or more becomes evictable once it has been in context for 3 calls.
- Evictions wait until at least 30,000 tokens can go at once.
- Each evicted output is replaced by a 40-token stub.
- The replay pays the full cache rewrite of everything still in context after the earliest evicted item.
- A re-fetch gives back the reads it saved, pays to write the output again and costs one extra model call to ask for it.

On Codex, the replay would have cut 15.4% of cost, in USD at the prices each call paid, if no evicted output were ever needed again. The rewrite ate 19% of what eviction saved. The rule stops paying once 38% of the evicted outputs have to be fetched back, and at a 25% re-fetch rate it still saves 5.1%.

On Claude Code, the same rule would have cut 7.0%. The rewrite ate 29% of the saving, and the rule stops paying at a 32% re-fetch rate. Count a re-fetch every time the agent later mentions an evicted output, which happens for 60% of them, and it loses 5.9%. That proxy overstates re-fetches, but the real rate would have to be about half of it before the rule broke even. I have no comparable measurement for Codex.

Most of the gap is the shape of the work, not the price sheet. Before any rewrite, eviction would have saved 19.1% on Codex and 9.8% on Claude Code, because my Codex sessions carry more stale tool output to remove. The price ratio then decides how much of that saving the rewrite takes back, and it bites harder on the two Claude models with the cheapest cache reads, Opus 5.5 and Fable 5.1, which served 27% of my Claude Code calls.

The mechanism is on the price sheet, and it is not my discovery. Anthropic's own guide to optimizing cost says every context-editing pass invalidates the cached prefix from the point it clears, so the next request pays to cache everything after it again. It reports a 20-issue run where context editing cost 74% more. Yan Song's July 17 paper, Cache-Aware Prompt Compression, shows the same tension for compression on Claude Sonnet 4.6: methods that compress differently for each query produce a new prefix on every call and invalidate the cache each time. What I can add is evidence. One rule, replayed over a large corpus of real coding-agent harness calls from two vendors, would have saved 15.4% on one and 7.0% on the other before any re-fetch, and on the second it loses money at the re-fetch rate a lexical proxy measures.

### The break-even rule

Delete a block of b tokens. S tokens sit after it, L calls are left, w is the cache-write price and r is the cache-read price. You save b × r on each remaining call. You pay S × (w - r) once to write the tail again. So:

**A deletion pays only if b × L > S × (w - r) / r.**

That is not one number per model. A small, early deletion under a huge tail needs far more calls to pay back. A large, late one needs far fewer. The table assumes the tail is about the size of what you delete (S ≈ b), which reduces the rule to L > (w - r) / r.

| Model | Cache read (x input price) | Rewrite (x input price) | Calls to pay back, 5-minute cache, if S ≈ b | Calls to pay back, 1-hour cache, if S ≈ b |
|---|---:|---:|---:|---:|
| GPT-5.6-sol (Codex) | 0.1 | 1.0 as replayed, 1.25 listed | 9 to 11.5 | n/a |
| Claude Sonnet 5, Opus 5, Opus 4.8 | 0.1 | 1.25 (5 min), 2 (1 hour) | 11.5 | 19 |
| Claude Opus 5.5 | 0.05 | 1.25 (5 min), 2 (1 hour) | 24 | 39 |
| Claude Fable 5.1 | 0.025 | 1.25 (5 min), 2 (1 hour) | 49 | 79 |

Prices are from the official pages, read September 30, 2026: platform.claude.com/docs/en/about-claude/pricing and developers.openai.com/api/docs/pricing. Per million tokens, Opus 5.5 is $4 input, $5 for a 5-minute cache write, $8 for a 1-hour write and $0.20 for a cache read. Fable 5.1 is $10, $12.50, $20 and $0.25. GPT-5.6-sol is $4.00 input, $0.40 cached input and $5.00 for a cache write. The Codex logs record no cache writes, so the replay prices a rewritten token as uncached input, which gives 9. At the listed cache-write price it is 11.5. To try other prices and tail sizes, use the [break-even calculator](https://futureenterprises.github.io/anatomy/calculator/).

*Modeled.* Real deletions are spread around that assumption, so here is the spread from my own logs. Across the 4,517 eviction batches the rule made on my Claude Code logs, the tail was a median 0.71 times the size of the evicted block, with the middle half between 0.48 and 1.12. The evicted tokens had a median 36 calls left to live, with the middle half between 15 and 77. Price every one of those batches at a single ratio, with no re-fetch, and the share that pays back falls from 86% at GPT-5.6-sol's 9 to 83% at 11.5 (Sonnet 5's 5-minute cache), 70% at 24 (Opus 5.5) and 50% at 49 (Fable 5.1). At the prices the calls actually paid, which mix models and both cache tiers, it is 76%.

Codex has more to remove. A Codex tool output stays in context for a median 29 more calls (observed), and tool output is 46% of Codex cost-weighted tokens against 18% of Claude Code cost. The rewrite ate 19% of what eviction saved on Codex and 29% on Claude Code, at the prices paid.

Cheaper cache reads are good news for everyone. They also make every deletion harder to pay back.

## 2. Where the money goes

Everything in this section is measured on one heavy user's logs: mine. It says where my tokens went. It does not say where yours go, and I make no claim about how common any pattern is.

The Claude Code corpus is 6,010 transcript files, 5,501 context threads and 191,390 API calls, from April 9 to September 29, 2026. The Codex corpus is 3,258 rollout files and 554,381 calls, most of them from September. A lot of my work fans out into many parallel agents, so multi-agent patterns weigh more here than they would for most people.

### Claude Code

*Observed.* Caching works. For every token written to the cache, about 30 were read back. Cache reads and writes together are 80% of my Claude Code cost: 42.4 billion tokens read, 1.43 billion written, 164 million generated. What caching can't fix is size and churn. 42.1% of the cost came from calls with 400,000 or more tokens in context, and 10 threads with over 1,000 calls each were 42.8% of it.

*Estimated*, share of Claude Code cost at API list-price equivalent:

| Pattern | Share |
|---|---:|
| Per-agent boot context (system prompt, tool schemas, listings) | 12.6% |
| Cache rebuilds after idle gaps | 10.7% |
| Stale tool-call arguments re-read every call (Write bodies, scripts, Edit strings) | 8.0% |
| Cache rebuilds from prefix changes, model switches and context edits | 7.6% |
| Hidden thinking re-read every turn | 6.7% |
| Bash output over 5,000 characters | 4.1% |
| Exploration output carried after its last use | 3.9% |
| Harness reminders re-injected mid-session | 3.6% |
| Files read again by sibling agents in the same session | 2.8% |

The last three tool-output rows overlap each other. The rest are disjoint.

Two of these deserve a sentence each. Boot context is the biggest: a subagent's first call carries a median 55,500 tokens (observed), and the part unique to its task is a median 343 (estimated). And 1,175 rebuilds broke the cache prefix with no idle gap, no model switch and no edit I could see in the transcript. They account for 4.6% of cost, and I can't tell you why they happened.

### Codex

*Observed.* Codex tells the same story with different weights. 97.4% of input tokens were cache hits, and those cheap reads are still 71.6% of the cost. The average call reads 145,661 tokens to write 353. Caching made each re-read cheap. It did nothing about how many there were.

*Estimated*, share of Codex cost-weighted tokens:

| Pattern | Share |
|---|---:|
| Stale tool output carried in context | 29.9% |
| Multi-agent coordination round trips | 17.5% |
| Compaction residue re-read after compaction | 17.2% |
| Fixed prefix re-read on every call | 16.9% |
| Serial read-only exploration round trips | 16.0% |
| Process polling round trips (write_stdin calls made directly) | 6.3% |

These rows overlap on purpose. Some say what gets re-read, others say why a call happened, and every call re-reads everything resident.

Two things I expected to find were not there. AGENTS.md averaged 122 tokens against a first-call median of 38,860. And work after the last edit or commit was 1.7% of cost.

One note for anyone estimating tokens from characters. Tool output in my Claude Code logs ran at 2.40 characters per token (estimated from calibration). Anthropic's pricing page gives about 4 as a rough estimate for English, which would undercount this context by about 40%. The same page says Claude 4.7 and later models use a tokenizer that produces about 30% more tokens for the same text, so the ratio also moves with the model. The Codex logs came out close to 4.

## 3. Seven context tricks, priced under caching

*Modeled.* I took seven context tricks and replayed each one on the Claude Code logs, charging it for every cache rewrite it causes and every re-fetch it needs. Some are standard advice. A few were ideas of mine that I hoped would work.

| Trick | What it does | Net result |
|---|---|---|
| Lease eviction | The rule from section 1 | +7.0% before re-fetches, -5.9% at the lexical re-fetch rate (break-even at 32%). |
| Head/tail admission | Keep the head and tail of a large output, fetch the rest on demand | +1.8% to +2.2%. 64% to 66% of truncated outputs were needed again. Perfect foresight caps it at 5.0% |
| Hindsight residue | Show a large result once, then keep only the lines the model referenced | Costs 5.5% more than doing nothing on the results it touches |
| Write-once tool arguments | Replace the agent's own large Write bodies and scripts with a stub after the first send | +3.0% optimistic, -6.8% pessimistic |
| Shared fan-out trunk | Preload the documents sibling agents are told to read into one shared cached prefix | -28% of multi-agent cost. Perfect hindsight finds +0.1% |
| Read-ahead prefetch | Fetch the files the agent will probably read next | Zero or negative in all 24 configurations |
| Batched context GC | Evict idle context in large batches | Tool results only: +0.8%. Adding old thinking and old tool arguments: +8.9% in 150K batches, +9.6% in 60K batches. Variants using 20K batches: -3.0% to +4.6% |

Positive means saved. Unless the row says otherwise, shares are of Claude Code cost at the list prices of the models I actually ran.

Once re-fetches are priced in, every trick that removes or truncates only tool output either lost money or netted under 3%. The best result, 9.6%, came from batch-deleting old tool results together with old thinking and old tool arguments. I don't trust it yet. Its effect on answers is unmeasured, and newer models bind thinking blocks to unedited history, so it would have to go through the vendor's own clearing feature.

What this replay can't tell you:

- **It is a replay.** No live agent ran under these policies. A modeled saving is not a live saving: removing context can change the agent's later decisions, its retries and whether it completes the task.
- **Re-fetch is a lexical proxy.** Any later mention of an evicted output's distinctive tokens counts as a re-fetch. That overstates re-fetches, which is why I also report the zero re-fetch best case.
- **Answer quality is not measured.** A trick that saves money and breaks answers saves nothing.
- **The two corpora differ in shape.** Claude contexts here run to 1 million tokens, and tool results are 18.2% of Claude Code cost against 46% of Codex cost-weighted tokens.

That is why the next step is a benchmark.

## 4. What survives

Three fixes pay off in replay on my logs. None of them edits the middle of a cached context.

**Scope subagent boots.** Trimming the boot works before the first cache write, so there is no tail to rewrite.

- *Observed.* Workflow agents carry a median 41 tool definitions and call a median 3. Unused definitions are 91% of schema characters.
- *Observed.* One built-in tool, Artifact, is 40.1% of all tool-schema characters in workflow agents. None of the 1,286 agents with a tool snapshot called it.
- A skill listing of a median 11,059 tokens (estimated) sat in 5,405 agents, and 98.6% of them never called Skill (observed). The deferred-tool listing, a median 5,462 tokens, went unused by 78.0%.
- *Modeled.* Across all of Claude Code cost, boot scoping addresses 3.8% to 9.5%. The tool's own audit prices the narrowest version, dropping only the listings a subagent never used: 4.5%.

**Keep the cache warm across short gaps, with a cap.** The same ratio decides this one, in the other direction. A keep-alive ping re-reads the prefix at r. A rebuild writes it again at w. So pinging pays while the gap needs fewer than (w - r) / r pings, ignoring the few tokens each ping generates. On Opus 5.5's 5-minute cache that is 24 pings, about two hours. On its 1-hour cache it is 39 pings, more than a day.

*Estimated.* The tool's keep-alive audit counts 801 rebuilds after a gap longer than the thread's cache lifetime, on the same model with no fallback in between, and puts their excess cost at 7.9% of my Claude Code cost. Section 2's 10.7% uses a broader definition that also counts 5-minute gaps on threads that mostly write 1-hour cache entries. *Modeled.* With perfect knowledge of every gap, keep-alive would have paid in 702 of those gaps and recovered 5.2%. A real pinger doesn't know how long you'll be away. One that pings until the pings have cost as much as one rebuild and then stops, paying that full cap after each session's last call too, nets 3.1%. Pinging through every gap with no cap loses 2.6%. claude-thermos and cachekeeper already do keep-alive, each with limits of its own, and the plan is to wrap them rather than rebuild them.

**Block instead of polling on Codex.** *Observed.* 44,858 of 46,183 Codex process polls (write_stdin calls, made directly or from code mode) sent no input, and 20,183 came back with no new output. *Modeled.* The tool's polls audit finds 19,794 model calls whose only new input was polls that came back empty. Had each run of those polls been one blocking wait that returns when the process prints or exits, those calls would not have happened: 2.4% of Codex cost. That is after charging the 265 waits that would have outlasted a 5-minute cache window, where the call that resumes pays to cache the prefix again. It assumes the empty polls told the agent nothing it needed, which the logs can't confirm. It removes round trips instead of editing the middle of the cached prefix.

## 5. A pre-registered benchmark is coming

Headlines in this space shrink under paired tests. rtk advertises 60 to 90% smaller output on common dev commands. In JetBrains' paired test on Claude Code, tasks with rtk cost a median 7.6% more at low effort. I don't want my numbers to go the same way, so I'm writing my predictions down before the runs.

What the benchmark tests:

- **The sign flip.** Per-turn observation masking (keep the last 10 tool outputs, replace older ones with a placeholder) is the standard baseline. JetBrains Research showed it roughly halves cost against a raw agent while matching LLM summarization's solve rate. I predict it lowers cache-correct dollars per solved task on GPT-5.6-sol and Claude Sonnet 5, and raises it on Opus 5.5 and Fable 5.1, with pass rate within 3 points of raw.
- **Price or vendor.** Sonnet 5 is the control: an Anthropic model with a ratio of 11.5, close to Codex. If the flip comes from the price ratio, Sonnet 5 behaves like GPT-5.6-sol.
- **The controller.** A break-even controller, specified and frozen with the predictions, is never worse than the better of raw and masking on dollars per solved task, on any of the four models, with pass rate within 3 points.
- **Calibration.** At least 80% of Anatomy's per-arm cost predictions land within 25% of the realized paired cost ratio.

The setup: a minimal bash-only agent of about 150 lines, to be published with the frozen set, with explicit prompt caching on both vendors. 60 SWE-bench Verified tasks, plus 4 marathon chains of 5 same-repo tasks, each chain run in one session, because long threads are where the money is. Three repeats per task and arm, because published work finds runs of the same task differing by up to 30x in tokens. The primary metric is cache-correct list-price dollars per solved task. Each arm runs in its own API workspace, so the vendor's usage report is the ground truth, and my ledger has to match it within 1%. Runs that bypass permission prompts happen only in isolated, disposable containers, one per run.

It is falsified if masking lowers Opus 5.5 dollars per solved task by 5% or more at equal pass rate, if the controller loses to the better baseline on any model outside the 95% interval, or if fewer than 80% of the predictions land within 25%. If that happens, the miss is the result, and I'll publish it that way.

None of this has run yet. The [benchmark design](https://futureenterprises.github.io/anatomy/benchmark.html) is published in the repo. After a disclosed 10-task pilot, and before the first main run, the task lists, arm configs, predictions, analysis script and controller spec will be committed and tagged, and I'll post their SHA-256. Results will be posted in the repo.

Then comes the part people actually run: Claude Code headless and Codex exec, bare and with a heavy public profile, stock and with popular token savers (rtk, caveman, headroom, claude-mem and code-review-graph). If you maintain one of these, send me the config you would recommend and I'll run that one. Scoring your tool on a config you didn't choose wouldn't be fair.

## 6. Run it on your own logs

The code is at [github.com/FutureEnterprises/anatomy](https://github.com/FutureEnterprises/anatomy), under Apache-2.0.

```
uvx --from git+https://github.com/FutureEnterprises/anatomy anatomy scan
```

It reads the transcripts Claude Code and Codex already keep on your machine, in ~/.claude/projects and ~/.codex. Nothing leaves your laptop. It uses only Python's standard library and makes no network calls. It prints numbers and labels only. Prompt text, file contents, paths and commands never become labels. A tool name appears only if it is on a fixed list of built-in Claude Code and Codex tools; anything else prints as "other", and every MCP tool as "mcp". Before anything prints, a gate checks every string and scans the output for path, email, URL and secret shapes. Costs are API list-price equivalents, and the scan and audit reports print the date and source of the price snapshot they used, because prices change and the break-even moves with them.

If you build on the Claude API directly, Anthropic's /claude-api cost-optimize command in Claude Code is the place to start. Their September 8 post describes it profiling where your tokens go and, if you supply an eval, computing cost and performance across effort levels and models. Anatomy doesn't replace vendor tools. It reads coding-agent harness transcripts locally, puts Claude Code and Codex in one ledger, and prices counterfactual fixes against your own model's cache ratio. `anatomy card` prints the fixes that clear break-even at your prices and the one evaluated trick that would have cost you money in replay.

Your numbers will differ from mine. That is the point, and I would love to hear where.

This stands on other people's work. ccusage made local cost reports for these tools normal. ccledger dedupes on the stable message id, the rule Anatomy's ledger applies across files. claude-thermos and cachekeeper already audit cache rebuilds and keep the cache warm. JetBrains Research's observation-masking work set the baseline everything here has to beat. TokenPilot and Yan Song's CAPC paper model cache-aware eviction and compression, and Anthropic's cost guide states the rewrite cost plainly. Anatomy tries to put the same arithmetic in front of the tools people use every day.

Iman Schrock
