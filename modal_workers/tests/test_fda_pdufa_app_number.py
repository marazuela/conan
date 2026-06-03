"""Tests for the PDUFA watchlist application-number source backfill (no network)."""

from __future__ import annotations

import pytest

from modal_workers.scanners import fda_pdufa_pipeline as pp


def test_appno_type():
    assert pp._appno_type("BLA761360") == "BLA"
    assert pp._appno_type("NDA215358") == "NDA"
    assert pp._appno_type("215358") is None


def test_single_application_resolves(monkeypatch):
    monkeypatch.setattr(pp, "_read_approval_cache",
                        lambda client, name: [{"application_number": "NDA218213", "submissions": []}])
    assert pp._single_application_from_cache(object(), "Augtyro") == {
        "application_number": "NDA218213", "application_type": "NDA"}


def test_multiple_applications_is_ambiguous_skips(monkeypatch):
    monkeypatch.setattr(pp, "_read_approval_cache",
                        lambda client, name: [{"application_number": "NDA1"}, {"application_number": "NDA2"}])
    assert pp._single_application_from_cache(object(), "metformin") is None


def test_empty_cache_skips(monkeypatch):
    monkeypatch.setattr(pp, "_read_approval_cache", lambda client, name: [])
    assert pp._single_application_from_cache(object(), "Augtyro") is None


def test_short_name_skips_without_cache_read(monkeypatch):
    called = {"n": 0}

    def fake(client, name):
        called["n"] += 1
        return [{"application_number": "NDA1"}]

    monkeypatch.setattr(pp, "_read_approval_cache", fake)
    assert pp._single_application_from_cache(object(), "NDA") is None  # cleans to "" -> too short
    assert called["n"] == 0


def test_crosscheck_backfills_pending_entry(monkeypatch):
    # Pending drug: not approved (result None), but openFDA cache has one application.
    monkeypatch.setattr(pp, "_check_fda_approval_status", lambda d, u, c: None)
    monkeypatch.setattr(pp, "_read_approval_cache",
                        lambda client, name: [{"application_number": "NDA218213", "submissions": []}])
    wl = [{"ticker": "TESTX", "drug_name": "Augtyro", "status": "active",
           "pdufa_date": "2026-09-01", "application_number": "", "nda_type": "NDA"}]
    pp._run_approval_crosscheck(wl, "ua", object(), max_checks=10)
    assert wl[0]["application_number"] == "NDA218213"
    assert wl[0]["nda_type"] == "NDA"


def test_crosscheck_does_not_overwrite_existing_appno(monkeypatch):
    monkeypatch.setattr(pp, "_check_fda_approval_status", lambda d, u, c: None)
    monkeypatch.setattr(pp, "_read_approval_cache",
                        lambda client, name: [{"application_number": "NDA999999"}])
    wl = [{"ticker": "TESTX", "drug_name": "Augtyro", "status": "active",
           "pdufa_date": "2026-09-01", "application_number": "NDA218213"}]
    pp._run_approval_crosscheck(wl, "ua", object(), max_checks=10)
    assert wl[0]["application_number"] == "NDA218213"  # unchanged


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
