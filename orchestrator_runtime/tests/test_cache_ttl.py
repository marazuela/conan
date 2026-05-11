"""Stream 3.5 — mixed-TTL caching test.

Verifies that build_system_blocks emits:
  - block A with ttl="1h" when static_prefix is supplied
  - block B with default (5m) ephemeral cache_control
  - block C (stage_system) without cache_control

Run: python -m pytest orchestrator_runtime/tests/test_cache_ttl.py -v
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from orchestrator_runtime.runtime import (
    build_static_prefix,
    build_system_blocks,
)
from orchestrator_runtime.memory import MemoryBlobs


def test_back_compat_two_blocks_when_no_static_prefix():
    blocks = build_system_blocks("shared content", "stage instructions")
    assert len(blocks) == 2
    assert blocks[0]["text"] == "shared content"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "ttl" not in blocks[0]["cache_control"]
    assert blocks[1]["text"] == "stage instructions"
    assert "cache_control" not in blocks[1]


def test_three_blocks_when_static_prefix_supplied():
    blocks = build_system_blocks(
        "shared", "stage", static_prefix="static memory hierarchy",
    )
    assert len(blocks) == 3
    # Block A — 1h TTL
    assert blocks[0]["text"] == "static memory hierarchy"
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # Block B — default 5m TTL
    assert blocks[1]["text"] == "shared"
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    # Block C — no cache
    assert blocks[2]["text"] == "stage"
    assert "cache_control" not in blocks[2]


def test_static_prefix_returns_none_when_memory_empty():
    ctx = {"memory_blobs": MemoryBlobs()}
    assert build_static_prefix(ctx) is None


def test_static_prefix_concats_memory_blobs():
    ctx = {"memory_blobs": MemoryBlobs(asset="a-content", indication="i-content")}
    out = build_static_prefix(ctx)
    assert out is not None
    assert "a-content" in out
    assert "i-content" in out
    assert "Memory hierarchy (static)" in out


def test_static_prefix_none_when_ctx_missing_blobs():
    assert build_static_prefix({}) is None
