"""Wave 4 deep-fix Phase B — atomic persist via persist_assessment_v3 RPC.

These tests pin the Python ↔ RPC contract: payload shape, idempotency key
plumbing, supersedence chain stamp expectations, return-value unwrap
defensiveness. The Postgres RPC itself is exercised by Supabase's
plpgsql + an end-to-end smoke (planned for Phase A.1 deploy); here we lock
the call surface so runtime.py can't quietly drift from the migration's
expected jsonb shape.

Run: python -m pytest orchestrator_runtime/tests/test_persist_atomicity.py -v
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
# Mock supabase client — captures RPC POST shape + simulates return modes
# ---------------------------------------------------------------------------


class _RpcCapturingMock:
    """Records every _rest call. The RPC POST gets its payload captured for
    inspection; other tables get generic-row stub responses. The RPC return
    mode is configurable per test (scalar string vs list-wrap vs dict-wrap)
    so we can prove runtime.py handles all three PostgREST shapes."""

    def __init__(self, *, rpc_return: Any = "rpc-uuid-test",
                 catalyst_events: Optional[List[Dict[str, Any]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self.rpc_return = rpc_return
        self.catalyst_events = catalyst_events or []

    def _rest(self, method: str, table: str, **kwargs) -> Any:
        self.calls.append({"method": method, "table": table, **kwargs})
        if method == "POST" and table == "rpc/persist_assessment_v3":
            return self.rpc_return
        if method == "GET" and table == "fda_regulatory_events":
            return self.catalyst_events
        if method == "POST":
            return [{"id": f"{table}-row-id"}]
        return []

    # The runtime's `_write_asset_memory_best_effort` uses MemoryStore which
    # calls write_cache / read_cache — stub them so memory writeback doesn't
    # crash the test (memory failure is logged + a flag is emitted, but the
    # parent assessment must still complete).
    def write_cache(self, *args, **kwargs):
        return True

    def read_cache(self, *args, **kwargs):
        return None

    def captured_rpc_payload(self) -> Dict[str, Any]:
        """Return the captured RPC body's inner payload dict (or {})."""
        for c in self.calls:
            if c["table"] == "rpc/persist_assessment_v3":
                return (c.get("json_body") or {}).get("payload") or {}
        return {}


# ---------------------------------------------------------------------------
# Fixtures — minimum-viable run state to drive _build_stage_10_secondaries
# + stage_10_persist end-to-end
# ---------------------------------------------------------------------------


def _baseline_ctx() -> Dict[str, Any]:
    return {
        "asset": {
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
        },
        "facts": [
            {"id": "aa11bb22cc33dd44", "document_id": "dd44ee55ff66aabb",
             "fact_type": "trial_result", "fact_text": "Endpoint met",
             "evidence_quote": "p<0.001", "confidence": 0.95},
        ],
        "documents": [{"id": "dd44ee55ff66aabb"}],
        "memory_text": None,
        "reference_class_anchor": None,
        "asset_doc_links": [],
        "prior_assessments": [],
    }


def _parsed_stub() -> Dict[str, Any]:
    return {
        "thesis_direction": "long",
        "conviction_pct": 72.0,
        "evidence_quality": 0.85,
        "thesis_summary": "Veligrotug Phase 3 PASS on primary endpoint.",
        "key_facts": [
            {"text": "Endpoint met (p<0.001)", "fact_id_short": "aa11bb22"},
        ],
        "uncertainties": [
            {"question": "AdComm path?", "why_matters": "label",
             "how_to_resolve": "TBD"},
        ],
        "cited_prose_blocks": [
            {"section": "Conclusion", "text": "Long thesis",
             "fact_citations": ["aa11bb22"], "doc_citations": ["dd44ee55"]},
        ],
    }


