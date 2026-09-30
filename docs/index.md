---
title: Anatomy
description: A local, cache-correct cost audit for Claude Code and Codex transcripts.
---

# Anatomy

A local, cache-correct cost audit for Claude Code and Codex. Both tools already keep a transcript of every session on your machine. Anatomy reads them, rebuilds what each API call would cost at list prices, with cache writes and cache reads priced separately and each response billed once, attributes that cost to what caused it, and prices candidate fixes against your own model's cache prices. Removing context from a cached prompt forces everything after it to be cached again, so a trick that saves money on one model can lose it on another. Anatomy runs on your machine, uses only Python's standard library, makes no network calls and prints numbers and labels only.

- [The anatomy of 745,771 agent calls](essay.html): the essay, on where the tokens went in one heavy user's Claude Code and Codex logs.
- [Break-even calculator](calculator/): does deleting context from a cached prompt pay at your prices?
- [Benchmark design](benchmark.html): the pre-registered benchmark. Nothing has run yet.
- [Method](method.html): how the ledger, the labels and the break-even rule work.
- [Source code](https://github.com/FutureEnterprises/anatomy) on GitHub, under Apache-2.0.

Run it on your own logs:

```
uvx --from git+https://github.com/FutureEnterprises/anatomy anatomy scan
```
