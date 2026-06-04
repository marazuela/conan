"""Unit tests for the binary-catalyst pre-gate input computation."""

from __future__ import annotations

import modal_workers.shared.bc_pregate_inputs as bpi
from modal_workers.shared.bc_pregate_inputs import (
    count_sponsor_prior_nda,
    first_time_sponsor,
    parse_designation_flags,
)


# --- parse_designation_flags -------------------------------------------------

def test_breakthrough_and_priority_both_detected():
    facts = [{"fact_text": "Veligrotug received both Breakthrough Therapy "
                            "Designation and Priority Review from FDA in 2025.",
              "confidence": 0.97}]
    assert parse_designation_flags(facts) == {
        "priority_review": True, "breakthrough_designation": True}


def test_breakthrough_only():
    facts = [{"fact_text": "FDA granted Breakthrough Therapy designation to the drug.",
              "confidence": 0.98}]
    flags = parse_designation_flags(facts)
    assert flags["breakthrough_designation"] is True
    assert flags["priority_review"] is False


def test_low_confidence_fact_ignored():
    facts = [{"fact_text": "Drug may receive Priority Review.", "confidence": 0.40}]
    assert parse_designation_flags(facts)["priority_review"] is False


def test_negation_not_counted():
    facts = [
        {"fact_text": "The company did not receive Breakthrough Therapy designation.",
         "confidence": 0.95},
        {"fact_text": "FDA denied Priority Review for the application.",
         "confidence": 0.95},
    ]
    assert parse_designation_flags(facts) == {
        "priority_review": False, "breakthrough_designation": False}


def test_eu_orphan_does_not_set_fda_flags():
    # An EMA orphan fact must not flip breakthrough/priority (those are FDA terms).
    facts = [{"fact_text": "European Medicines Agency granted Orphan Drug designation.",
              "confidence": 0.98}]
    assert parse_designation_flags(facts) == {
        "priority_review": False, "breakthrough_designation": False}


def test_empty_and_malformed_confidence():
    facts = [
        {"fact_text": "Breakthrough Therapy designation granted.", "confidence": None},
        {"fact_text": "Priority Review granted.", "confidence": "not-a-number"},
    ]
    # confidence None/garbage -> treated as 0 -> below threshold -> ignored
    assert parse_designation_flags(facts) == {
        "priority_review": False, "breakthrough_designation": False}


# --- count_sponsor_prior_nda / first_time_sponsor ----------------------------

def test_sponsor_count_distinct_applications(monkeypatch):
    def fake_get(path, params=None, **kw):
        assert path == "drug/drugsfda.json"
        assert 'sponsor_name:"PFIZER"' in params["search"]
        return {"results": [
            {"application_number": "NDA019440"},
            {"application_number": "NDA020753"},
            {"application_number": "NDA019440"},  # dup collapses
            {"sponsor_name": "noise-without-appl"},
        ]}
    monkeypatch.setattr(bpi, "openfda_get", fake_get)
    assert count_sponsor_prior_nda("PFIZER") == 2


def test_sponsor_404_is_zero_first_time(monkeypatch):
    monkeypatch.setattr(bpi, "openfda_get", lambda *a, **k: None)  # 404
    assert count_sponsor_prior_nda("VERA THERAPEUTICS") == 0
    assert first_time_sponsor(0) is True


def test_sponsor_lookup_failure_is_unknown(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("openfda 503")
    monkeypatch.setattr(bpi, "openfda_get", boom)
    assert count_sponsor_prior_nda("X") is None
    assert first_time_sponsor(None) is None


def test_empty_sponsor_name_is_unknown():
    assert count_sponsor_prior_nda("") is None
    assert count_sponsor_prior_nda(None) is None
    assert count_sponsor_prior_nda('  "  ') is None


def test_first_time_sponsor_thresholds():
    assert first_time_sponsor(0) is True
    assert first_time_sponsor(1) is False
    assert first_time_sponsor(5) is False
    assert first_time_sponsor(None) is None
