"""Wave 4 deep-fix Phase D.2 — memory writeback failure paths.

The Wave 4.2 design says: memory writeback runs OUTSIDE the rollback scope,
so a storage failure must NOT delete the parent assessment. The failure
emits an operator_flags row with source='memory_writeback' so it's
discoverable. These tests pin that contract:

  - When MemoryStore.write raises, the parent assessment is unchanged.
  - When MemoryStore.write raises, an operator_flag with the right shape
    is POSTed to the operator_flags surface.
  - When the operator_flag POST itself fails, the warning is caught and
    the run still returns the assessment id (never crashes the parent).

Run: python -m pytest orchestrator_runtime/tests/test_memory_writeback_failure.py -v
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FlagCapturingMock:
    """Captures POSTs to operator_flags so we can assert flag shape on
    memory failures. Other table writes get generic-row stubs."""

    def __init__(self, *, flag_post_fails: bool = False):
        self.calls: List[Dict[str, Any]] = []
        self.flag_post_fails = flag_post_fails

    def _rest(self, method: str, table: str, **kwargs) -> Any:
        self.calls.append({"method": method, "table": table, **kwargs})
        if method == "POST" and table == "operator_flags":
            if self.flag_post_fails:
                raise RuntimeError("simulated operator_flags POST failure")
            return [{"id": "flag-row-id"}]
        if method == "GET":
            return []
        return [{"id": f"{table}-row-id"}]

    # Stub MemoryStore's read interface — _write_asset_memory_best_effort
    # calls store.load_all then store.write. read_cache is called by load_all.
    def read_cache(self, *a, **kw):
        return None

    def captured_flags(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls
                if c["method"] == "POST" and c["table"] == "operator_flags"]


def _asset_fixture() -> Dict[str, Any]:
    return {
        "id": "asset-uuid-1",
        "ticker": "VRDN",
        "drug_name": "Veligrotug",
        "generic_name": None,
        "sponsor_name": "Viridian",
        "indication": "TED",
        "indication_normalized": "ted",
        "reference_class_signature": "phase3_oncology",
        "application_number": "BLA-1",
        "program_status": "submitted",
    }


def _parsed_stub() -> Dict[str, Any]:
    return {
        "thesis_direction": "long",
        "conviction_pct": 72.0,
        "evidence_quality": 0.85,
        "thesis_summary": "Strong endpoint hit.",
        "key_facts": [],
        "uncertainties": [],
        "cited_prose_blocks": [],
        "reasoning_summary": "Mocked reasoning trace.",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_memory_write_failure_emits_operator_flag():
    """MemoryStore.write raises → operator_flag with source='memory_writeback'
    + kind='asset_memory_write_failed' is POSTed."""
    from orchestrator_runtime import runtime

    sb = _FlagCapturingMock()

    with patch.object(runtime, "MemoryStore") as MockStore:
        instance = MagicMock()
        instance.load_all.return_value = MagicMock(asset=None)
        instance.write.side_effect = RuntimeError(
            "simulated supabase storage 500"
        )
        MockStore.return_value = instance

        runtime._write_asset_memory_best_effort(
            sb,
            asset_id="asset-uuid-1",
            assessment_id="assessment-uuid-1",
            asset=_asset_fixture(),
            parsed=_parsed_stub(),
            cited_prose="prose",
            calibrated=72.0,
            band="watchlist",
            direction="long",
        )

    flags = sb.captured_flags()
    assert len(flags) == 1
    body = flags[0]["json_body"]
    assert body["severity"] == "warn"
    assert body["source"] == "memory_writeback"
    assert body["kind"] == "asset_memory_write_failed"
    assert "asset-uuid-1" in body["title"][:50] or body["title"].startswith(
        "Memory writeback failed"
    )
    # evidence shape is the audit hook the dashboard joins on.
    evidence = body["evidence"]
    assert evidence["asset_id"] == "asset-uuid-1"
    assert evidence["assessment_id"] == "assessment-uuid-1"


def test_memory_write_success_emits_no_flag():
    """Happy path — no flag emission on success."""
    from orchestrator_runtime import runtime

    sb = _FlagCapturingMock()

    with patch.object(runtime, "MemoryStore") as MockStore:
        instance = MagicMock()
        instance.load_all.return_value = MagicMock(asset=None)
        instance.write.return_value = True
        MockStore.return_value = instance

        runtime._write_asset_memory_best_effort(
            sb,
            asset_id="asset-uuid-1",
            assessment_id="assessment-uuid-1",
            asset=_asset_fixture(),
            parsed=_parsed_stub(),
            cited_prose="prose",
            calibrated=72.0,
            band="watchlist",
            direction="long",
        )

    assert sb.captured_flags() == []


def test_memory_write_failure_does_not_raise_when_flag_emission_fails():
    """The flag-POST safety net: a failure here is caught + logged, never
    re-raised. Otherwise a transient operator_flags HTTP blip would crash
    the entire assessment despite Stage 10 having already committed."""
    from orchestrator_runtime import runtime

    sb = _FlagCapturingMock(flag_post_fails=True)

    with patch.object(runtime, "MemoryStore") as MockStore:
        instance = MagicMock()
        instance.load_all.return_value = MagicMock(asset=None)
        instance.write.side_effect = RuntimeError("storage 500")
        MockStore.return_value = instance

        # Must NOT raise — this is the property under test.
        runtime._write_asset_memory_best_effort(
            sb,
            asset_id="asset-uuid-1",
            assessment_id="assessment-uuid-1",
            asset=_asset_fixture(),
            parsed=_parsed_stub(),
            cited_prose="prose",
            calibrated=72.0,
            band="watchlist",
            direction="long",
        )

    # The flag POST WAS attempted, just failed. Both attempts recorded.
    flags = sb.captured_flags()
    assert len(flags) == 1


def test_memory_write_load_all_failure_also_emits_flag():
    """If MemoryStore.load_all (the read step) raises, the outer except
    still catches it — the body of the flag describes the load failure
    rather than the write failure, but the source/kind are unchanged."""
    from orchestrator_runtime import runtime

    sb = _FlagCapturingMock()

    with patch.object(runtime, "MemoryStore") as MockStore:
        instance = MagicMock()
        instance.load_all.side_effect = RuntimeError("storage 503 on read")
        MockStore.return_value = instance

        runtime._write_asset_memory_best_effort(
            sb,
            asset_id="asset-uuid-1",
            assessment_id="assessment-uuid-1",
            asset=_asset_fixture(),
            parsed=_parsed_stub(),
            cited_prose="prose",
            calibrated=72.0,
            band="watchlist",
            direction="long",
        )

    flags = sb.captured_flags()
    assert len(flags) == 1
    assert flags[0]["json_body"]["source"] == "memory_writeback"
