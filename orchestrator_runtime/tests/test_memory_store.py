"""Tests for orchestrator_runtime.memory.MemoryStore.

Run: python -m pytest orchestrator_runtime/tests/test_memory_store.py -v
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from orchestrator_runtime.memory import (
    MEMORY_BUCKET,
    MemoryBlobs,
    MemoryStore,
    _storage_path,
)


def test_storage_path_format():
    assert _storage_path("asset", "abc-123") == "asset/abc-123.md"
    assert _storage_path("sub_agent", "literature/asset-id") == "sub_agent/literature/asset-id.md"


def test_blobs_as_text_includes_section_headers():
    b = MemoryBlobs(asset="a-content", indication="i-content")
    text = b.as_text()
    assert '<memory scope="asset">' in text
    assert "a-content" in text
    assert '<memory scope="indication">' in text
    assert "i-content" in text
    assert '<memory scope="reviewer_panel">' not in text


def test_blobs_is_empty():
    assert MemoryBlobs().is_empty() is True
    assert MemoryBlobs(asset="x").is_empty() is False


def test_load_all_parallel_reads_four_scopes():
    sb = MagicMock()
    # read_cache returns bytes for each scope's path
    def fake_read(bucket, path):
        assert bucket == MEMORY_BUCKET
        return f"content-for-{path}".encode("utf-8")
    sb.read_cache.side_effect = fake_read
    store = MemoryStore(sb)
    blobs = store.load_all(
        asset_id="asset-1",
        indication="onco",
        reviewer_panel_id="panel-A",
        sub_agent_key="literature/asset-1",
    )
    assert blobs.asset == "content-for-asset/asset-1.md"
    assert blobs.indication == "content-for-indication/onco.md"
    assert blobs.reviewer_panel == "content-for-reviewer_panel/panel-A.md"
    assert blobs.sub_agent == "content-for-sub_agent/literature/asset-1.md"
    # 4 reads regardless of order
    assert sb.read_cache.call_count == 4


def test_load_all_handles_missing_blobs():
    sb = MagicMock()
    sb.read_cache.return_value = None  # all misses
    store = MemoryStore(sb)
    blobs = store.load_all(asset_id="x")
    assert blobs.is_empty() is True


def test_load_all_skips_none_scope_ids():
    sb = MagicMock()
    sb.read_cache.return_value = b"x"
    store = MemoryStore(sb)
    # Only asset_id provided; the other 3 lookups should pass scope_id=None and short-circuit
    blobs = store.load_all(asset_id="asset-1")
    assert blobs.asset == "x"
    # Only 1 read_cache call (the others short-circuit on scope_id=None)
    assert sb.read_cache.call_count == 1


def test_write_persists_to_storage_and_index():
    sb = MagicMock()
    store = MemoryStore(sb)
    store.write("asset", "asset-uuid-1", "## hello world")
    sb.write_cache.assert_called_once()
    args = sb.write_cache.call_args
    assert args.args[0] == MEMORY_BUCKET
    assert args.args[1] == "asset/asset-uuid-1.md"
    # _rest call to memory_files index
    assert sb._rest.called
    rest_args = sb._rest.call_args
    assert rest_args.args[0] == "POST"
    assert rest_args.args[1] == "memory_files"
    assert rest_args.kwargs["json_body"]["scope"] == "asset"
    assert rest_args.kwargs["json_body"]["scope_id"] == "asset-uuid-1"


def test_write_rejects_invalid_scope():
    sb = MagicMock()
    store = MemoryStore(sb)
    with pytest.raises(ValueError, match="invalid scope"):
        store.write("not_a_scope", "x", "content")
    with pytest.raises(ValueError, match="must be non-empty"):
        store.write("asset", "", "content")
