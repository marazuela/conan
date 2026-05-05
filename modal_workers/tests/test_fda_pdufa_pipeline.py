from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from modal_workers.scanners import fda_pdufa_pipeline as scanner
from modal_workers.scanners.fda_pdufa_pipeline import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
    SIGNAL_TYPE_DATE_DELAYED,
    SIGNAL_TYPE_WATCHLIST,
    _add_to_watchlist,
    _apply_designation_modifiers,
    _assess_strength,
    _build_signal,
    _classify_subtype,
    _days_until,
    _extract_designations,
    _thesis_direction,
)


def _entry(**overrides: object) -> dict:
    today = datetime.now(timezone.utc).date()
    base = {
        "ticker": "AXSM",
        "drug_name": "AXS-05",
        "company_name": "Axsome Therapeutics",
        "indication": "",
        "status": "active",
        "pdufa_date": (today + timedelta(days=45)).isoformat(),
        "previous_pdufa_date": None,
        "pdufa_date_change_kind": None,
        "pdufa_date_changed_at": None,
        "crl_date": None,
        "is_resubmission": False,
        "notes": "",
        "enrichment": {},
    }
    base.update(overrides)
    return base


@pytest.fixture
def fake_client(monkeypatch):
    """SupabaseClient stand-in plus a fixed base-rates table (cancer at 0.45,
    default at 0.58). Bypasses load_market_snapshot so tests don't hit the network."""
    fake = MagicMock()
    fake.read_cache.return_value = None
    fake.write_cache.return_value = None
    monkeypatch.setattr(
        scanner, "load_base_rates",
        lambda _c: {"oncology_solid_tumor": 0.45, "default": 0.58},
    )

    # Block market_snapshot import path — it tries to talk to Supabase.
    import modal_workers.shared.market_snapshot as ms
    monkeypatch.setattr(ms, "load_market_snapshot", lambda *_a, **_kw: None)
    return fake


# ---------------------------------------------------------------------------
# Existing parity tests (preserved)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Phase 1 — base rates + designations + magnitude defaults
# ---------------------------------------------------------------------------

def _scan_date() -> datetime:
    return datetime.now(timezone.utc)


def test_build_signal_populates_approval_probability_from_indication(fake_client):
    entry = _entry(
        indication="metastatic NSCLC carcinoma",
        enrichment={
            "trial": {"status": "ACTIVE_NOT_RECRUITING",
                      "conditions": ["Non-Small Cell Lung Cancer"]},
        },
    )

    sig = _build_signal(entry, days=45, scan_date=_scan_date(),
                       issuer_figi=None, client=fake_client)
    assert sig is not None
    assert sig.raw_payload["base_rate_key"] == "oncology_solid_tumor"
    # 0.45 base; no designations / no resubmission → 0.45 untouched.
    assert sig.raw_payload["approval_probability"] == pytest.approx(0.45)


def test_build_signal_falls_back_to_default_base_rate(fake_client):
    entry = _entry(indication="", enrichment={})  # nothing matches INDICATION_MAP
    sig = _build_signal(entry, days=45, scan_date=_scan_date(),
                       issuer_figi=None, client=fake_client)
    assert sig is not None
    assert sig.raw_payload["base_rate_key"] == "default"
    assert sig.raw_payload["approval_probability"] == pytest.approx(0.58)


def test_priority_review_lifts_approval_probability_and_strength(fake_client):
    entry = _entry(
        indication="metastatic NSCLC carcinoma",
        enrichment={
            "trial": {"status": "ACTIVE_NOT_RECRUITING",
                      "conditions": ["Non-Small Cell Lung Cancer"]},
            "designations": {"priority_review": True,
                             "breakthrough_designation": False,
                             "accelerated_approval": False,
                             "orphan_drug": False},
        },
    )

    sig = _build_signal(entry, days=45, scan_date=_scan_date(),
                       issuer_figi=None, client=fake_client)
    assert sig is not None
    # 0.45 + 0.05 priority lift.
    assert sig.raw_payload["approval_probability"] == pytest.approx(0.50)
    assert sig.raw_payload["priority_review"] is True
    # Strength: base 2 +1 trial +1 ACTIVE_NOT_RECRUITING +1 priority_review = 5.
    assert sig.strength_estimate == 5


