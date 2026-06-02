"""v4 Phase 9b — prompt_registry tests.

Pure unit tests using a stubbed SupabaseClient — no DB round trip. Covers:
  - hash determinism
  - cache hit short-circuit (no DB call on repeat)
  - DB-hit path (existing row, no insert)
  - DB-miss path (insert + supersede prior active)
  - registry failure → returns None, doesn't raise
  - unknown stage → returns None, doesn't raise

Run: python -m pytest orchestrator_runtime/tests/test_prompt_registry.py -v
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "x")

from orchestrator_runtime import prompt_registry
from orchestrator_runtime.prompt_registry import (
    STAGE_1,
    STAGE_9,
    clear_cache,
    get_cached,
    hash_prompt,
    register_prompt,
)


class _StubSupabase:
    """Minimal SupabaseClient stand-in. Records every call as
    (method, path, params, json_body, prefer). The .responses queue is
    drained in order — each pop returns the next call's result.
    Test setup pushes the expected response shape for each anticipated
    call; if .responses runs dry the test fails loudly."""

    def __init__(self, responses: Optional[List[Any]] = None) -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any], Any, Optional[str]]] = []
        self.responses: List[Any] = list(responses or [])

    def _rest(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        prefer: Optional[str] = None,
    ) -> Any:
        self.calls.append((method, path, params or {}, json_body, prefer))
        if not self.responses:
            raise AssertionError(
                f"_StubSupabase: ran out of queued responses; "
                f"unexpected call {method} {path}"
            )
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_cache()
    yield
    clear_cache()


def test_hash_prompt_deterministic():
    assert hash_prompt("hello") == hash_prompt("hello")
    assert hash_prompt("hello") != hash_prompt("world")
    # SHA-256 hex = 64 chars
    assert len(hash_prompt("anything")) == 64


def test_cache_hit_skips_db():
    """Second call for the same (stage, hash) must not round-trip."""
    sb = _StubSupabase(responses=[
        [{"id": "abc-123"}],  # initial SELECT hit
    ])
    uid1 = register_prompt(sb, STAGE_1, "first prompt text")
    assert uid1 == "abc-123"
    assert len(sb.calls) == 1

    # Second call: cache hit, no DB.
    uid2 = register_prompt(sb, STAGE_1, "first prompt text")
    assert uid2 == "abc-123"
    assert len(sb.calls) == 1  # unchanged


def test_db_miss_inserts_and_supersedes():
    """Missing row → INSERT then supersede prior active."""
    sb = _StubSupabase(responses=[
        [],                                        # SELECT: not found
        [{"id": "new-uuid-456"}],                  # INSERT returns row
        None,                                      # PATCH supersede prior
    ])
    uid = register_prompt(sb, STAGE_1, "brand new prompt")
    assert uid == "new-uuid-456"
    assert len(sb.calls) == 3

    # Verify call ordering + key invariants.
    method, path, params, _, _ = sb.calls[0]
    assert method == "GET"
    assert path == "prompt_versions"
    assert params["stage"] == f"eq.{STAGE_1}"

    method, path, _, body, prefer = sb.calls[1]
    assert method == "POST"
    assert path == "prompt_versions"
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["stage"] == STAGE_1
    assert body[0]["prompt_text"] == "brand new prompt"
    assert body[0]["is_active"] is True
    assert "merge-duplicates" in (prefer or "")

    method, path, params, body, _ = sb.calls[2]
    assert method == "PATCH"
    assert path == "prompt_versions"
    assert params["stage"] == f"eq.{STAGE_1}"
    assert params["is_active"] == "eq.true"
    assert params["id"] == "neq.new-uuid-456"
    assert body["is_active"] is False
    assert body["superseded_by"] == "new-uuid-456"


def test_db_hit_path_no_insert():
    """Existing row → return its id, no INSERT."""
    sb = _StubSupabase(responses=[
        [{"id": "existing-789"}],
    ])
    uid = register_prompt(sb, STAGE_9, "already-registered prompt")
    assert uid == "existing-789"
    assert len(sb.calls) == 1
    method, _, _, _, _ = sb.calls[0]
    assert method == "GET"


def test_registry_failure_returns_none_does_not_raise():
    """Supabase raising mid-call must surface as None, not propagate."""
    sb = _StubSupabase(responses=[
        RuntimeError("supabase down"),
    ])
    uid = register_prompt(sb, STAGE_1, "any prompt")
    assert uid is None


def test_unknown_stage_returns_none():
    """Defensive: stage label not in VALID_STAGES must not blow up."""
    sb = _StubSupabase(responses=[])
    uid = register_prompt(sb, "stage_zzz", "any text")
    assert uid is None
    assert sb.calls == []  # no DB round trip


def test_get_cached_probe_no_db():
    """get_cached must never touch the DB."""
    # Seed the cache via a successful register_prompt.
    sb = _StubSupabase(responses=[[{"id": "probe-1"}]])
    register_prompt(sb, STAGE_1, "cached text")
    assert get_cached(STAGE_1, "cached text") == "probe-1"
    assert get_cached(STAGE_1, "different text") is None
    assert get_cached(STAGE_9, "cached text") is None


def test_supersede_failure_does_not_unwind_insert():
    """PATCH supersede raises — the new row still persists + UUID returned."""
    sb = _StubSupabase(responses=[
        [],                                # SELECT: miss
        [{"id": "new-row-uuid"}],          # INSERT succeeds
        RuntimeError("PATCH timeout"),     # supersede fails
    ])
    uid = register_prompt(sb, STAGE_1, "racy prompt")
    assert uid == "new-row-uuid"
    # Cache populated even though supersede failed.
    assert get_cached(STAGE_1, "racy prompt") == "new-row-uuid"


def test_empty_insert_payload_falls_back_to_select():
    """Some UPSERT paths return empty body; registry must re-SELECT."""
    sb = _StubSupabase(responses=[
        [],                                  # SELECT: miss
        None,                                # INSERT returns nothing
        [{"id": "recovered-uuid"}],          # follow-up SELECT
        None,                                # PATCH supersede
    ])
    uid = register_prompt(sb, STAGE_9, "tricky prompt")
    assert uid == "recovered-uuid"
    assert len(sb.calls) == 4


def test_distinct_stages_dont_collide_in_cache():
    """Same prompt text under different stages → different cache entries."""
    sb = _StubSupabase(responses=[
        [{"id": "s1-uuid"}],
        [{"id": "s9-uuid"}],
    ])
    u1 = register_prompt(sb, STAGE_1, "shared text")
    u9 = register_prompt(sb, STAGE_9, "shared text")
    assert u1 == "s1-uuid"
    assert u9 == "s9-uuid"
    assert u1 != u9
    assert len(sb.calls) == 2  # both round-tripped


def test_prompt_text_changes_short_label():
    """version label embeds the first 8 hex of the hash."""
    sb = _StubSupabase(responses=[
        [],
        [{"id": "u"}],
        None,
    ])
    register_prompt(sb, STAGE_1, "label-check")
    _, _, _, body, _ = sb.calls[1]
    payload = body[0]
    expected_prefix = f"v4-{hash_prompt('label-check')[:8]}"
    assert payload["version"] == expected_prefix
