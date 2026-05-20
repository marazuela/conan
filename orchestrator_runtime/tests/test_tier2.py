"""Tests for Phase 4B Tier-2 (Cowork bulk) runtime harness.

Locks the deterministic glue between the bulk_orchestrator skill and the
existing Tier-1 infrastructure: input blob assembly, output validation,
DB persistence, and the escalation rule (high conviction / direction
change / new primary doc per bulk_orchestrator.md §Escalation rule).

Run: python3 -m pytest orchestrator_runtime/tests/test_tier2.py -v
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import pytest

from modal_workers.shared.supabase_client import SupabaseClient
from orchestrator_runtime import tier2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_payload(**overrides: Any) -> Dict[str, Any]:
    """Minimal well-formed Tier-2 emit. Override fields per test."""
    base: Dict[str, Any] = {
        "schema_version": "convergence_assessment_v1",
        "asset_id": "asset-uuid-1",
        "tier": 2,
        "orchestrator_version": tier2.TIER2_ORCHESTRATOR_VERSION,
        "thesis_direction": "long",
        "raw_conviction_pct": 55.0,
        "conviction_pct_calibrated": 52.0,
        "conviction_pct": 52.0,
        "band": "watchlist",
        "hypotheses": [
            {"label": "bull", "kill_conditions": ["k1", "k2"]},
            {"label": "base", "kill_conditions": ["k3", "k4"]},
            {"label": "bear", "kill_conditions": ["k5", "k6"]},
        ],
        "cited_prose_blocks": [{"text": "Approval likely.", "citations": []}],
        "key_facts": [{"text": "PDUFA 2026-09-15"}],
        "uncertainties": [{"question": "AdComm convened?"}],
        "citations": [],
        "reference_class": "phase3_psych_NDA",
        "reference_class_base_rate": 0.62,
        "similar_resolved_case_ids": [],
        "evidence_quality": 0.7,
    }
    base.update(overrides)
    return base


def _stub_client(rest_handler):
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"
    sb._rest = rest_handler.__get__(sb, SupabaseClient)  # type: ignore[attr-defined]
    return sb


# ---------------------------------------------------------------------------
# build_tier2_input_blob
# ---------------------------------------------------------------------------

def test_build_tier2_input_blob_assembles_all_fields(monkeypatch):
    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path, "params": params})
        if method == "GET" and path == "fda_assets":
            return [{
                "id": "asset-uuid-1", "ticker": "AXSM", "drug_name": "AXS-05",
                "indication": "MDD", "indication_normalized": "mdd",
                "reference_class_signature": "phase3_psych_NDA",
                "watch_priority": 1,
            }]
        if method == "GET" and path == "extracted_facts":
            return [{"id": "f1"}, {"id": "f2"}]
        if method == "GET" and path == "asset_documents":
            return [{"document_id": "d1", "link_type": "primary", "is_material": True}]
        if method == "GET" and path == "convergence_assessments":
            return [{
                "id": "prev-1", "tier": 1, "thesis_direction": "long",
                "conviction_pct": 48.0, "document_ids": ["d0"],
            }]
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    blob = tier2.build_tier2_input_blob(sb, "asset-uuid-1")

    assert blob.asset_id == "asset-uuid-1"
    assert blob.ticker == "AXSM"
    assert blob.drug_name == "AXS-05"
    # indication_normalized preferred when present
    assert blob.indication == "mdd"
    assert blob.reference_class_signature == "phase3_psych_NDA"
    assert blob.evidence_packet["ok"] is True
    assert blob.evidence_packet["counts"]["material_primary_documents"] == 1
    assert len(blob.extracted_facts) == 2
    assert len(blob.asset_documents) == 1
    assert blob.prior_assessment is not None
    assert blob.prior_assessment["id"] == "prev-1"

    # Tier-2 limits actually go down to PostgREST
    fact_call = next(c for c in captured if c["path"] == "extracted_facts")
    assert fact_call["params"]["limit"] == str(tier2.TIER2_MAX_FACTS)
    doc_call = next(c for c in captured if c["path"] == "asset_documents")
    assert doc_call["params"]["limit"] == str(tier2.TIER2_MAX_DOCS)


def test_build_tier2_input_blob_raises_on_unknown_asset(monkeypatch):
    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    with pytest.raises(ValueError, match="not found"):
        tier2.build_tier2_input_blob(sb, "nonexistent")


def test_build_tier2_input_blob_handles_no_prior(monkeypatch):
    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "fda_assets":
            return [{"id": "a", "ticker": "T", "drug_name": "D",
                     "indication": "i"}]
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    blob = tier2.build_tier2_input_blob(sb, "a")
    assert blob.prior_assessment is None
    assert blob.evidence_packet["ok"] is False
    assert "missing_material_primary_document" in blob.evidence_packet["errors"]


def test_build_tier2_input_blob_can_require_evidence_packet(monkeypatch):
    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "fda_assets":
            return [{"id": "a", "ticker": "T", "drug_name": "D",
                     "indication": "i"}]
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    with pytest.raises(ValueError, match="evidence packet incomplete"):
        tier2.build_tier2_input_blob(sb, "a", require_evidence_packet=True)


# ---------------------------------------------------------------------------
# validate_tier2_output
# ---------------------------------------------------------------------------

def test_validate_tier2_output_accepts_minimal_valid_payload():
    assert tier2.validate_tier2_output(_valid_payload()) == []


def test_validate_tier2_output_rejects_missing_required_field():
    payload = _valid_payload()
    del payload["thesis_direction"]
    errors = tier2.validate_tier2_output(payload)
    assert any("thesis_direction" in e for e in errors)


def test_validate_tier2_output_rejects_missing_hypotheses_field():
    payload = _valid_payload()
    del payload["hypotheses"]
    errors = tier2.validate_tier2_output(payload)
    assert any("hypotheses" in e for e in errors)


def test_validate_tier2_output_rejects_wrong_tier():
    errors = tier2.validate_tier2_output(_valid_payload(tier=1))
    assert any("tier must be 2" in e for e in errors)


def test_validate_tier2_output_rejects_bad_orchestrator_version():
    errors = tier2.validate_tier2_output(
        _valid_payload(orchestrator_version="orch-v0.4.0-mvp"),
    )
    assert any("orchestrator_version" in e for e in errors)


def test_validate_tier2_output_rejects_invalid_thesis_direction():
    errors = tier2.validate_tier2_output(
        _valid_payload(thesis_direction="moonshot"),
    )
    assert any("thesis_direction" in e for e in errors)


def test_validate_tier2_output_rejects_out_of_range_conviction():
    errors = tier2.validate_tier2_output(_valid_payload(conviction_pct=150))
    assert any("conviction_pct" in e and "[0, 100]" in e for e in errors)


def test_validate_tier2_output_rejects_out_of_range_evidence_quality():
    errors = tier2.validate_tier2_output(_valid_payload(evidence_quality=2.5))
    assert any("evidence_quality" in e for e in errors)


def test_validate_tier2_output_rejects_hypothesis_with_too_few_kill_conditions():
    bad_hyps = [
        {"label": "bull", "kill_conditions": ["only_one"]},
    ]
    errors = tier2.validate_tier2_output(_valid_payload(hypotheses=bad_hyps))
    assert any("kill_conditions" in e for e in errors)


def test_validate_tier2_output_accepts_keyed_hypotheses_object():
    """Production Cowork drift emitted {bull,base,bear}; canonicalize it."""
    keyed = {
        "bull": {"claim": "approval", "kill_conditions": ["k1", "k2"]},
        "base": {"claim": "delay", "kill_conditions": ["k3", "k4"]},
        "bear": {"claim": "crl", "kill_conditions": ["k5", "k6"]},
    }
    assert tier2.validate_tier2_output(_valid_payload(hypotheses=keyed)) == []

    normalized = tier2.normalize_tier2_payload(
        _valid_payload(hypotheses=keyed),
    )
    assert [h["label"] for h in normalized["hypotheses"]] == [
        "bull", "base", "bear",
    ]


def test_validate_tier2_output_accepts_arbitrary_keyed_hypotheses_dict():
    """Cowork drift: dict-of-dicts with non-bull/base/bear labels."""
    keyed = {
        "primary": {"claim": "approval", "kill_conditions": ["k1", "k2"]},
        "alt": {"claim": "delay", "kill_conditions": ["k3", "k4"]},
    }
    assert tier2.validate_tier2_output(_valid_payload(hypotheses=keyed)) == []
    normalized = tier2.normalize_tier2_payload(
        _valid_payload(hypotheses=keyed),
    )
    labels = sorted(h["label"] for h in normalized["hypotheses"])
    assert labels == ["alt", "primary"]


def test_validate_tier2_output_accepts_json_encoded_hypotheses_string():
    """Cowork drift: hypotheses serialized as a JSON string."""
    raw_list = [
        {"label": "bull", "claim": "approval",
         "kill_conditions": ["k1", "k2"]},
        {"label": "bear", "claim": "crl",
         "kill_conditions": ["k3", "k4"]},
    ]
    payload = _valid_payload(hypotheses=json.dumps(raw_list))
    assert tier2.validate_tier2_output(payload) == []
    normalized = tier2.normalize_tier2_payload(payload)
    assert isinstance(normalized["hypotheses"], list)
    assert len(normalized["hypotheses"]) == 2


def test_validate_tier2_output_rejects_scalar_list_fields():
    errors = tier2.validate_tier2_output(
        _valid_payload(citations="not-a-list"),
    )

    assert any("citations must be a list" in e for e in errors)


def test_validate_tier2_output_rejects_tier1_only_fields_set():
    # Tier-1's ensemble dispersion in a Tier-2 payload = leak
    errors = tier2.validate_tier2_output(_valid_payload(ensemble_dispersion=4.5))
    assert any("ensemble_dispersion" in e for e in errors)


def test_validate_tier2_output_allows_explicit_null_for_tier1_fields():
    payload = _valid_payload(
        ensemble_dispersion=None, pre_mortem=None, options_iv=None,
    )
    assert tier2.validate_tier2_output(payload) == []


def test_validate_tier2_output_rejects_non_dict():
    errors = tier2.validate_tier2_output("not a dict")  # type: ignore[arg-type]
    assert errors
    assert "dict" in errors[0]


# ---------------------------------------------------------------------------
# persist_tier2_assessment
# ---------------------------------------------------------------------------

def test_persist_tier2_assessment_writes_tier_and_supersedes_prior(monkeypatch):
    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path, "params": params,
                         "json_body": json_body})
        if method == "POST" and path == "convergence_assessments":
            return [{"id": "new-assessment-1"}]
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    new_id = tier2.persist_tier2_assessment(
        sb, "asset-uuid-1", _valid_payload(),
        trigger_type="scheduled",
        cost_usd=0.42, latency_ms=45000,
    )
    assert new_id == "new-assessment-1"

    posts = [c for c in captured if c["method"] == "POST"]
    assert len(posts) == 1
    body = posts[0]["json_body"]
    assert body["tier"] == 2
    assert body["orchestrator_version"] == tier2.TIER2_ORCHESTRATOR_VERSION
    assert body["model_id"] == tier2.TIER2_MODEL_ID
    assert body["asset_id"] == "asset-uuid-1"
    assert body["trigger_type"] == "scheduled"
    assert body["trigger_doc_id"] is None
    assert body["document_window_start"]
    assert body["document_window_end"]
    assert body["document_ids"] == []
    assert body["fact_ids"] == []
    assert body["evidence_ledger"] == {}
    assert body["conviction_pct"] == 52.0
    assert body["cost_usd"] == 0.42
    assert body["latency_ms"] == 45000

    # Supersession PATCH excludes the just-inserted row
    patches = [c for c in captured if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["params"]["asset_id"] == "eq.asset-uuid-1"
    assert patches[0]["params"]["superseded_at"] == "is.null"
    assert patches[0]["params"]["id"] == "neq.new-assessment-1"
    assert patches[0]["json_body"]["superseded_by"] == "new-assessment-1"


def test_persist_tier2_assessment_validates_before_writing(monkeypatch):
    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path})
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    bad_payload = _valid_payload(tier=1)
    with pytest.raises(ValueError, match="failed validation"):
        tier2.persist_tier2_assessment(sb, "asset-uuid-1", bad_payload)
    # No DB writes happened
    assert all(c["method"] not in ("POST", "PATCH") for c in captured)


def test_persist_tier2_assessment_honors_payload_window_and_keyed_hypotheses(monkeypatch):
    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path, "json_body": json_body})
        if method == "POST" and path == "convergence_assessments":
            return [{"id": "new-assessment-1"}]
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    tier2.persist_tier2_assessment(
        sb,
        "asset-uuid-1",
        _valid_payload(
            hypotheses={
                "bull": {"claim": "approval", "kill_conditions": ["k1", "k2"]},
                "base": {"claim": "delay", "kill_conditions": ["k3", "k4"]},
                "bear": {"claim": "crl", "kill_conditions": ["k5", "k6"]},
            },
            document_ids=["11111111-1111-1111-1111-111111111111"],
            document_window_start="2026-01-01T00:00:00+00:00",
            document_window_end="2026-05-01T00:00:00+00:00",
            thesis_summary="Tier-2 summary",
            reasoning_trace="Tier-2 reasoning",
        ),
        trigger_type="new_doc",
        trigger_doc_id="11111111-1111-1111-1111-111111111111",
    )

    body = next(c for c in captured
                if c["method"] == "POST")["json_body"]
    assert body["trigger_type"] == "new_doc"
    assert body["trigger_doc_id"] == "11111111-1111-1111-1111-111111111111"
    assert body["document_window_start"] == "2026-01-01T00:00:00+00:00"
    assert body["document_window_end"] == "2026-05-01T00:00:00+00:00"
    assert body["document_ids"] == ["11111111-1111-1111-1111-111111111111"]
    assert [h["label"] for h in body["hypotheses"]] == [
        "bull", "base", "bear",
    ]
    assert body["thesis_summary"] == "Tier-2 summary"
    assert body["reasoning_trace"] == "Tier-2 reasoning"


# ---------------------------------------------------------------------------
# check_tier1_escalation
# ---------------------------------------------------------------------------

def test_check_tier1_escalation_high_conviction():
    decision = tier2.check_tier1_escalation(
        prior=None,
        current=_valid_payload(conviction_pct=72.0),
    )
    assert decision.escalate
    assert any("high_conviction" in r for r in decision.reasons)


def test_check_tier1_escalation_below_threshold_no_escalate():
    decision = tier2.check_tier1_escalation(
        prior={"thesis_direction": "long"},
        current=_valid_payload(conviction_pct=45.0, thesis_direction="long"),
    )
    assert not decision.escalate
    assert decision.reasons == []


def test_check_tier1_escalation_high_uncertainty_material_asset():
    decision = tier2.check_tier1_escalation(
        prior=None,
        current=_valid_payload(conviction_pct=48.0, evidence_quality=0.30),
    )
    assert decision.escalate
    assert any("high_uncertainty_material_asset" in r for r in decision.reasons)


def test_check_tier1_escalation_direction_change():
    decision = tier2.check_tier1_escalation(
        prior={"thesis_direction": "long", "conviction_pct": 50.0},
        current=_valid_payload(thesis_direction="short", conviction_pct=40.0),
    )
    assert decision.escalate
    assert any("direction_change" in r for r in decision.reasons)


def test_check_tier1_escalation_no_change_when_prior_direction_null():
    decision = tier2.check_tier1_escalation(
        prior={"thesis_direction": None},
        current=_valid_payload(thesis_direction="long", conviction_pct=40.0),
    )
    assert not decision.escalate


def test_check_tier1_escalation_new_primary_doc():
    decision = tier2.check_tier1_escalation(
        prior={"thesis_direction": "long"},
        current=_valid_payload(conviction_pct=40.0, thesis_direction="long"),
        new_primary_doc_types={"label", "press_release"},
    )
    assert decision.escalate
    assert any("new_primary_doc" in r for r in decision.reasons)


def test_check_tier1_escalation_non_primary_doc_does_not_trigger():
    decision = tier2.check_tier1_escalation(
        prior={"thesis_direction": "long"},
        current=_valid_payload(conviction_pct=40.0, thesis_direction="long"),
        new_primary_doc_types={"sec_filing", "news_blog"},
    )
    assert not decision.escalate


def test_check_tier1_escalation_compounds_reasons():
    decision = tier2.check_tier1_escalation(
        prior={"thesis_direction": "long"},
        current=_valid_payload(conviction_pct=85.0, thesis_direction="short"),
        new_primary_doc_types={"crl"},
    )
    assert decision.escalate
    assert len(decision.reasons) == 3


# ---------------------------------------------------------------------------
# enqueue_tier1_escalation
# ---------------------------------------------------------------------------

def test_enqueue_tier1_escalation_inserts_orchestrator_run(monkeypatch):
    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path,
                         "json_body": json_body})
        if method == "POST" and path == "orchestrator_runs":
            return [{"id": "run-1"}]
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    run_id = tier2.enqueue_tier1_escalation(
        sb, "asset-uuid-1",
        triggering_assessment_id="assess-1",
        reasons=["high_conviction (conviction_pct=85.0 ≥ 60.0)"],
    )
    assert run_id == "run-1"

    posts = [c for c in captured if c["method"] == "POST"]
    assert len(posts) == 1
    body = posts[0]["json_body"]
    assert body["asset_id"] == "asset-uuid-1"
    assert body["trigger_type"] == "tier2_escalation"
    assert body["tier"] == 1
    assert body["status"] == "pending"
    assert body["scheduled_at"]
    assert body["notes"]["triggering_assessment_id"] == "assess-1"
    assert "high_conviction" in body["notes"]["escalation_reasons"][0]


def test_enqueue_tier1_escalation_rejects_empty_reasons():
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"
    with pytest.raises(ValueError, match="no reasons"):
        tier2.enqueue_tier1_escalation(
            sb, "asset-uuid-1",
            triggering_assessment_id="assess-1",
            reasons=[],
        )


# ---------------------------------------------------------------------------
# Tier2InputBlob.to_json
# ---------------------------------------------------------------------------

def test_tier2_input_blob_to_json_is_serializable():
    import json

    blob = tier2.Tier2InputBlob(
        asset_id="a",
        ticker="T",
        drug_name="D",
        indication="i",
        reference_class_signature="rc",
        evidence_packet={"ok": True, "errors": []},
        extracted_facts=[{"id": "f1"}],
        asset_documents=[{"document_id": "d1", "link_type": "primary", "is_material": True}],
        prior_assessment=None,
    )
    out = json.loads(blob.to_json())
    assert out["asset_id"] == "a"
    assert out["extracted_facts"] == [{"id": "f1"}]
    assert out["prior_assessment"] is None


# ---------------------------------------------------------------------------
# Modal-endpoint orchestration helpers
# ---------------------------------------------------------------------------

def _make_rest_recorder(*, fda_assets=None, facts=None, asset_documents=None,
                       prior_convergence=None, doc_types=None,
                       run_row=None, insert_run_id=None,
                       insert_assessment_id=None, insert_escalation_id=None):
    """Build a fake _rest implementation with canned responses keyed on
    (method, path, params filter). Returns (rest_fn, captured_calls)."""
    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        params = params or {}
        captured.append({
            "method": method, "path": path, "params": params,
            "json_body": json_body, "prefer": prefer,
        })
        if method == "GET" and path == "fda_assets":
            return fda_assets or []
        if method == "GET" and path == "extracted_facts":
            return facts or []
        if method == "GET" and path == "asset_documents":
            return asset_documents or []
        if method == "GET" and path == "convergence_assessments":
            return prior_convergence or []
        if method == "GET" and path == "documents":
            ids_filter = params.get("id", "")
            return doc_types if doc_types is not None else []
        if method == "GET" and path == "orchestrator_runs":
            return [run_row] if run_row else []
        if method == "POST" and path == "orchestrator_runs":
            body = json_body or {}
            if body.get("trigger_type") == "tier2_escalation":
                return [{"id": insert_escalation_id or "esc-1"}]
            return [{"id": insert_run_id or "run-1"}]
        if method == "POST" and path == "convergence_assessments":
            return [{"id": insert_assessment_id or "assess-1"}]
        return []

    return fake_rest, captured


def test_enqueue_tier2_bulk_inserts_per_asset(monkeypatch):
    fake_rest, captured = _make_rest_recorder(
        fda_assets=[{
            "id": "a1", "ticker": "AXSM", "drug_name": "AXS-05",
            "indication": "MDD", "indication_normalized": "mdd",
        }],
        facts=[{"id": "f1"}],
        asset_documents=[{"document_id": "d1", "link_type": "primary", "is_material": True}],
        prior_convergence=[],
    )
    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    result = tier2.enqueue_tier2_bulk(sb, ["a1"])

    assert result["enqueued_count"] == 1
    assert result["failed_count"] == 0
    assert result["enqueued"][0]["asset_id"] == "a1"
    assert result["enqueued"][0]["run_id"] == "run-1"
    assert result["enqueued"][0]["blob"]["ticker"] == "AXSM"

    # Inserted row carries tier=2 and source notes
    inserts = [c for c in captured
               if c["method"] == "POST" and c["path"] == "orchestrator_runs"]
    assert len(inserts) == 1
    assert inserts[0]["json_body"]["tier"] == 2
    assert inserts[0]["json_body"]["status"] == "pending"
    assert inserts[0]["json_body"]["notes"]["source"] == "tier2_bulk_enqueue"


def test_enqueue_tier2_bulk_isolates_per_asset_failures(monkeypatch):
    """One bad asset_id (returns no row from fda_assets) does NOT abort
    the rest of the batch."""
    call_count = {"n": 0}

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        if method == "POST" and path == "orchestrator_runs":
            call_count["n"] += 1
            return [{"id": f"run-{call_count['n']}"}]
        if method == "GET" and path == "fda_assets":
            asset_id = (params or {}).get("id", "").replace("eq.", "")
            if asset_id == "good":
                return [{"id": "good", "ticker": "T", "drug_name": "D"}]
            return []  # 'bad' triggers ValueError in build_tier2_input_blob
        if method == "GET" and path == "asset_documents":
            return [{"document_id": "d1", "link_type": "primary", "is_material": True}]
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    result = tier2.enqueue_tier2_bulk(sb, ["good", "bad"])
    assert result["enqueued_count"] == 1
    assert result["failed_count"] == 1
    assert result["enqueued"][0]["asset_id"] == "good"
    assert result["failed"][0]["asset_id"] == "bad"


def test_complete_tier2_run_happy_path(monkeypatch):
    fake_rest, captured = _make_rest_recorder(
        run_row={"id": "run-X", "asset_id": "a1", "status": "pending",
                 "tier": 2, "trigger_type": "scheduled",
                 "trigger_doc_id": None},
        prior_convergence=[],
        insert_assessment_id="assess-X",
    )
    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    result = tier2.complete_tier2_run(
        sb, "run-X", _valid_payload(),
        cost_usd=0.42, latency_ms=45000,
    )
    assert result["status"] == "completed"
    assert result["assessment_id"] == "assess-X"
    assert result["escalated"] is False
    assert result["escalation_run_id"] is None

    # convergence_assessments POST went out
    posts = [c for c in captured
             if c["method"] == "POST" and c["path"] == "convergence_assessments"]
    assert len(posts) == 1
    assert posts[0]["json_body"]["tier"] == 2
    assert posts[0]["json_body"]["trigger_type"] == "scheduled"
    assert posts[0]["json_body"]["document_window_start"]
    assert posts[0]["json_body"]["document_window_end"]

    # Run patched twice: running → completed
    run_patches = [c for c in captured
                   if c["method"] == "PATCH" and c["path"] == "orchestrator_runs"]
    statuses = [p["json_body"].get("status") for p in run_patches]
    assert "running" in statuses
    assert "completed" in statuses


def test_complete_tier2_run_rejects_wrong_tier(monkeypatch):
    fake_rest, _ = _make_rest_recorder(
        run_row={"id": "run-X", "asset_id": "a1", "status": "pending",
                 "tier": 1},  # WRONG tier
    )
    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    with pytest.raises(ValueError, match="not 2"):
        tier2.complete_tier2_run(sb, "run-X", _valid_payload())


def test_complete_tier2_run_invalid_payload_marks_failed(monkeypatch):
    """A validator-rejected payload marks orchestrator_runs failed and
    returns errors WITHOUT writing convergence_assessments."""
    fake_rest, captured = _make_rest_recorder(
        run_row={"id": "run-X", "asset_id": "a1", "status": "pending",
                 "tier": 2},
    )
    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    bad_payload = _valid_payload(tier=1)  # tier mismatch
    result = tier2.complete_tier2_run(sb, "run-X", bad_payload)
    assert result["status"] == "failed_validation"
    assert any("tier" in e for e in result["errors"])

    posts_to_assess = [
        c for c in captured
        if c["method"] == "POST" and c["path"] == "convergence_assessments"
    ]
    assert posts_to_assess == []

    failed_patch = next(
        c for c in captured
        if c["method"] == "PATCH" and c["path"] == "orchestrator_runs"
        and c["json_body"].get("status") == "failed"
    )
    assert "tier" in failed_patch["json_body"]["error_message"]


def test_complete_tier2_run_triggers_high_conviction_escalation(monkeypatch):
    fake_rest, captured = _make_rest_recorder(
        run_row={"id": "run-X", "asset_id": "a1", "status": "pending",
                 "tier": 2},
        prior_convergence=[],
        insert_assessment_id="assess-X",
        insert_escalation_id="esc-1",
    )
    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    high_conviction_payload = _valid_payload(conviction_pct=88.0)
    result = tier2.complete_tier2_run(sb, "run-X", high_conviction_payload)

    assert result["escalated"] is True
    assert result["escalation_run_id"] == "esc-1"
    assert any("high_conviction" in r for r in result["escalation_reasons"])

    # Two POSTs to orchestrator_runs: enqueue (no — we GET only) + escalation
    escal_posts = [
        c for c in captured
        if c["method"] == "POST" and c["path"] == "orchestrator_runs"
    ]
    assert len(escal_posts) == 1
    assert escal_posts[0]["json_body"]["trigger_type"] == "tier2_escalation"


def test_complete_tier2_run_resolves_new_primary_doc_types(monkeypatch):
    """When current.document_ids includes a doc not in prior, we GET
    documents.doc_type to feed the new_primary_doc rule."""
    fake_rest, captured = _make_rest_recorder(
        run_row={"id": "run-X", "asset_id": "a1", "status": "pending",
                 "tier": 2},
        prior_convergence=[{
            "id": "prev", "thesis_direction": "long",
            "conviction_pct": 50.0, "document_ids": ["d-old"],
        }],
        doc_types=[{"doc_type": "label"}],
        insert_assessment_id="assess-X",
        insert_escalation_id="esc-1",
    )
    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    payload = _valid_payload(
        thesis_direction="long",
        conviction_pct=40.0,  # not high
        document_ids=["d-old", "d-new"],
    )
    result = tier2.complete_tier2_run(sb, "run-X", payload)

    assert result["escalated"] is True
    assert any("new_primary_doc" in r for r in result["escalation_reasons"])
    # documents lookup happened with the diff
    doc_lookups = [
        c for c in captured
        if c["method"] == "GET" and c["path"] == "documents"
    ]
    assert len(doc_lookups) == 1
    assert "d-new" in doc_lookups[0]["params"]["id"]
    assert "d-old" not in doc_lookups[0]["params"]["id"]


def test_complete_tier2_run_swallows_escalation_enqueue_failure(monkeypatch):
    """If enqueue_tier1_escalation throws, completion still succeeds with
    escalation_run_id=None and a tail reason recording the failure."""
    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "orchestrator_runs":
            return [{"id": "run-X", "asset_id": "a1", "tier": 2}]
        if method == "GET" and path == "convergence_assessments":
            return []
        if method == "GET" and path == "documents":
            return []
        if method == "POST" and path == "convergence_assessments":
            return [{"id": "assess-X"}]
        if method == "POST" and path == "orchestrator_runs":
            return []  # simulate insert returning no row
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    result = tier2.complete_tier2_run(
        sb, "run-X", _valid_payload(conviction_pct=85.0),
    )
    assert result["status"] == "completed"
    assert result["escalated"] is True
    assert result["escalation_run_id"] is None
    assert any("escalation_enqueue_failed" in r
               for r in result["escalation_reasons"])


def test_fail_tier2_run_patches_with_tier_filter(monkeypatch):
    """fail_tier2_run must include `tier=eq.2` in the WHERE so it can't
    accidentally fail a Tier-1 row."""
    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path, "params": params,
                         "json_body": json_body})
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    result = tier2.fail_tier2_run(sb, "run-X", "modal timeout")
    assert result == {"run_id": "run-X", "status": "failed"}

    patch = captured[0]
    assert patch["method"] == "PATCH"
    assert patch["path"] == "orchestrator_runs"
    assert patch["params"]["id"] == "eq.run-X"
    assert patch["params"]["tier"] == "eq.2"
    assert patch["json_body"]["status"] == "failed"
    assert "modal timeout" in patch["json_body"]["error_message"]


def test_fail_tier2_run_uses_default_message_when_empty(monkeypatch):
    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path,
                         "json_body": json_body})
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"

    tier2.fail_tier2_run(sb, "run-X", "")
    assert captured[0]["json_body"]["error_message"] == "tier2 skill error"