def test_resubmission_lowers_approval_probability(fake_client):
    entry = _entry(
        indication="metastatic NSCLC carcinoma",
        is_resubmission=True,
        enrichment={"trial": {"status": "ACTIVE_NOT_RECRUITING",
                              "conditions": ["NSCLC"]}},
    )

    sig = _build_signal(entry, days=45, scan_date=_scan_date(),
                       issuer_figi=None, client=fake_client)
    assert sig is not None
    # 0.45 base − 0.10 resubmission penalty.
    assert sig.raw_payload["approval_probability"] == pytest.approx(0.35)


def test_magnitude_defaults_present(fake_client):
    entry = _entry()
    sig = _build_signal(entry, days=45, scan_date=_scan_date(),
                       issuer_figi=None, client=fake_client)
    assert sig is not None
    assert sig.raw_payload["upside_pct"] == 50.0
    assert sig.raw_payload["downside_pct"] == 35.0


def test_thesis_direction_long_when_no_negative_signals(fake_client):
    entry = _entry()
    sig = _build_signal(entry, days=45, scan_date=_scan_date(),
                       issuer_figi=None, client=fake_client)
    assert sig is not None
    assert sig.thesis_direction == DIRECTION_LONG


def test_apply_designation_modifiers_clamps_to_max():
    # Maxed designations + a high base rate should clamp at 0.95.
    out = _apply_designation_modifiers(
        base_prob=0.92,
        designations={"priority_review": True,
                      "breakthrough_designation": True,
                      "accelerated_approval": True},
        is_resubmission=False,
    )
    assert out == pytest.approx(0.95)


def test_apply_designation_modifiers_clamps_to_min():
    out = _apply_designation_modifiers(
        base_prob=0.05,
        designations={},
        is_resubmission=True,
    )
    assert out == 0.0


def test_extract_designations_priority_review_only():
    history = [{
        "submissions": [
            {"submission_status": "SUBMITTED", "review_priority": "PRIORITY"},
        ],
    }]
    flags = _extract_designations(history)
    assert flags["priority_review"] is True
    assert flags["breakthrough_designation"] is False


def test_extract_designations_handles_empty_history():
    flags = _extract_designations(None)
    assert flags == {
        "priority_review": False,
        "breakthrough_designation": False,
        "accelerated_approval": False,
        "orphan_drug": False,
    }


# ---------------------------------------------------------------------------
# Phase 1e — UTC datetime correctness (audit F-103)
# ---------------------------------------------------------------------------

def test_days_until_returns_zero_or_negative_one_on_today():
    # `target - now()` semantics: target is midnight UTC, now() is some moment after,
    # so timedelta.days is -1 for any time after UTC midnight, 0 only at exactly midnight.
    today = datetime.now(timezone.utc).date()
    assert _days_until(today.isoformat()) in (-1, 0)


def test_days_until_handles_future_dates():
    target = (datetime.now(timezone.utc).date() + timedelta(days=10)).isoformat()
    # Allow ±1 to absorb any midnight rollover during the test run.
    assert _days_until(target) in (9, 10)


def test_days_until_returns_none_on_invalid_input():
    assert _days_until("not-a-date") is None
    assert _days_until("") is None


def test_assess_strength_no_longer_bumps_on_resubmission():
    # Pure resubmission with no trial / no designation: strength stays at base 2.
    entry = _entry(is_resubmission=True, enrichment={})
    assert _assess_strength(entry) == 2


def test_assess_strength_priority_review_adds_one():
    entry = _entry(enrichment={"designations": {"priority_review": True}})
    # Base 2 + 1 priority = 3 (no trial enrichment).
    assert _assess_strength(entry) == 3


# ---------------------------------------------------------------------------
# Phase 2 — CRL discovery + presumed_crl auto-promotion
# ---------------------------------------------------------------------------

def _crl_hit(ticker: str, file_date: str) -> dict:
    return {
        "_id": f"0001234567-26-000001:filename.htm",
        "_source": {
            "display_names": [f"{ticker} Therapeutics ({ticker}) (CIK 0001234567)"],
            "ciks": ["0001234567"],
            "adsh": "0001234567-26-000001",
            "file_date": file_date,
            "form": "8-K",
        },
    }


def test_crl_discovery_short_biases_signal(monkeypatch):
    today = datetime.now(timezone.utc).date()
    pdufa = (today - timedelta(days=2)).isoformat()
    file_date = today.isoformat()

    watchlist = [_entry(ticker="VKTX", drug_name="VK2735", pdufa_date=pdufa)]
    monkeypatch.setattr(
        "modal_workers.shared.edgar_efts.efts_search",
        lambda *_a, **_kw: [_crl_hit("VKTX", file_date)],
    )

    marked = scanner._discover_crls_from_edgar(watchlist, user_agent="ua@test")
    assert marked == ["VKTX"]
    assert watchlist[0]["status"] == "crl"
    assert watchlist[0]["crl_date"] == file_date
    assert _classify_subtype(watchlist[0], days=-2) == scanner.SIGNAL_TYPE_DECISION
    assert _thesis_direction(watchlist[0]) == DIRECTION_SHORT


