---
title: Benchmark design
description: The pre-registered, cache-correct benchmark behind Anatomy's replay results. Design only; nothing has run yet.
---

# Benchmark design

The savings in the [essay](https://futureenterprises.github.io/anatomy/essay.html) are replays on recorded calls. This benchmark tests them on live agents. This page is the design. Nothing has run yet.

## Order of work

1. **Pilot.** A disclosed 10-task pilot, disjoint from the main task set. It sizes the cost per run and fits the controller's two parameters, and it is disclosed with the results.
2. **Freeze.** The task lists, arm configs, predictions, analysis script, controller spec and the minimal agent are committed and tagged in this repo, and their SHA-256 is posted publicly. This happens before the first main run.
3. **Main runs.**
4. **Results** are posted in this repo, including any prediction that misses.

## The claims

All three must hold.

1. **Sign flip.** Per-turn observation masking (keep the last 10 tool outputs, replace older ones with a placeholder) is the standard baseline in the literature. It lowers cache-correct dollars per solved task on GPT-5.6-sol and Claude Sonnet 5, and raises it on Claude Opus 5.5 and Claude Fable 5.1, with pass rate within 3 points of raw.
2. **Controller.** The break-even controller is never worse than the better of raw and masking on dollars per solved task, on any of the four models, with pass rate within 3 points.
3. **Calibration.** At least 80% of Anatomy's per-arm cost predictions land within 25% of the realized paired cost ratio.

Sonnet 5 is the control: an Anthropic model whose write/read ratio (11.5) is close to GPT-5.6-sol's. If the flip comes from the price ratio and not the vendor, Sonnet 5 behaves like GPT-5.6-sol.

**The claims are falsified if** masking lowers Opus 5.5 dollars per solved task by 5% or more at equal pass rate, if the controller loses to the better baseline on any model outside the 95% interval, or if fewer than 80% of the predictions land within 25%. A miss is a result and is published as one.

## Loop layer

This layer tests policies that need control of the history, which Claude Code and Codex do not expose.

- **Agent:** a minimal bash-only loop of about 150 lines, published with the frozen set, with explicit prompt caching on both vendors.
- **Models and write/read ratios:** Opus 5.5 (24), Fable 5.1 (49), Sonnet 5 (11.5), GPT-5.6-sol (9 to 11.5). Ratios come from the list prices in this repo's price snapshots, on the 5-minute cache for the Claude models.
- **Arms:** raw; observation masking (window 10); LLM summarization at 60% of a 200K-token budget; the break-even controller.
- **Controller:** evicts a batch of b tokens only when b × L_hat × r > S × (w - r) + p_hat × (re-fetch cost). L_hat (calls the batch would stay) and p_hat (re-fetch probability) are fitted on the pilot. Candidate batches include old tool output and, through the vendors' own clearing features, old thinking and old tool arguments.
- **Tasks:** 60 SWE-bench Verified tasks, stratified by difficulty, plus 4 marathon chains of 5 same-repo tasks, each chain run in one session. Long threads are where the money is: in the launch corpus, 10 threads with over 1,000 calls each were 42.8% of Claude Code cost.
- **Repeats:** 3 per task and arm, because published work finds runs of the same task differing by up to 30x in tokens.

## Harness layer

This is what people actually run.

- **Tools:** Claude Code headless and Codex exec.
- **Profiles:** bare (no user configuration), and heavy: public and synthetic, with 12 public MCP servers, 60 public skills and a 3K-token CLAUDE.md or AGENTS.md, built to reproduce a boot of about 55K tokens with about 40 tools.
- **Arms:** stock, and stock plus each popular token saver at its maintainer's recommended config: rtk, caveman, headroom, claude-mem and code-review-graph. Maintainers are invited to send the config they want tested.
- **Tasks:** 40 SWE-bench Verified tasks, 30 Terminal-Bench 2.0 tasks and 20 fan-out tasks on public repos (8 parallel subagents each, deterministic checkers).

## Metrics

- **Primary:** cache-correct list-price dollars per solved task. Cost-weighted tokens per solved task for comparisons across vendors.
- **Pass rate** with Wilson 95% intervals. Wall time per task, p50 and p90.
- **Secondary:** tokens by billing component (uncached input, 5-minute write, 1-hour write, cache read, output, thinking), round trips, cache rebuilds by cause and re-fetch rate.
- **Statistics:** paired bootstrap over tasks (10,000 resamples) of per-task cost ratios, non-inferiority on pass rate, and Holm correction across arms.

## Reproducibility and safety

- Each arm runs in its own API workspace or project, so the vendor's usage report is the ground truth for cost. Anatomy's ledger has to match it within 1%.
- Every run gets its own isolated, disposable container, discarded afterwards. SWE-bench tasks are graded with the official evaluation harness.
- Runs that bypass permission prompts happen only inside those disposable containers, never on a personal or shared machine.
