"""Tests for the already-approved-drug filter added to pre_phase3_readout_scanner
(2026-04-27 fix for the AbbVie / Sanofi MenQuadfi / AstraZeneca DLQ batch).

Coverage:
  - sponsor normalisation drops corporate-suffix noise tokens
  - sponsor matching is conservative (substring after noise strip)
  - intervention extraction filters placebos and non-drug intervention types
  - Orange Book hit + sponsor match -> trial dropped
  - Orange Book hit but DIFFERENT sponsor -> trial NOT dropped
  - Orange Book results all non-AP -> trial NOT dropped
  - openFDA HTTP error -> fail open (warning attached, signal still emitted)
  - End-to-end scan() drops the approved trial and emits the unapproved one,
    with run_metrics.skipped_already_approved == 1
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
import requests

from modal_workers.scanners import pre_phase3_readout_scanner as scanner
from modal_workers.shared.scanner_base import ScannerResult


SCAN_DATE = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Sponsor normalisation + matching
# ---------------------------------------------------------------------------

class TestSponsorMatching:
    def test_strips_corp_suffixes(self):
        assert scanner._normalize_sponsor("Sanofi Pasteur Inc.") == "sanofi pasteur"
        assert scanner._normalize_sponsor("AbbVie Inc.") == "abbvie"
        assert scanner._normalize_sponsor("AstraZeneca AB") == "astrazeneca"

    def test_matches_parent_subsidiary(self):
        assert scanner._sponsor_matches("Sanofi Pasteur", "Sanofi Pasteur Inc.")
        assert scanner._sponsor_matches("Sanofi", "Sanofi Pasteur Inc.")
        assert scanner._sponsor_matches("AbbVie", "AbbVie Inc.")

    def test_rejects_unrelated_sponsors(self):
        assert not scanner._sponsor_matches("Pfizer Inc.", "Hospira Inc.")
        assert not scanner._sponsor_matches("Merck & Co.", "GlaxoSmithKline plc")

    def test_empty_inputs_never_match(self):
        assert not scanner._sponsor_matches("", "Sanofi Inc.")
        assert not scanner._sponsor_matches("Sanofi Inc.", "")


# ---------------------------------------------------------------------------
# Intervention extraction
# ---------------------------------------------------------------------------

class TestInterventionExtraction:
    def test_keeps_drug_and_biological(self):
        out = scanner._extract_drug_interventions([
            {"name": "Adalimumab", "type": "DRUG"},
            {"name": "MenQuadfi", "type": "BIOLOGICAL"},
        ])
        assert [i["name"] for i in out] == ["Adalimumab", "MenQuadfi"]

    def test_drops_placebo_and_sham(self):
        out = scanner._extract_drug_interventions([
            {"name": "Placebo", "type": "DRUG"},
            {"name": "Matching Placebo", "type": "DRUG"},
            {"name": "Sham injection", "type": "DRUG"},
            {"name": "Standard of Care", "type": "DRUG"},
        ])
        assert out == []

    def test_drops_device_and_behavioral(self):
        out = scanner._extract_drug_interventions([
            {"name": "Some Device", "type": "DEVICE"},
            {"name": "CBT therapy", "type": "BEHAVIORAL"},
            {"name": "Adalimumab", "type": "DRUG"},
        ])
        assert [i["name"] for i in out] == ["Adalimumab"]

    def test_keeps_drug_when_type_is_missing(self):
        # Some CT.gov records omit the type. Accept rather than over-filter.
        out = scanner._extract_drug_interventions([{"name": "Foobarinib"}])
        assert [i["name"] for i in out] == ["Foobarinib"]


# ---------------------------------------------------------------------------
# Lead-drug-name picker (feeds auto_seed_fda_asset hint)
# ---------------------------------------------------------------------------

class TestPickLeadDrugName:
    def test_strips_short_route_prefix(self):
        # "IV Tulisokibart" / "SC Tulisokibart" both seen in CT.gov data.
        assert scanner._pick_lead_drug_name(
            [{"name": "IV Tulisokibart", "type": "DRUG"}]
        ) == "Tulisokibart"
        assert scanner._pick_lead_drug_name(
            [{"name": "SC Tulisokibart", "type": "DRUG"}]
        ) == "Tulisokibart"
        assert scanner._pick_lead_drug_name(
            [{"name": "PO Foobarinib", "type": "DRUG"}]
        ) == "Foobarinib"

    def test_returns_first_intervention(self):
        # Caller is responsible for filtering placebos; picker just takes the
        # first usable name.
        assert scanner._pick_lead_drug_name([
            {"name": "Retatrutide", "type": "DRUG"},
            {"name": "Semaglutide", "type": "DRUG"},
        ]) == "Retatrutide"

    def test_does_not_strip_long_route_word(self):
        # "Topical" is often part of the brand name (e.g. "Patidegib Topical
        # Gel"); only short 2-3 char abbreviations get stripped.
        assert scanner._pick_lead_drug_name(
            [{"name": "Patidegib Topical Gel", "type": "DRUG"}]
        ) == "Patidegib Topical Gel"

    def test_empty_returns_none(self):
        assert scanner._pick_lead_drug_name([]) is None
        assert scanner._pick_lead_drug_name([{"name": "", "type": "DRUG"}]) is None
        assert scanner._pick_lead_drug_name(None) is None


# ---------------------------------------------------------------------------
# Orange Book lookup helpers
# ---------------------------------------------------------------------------

def _stub_response(status_code: int = 200,
                   json_body: Optional[Dict[str, Any]] = None,
                   raises: Optional[Exception] = None):
    if raises is not None:
        def _get(*_a, **_kw):
            raise raises
        return _get
    body = json_body or {"results": []}

    def _get(*_a, **_kw):
        m = MagicMock()
        m.status_code = status_code
        m.json.return_value = body
        return m
    return _get


def _approved_response(drug_name: str, sponsor: str,
                       application_number: str = "BLA125709",
                       approval_date: str = "2020-04-24"):
    return {
        "results": [{
            "application_number": application_number,
            "sponsor_name": sponsor,
            "openfda": {"brand_name": [drug_name]},
            "submissions": [
                {"submission_type": "ORIG", "submission_status": "AP",
                 "submission_status_date": approval_date},
            ],
        }],
    }


def _no_ap_response(drug_name: str, sponsor: str):
    return {
        "results": [{
            "application_number": "NDA000000",
            "sponsor_name": sponsor,
            "submissions": [
                {"submission_type": "ORIG", "submission_status": "TA",  # tentative
                 "submission_status_date": "2024-01-01"},
            ],
        }],
    }


class TestIsAlreadyApproved:
    def test_match_when_sponsor_and_status_align(self, monkeypatch):
        monkeypatch.setattr(
            scanner.requests, "get",
            _stub_response(json_body=_approved_response("MenQuadfi", "Sanofi Pasteur Inc.")),
        )
        match, err = scanner._is_already_approved(
            "MenQuadfi", "Sanofi Pasteur", client=None,
        )
        assert err is None
        assert match is not None
        assert match["drug_name"] == "MenQuadfi"
        assert match["application_number"] == "BLA125709"
        assert match["fda_sponsor_name"] == "Sanofi Pasteur Inc."

    def test_no_match_when_sponsor_differs(self, monkeypatch):
        # Same drug name approved, but to a competitor — don't drop.
        monkeypatch.setattr(
            scanner.requests, "get",
            _stub_response(json_body=_approved_response("Generic-X", "OtherPharma Inc.")),
        )
        match, err = scanner._is_already_approved("Generic-X", "Sanofi", client=None)
        assert err is None
        assert match is None

    def test_no_match_when_no_ap_submission(self, monkeypatch):
        # Sponsor matches, but the application is only tentatively approved.
        monkeypatch.setattr(
            scanner.requests, "get",
            _stub_response(json_body=_no_ap_response("FoobarX", "Sanofi Inc.")),
        )
        match, err = scanner._is_already_approved("FoobarX", "Sanofi", client=None)
        assert err is None
        assert match is None

    def test_404_treated_as_no_match(self, monkeypatch):
        monkeypatch.setattr(
            scanner.requests, "get",
            _stub_response(status_code=404, json_body={}),
        )
        match, err = scanner._is_already_approved("Newmolecule", "Sponsor", client=None)
        assert err is None
        assert match is None

    def test_http_error_returns_warning(self, monkeypatch):
        monkeypatch.setattr(
            scanner.requests, "get",
            _stub_response(raises=requests.exceptions.Timeout("timed out")),
        )
        match, err = scanner._is_already_approved("Drug", "Sponsor", client=None)
        assert match is None
        assert err is not None
        assert "Timeout" in err

    def test_short_name_skipped(self, monkeypatch):
        # Avoid over-broad openFDA queries on 1-2 char "names".
        called = MagicMock()
        monkeypatch.setattr(scanner.requests, "get", called)
        match, err = scanner._is_already_approved("ab", "Sponsor", client=None)
        assert match is None
        assert err is None
        called.assert_not_called()


class TestCheckTrialDrugApproved:
    def test_returns_first_matching_intervention(self, monkeypatch):
        # Placebo arms are already stripped by _extract_drug_interventions
        # before this helper sees them.
        scored = {
            "sponsor_name": "Sanofi",
            "interventions": [
                {"name": "MenQuadfi", "type": "BIOLOGICAL"},
            ],
        }
        monkeypatch.setattr(
            scanner.requests, "get",
            _stub_response(json_body=_approved_response("MenQuadfi", "Sanofi Pasteur Inc.")),
        )
        match, warnings = scanner._check_trial_drug_approved(scored, client=None)
        assert match is not None
        assert match["drug_name"] == "MenQuadfi"
        assert warnings == []

    def test_no_match_for_truly_novel_drug(self, monkeypatch):
        scored = {
            "sponsor_name": "BioNewCo",
            "interventions": [{"name": "BNC-001", "type": "DRUG"}],
        }
        monkeypatch.setattr(
            scanner.requests, "get",
            _stub_response(status_code=404, json_body={}),
        )
        match, warnings = scanner._check_trial_drug_approved(scored, client=None)
        assert match is None
        assert warnings == []

    def test_lookup_error_collected_as_warning_not_drop(self, monkeypatch):
        scored = {
            "sponsor_name": "Sponsor",
            "interventions": [{"name": "Drugname", "type": "DRUG"}],
        }
        monkeypatch.setattr(
            scanner.requests, "get",
            _stub_response(raises=requests.exceptions.ConnectionError("nope")),
        )
        match, warnings = scanner._check_trial_drug_approved(scored, client=None)
        assert match is None
        assert any("ConnectionError" in w for w in warnings)


# ---------------------------------------------------------------------------
# End-to-end scan() — verifies the filter integrates into the pipeline
# ---------------------------------------------------------------------------

def _mk_trial(*, nct_id: str, sponsor: str,
              intervention_name: str, intervention_type: str = "BIOLOGICAL",
              condition: str = "Influenza",
              enrollment: int = 1500,
              status: str = "COMPLETED",
              primary_completion: str = "2026-05-15",
              brief_title: Optional[str] = None) -> Dict[str, Any]:
    return {
        "protocolSection": {
            "identificationModule": {
                "nctId": nct_id,
                "briefTitle": brief_title or f"Phase 3 study of {intervention_name}",
            },
            "statusModule": {
                "overallStatus": status,
                "primaryCompletionDateStruct": {"date": primary_completion},
            },
            "designModule": {"enrollmentInfo": {"count": enrollment}},
            "sponsorCollaboratorsModule": {
                "leadSponsor": {"name": sponsor, "class": "INDUSTRY"},
            },
            "conditionsModule": {"conditions": [condition]},
            "outcomesModule": {
                "primaryOutcomes": [{"measure": "Primary endpoint X"}],
            },
            "armsInterventionsModule": {
                "interventions": [
                    {"name": intervention_name, "type": intervention_type},
                ],
            },
        },
    }


@pytest.fixture
def fake_supabase(monkeypatch):
    """Replace SupabaseClient() with a MagicMock that returns no cached data
    and accepts cache writes silently. Keeps scan() runnable in unit tests."""
    fake = MagicMock()
    fake.read_cache.return_value = None
    fake.write_cache.return_value = None
    fake.openfigi_cache_backend.return_value = (lambda *_a, **_kw: None,
                                                lambda *_a, **_kw: None)
    monkeypatch.setattr(scanner, "SupabaseClient", lambda: fake)
    monkeypatch.setattr(scanner, "_load_base_rates", lambda _c: {"default": 0.58})
    return fake


def test_scan_drops_already_approved_emits_novel(monkeypatch, fake_supabase):
    """Two trials enter the pipeline; only the one with a novel drug emits."""
    sanofi_trial = _mk_trial(
        nct_id="NCT99990001",
        sponsor="Sanofi Pasteur",
        intervention_name="MenQuadfi",
    )
    novel_trial = _mk_trial(
        nct_id="NCT99990002",
        sponsor="BioNewCo",
        intervention_name="BNC-1234",
    )

    monkeypatch.setattr(
        scanner, "_fetch_phase3_readout_trials",
        lambda budget_s, scanner_cache_client=None: ([sanofi_trial, novel_trial], []),
    )

    # openFDA: MenQuadfi -> approved by Sanofi; BNC-1234 -> 404.
    def fake_get(url, params=None, headers=None, timeout=None):
        m = MagicMock()
        search = (params or {}).get("search", "")
        if "MenQuadfi" in search:
            m.status_code = 200
            m.json.return_value = _approved_response("MenQuadfi", "Sanofi Pasteur Inc.")
        else:
            m.status_code = 404
            m.json.return_value = {}
        return m

    monkeypatch.setattr(scanner.requests, "get", fake_get)

    cfg = MagicMock()
    cfg.timeout_soft_s = 60
    result: ScannerResult = scanner.scan(cfg)

    emitted_ncts = [s.raw_payload["nct_id"] for s in result.signals]
    assert emitted_ncts == ["NCT99990002"]
    assert result.run_metrics["skipped_already_approved"] == 1
    assert any("NCT99990001" in w and "MenQuadfi" in w for w in result.warnings)


def test_scan_does_not_drop_when_orange_book_lookup_fails(monkeypatch, fake_supabase):
    """If openFDA is unreachable, fail open: emit the signal with a warning."""
    trial = _mk_trial(
        nct_id="NCT99990003",
        sponsor="Sanofi",
        intervention_name="MysteryDrug",
    )
    monkeypatch.setattr(
        scanner, "_fetch_phase3_readout_trials",
        lambda budget_s, scanner_cache_client=None: ([trial], []),
    )

    def fake_get(*_a, **_kw):
        raise requests.exceptions.ConnectionError("openfda down")

    monkeypatch.setattr(scanner.requests, "get", fake_get)

    cfg = MagicMock()
    cfg.timeout_soft_s = 60
    result: ScannerResult = scanner.scan(cfg)

    assert len(result.signals) == 1
    assert result.signals[0].raw_payload["nct_id"] == "NCT99990003"
    assert result.run_metrics["skipped_already_approved"] == 0
    assert any("orange_book" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# auto_seed_fda_asset hint — drives the SQL trigger that creates stub fda_assets
# ---------------------------------------------------------------------------

def _mk_scored(*, sponsor: str = "Acme Bio", base_rate_key: str = "metabolic_diabetes",
               interventions: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """Build a minimal `scored` dict matching _score_trial's output, just enough
    for _build_signal to consume."""
    return {
        "nct_id": "NCT12345678",
        "brief_title": "Phase 3 study of widgetinib",
        "sponsor_name": sponsor,
        "sponsor_class": "INDUSTRY",
        "status": "COMPLETED",
        "primary_completion_date": "2026-08-01",
        "days_until_readout": 90,
        "enrollment": 800,
        "conditions": ["Type 2 Diabetes"],
        "interventions": interventions if interventions is not None else [
            {"name": "IV Widgetinib", "type": "DRUG"},
        ],
        "primary_outcomes": ["A1C reduction at 24 weeks"],
        "base_rate_key": base_rate_key,
        "base_rate_approval": 0.55,
        "matched_indications": ["diabetes|type 2 DM|T2DM"],
        "patterns_hit": 4,
        "pattern_names": ["industry_sponsored", "single_primary_endpoint",
                          "enrollment_complete_readout_imminent", "meaningful_enrollment"],
    }


class TestAutoSeedFdaAssetHint:
    def test_hint_present_when_ticker_resolves(self, monkeypatch):
        """When SEC issuer match resolves a ticker, the signal carries an
        auto_seed_fda_asset payload that the SQL trigger consumes."""
        idx = MagicMock()
        idx.resolve.return_value = scanner.IssuerMatch(
            ticker="ABCD", cik="0001234567", title="Acme Bio Inc.",
            match_kind="exact",
        )
        # openfigi is best-effort; force it to no-op.
        monkeypatch.setattr(
            "modal_workers.shared.openfigi_resolver.resolve_ticker",
            lambda *_a, **_kw: MagicMock(resolved=False, issuer_figi=None, mic=None),
        )

        sig = scanner._build_signal(
            _mk_scored(),
            scan_date=SCAN_DATE,
            issuer_index=idx,
        )
        assert sig is not None
        hint = sig.raw_payload.get("auto_seed_fda_asset")
        assert hint is not None
        assert hint["ticker"] == "ABCD"
        # IV route prefix is stripped.
        assert hint["drug_name"] == "Widgetinib"
        # Indication carries the base_rate_key for the asset_linker scope.
        assert hint["indication"] == "metabolic_diabetes"
        assert hint["nct_id"] == "NCT12345678"
        assert hint["primary_completion_date"] == "2026-08-01"
        # Sponsor uses the SEC-authoritative title when resolved.
        assert hint["sponsor_name"] == "Acme Bio Inc."

    def test_hint_absent_when_ticker_does_not_resolve(self):
        """No SEC issuer match → no hint (the SQL trigger no-ops on absence).
        This is the common path for foreign listings and private biotechs."""
        idx = MagicMock()
        idx.resolve.return_value = None  # SEC universe missed this sponsor

        sig = scanner._build_signal(
            _mk_scored(sponsor="Obscure EU Biotech AG"),
            scan_date=SCAN_DATE,
            issuer_index=idx,
        )
        assert sig is not None
        assert "auto_seed_fda_asset" not in sig.raw_payload

    def test_hint_absent_when_no_drug_intervention(self, monkeypatch):
        """Ticker resolved but interventions all placebo/empty → no hint."""
        idx = MagicMock()
        idx.resolve.return_value = scanner.IssuerMatch(
            ticker="ABCD", cik="0001234567", title="Acme Bio Inc.",
            match_kind="exact",
        )
        monkeypatch.setattr(
            "modal_workers.shared.openfigi_resolver.resolve_ticker",
            lambda *_a, **_kw: MagicMock(resolved=False, issuer_figi=None, mic=None),
        )

        sig = scanner._build_signal(
            _mk_scored(interventions=[]),
            scan_date=SCAN_DATE,
            issuer_index=idx,
        )
        assert sig is not None
        assert "auto_seed_fda_asset" not in sig.raw_payload
