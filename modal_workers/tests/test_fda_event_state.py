"""
Phase 1 acceptance tests — every watchlist row maps to exactly one asset and
the right number of events per status. Pure-Python transform, no DB.

Source watchlist fixture is the live v1 file at
unified_system/unified_system/signals/legacy_t1/pdufa_watchlist.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modal_workers.scanners.fda_event_state import (
    _asset_key,
    _is_linked_status,
    transform_watchlist_payload,
)


WATCHLIST_PATH = (
    Path(__file__).resolve().parents[2]
    / "unified_system/unified_system/signals/legacy_t1/pdufa_watchlist.json"
)


@pytest.fixture(scope="module")
def watchlist_payload():
    with WATCHLIST_PATH.open("rb") as fh:
        return json.loads(fh.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Status-rule unit tests (synthetic rows, focused invariants)
# ---------------------------------------------------------------------------


def test_active_row_emits_one_pending_pdufa_event():
    rows = [
        {
            "ticker": "AXSM",
            "drug_name": "AXS-05",
            "application_number": "",
            "indication": "Agitation",
            "company_name": "Axsome",
            "status": "active",
            "pdufa_date": "2026-04-30",
            "nda_type": "sNDA",
            "is_resubmission": False,
        }
    ]
    result = transform_watchlist_payload(rows)
    assert len(result["assets"]) == 1
    assert len(result["events"]) == 1
    assert len(result["evidence"]) == 1
    e = result["events"][0]
    assert e["event_type"] == "pdufa"
    assert e["event_status"] == "pending"
    assert e["event_date"] == "2026-04-30"


def test_active_row_with_adcom_date_emits_pdufa_plus_adcom():
    rows = [
        {
            "ticker": "FOO",
            "drug_name": "Foozumab",
            "application_number": "",
            "status": "active",
            "pdufa_date": "2026-09-15",
            "adcom_date": "2026-08-12",
            "is_resubmission": False,
        }
    ]
    result = transform_watchlist_payload(rows)
    assert len(result["events"]) == 2
    types = {e["event_type"] for e in result["events"]}
    assert types == {"pdufa", "adcom"}
    adcom = next(e for e in result["events"] if e["event_type"] == "adcom")
    assert adcom["event_status"] == "pending"
    assert adcom["event_date"] == "2026-08-12"


def test_approved_row_emits_resolved_approval_event():
    rows = [
        {
            "ticker": "TVTX",
            "drug_name": "FILSPARI (sparsentan)",
            "application_number": "",
            "status": "approved",
            "pdufa_date": "2026-04-13",
            "resolution_date": "2026-04-13",
            "resolution_note": "FDA approved FILSPARI for FSGS 2026-04-13",
            "is_resubmission": True,
        }
    ]
    result = transform_watchlist_payload(rows)
    assert len(result["events"]) == 1
    e = result["events"][0]
    assert e["event_type"] == "approval"
    assert e["event_status"] == "resolved"
    assert e["event_date"] == "2026-04-13"


def test_resolved_crl_emits_resolved_crl_event():
    rows = [
        {
            "ticker": "PFMPY",
            "drug_name": "Tabelecleucel",
            "application_number": "",
            "status": "resolved_crl",
            "pdufa_date": None,
            "crl_date": "2026-01-09",
            "is_resubmission": False,
        }
    ]
    result = transform_watchlist_payload(rows)
    assert len(result["events"]) == 1
    e = result["events"][0]
    assert e["event_type"] == "crl"
    assert e["event_status"] == "resolved"
    assert e["event_date"] == "2026-01-09"


def test_linked_to_status_emits_asset_but_no_event():
    rows = [
        {
            "ticker": "ACLX",
            "drug_name": "Anito-cel",
            "application_number": "",
            "status": "linked_to_GILD",
            "pdufa_date": "2026-12-23",
            "is_resubmission": False,
        }
    ]
    result = transform_watchlist_payload(rows)
    assert len(result["assets"]) == 1
    assert len(result["events"]) == 0
    assert len(result["evidence"]) == 0
    assert result["assets"][0]["extensions"]["linked_to"] == "GILD"


def test_non_tradeable_emits_asset_but_no_event():
    rows = [
        {
            "ticker": "XSPRAY.ST",
            "drug_name": "Dasynoc",
            "application_number": "",
            "status": "non_tradeable",
            "pdufa_date": None,
            "is_resubmission": True,
        }
    ]
    result = transform_watchlist_payload(rows)
    assert len(result["assets"]) == 1
    assert len(result["events"]) == 0


def test_dedup_assets_on_ticker_drug_appnum():
    rows = [
        {
            "ticker": "AXSM",
            "drug_name": "AXS-05",
            "application_number": "",
            "status": "active",
            "pdufa_date": "2026-04-30",
        },
        {
            "ticker": "AXSM",
            "drug_name": "AXS-05",
            "application_number": "",
            "status": "active",
            "pdufa_date": "2026-05-30",  # date change after re-fetch
        },
    ]
    result = transform_watchlist_payload(rows)
    # asset is shared, two distinct events (different dates -> different hashes)
    assert len(result["assets"]) == 1
    assert len(result["events"]) == 2
    hashes = {e["source_content_hash"] for e in result["events"]}
    assert len(hashes) == 2  # different event_dates produce different hashes


def test_source_content_hash_is_stable_for_same_inputs():
    rows1 = [
        {"ticker": "T", "drug_name": "D", "application_number": "", "status": "active",
         "pdufa_date": "2026-09-15"}
    ]
    rows2 = [
        {"ticker": "T", "drug_name": "D", "application_number": "", "status": "active",
         "pdufa_date": "2026-09-15"}
    ]
    h1 = transform_watchlist_payload(rows1)["events"][0]["source_content_hash"]
    h2 = transform_watchlist_payload(rows2)["events"][0]["source_content_hash"]
    assert h1 == h2
    assert len(h1) == 64  # sha256 hexdigest


def test_missing_ticker_or_drug_skipped():
    rows = [
        {"ticker": "", "drug_name": "X", "status": "active", "pdufa_date": "2026-05-01"},
        {"ticker": "FOO", "drug_name": "", "status": "active", "pdufa_date": "2026-05-01"},
        {"ticker": "BAR", "drug_name": "Y", "status": "active", "pdufa_date": "2026-05-01"},
    ]
    result = transform_watchlist_payload(rows)
    assert len(result["assets"]) == 1
    assert result["assets"][0]["ticker"] == "BAR"


def test_evidence_one_per_event():
    rows = [
        {"ticker": "A", "drug_name": "Aa", "application_number": "", "status": "active",
         "pdufa_date": "2026-09-15", "adcom_date": "2026-08-12"},
        {"ticker": "B", "drug_name": "Bb", "application_number": "", "status": "approved",
         "pdufa_date": "2026-03-01", "resolution_date": "2026-03-01"},
        {"ticker": "C", "drug_name": "Cc", "application_number": "", "status": "linked_to_X"},
    ]
    result = transform_watchlist_payload(rows)
    assert len(result["events"]) == len(result["evidence"])
    # cross-reference by event_key
    ev_keys = {ev["event_key"] for ev in result["evidence"]}
    expected_keys = {
        f"{_asset_key(e_row['ticker'], e_row['drug'], '')}|{e_type}|{e_date}"
        for e_row, e_type, e_date in [
            ({"ticker": "A", "drug": "Aa"}, "pdufa", "2026-09-15"),
            ({"ticker": "A", "drug": "Aa"}, "adcom", "2026-08-12"),
            ({"ticker": "B", "drug": "Bb"}, "approval", "2026-03-01"),
        ]
    }
    assert ev_keys == expected_keys


def test_helper_is_linked_status():
    assert _is_linked_status("linked_to_GILD")
    assert _is_linked_status("linked_to_TVTX")
    assert not _is_linked_status("active")
    assert not _is_linked_status("approved")


# ---------------------------------------------------------------------------
# Live watchlist invariants (full file)
# ---------------------------------------------------------------------------


def test_live_watchlist_one_asset_per_unique_drug(watchlist_payload):
    result = transform_watchlist_payload(watchlist_payload)
    keys = [
        _asset_key(a["ticker"], a["drug_name"], a.get("application_number") or "")
        for a in result["assets"]
    ]
    assert len(keys) == len(set(keys)), "duplicate asset keys"


def test_live_watchlist_event_count_matches_status_rules(watchlist_payload):
    result = transform_watchlist_payload(watchlist_payload)

    # Tally expected events per row given the status rules.
    expected = 0
    for row in watchlist_payload:
        status = (row.get("status") or "").strip()
        if not row.get("ticker") or not row.get("drug_name"):
            continue
        if status == "active":
            if row.get("pdufa_date"):
                expected += 1
            if row.get("adcom_date"):
                expected += 1
        elif status == "approved":
            expected += 1
        elif status == "resolved_crl":
            expected += 1
        # linked_to_* / non_tradeable contribute 0
    assert len(result["events"]) == expected


def test_live_watchlist_resolution_events_are_resolved(watchlist_payload):
    result = transform_watchlist_payload(watchlist_payload)
    resolution_types = {"approval", "crl", "presumed_crl", "withdrawal"}
    for ev in result["events"]:
        if ev["event_type"] in resolution_types:
            assert ev["event_status"] == "resolved", (
                f"event {ev['event_type']} with status {ev['event_status']} "
                "must be resolved (Phase 3 invariant)"
            )


def test_live_watchlist_evidence_one_per_event(watchlist_payload):
    result = transform_watchlist_payload(watchlist_payload)
    assert len(result["evidence"]) == len(result["events"])
