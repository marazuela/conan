from __future__ import annotations

from datetime import datetime, timedelta, timezone

from modal_workers.scanners.fda_pdufa_pipeline import (
    DIRECTION_SHORT,
    SIGNAL_TYPE_DATE_DELAYED,
    SIGNAL_TYPE_WATCHLIST,
    _add_to_watchlist,
    _classify_subtype,
    _thesis_direction,
)


def _entry(**overrides: object) -> dict:
    today = datetime.now(timezone.utc).date()
    base = {
        "ticker": "AXSM",
        "drug_name": "AXS-05",
        "status": "active",
        "pdufa_date": (today + timedelta(days=45)).isoformat(),
        "previous_pdufa_date": None,
        "pdufa_date_change_kind": None,
        "pdufa_date_changed_at": None,
        "crl_date": None,
        "notes": "",
        "enrichment": {},
    }
    base.update(overrides)
    return base


def test_recent_pdufa_delay_gets_decision_adjacent_subtype_and_short_bias():
    today = datetime.now(timezone.utc).date()
    entry = _entry(
        pdufa_date=(today + timedelta(days=120)).isoformat(),
        previous_pdufa_date=(today + timedelta(days=45)).isoformat(),
        pdufa_date_change_kind="delayed",
        pdufa_date_changed_at=today.isoformat(),
    )

    assert _classify_subtype(entry, 120) == SIGNAL_TYPE_DATE_DELAYED
    assert _thesis_direction(entry) == DIRECTION_SHORT


def test_stale_date_change_reverts_to_standard_proximity_signal():
    stale_day = (datetime.now(timezone.utc) - timedelta(days=20)).date().isoformat()
    entry = _entry(
        pdufa_date_change_kind="delayed",
        pdufa_date_changed_at=stale_day,
    )

    assert _classify_subtype(entry, 45) == SIGNAL_TYPE_WATCHLIST


def test_watchlist_update_records_prior_pdufa_date_metadata():
    entries = [{
        "ticker": "AXSM",
        "drug_name": "(auto-discovered)",
        "pdufa_date": "2026-07-01",
        "status": "active",
        "notes": "",
        "enrichment": {},
    }]

    updated = _add_to_watchlist(
        entries,
        ticker="AXSM",
        drug_name="(auto-discovered)",
        pdufa_date="2026-08-15",
    )

    assert updated[0]["pdufa_date"] == "2026-08-15"
    assert updated[0]["previous_pdufa_date"] == "2026-07-01"
    assert updated[0]["pdufa_date_change_kind"] == "delayed"
    assert updated[0]["pdufa_date_changed_at"] is not None