def _build_run(run_id: Optional[str] = None):
    from orchestrator_runtime.runtime import AssessmentRun
    return AssessmentRun(
        asset_id="asset-uuid-1",
        trigger_type="scheduled",
        orchestrator_run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_build_payload_shape_includes_all_required_keys():
    """The pure builder emits the four arrays + stub the RPC contract requires."""
    from orchestrator_runtime.runtime import _build_stage_10_secondaries

    run = _build_run()
    run.stage_metrics = []  # empty is allowed
    ctx = _baseline_ctx()
    parsed = _parsed_stub()

    payload = _build_stage_10_secondaries(run, ctx, parsed)

    assert set(payload.keys()) == {
        "stage_metrics", "hypotheses", "premortem_verdicts", "post_mortem_stub"
    }
    assert isinstance(payload["stage_metrics"], list)
    assert isinstance(payload["hypotheses"], list)
    assert isinstance(payload["premortem_verdicts"], list)
    assert isinstance(payload["post_mortem_stub"], dict)
    # Empty-hypothesis path produces empty arrays, NOT None.
    assert payload["hypotheses"] == []
    assert payload["premortem_verdicts"] == []
    # post_mortem_stub has direction-derived predicted_outcome.
    assert payload["post_mortem_stub"]["predicted_outcome"] == "approved"
    assert payload["post_mortem_stub"]["asset_id"] == "asset-uuid-1"
    # Catalyst fields filled in by the caller (not the pure builder).
    assert "outcome_window_end" not in payload["post_mortem_stub"]
    assert "catalyst_resolution_marker" not in payload["post_mortem_stub"]


def test_resolve_catalyst_window_picks_marker_for_pending_pdufa():
    """When a pending PDUFA exists for the asset, marker = 'pdufa:<id>'."""
    from orchestrator_runtime.runtime import _resolve_catalyst_window

    sb = _RpcCapturingMock(catalyst_events=[
        {"id": "evt-1", "event_date": "2026-09-15", "event_type": "pdufa",
         "event_status": "pending"},
    ])
    window_end, marker = _resolve_catalyst_window(sb, "asset-uuid-1")
    assert marker == "pdufa:evt-1"
    # 2026-09-15 + 2 days window padding
    assert window_end.isoformat().startswith("2026-09-17")


def test_resolve_catalyst_window_picks_marker_for_advisory_committee():
    """Phase C.1 — AdComm events ARE eligible (was PDUFA-only before)."""
    from orchestrator_runtime.runtime import _resolve_catalyst_window

    sb = _RpcCapturingMock(catalyst_events=[
        {"id": "evt-2", "event_date": "2026-07-01", "event_type": "advisory_committee",
         "event_status": "pending"},
    ])
    _, marker = _resolve_catalyst_window(sb, "asset-uuid-1")
    assert marker == "advisory_committee:evt-2"


def test_resolve_catalyst_window_no_event_falls_back_to_default():
    """Phase C — fallback marker carries the configured window-days in its name."""
    from orchestrator_runtime.runtime import (
        _resolve_catalyst_window, DEFAULT_POST_MORTEM_WINDOW_DAYS,
    )

    sb = _RpcCapturingMock(catalyst_events=[])
    _, marker = _resolve_catalyst_window(sb, "asset-uuid-1")
    assert marker == f"default_{DEFAULT_POST_MORTEM_WINDOW_DAYS}d_fallback"


def test_resolve_catalyst_window_queries_expanded_event_types():
    """Phase C.1 — the GET request includes all four eligible event types."""
    from orchestrator_runtime.runtime import (
        _resolve_catalyst_window, CATALYST_EVENT_TYPES,
    )

    sb = _RpcCapturingMock(catalyst_events=[])
    _resolve_catalyst_window(sb, "asset-uuid-1")
    # Find the GET call to fda_regulatory_events
    get_calls = [c for c in sb.calls
                 if c["method"] == "GET" and c["table"] == "fda_regulatory_events"]
    assert len(get_calls) == 1
    params = get_calls[0]["params"]
    # event_type filter is `in.(pdufa,advisory_committee,eop2,readout)`
    type_filter = params["event_type"]
    assert type_filter.startswith("in.(")
    for et in CATALYST_EVENT_TYPES:
        assert et in type_filter
    # event_status is in.(pending,resolved) — both, not just pending
    assert params["event_status"] == "in.(pending,resolved)"


def test_rpc_return_unwrapped_for_scalar():
    """PostgREST returns scalar uuid for single-value functions — runtime
    unwraps it without choking."""
    sb = _RpcCapturingMock(rpc_return="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    aid = _drive_stage_10(sb)
    assert aid == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_rpc_return_unwrapped_for_list_wrap():
    """Some PostgREST configs wrap the scalar in a one-element list."""
    sb = _RpcCapturingMock(
        rpc_return=["bbbbbbbb-cccc-dddd-eeee-ffffffffffff"]
    )
    aid = _drive_stage_10(sb)
    assert aid == "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


def test_rpc_return_unwrapped_for_named_column():
    """When configured to return a record, PostgREST emits {colname: value}."""
    sb = _RpcCapturingMock(
        rpc_return={"persist_assessment_v3":
                    "cccccccc-dddd-eeee-ffff-000000000000"}
    )
    aid = _drive_stage_10(sb)
    assert aid == "cccccccc-dddd-eeee-ffff-000000000000"


def test_rpc_call_carries_orchestrator_run_id_for_idempotency():
    """run.orchestrator_run_id MUST land on payload.orchestrator_run_id so
    the RPC can short-circuit on retry. Without this, a retried run inserts
    a duplicate assessment (the failure mode the column was added to close)."""
    sb = _RpcCapturingMock(rpc_return="aid-test")
    run_id = "orch-run-7777"
    aid = _drive_stage_10(sb, run_id=run_id)
    assert aid == "aid-test"
    captured = sb.captured_rpc_payload()
    assert captured["orchestrator_run_id"] == run_id


def test_rpc_call_carries_assessment_row_with_expected_keys():
    """The convergence_assessments row dict lives under payload.assessment.
    Spot-check the keys we know runtime builds — schema drift here means the
    RPC's jsonb_populate_record will silently drop the field."""
    sb = _RpcCapturingMock(rpc_return="aid-test")
    _drive_stage_10(sb)
    captured = sb.captured_rpc_payload()
    assessment = captured["assessment"]
    # Required columns that gate downstream behavior — if any are missing,
    # the assessment row will be persisted with a default + we have a bug.
    for k in (
        "asset_id", "trigger_type", "model_id", "thesis_direction",
        "conviction_pct", "raw_conviction_pct", "conviction_pct_calibrated",
        "band", "ensemble_n", "shrinkage_factor",
    ):
        assert k in assessment, f"assessment row missing key {k!r}"


def test_rpc_call_supplies_not_null_columns_without_natural_defaults():
    """Regression for the 23502 NOT NULL bug on convergence_assessments INSERT
    (orchestrator_runs 2026-05-25..2026-06-01 burn).

    The RPC's `INSERT ... SELECT (jsonb_populate_record(NULL::table, jsonb)).*`
    pattern strips column DEFAULTs: any NOT NULL DEFAULT column omitted from
    the assessment jsonb becomes an explicit NULL in the INSERT and 23502s.
    `id` and `created_at` are now defaulted server-side via defensive
    `jsonb_build_object || v_assessment` in the SQL function, but `tier` and
    `constitutional_retries` are domain-owned constants — runtime.py supplies
    them explicitly. This test pins that contract so a future row-dict
    refactor can't silently drop them.

    If this test fails, every Tier-1 v3/v4 run will 23502 in production.
    """
    sb = _RpcCapturingMock(rpc_return="aid-test")
    _drive_stage_10(sb)
    captured = sb.captured_rpc_payload()
    assessment = captured["assessment"]

    assert "tier" in assessment, (
        "row dict must supply `tier` — schema is NOT NULL DEFAULT 1 but the "
        "RPC's jsonb_populate_record pattern bypasses the DEFAULT"
    )
    assert assessment["tier"] == 1, (
        f"Tier-1 path must persist tier=1, got {assessment['tier']!r}"
    )
    assert "constitutional_retries" in assessment, (
        "row dict must supply `constitutional_retries` — schema is NOT NULL "
        "DEFAULT 0 but the RPC's jsonb_populate_record pattern bypasses the "
        "DEFAULT"
    )
    assert assessment["constitutional_retries"] == 0


def test_rpc_call_carries_post_mortem_stub_with_catalyst_fields():
    """post_mortem_stub MUST include outcome_window_end + catalyst_resolution_marker
    by the time it reaches the RPC. The pure builder leaves them out; the
    caller (stage_10_persist) fills them via _resolve_catalyst_window."""
    sb = _RpcCapturingMock(rpc_return="aid-test", catalyst_events=[])
    _drive_stage_10(sb)
    captured = sb.captured_rpc_payload()
    stub = captured["post_mortem_stub"]
    assert "outcome_window_end" in stub
    assert "catalyst_resolution_marker" in stub


def test_rpc_call_carries_stage_metrics_with_no_assessment_id():
    """Each stage_metric in the payload should NOT carry assessment_id —
    the RPC stamps it from the new parent. If the caller pre-stamps a
    placeholder, the RPC's jsonb_build_object overwrites it but the test
    catches accidental leakage of None/empty strings."""
    sb = _RpcCapturingMock(rpc_return="aid-test")
    _drive_stage_10(sb, with_stage_metric=True)
    captured = sb.captured_rpc_payload()
    metrics = captured["stage_metrics"]
    assert len(metrics) >= 1
    for m in metrics:
        assert "assessment_id" not in m


def test_no_legacy_post_to_convergence_assessments():
    """Wave 4 deep-fix Phase B — the direct POST/DELETE dance is GONE.
    Stage 10 must hit the RPC and nothing else (besides the catalyst GET
    + the memory writeback path which lives outside the rollback scope)."""
    sb = _RpcCapturingMock(rpc_return="aid-test")
    _drive_stage_10(sb)
    direct_posts = [c for c in sb.calls
                    if c["method"] == "POST" and c["table"] == "convergence_assessments"]
    direct_deletes = [c for c in sb.calls
                      if c["method"] == "DELETE"
                      and c["table"] == "convergence_assessments"]
    assert direct_posts == []
    assert direct_deletes == []


# ---------------------------------------------------------------------------
# Helper: drive stage_10_persist end-to-end with a mocked sb client
# ---------------------------------------------------------------------------


def _drive_stage_10(sb: _RpcCapturingMock, *,
                    run_id: Optional[str] = None,
                    with_stage_metric: bool = False) -> Optional[str]:
    from orchestrator_runtime import runtime
    from orchestrator_runtime.runtime import StageMetric, stage_10_persist

    run = _build_run(run_id=run_id)
    if with_stage_metric:
        run.stage_metrics = [StageMetric(
            stage_name="stage_1_synthesis",
            model="claude-sonnet-4-5-20250929",
            input_tokens=100, output_tokens=50, cost_usd=0.001,
        )]

    ctx = _baseline_ctx()
    parsed = _parsed_stub()

    # Stage 8 calibration + market-side gate + signature lookups all hit the
    # SupabaseClient — stub them so the test drives stage_10_persist without
    # needing live infra. These are not what the persist-shape tests are
    # asserting against.
    with patch.object(runtime, "get_active_calibration_curve", return_value=None), \
         patch.object(runtime, "compute_market_side_context",
                      return_value=({}, "watchlist", None)), \
         patch.object(runtime, "_find_existing_convergence_signature",
                      return_value=None), \
         patch.object(runtime, "compute_document_set_hash",
                      return_value="hash-test"), \
         patch.object(runtime, "_resolve_catalyst_window",
                      return_value=(
                          __import__("datetime").datetime(
                              2026, 9, 17, tzinfo=__import__("datetime").timezone.utc
                          ),
                          "default_180d_fallback",
                      )), \
         patch.object(runtime, "_supersede_prior_ic_memo_best_effort"), \
         patch.object(runtime, "_maybe_trigger_ic_memo_best_effort"), \
         patch.object(runtime.MemoryStore, "load_all", return_value=[]), \
         patch.object(runtime.MemoryStore, "write", return_value=None):
        return stage_10_persist(
            sb, "asset-uuid-1", run,
            cited_prose="Long thesis prose",
            parsed=parsed,
            ctx=ctx,
            model="claude-sonnet-4-5-20250929",
            extractor_model="claude-sonnet-4-5-20250929",
            constitutional_result=None,
        )