def test_crl_discovery_skips_old_pdufa(monkeypatch):
    today = datetime.now(timezone.utc).date()
    six_months_ago = (today - timedelta(days=180)).isoformat()
    watchlist = [_entry(ticker="ARWR", drug_name="ARO-AAT", pdufa_date=six_months_ago)]
    monkeypatch.setattr(
        "modal_workers.shared.edgar_efts.efts_search",
        lambda *_a, **_kw: [_crl_hit("ARWR", today.isoformat())],
    )
    assert scanner._discover_crls_from_edgar(watchlist, user_agent="ua@test") == []
    assert watchlist[0]["status"] == "active"


def test_presumed_crl_auto_promotion(monkeypatch):
    today = datetime.now(timezone.utc).date()
    past_pdufa = (today - timedelta(days=5)).isoformat()
    watchlist = [_entry(ticker="GERN", drug_name="Imetelstat", pdufa_date=past_pdufa)]

    fake_client = MagicMock()
    monkeypatch.setattr(scanner, "_check_fda_approval_status",
                        lambda _drug, _ua, _c: None)

    promoted = scanner._apply_presumed_crl(watchlist, fake_client, "ua@test")
    assert promoted == ["GERN"]
    assert watchlist[0]["status"] == "presumed_crl"
    assert _thesis_direction(watchlist[0]) == DIRECTION_SHORT


def test_presumed_crl_skips_when_recent_ap_in_window(monkeypatch):
    today = datetime.now(timezone.utc).date()
    past_pdufa = (today - timedelta(days=4)).isoformat()
    watchlist = [_entry(ticker="ABCD", drug_name="DrugX", pdufa_date=past_pdufa)]

    fake_client = MagicMock()
    monkeypatch.setattr(
        scanner, "_check_fda_approval_status",
        lambda _drug, _ua, _c: {"approved": True,
                                "approval_date": (today - timedelta(days=1)).strftime("%Y%m%d"),
                                "application_number": "NDA12345"},
    )

    promoted = scanner._apply_presumed_crl(watchlist, fake_client, "ua@test")
    assert promoted == []
    assert watchlist[0]["status"] == "active"


def test_presumed_crl_skips_when_pdufa_not_yet_past(monkeypatch):
    today = datetime.now(timezone.utc).date()
    future_pdufa = (today + timedelta(days=30)).isoformat()
    watchlist = [_entry(ticker="ABCD", drug_name="DrugX", pdufa_date=future_pdufa)]
    fake_client = MagicMock()
    monkeypatch.setattr(scanner, "_check_fda_approval_status",
                        lambda _drug, _ua, _c: None)
    assert scanner._apply_presumed_crl(watchlist, fake_client, "ua@test") == []


def test_presumed_crl_promotes_auto_discovered_without_drug_name(monkeypatch):
    today = datetime.now(timezone.utc).date()
    past_pdufa = (today - timedelta(days=10)).isoformat()
    watchlist = [_entry(ticker="ABCD", drug_name="(auto-discovered)",
                        pdufa_date=past_pdufa)]
    fake_client = MagicMock()
    promoted = scanner._apply_presumed_crl(watchlist, fake_client, "ua@test")
    assert promoted == ["ABCD"]
    assert watchlist[0]["status"] == "presumed_crl"


def test_presumed_crl_signal_routes_to_fda_decision_with_short_direction(fake_client):
    today = datetime.now(timezone.utc).date()
    past_pdufa = (today - timedelta(days=10)).isoformat()
    entry = _entry(
        ticker="ABCD", drug_name="(auto-discovered)",
        pdufa_date=past_pdufa, status="presumed_crl",
    )
    sig = _build_signal(entry, days=-10, scan_date=_scan_date(),
                       issuer_figi=None, client=fake_client)
    assert sig is not None
    assert sig.signal_type == scanner.SIGNAL_TYPE_DECISION
    assert sig.thesis_direction == DIRECTION_SHORT


# ---------------------------------------------------------------------------
# Phase 4 — drug-name extraction + market-cap-weighted magnitude
# ---------------------------------------------------------------------------

