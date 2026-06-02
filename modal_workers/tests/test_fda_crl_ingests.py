"""Phase-1 fixture-based parsing tests for the CRL feature ingests.

No network: the fetchers' live HTTP is injected (`fetch_raw`) and the Supabase
client is faked, so only the pure parse/normalize/upsert logic is exercised.
"""

from __future__ import annotations

import pytest

from modal_workers.fetchers.universe import fda_inspections, fda_warning_letters
from modal_workers.ingestion.openfda_ingest import extract_submission_rows
from modal_workers.shared.fda_crl import router


class FakeClient:
    def __init__(self):
        self.calls = []

    def _rest_with_retry(self, method, path, *, json_body=None, params=None, prefer=None):
        self.calls.append(
            {"method": method, "path": path, "json_body": json_body,
             "params": params, "prefer": prefer}
        )
        return None


# --------------------------------------------------------------------------
# Ingest 1 — drugsfda submissions extraction (keystone)
# --------------------------------------------------------------------------

_APP = {
    "application_number": "210854",
    "sponsor_name": "GENENTECH INC",
    "submissions": [
        {"submission_type": "ORIG", "submission_number": "1", "submission_status": "AP",
         "submission_status_date": "20181024", "submission_class_code": "TYPE 1",
         "review_priority": "PRIORITY"},
        {"submission_type": "SUPPL", "submission_number": "5", "submission_status": "CR",
         "submission_status_date": "20201123", "submission_class_code": "EFFICACY",
         "submission_class_code_description": "Efficacy", "review_priority": "STANDARD"},
        {"submission_type": "SUPPL", "submission_number": "12", "submission_status": "AP",
         "submission_class_code": "LABELING"},
        {"submission_number": "99"},  # missing submission_type -> skipped
    ],
}


def test_extract_submission_rows_normalizes_and_skips():
    rows = extract_submission_rows(_APP, sponsor_name="GENENTECH INC", ticker="RHHBY")
    assert len(rows) == 3  # the keyless 4th submission is dropped

    orig = next(r for r in rows if r["submission_type"] == "ORIG")
    assert orig["submission_class_code"] == "TYPE 1"
    assert orig["review_priority"] == "PRIORITY"
    assert orig["submission_status_date"] == "2018-10-24"
    assert orig["sponsor_name"] == "GENENTECH INC"
    assert orig["ticker"] == "RHHBY"
    assert orig["application_number"] == "210854"

    eff = next(r for r in rows if r["submission_number"] == "5")
    assert eff["submission_class_code"] == "EFFICACY"

    lab = next(r for r in rows if r["submission_number"] == "12")
    assert lab["submission_status_date"] is None  # missing date tolerated


def test_extract_submission_rows_no_appno_is_empty():
    assert extract_submission_rows({"submissions": [{"submission_type": "ORIG", "submission_number": "1"}]}) == []


def test_extracted_submissions_route_as_expected():
    # Keystone payoff: the structured rows route correctly through the model gate.
    rows = extract_submission_rows(_APP, sponsor_name="GENENTECH INC")
    by_num = {r["submission_number"]: r for r in rows}
    assert router.classify_scope({"application_type": "NDA", **by_num["1"]})["scope"] == router.ORIGINAL
    assert router.classify_scope(by_num["5"])["scope"] == router.EFFICACY_SUPPLEMENT
    assert router.classify_scope(by_num["12"])["scope"] == router.REFUSED  # labeling


# --------------------------------------------------------------------------
# Ingest 2 — inspections
# --------------------------------------------------------------------------


def test_parse_inspection_record_drug_ok():
    row = fda_inspections.parse_inspection_record(
        {"LegalName": "Acme Pharma, Inc.", "FEINumber": "3001234567",
         "InspectionEndDate": "2024-03-15", "ClassificationCode": "OAI",
         "ProductType": "Drugs", "PostedCitations": "Y"}
    )
    assert row is not None
    assert row["firm_name_norm"] == "acme pharma, inc."
    assert row["inspection_end_date"] == "2024-03-15"
    assert row["classification"] == "OAI"
    assert row["posted_citations"] is True
    assert len(row["inspection_id"]) == 32


