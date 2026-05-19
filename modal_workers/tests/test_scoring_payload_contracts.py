from __future__ import annotations

from datetime import datetime, timezone

from modal_workers.scanners.courtlistener_scanner import _docket_to_signal
from modal_workers.scanners.fda_pdufa_pipeline import _build_signal as build_pdufa_signal
from modal_workers.scanners.pre_phase3_readout_scanner import _build_signal as build_prephase3_signal
from modal_workers.scanners.sec_enforcement_scanner import _build_signal as build_sec_signal


def test_courtlistener_signal_includes_structured_stage_hints():
    sig = _docket_to_signal(
        {
            "caseName": "Acme Holdings v. BigCo",
            "_nos_queried": "410",
            "court_id": "nysd",
            "dateFiled": "2026-04-20",
            "id": 123,
        },
        scan_date=datetime(2026, 4, 21, tzinfo=timezone.utc),
        issuer_index=None,
        cfg_overrides={},
    )

    assert sig is not None
    raw = sig.raw_payload
    assert raw["case_family"] == "antitrust"
    assert raw["procedural_stage"] == "complaint_filed"
    assert raw["procedural_stage_confidence"] == "high"
    assert raw["resolution_timeline_bucket"] == ">12m"


def test_sec_enforcement_signal_includes_structured_stage_hints():
    sig = build_sec_signal(
        "admin",
        {
            "title": "SEC Announces Cease-and-Desist Against Acme Corp (ACME)",
            "link": "https://example.com/sec",
            "description": "desc",
            "pub_date": "Tue, 22 Apr 2026 00:00:00 GMT",
            "release_id": "34-12345",
        },
        datetime(2026, 4, 22, tzinfo=timezone.utc),
    )

    assert sig is not None
    raw = sig.raw_payload
    assert raw["case_family"] == "sec_admin"
    assert raw["procedural_stage"] == "cease_and_desist"
    assert raw["procedural_stage_confidence"] == "high"
    assert raw["ticker_hint_present"] is True


def test_pre_phase3_signal_includes_structured_trial_flags():
    sig = build_prephase3_signal(
        {
            "nct_id": "NCT123",
            "brief_title": "Acme Phase 3 study",
            "sponsor_name": "Acme Therapeutics",
            "sponsor_class": "INDUSTRY",
            "status": "ACTIVE_NOT_RECRUITING",
            "primary_completion_date": "2026-05-10",
            "days_until_readout": 19,
            "enrollment": 420,
            "conditions": ["oncology"],
            "primary_outcomes": ["Overall survival"],
            "base_rate_key": "oncology_solid_tumor",
            "base_rate_approval": 0.72,
            "matched_indications": ["oncology_solid_tumor"],
            "patterns_hit": 4,
            "pattern_names": ["single_primary_endpoint", "industry_sponsored"],
        },
        datetime(2026, 4, 21, tzinfo=timezone.utc),
    )

    assert sig is not None
    raw = sig.raw_payload
    assert raw["days_until_readout"] == 19
    assert raw["single_primary_endpoint"] is True
    assert raw["industry_sponsored"] is True
    assert raw["meaningful_enrollment"] is True
    assert raw["matched_indications"] == ["oncology_solid_tumor"]


def test_pdufa_signal_includes_structured_binary_hints(monkeypatch):
    # Keep the test hermetic: _build_signal calls load_market_snapshot when
    # ticker is non-empty, which would otherwise hit yfinance over the network.
    from modal_workers.shared import market_snapshot
    monkeypatch.setattr(market_snapshot, "load_market_snapshot", lambda ticker, **kw: None)

    sig = build_pdufa_signal(
        {
            "ticker": "AXSM",
            "company_name": "Axsome Therapeutics",
            "drug_name": "DrugX",
            "indication": "CNS",
            "pdufa_date": "2026-05-01",
            "previous_pdufa_date": None,
            "pdufa_date_change_kind": None,
            "pdufa_date_changed_at": None,
            "nda_type": "NDA",
            "application_number": "123456",
            "phase3_nctid": "NCT999",
            "is_resubmission": False,
            "adcom_date": "2026-04-25",
            "adcom_vote": "10-2",
            "crl_date": None,
            "status": "active",
            "notes": "",
            "enrichment": {
                "trial": {"status": "COMPLETED"},
                "trials": [{"status": "COMPLETED"}],
                "fda_history": [{"submissions": [{"status": "AP"}]}],
            },
        },
        10,
        datetime(2026, 4, 21, tzinfo=timezone.utc),
        issuer_figi=None,
        client=None,
    )

    assert sig is not None
    raw = sig.raw_payload
    assert raw["adcom_support_ratio"] == 10 / 12
    assert raw["trial_status"] == "COMPLETED"
    assert raw["approval_history_count"] == 1
