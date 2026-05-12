"""Anthropic API pricing — cache-aware.

Centralizes per-model rates so client.py and any other call-site computes
cost the same way. Fixes the prior bug where cache_create and cache_read
tokens were silently dropped.

Pricing convention: rates are USD per 1M tokens.
  - input        = uncached input tokens
  - output       = output tokens (always billed at output rate)
  - cache_create = "ephemeral" cache write at 1.25x input
  - cache_read   = cache read at 0.10x input

These ratios match Anthropic's published pricing for Sonnet 4.5 / Haiku 4.5
as of 2026-05. Verify against the live pricing page when bumping the table.
"""
from __future__ import annotations

from typing import Tuple

# (input, output, cache_create, cache_read) per 1M tokens, USD.
COST_TABLE: dict[str, Tuple[float, float, float, float]] = {
    # Sonnet 4.5 — current orchestrator default
    "claude-sonnet-4-5-20250929": (3.00, 15.00, 3.75, 0.30),
    # Haiku 4.5 — used by asset_linker pass-2 and rag.contextual_augmenter
    "claude-haiku-4-5-20251001": (1.00, 5.00, 1.25, 0.10),
    # Opus 4.7 — placeholder; bump when GA pricing is confirmed
    "claude-opus-4-7-20260101": (15.00, 75.00, 18.75, 1.50),
    # Opus 4.7 alias (no date suffix) — matches DEFAULT_MODEL in client.py
    "claude-opus-4-7": (15.00, 75.00, 18.75, 1.50),
}


def estimate_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Compute USD cost for a single Anthropic call.

    Returns 0.0 for unknown models so callers don't crash on a new model id.
    """
    rates = COST_TABLE.get(model)
    if not rates:
        return 0.0
    in_rate, out_rate, cache_create_rate, cache_read_rate = rates
    return (
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_creation_tokens * cache_create_rate
        + cache_read_tokens * cache_read_rate
    ) / 1_000_000


def is_known_model(model: str) -> bool:
    return model in COST_TABLE