def test_parse_inspection_record_filters_non_drug_and_missing_firm():
    assert fda_inspections.parse_inspection_record({"LegalName": "X", "ProductType": "Devices"}) is None
    assert fda_inspections.parse_inspection_record({"ProductType": "Drugs"}) is None


def test_parse_inspection_record_id_deterministic():
    rec = {"LegalName": "Acme", "InspectionEndDate": "2024-03-15", "ClassificationCode": "VAI", "ProductType": "Biologics"}
    assert fda_inspections.parse_inspection_record(rec)["inspection_id"] == \
        fda_inspections.parse_inspection_record(dict(rec))["inspection_id"]


def test_inspections_fetch_upserts_and_dedups(monkeypatch):
    monkeypatch.setattr(fda_inspections, "_resolve_ticker", lambda firm: None)
    records = [
        {"LegalName": "Acme Pharma", "InspectionEndDate": "2024-01-01", "ClassificationCode": "OAI", "ProductType": "Drugs"},
        {"LegalName": "Acme Pharma", "InspectionEndDate": "2024-01-01", "ClassificationCode": "OAI", "ProductType": "Drugs"},  # dup
        {"LegalName": "Device Co", "ProductType": "Devices"},  # filtered
        {"ProductType": "Drugs"},  # missing firm
    ]
    client = FakeClient()
    res = fda_inspections.fetch(client, dry_run=False, fetch_raw=lambda _yrs: records)
    assert res == {"parsed": 1, "skipped": 2, "upserted": 1}
    assert len(client.calls) == 1
    assert client.calls[0]["path"] == "fda_drug_inspections"
    assert client.calls[0]["params"]["on_conflict"] == "inspection_id"


def test_inspections_fetch_dry_run_writes_nothing(monkeypatch):
    monkeypatch.setattr(fda_inspections, "_resolve_ticker", lambda firm: None)
    client = FakeClient()
    res = fda_inspections.fetch(
        client, dry_run=True,
        fetch_raw=lambda _yrs: [{"LegalName": "Acme", "ProductType": "Drugs", "InspectionEndDate": "2024-01-01"}],
    )
    assert res["upserted"] == 0
    assert client.calls == []


# --------------------------------------------------------------------------
# Ingest 3 — warning letters
# --------------------------------------------------------------------------


def test_parse_warning_letter_aliases():
    row = fda_warning_letters.parse_warning_letter_record(
        {"companyName": "Beta Bio LLC", "letterIssueDate": "2023-10-20",
         "letterURL": "https://fda.gov/x", "subject": "CGMP"}
    )
    assert row["firm_name_norm"] == "beta bio llc"
    assert row["issue_date"] == "2023-10-20"
    assert row["letter_url"] == "https://fda.gov/x"
    assert len(row["letter_id"]) == 32


def test_parse_warning_letter_missing_firm_none():
    assert fda_warning_letters.parse_warning_letter_record({"issueDate": "2023-01-01"}) is None


def test_warning_letters_fetch_upserts(monkeypatch):
    monkeypatch.setattr(fda_warning_letters, "_resolve_ticker", lambda firm: None)
    client = FakeClient()
    res = fda_warning_letters.fetch(
        client, dry_run=False,
        fetch_raw=lambda _yrs: [
            {"companyName": "Beta Bio", "issueDate": "2023-10-20", "subject": "CGMP"},
            {"issueDate": "2023-01-01"},  # missing firm -> skipped
        ],
    )
    assert res == {"parsed": 1, "skipped": 1, "upserted": 1}
    assert client.calls[0]["path"] == "fda_warning_letters"