def test_extract_drug_name_recognizes_inn_suffix():
    body = ("Item 8.01 Other Events. The Company announced today that the FDA "
            "has accepted for review the New Drug Application for Lifyorli "
            "(relacorilant) for the treatment of Cushing syndrome.")
    assert scanner._extract_drug_name(body) == "relacorilant"


def test_extract_drug_name_returns_none_when_no_inn_match():
    body = "Item 8.01. The Company today announced quarterly results. No drug names."
    assert scanner._extract_drug_name(body) is None


def test_parse_filing_returns_both_date_and_drug(monkeypatch):
    body = ("PDUFA action date of January 5, 2026 has been assigned for "
            "tovorafenib for the treatment of pediatric low-grade glioma.")
    monkeypatch.setattr(
        "modal_workers.shared.edgar_efts.fetch_filing_text",
        lambda *_a, **_kw: body,
    )
    date_iso, drug = scanner._parse_filing_for_pdufa(
        "0001:abc.htm", "0001234567", "0001234567-26-000001",
        user_agent="ua@test",
    )
    assert date_iso == "2026-01-05"
    assert drug == "tovorafenib"


def test_parse_filing_returns_none_tuple_when_body_missing(monkeypatch):
    monkeypatch.setattr(
        "modal_workers.shared.edgar_efts.fetch_filing_text",
        lambda *_a, **_kw: None,
    )
    date_iso, drug = scanner._parse_filing_for_pdufa(
        "x:y", "0", "z", user_agent="ua@test")
    assert date_iso is None and drug is None


def test_extract_pdufa_date_shim_matches_full_parser(monkeypatch):
    body = "PDUFA action date of March 15, 2026 has been set."
    monkeypatch.setattr(
        "modal_workers.shared.edgar_efts.fetch_filing_text",
        lambda *_a, **_kw: body,
    )
    assert scanner._extract_pdufa_date_from_filing(
        "x:y.htm", "0", "z", user_agent="ua@test") == "2026-03-15"


def test_magnitude_defaults_for_unknown_mcap_returns_legacy():
    assert scanner._magnitude_defaults_for(None) == (50.0, 35.0)


def test_magnitude_defaults_for_small_cap():
    # < $1B in USD → 60/40
    assert scanner._magnitude_defaults_for(500_000_000.0) == (60.0, 40.0)


def test_magnitude_defaults_for_megacap():
    # > $50B → 4/3
    assert scanner._magnitude_defaults_for(150_000_000_000.0) == (4.0, 3.0)


def test_build_signal_uses_megacap_magnitude_defaults(fake_client, monkeypatch):
    # Stub load_market_snapshot to return a megacap mcap.
    import modal_workers.shared.market_snapshot as ms
    monkeypatch.setattr(
        ms, "load_market_snapshot",
        lambda *_a, **_kw: {"market_cap_usd": 200_000_000_000.0,
                            "adv_usd": 5_000_000_000.0,
                            "source_liveness": "live"},
    )
    entry = _entry(ticker="JNJ", drug_name="megacapDrug")
    sig = _build_signal(entry, days=45, scan_date=_scan_date(),
                       issuer_figi=None, client=fake_client)
    assert sig is not None
    assert sig.raw_payload["upside_pct"] == 4.0
    assert sig.raw_payload["downside_pct"] == 3.0


def test_build_signal_uses_small_cap_magnitude_defaults(fake_client, monkeypatch):
    import modal_workers.shared.market_snapshot as ms
    monkeypatch.setattr(
        ms, "load_market_snapshot",
        lambda *_a, **_kw: {"market_cap_usd": 400_000_000.0,
                            "adv_usd": 10_000_000.0,
                            "source_liveness": "live"},
    )
    entry = _entry(ticker="VKTX", drug_name="smallcapDrug")
    sig = _build_signal(entry, days=45, scan_date=_scan_date(),
                       issuer_figi=None, client=fake_client)
    assert sig is not None
    assert sig.raw_payload["upside_pct"] == 60.0
    assert sig.raw_payload["downside_pct"] == 40.0


def test_build_signal_falls_back_to_legacy_magnitude_when_no_snapshot(fake_client):
    # fake_client fixture already stubs load_market_snapshot to return None.
    # No mcap → legacy 50/35 default.
    entry = _entry(ticker="UNKWN", drug_name="someDrug")
    sig = _build_signal(entry, days=45, scan_date=_scan_date(),
                       issuer_figi=None, client=fake_client)
    assert sig is not None
    assert sig.raw_payload["upside_pct"] == 50.0
    assert sig.raw_payload["downside_pct"] == 35.0
