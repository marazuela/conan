"""WI-7 — tests for harvest_fda_events helpers.

Covers the pure openFDA→event-row mapper, the content-hash function, the
merge accumulator, and the EDGAR 8-K stub. End-to-end DB integration is
exercised manually in dry-run mode against the live openFDA API.

Run: python -m pytest modal_workers/tests/test_harvest_fda_events.py -v
"""
from __future__ import annotations

from datetime import date

import pytest

from modal_workers.scripts.harvest_fda_events import (
    HarvestResult,
    SUB_STATUS_TO_EVENT,
    _hash_event,
    _harvest_edgar_8k_stub,
    _map_openfda_drug_to_event_rows,
    _merge_into_result,
)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def _openfda_drug(
    *,
    application_number: str = "NDA-100001",
    sponsor: str = "Axsome Therapeutics Inc",
    brand: str = "AXS-05",
    submissions=None,
):
    return {
        "application_number": application_number,
        "sponsor_name": sponsor,
        "products": [{
            "brand_name": brand,
            "active_ingredients": [{"name": "axsome-generic"}],
        }],
        "submissions": submissions or [],
    }


def _sub(status: str, sub_date: str, sub_num: str = "S-1"):
    return {
        "submission_status": status,
        "submission_status_date": sub_date,
        "submission_number": sub_num,
        "submission_type": "ORIG",
    }


# ---------------------------------------------------------------------------
# Status → event-type mapping
# ---------------------------------------------------------------------------


def test_only_ap_and_cr_submissions_map_to_events():
    drug = _openfda_drug(submissions=[
        _sub("AP", "20260515"),
        _sub("CR", "20260601"),
        _sub("TA", "20260620"),  # tentative — should NOT map
        _sub("WD", "20260625"),  # withdrawn — should NOT map
    ])
    rows = _map_openfda_drug_to_event_rows(
        drug, start_date=date(2026, 5, 1), end_date=date(2026, 7, 1),
    )
    assert {r["event_type"] for r in rows} == {"approval", "crl"}
    assert len(rows) == 2


def test_status_mapping_matches_constant():
    assert SUB_STATUS_TO_EVENT == {"AP": "approval", "CR": "crl"}


# ---------------------------------------------------------------------------
# Date-range filter
# ---------------------------------------------------------------------------


def test_submissions_outside_window_are_filtered():
    drug = _openfda_drug(submissions=[
        _sub("AP", "20260101"),  # before window
        _sub("AP", "20260515"),  # in window
        _sub("AP", "20260901"),  # after window
    ])
    rows = _map_openfda_drug_to_event_rows(
        drug, start_date=date(2026, 5, 1), end_date=date(2026, 6, 1),
    )
    assert len(rows) == 1
    assert rows[0]["event_date"] == "2026-05-15"


def test_malformed_date_skipped_gracefully():
    drug = _openfda_drug(submissions=[
        _sub("AP", "BAD-DATE"),
        _sub("AP", "20260515"),
    ])
    rows = _map_openfda_drug_to_event_rows(
        drug, start_date=date(2026, 5, 1), end_date=date(2026, 6, 1),
    )
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# event_status semantics — approvals/CRLs are 'resolved' on emission
# ---------------------------------------------------------------------------


def test_approval_emits_resolved_status():
    drug = _openfda_drug(submissions=[_sub("AP", "20260515")])
    rows = _map_openfda_drug_to_event_rows(
        drug, start_date=date(2026, 5, 1), end_date=date(2026, 6, 1),
    )
    assert rows[0]["event_status"] == "resolved"


def test_crl_emits_resolved_status():
    drug = _openfda_drug(submissions=[_sub("CR", "20260515")])
    rows = _map_openfda_drug_to_event_rows(
        drug, start_date=date(2026, 5, 1), end_date=date(2026, 6, 1),
    )
    assert rows[0]["event_status"] == "resolved"


# ---------------------------------------------------------------------------
# Asset hints + extensions
# ---------------------------------------------------------------------------


def test_asset_hints_carry_sponsor_and_app_number():
    drug = _openfda_drug(
        application_number="NDA-200002",
        sponsor="Verve Therapeutics Inc",
        brand="VERV-101",
        submissions=[_sub("AP", "20260515")],
    )
    rows = _map_openfda_drug_to_event_rows(
        drug, start_date=date(2026, 5, 1), end_date=date(2026, 6, 1),
    )
    hints = rows[0]["asset_hints"]
    assert hints["application_number"] == "NDA-200002"
    assert hints["sponsor_name"] == "Verve Therapeutics Inc"
    assert hints["drug_name"] == "VERV-101"


def test_extensions_capture_source_and_application_number():
    drug = _openfda_drug(
        application_number="NDA-300003",
        submissions=[_sub("AP", "20260515", sub_num="S-7")],
    )
    rows = _map_openfda_drug_to_event_rows(
        drug, start_date=date(2026, 5, 1), end_date=date(2026, 6, 1),
    )
    ext = rows[0]["extensions"]
    assert ext["source"] == "openfda"
    assert ext["application_number"] == "NDA-300003"
    assert ext["submission_number"] == "S-7"


# ---------------------------------------------------------------------------
# Content hash determinism
# ---------------------------------------------------------------------------


def test_content_hash_is_deterministic():
    h1 = _hash_event("openfda", "NDA-1", "approval", "2026-05-15", "S-1")
    h2 = _hash_event("openfda", "NDA-1", "approval", "2026-05-15", "S-1")
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_content_hash_changes_on_any_input():
    base = _hash_event("openfda", "NDA-1", "approval", "2026-05-15", "S-1")
    diff_app = _hash_event("openfda", "NDA-2", "approval", "2026-05-15", "S-1")
    diff_type = _hash_event("openfda", "NDA-1", "crl", "2026-05-15", "S-1")
    diff_date = _hash_event("openfda", "NDA-1", "approval", "2026-05-16", "S-1")
    assert len({base, diff_app, diff_type, diff_date}) == 4


# ---------------------------------------------------------------------------
# HarvestResult merge
# ---------------------------------------------------------------------------


def test_merge_into_result_accumulates_counts_and_breakdown():
    into = HarvestResult()
    sub_a = HarvestResult(fetched=10, upserted=8, skipped=2)
    sub_b = HarvestResult(fetched=5, upserted=4, skipped=1,
                          errors=[{"x": "y"}])
    _merge_into_result(into, sub_a, source_label="openfda")
    _merge_into_result(into, sub_b, source_label="edgar_8k")
    assert into.fetched == 15
    assert into.upserted == 12
    assert into.skipped == 3
    assert into.errors == [{"x": "y"}]
    assert into.source_breakdown == {"openfda": 8, "edgar_8k": 4}


# ---------------------------------------------------------------------------
# EDGAR 8-K stub returns empty until follow-up wires it
# ---------------------------------------------------------------------------


def test_edgar_8k_stub_returns_empty_result():
    sub = _harvest_edgar_8k_stub(
        None, start_date=date(2026, 5, 1), end_date=date(2026, 6, 1),
    )
    assert sub.fetched == 0
    assert sub.upserted == 0
    assert sub.errors == []
