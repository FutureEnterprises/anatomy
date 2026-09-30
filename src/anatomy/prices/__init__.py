"""Dated price snapshots, loaded from the TOML files next to this module.

Each snapshot carries its source URL and date. Callers may pass their own TOML
path (tests do) so that no number is hard-coded outside a snapshot file.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

_DATED = re.compile(r'^(?P<base>.+)-(?P<date>\d{8})$')


def _read(provider: str, path: str | Path | None) -> dict:
    if path is not None:
        with open(path, 'rb') as fh:
            return tomllib.load(fh)
    with resources.files(__package__).joinpath(provider + '.toml').open('rb') as fh:
        return tomllib.load(fh)


@dataclass(frozen=True)
class ClaudeRates:
    """USD per token (not per million) for one Claude call."""
    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float


class AnthropicPrices:
    def __init__(self, path: str | Path | None = None):
        d = _read('anthropic', path)
        self.snapshot_date = d.get('snapshot_date')
        self.source_url = d.get('source_url')
        self.models = d.get('models', {})
        self.fast = d.get('fast_mode', {})
        self.geo_us = float(d.get('data_residency', {}).get('us_multiplier', 1.0))
        self._cache: dict = {}

    def base_row(self, model: str | None):
        """The price row for a model id: exact match, or a dated snapshot of a listed id."""
        if not model:
            return None, None
        if model in self.models:
            return model, self.models[model]
        m = _DATED.match(model)
        if m and m.group('base') in self.models:
            return m.group('base'), self.models[m.group('base')]
        return None, None

    def rates(self, model: str | None, speed: str | None = None, geo: str | None = None) -> ClaudeRates | None:
        key = (model, speed, geo)
        if key in self._cache:
            return self._cache[key]
        base, row = self.base_row(model)
        r = None
        if row is not None:
            inp, out = row['input'], row['output']
            w5, w1, rd = row['cache_write_5m'], row['cache_write_1h'], row['cache_read']
            if speed == 'fast' and base in self.fast:
                f = self.fast[base]
                read_mult = rd / inp
                inp, out = f['input'], f['output']
                w5, w1, rd = inp * 1.25, inp * 2.0, inp * read_mult
            mult = self.geo_us if geo == 'us' else 1.0
            r = ClaudeRates(inp * mult / 1e6, w5 * mult / 1e6, w1 * mult / 1e6, rd * mult / 1e6, out * mult / 1e6)
        self._cache[key] = r
        return r


@dataclass(frozen=True)
class OpenAIRates:
    """USD per token for one Codex call, already switched to long-context rates when they apply."""
    input: float
    cached_input: float
    cache_write: float
    output: float


class OpenAIPrices:
    def __init__(self, path: str | Path | None = None):
        d = _read('openai', path)
        self.snapshot_date = d.get('snapshot_date')
        self.source_url = d.get('source_url')
        self.long_threshold = int(d.get('long_context_threshold', 272_000))
        self.models = d.get('models', {})

    def row(self, model: str | None):
        if not model:
            return None
        if model in self.models:
            return self.models[model]
        # dated snapshots such as '<id>-2026-09-01' map to their id, never to a different family
        for k, v in self.models.items():
            if model.startswith(k + '-2'):
                return v
        return None

    def rates(self, model: str | None, input_tokens: int = 0) -> OpenAIRates | None:
        row = self.row(model)
        if row is None:
            return None
        src = row
        if input_tokens > self.long_threshold and isinstance(row.get('long_context'), dict):
            src = row['long_context']
        inp = src['input']
        return OpenAIRates(inp / 1e6, src['cached_input'] / 1e6, src.get('cache_write', inp) / 1e6, src['output'] / 1e6)

    def is_long_context(self, model: str | None, input_tokens: int) -> bool:
        row = self.row(model)
        return bool(row and input_tokens > self.long_threshold and isinstance(row.get('long_context'), dict))
