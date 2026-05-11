"""Tests for orchestrator_runtime.pricing — cache-aware estimate_cost.

Run: python -m pytest orchestrator_runtime/tests/test_pricing.py -v
"""
from __future__ import annotations

import os

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from orchestrator_runtime.pricing import (
    COST_TABLE, estimate_cost, is_known_model,
)


def test_unknown_model_returns_zero():
    assert estimate_cost("not-a-model", 1000, 1000) == 0.0


def test_sonnet_input_only():
    # 1M input tokens at $3/M = $3.00
    assert estimate_cost(
        "claude-sonnet-4-5-20250929",
        input_tokens=1_000_000,
    ) == 3.0


def test_sonnet_output_only():
    # 1M output tokens at $15/M = $15.00
    assert estimate_cost(
        "claude-sonnet-4-5-20250929",
        output_tokens=1_000_000,
    ) == 15.0


def test_sonnet_cache_create_billed():
    # 1M cache_create at $3.75/M = $3.75 — was silently dropped before fix
    assert estimate_cost(
        "claude-sonnet-4-5-20250929",
        cache_creation_tokens=1_000_000,
    ) == 3.75


def test_sonnet_cache_read_billed():
    # 1M cache_read at $0.30/M = $0.30 — 10x cheaper than uncached input
    assert estimate_cost(
        "claude-sonnet-4-5-20250929",
        cache_read_tokens=1_000_000,
    ) == 0.30


def test_sonnet_full_mix():
    # 100k in + 50k out + 200k cache_create + 800k cache_read
    cost = estimate_cost(
        "claude-sonnet-4-5-20250929",
        input_tokens=100_000,
        output_tokens=50_000,
        cache_creation_tokens=200_000,
        cache_read_tokens=800_000,
    )
    expected = (
        100_000 * 3.0
        + 50_000 * 15.0
        + 200_000 * 3.75
        + 800_000 * 0.30
    ) / 1_000_000
    assert abs(cost - expected) < 1e-9


def test_haiku_pricing():
    # 1M each: in $1, out $5, cache_create $1.25, cache_read $0.10
    cost = estimate_cost(
        "claude-haiku-4-5-20251001",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    )
    assert abs(cost - (1.0 + 5.0 + 1.25 + 0.10)) < 1e-9


def test_pricing_table_has_all_models():
    for model in (
        "claude-sonnet-4-5-20250929",
        "claude-haiku-4-5-20251001",
        "claude-opus-4-7-20260101",
    ):
        assert model in COST_TABLE
        assert is_known_model(model)
        rates = COST_TABLE[model]
        assert len(rates) == 4
        # Cache create > input rate (cache write premium)
        assert rates[2] > rates[0]
        # Cache read < input rate (cache read discount)
        assert rates[3] < rates[0]


def test_zero_tokens_zero_cost():
    assert estimate_cost("claude-sonnet-4-5-20250929") == 0.0


def test_backwards_compatible_3_arg_call():
    # Old callers using positional 3-arg estimate_cost still work
    cost = estimate_cost("claude-sonnet-4-5-20250929", 100_000, 50_000)
    expected = (100_000 * 3.0 + 50_000 * 15.0) / 1_000_000
    assert abs(cost - expected) < 1e-9
