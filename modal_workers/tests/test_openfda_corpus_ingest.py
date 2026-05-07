"""Tests for the v3 RAG openFDA corpus ingest path.

Covers the two weaknesses that this change fixes:
  1. Page-cap silent truncation — old code stopped after max_pages=5 (drugsfda)
     or max_pages=10 (label) regardless of how many records remained.
  2. No backfill catch-up — default 30d window cannot see corrections to older
     records.

Plus the scanner shim's mode resolution and signal-less ScannerResult shape.

All tests mock _openfda_get and DocumentWriter — no live network or DB calls.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("ANTHROPIC_ORCHESTRATOR_KEY", "x")

from modal_workers.ingestion import openfda_ingest
from modal_workers.ingestion.openfda_ingest import (
    DEEP_SWEEP_DAYS,
    MAX_PAGES_HARD_CAP,
    deep_sweep_openfda,
    ingest_drug_label_recent,
    ingest_drugsfda_approvals,
)
from modal_workers.scanners import openfda_corpus_ingest
from modal_workers.shared.document_writer import WriteResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drugsfda_record(application_number: str = "NDA000001",
                     status_date: str = "20260501") -> dict:
    return {
        "application_number": application_number,
        "sponsor_name": "Test Pharma",
        "products": [{"brand_name": "TESTDRUG"}],
        "submissions": [{
            "submission_status_date": status_date,
            "submission_status": "AP",
            "submission_type": "ORIG",
            "submission_number": "1",
        }],
    }


def _label_record(set_id: str = "set_001",
                  effective_time: str = "20260501") -> dict:
    return {
        "set_id": set_id,
        "version": "1",
        "effective_time": effective_time,
        "openfda": {"brand_name": ["TESTLABEL"]},
        "indications_and_usage": "for testing.",
    }


def _make_mock_writer(was_new: bool = True) -> MagicMock:
    writer = MagicMock()
    counter = {"n": 0}

    def _write_document(**kwargs):
        counter["n"] += 1
        return WriteResult(
            document_id=f"doc-{counter['n']}",
            was_new=was_new,
            storage_path=None,
            anthropic_file_id=None,
        )

    writer.write_document.side_effect = _write_document
    return writer


def _paged_responses(*pages):
    """Return a side_effect callable that yields each page in sequence then
    None (404 / no more results) thereafter."""
    queue = list(pages)

    def _get(path, params, **_):
        if not queue:
            return None
        return queue.pop(0)

    return _get


# ---------------------------------------------------------------------------
# Page-until-empty (the core fix)
# ---------------------------------------------------------------------------


def test_drugsfda_walks_past_old_5_page_cap():
    """Old code capped at max_pages=5. New code must keep paging until a
    short page arrives. Mock 6 full pages (600 records) then one half page."""
    writer = _make_mock_writer()
    # 6 full pages of 100 records + 1 page of 50.
    pages = []
    for page_idx in range(6):
        pages.append({"results": [
            _drugsfda_record(f"NDA{page_idx:03d}{i:03d}") for i in range(100)
        ]})
    pages.append({"results": [
        _drugsfda_record(f"NDA999{i:03d}") for i in range(50)
    ]})

    with patch.object(openfda_ingest, "_openfda_get",
                      side_effect=_paged_responses(*pages)):
        r = ingest_drugsfda_approvals(writer=writer)

    assert r.documents_seen == 650, (
        f"old 5-page cap would yield 500; got {r.documents_seen}")
    assert writer.write_document.call_count == 650


def test_label_walks_past_old_10_page_cap():
    """Old code capped at max_pages=10. Mock 11 full pages (1100 records)."""
    writer = _make_mock_writer()
    pages = []
    for page_idx in range(11):
        pages.append({"results": [
            _label_record(f"set_{page_idx:02d}_{i:03d}") for i in range(100)
        ]})
    pages.append({"results": [_label_record("set_tail_001")]})

    with patch.object(openfda_ingest, "_openfda_get",
                      side_effect=_paged_responses(*pages)):
        r = ingest_drug_label_recent(writer=writer)

    assert r.documents_seen == 1101, (
        f"old 10-page cap would yield 1000; got {r.documents_seen}")


def test_explicit_max_pages_still_caps_for_backfill_callers():
    """backfill_document_set.py passes max_pages=2; that contract must still
    hold so a backfill of one application doesn't accidentally rip through
    the entire window."""
    writer = _make_mock_writer()
    pages = [
        {"results": [_drugsfda_record(f"P0{i:03d}") for i in range(100)]},
        {"results": [_drugsfda_record(f"P1{i:03d}") for i in range(100)]},
        {"results": [_drugsfda_record(f"P2{i:03d}") for i in range(100)]},  # never read
    ]

    with patch.object(openfda_ingest, "_openfda_get",
                      side_effect=_paged_responses(*pages)):
        r = ingest_drugsfda_approvals(max_pages=2, writer=writer)

    assert r.documents_seen == 200


# ---------------------------------------------------------------------------
# Hard cap safety net
# ---------------------------------------------------------------------------


def test_safety_cap_terminates_runaway_loop_with_warning(caplog):
    """If the API keeps returning full pages forever, the loop must exit at
    MAX_PAGES_HARD_CAP and emit a warning so on-call notices the truncation."""
    writer = _make_mock_writer()

    def _always_full_page(path, params, **_):
        return {"results": [_drugsfda_record(f"X{params.get('skip', 0)}-{i}")
                            for i in range(100)]}

    with caplog.at_level(logging.WARNING, logger="modal_workers.ingestion.openfda_ingest"):
        with patch.object(openfda_ingest, "_openfda_get",
                          side_effect=_always_full_page):
            r = ingest_drugsfda_approvals(writer=writer)

    assert r.documents_seen == MAX_PAGES_HARD_CAP * 100
    assert any("MAX_PAGES_HARD_CAP" in rec.message for rec in caplog.records), (
        f"expected MAX_PAGES_HARD_CAP warning; got: "
        f"{[r.message for r in caplog.records]}")


# ---------------------------------------------------------------------------
# deep_sweep_openfda widens the since window to 180d
# ---------------------------------------------------------------------------


def test_deep_sweep_uses_180d_since_in_query():
    """deep_sweep_openfda must build a `since` cutoff of today - 180d for both
    drugsfda and dailymed label searches."""
    seen_searches: list[str] = []

    def _capture(path, params, **_):
        seen_searches.append(params.get("search", ""))
        return None  # treat as no-results so the loop exits cleanly

    writer = _make_mock_writer()
    with patch.object(openfda_ingest, "_openfda_get", side_effect=_capture):
        deep_sweep_openfda(writer=writer)

    assert len(seen_searches) >= 2, "expected at least one call per feed"
    expected_since = (date.today() -
                      __import__("datetime").timedelta(days=DEEP_SWEEP_DAYS))
    drugsfda_iso = expected_since.isoformat()
    label_yyyymmdd = expected_since.strftime("%Y%m%d")
    assert any(drugsfda_iso in s for s in seen_searches), (
        f"drugsfda search should contain {drugsfda_iso}; saw {seen_searches}")
    assert any(label_yyyymmdd in s for s in seen_searches), (
        f"label search should contain {label_yyyymmdd}; saw {seen_searches}")


# ---------------------------------------------------------------------------
# Idempotency — re-runs on same data write zero new
# ---------------------------------------------------------------------------


def test_rerun_on_same_window_writes_zero_new_documents():
    """DocumentWriter dedup must let us re-run a wider window without dupes.
    Mock writer flips was_new=False on every call to simulate the conflict path."""
    writer = _make_mock_writer(was_new=False)
    pages = [
        {"results": [_drugsfda_record(f"NDA{i:03d}") for i in range(50)]},
    ]

    with patch.object(openfda_ingest, "_openfda_get",
                      side_effect=_paged_responses(*pages)):
        r = ingest_drugsfda_approvals(writer=writer)

    assert r.documents_seen == 50
    assert r.documents_written == 0
    assert r.documents_dedup_hit == 50


# ---------------------------------------------------------------------------
# Scanner shim — mode resolution + signal-less ScannerResult
# ---------------------------------------------------------------------------


def test_resolve_mode_env_override_wins():
    with patch.dict(os.environ, {"OPENFDA_INGEST_MODE": "deep"}):
        # Tuesday — would normally be shallow.
        tuesday = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        assert openfda_corpus_ingest._resolve_mode(tuesday) == "deep"


def test_resolve_mode_sunday_auto_deep():
    env = {k: v for k, v in os.environ.items() if k != "OPENFDA_INGEST_MODE"}
    with patch.dict(os.environ, env, clear=True):
        # 2026-05-10 is a Sunday.
        sunday = datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc)
        assert openfda_corpus_ingest._resolve_mode(sunday) == "deep"


def test_resolve_mode_weekday_default_shallow():
    env = {k: v for k, v in os.environ.items() if k != "OPENFDA_INGEST_MODE"}
    with patch.dict(os.environ, env, clear=True):
        # 2026-05-07 is a Thursday.
        thursday = datetime(2026, 5, 7, 6, 0, tzinfo=timezone.utc)
        assert openfda_corpus_ingest._resolve_mode(thursday) == "shallow"


def test_scan_emits_no_signals_and_carries_run_metrics():
    """Shim must return ScannerResult(signals=[]) with mode + per-feed counts
    in run_metrics so the scanner_runs row preserves them."""
    fake_drugsfda = openfda_ingest.IngestRunResult(
        documents_seen=12, documents_written=10, documents_dedup_hit=2)
    fake_label = openfda_ingest.IngestRunResult(
        documents_seen=7, documents_written=7)

    cfg = MagicMock()
    env = {k: v for k, v in os.environ.items() if k != "OPENFDA_INGEST_MODE"}
    with patch.dict(os.environ, env, clear=True), \
         patch.object(openfda_corpus_ingest, "ingest_drugsfda_approvals",
                      return_value=fake_drugsfda), \
         patch.object(openfda_corpus_ingest, "ingest_drug_label_recent",
                      return_value=fake_label), \
         patch("modal_workers.scanners.openfda_corpus_ingest.datetime") as dt_mock:
        dt_mock.now.return_value = datetime(2026, 5, 7, 6, 0, tzinfo=timezone.utc)
        result = openfda_corpus_ingest.scan(cfg)

    assert result.scanner == "openfda_corpus_ingest"
    assert result.status == "ok"
    assert result.signals == []
    assert result.fetched_records == 19
    assert result.run_metrics["mode"] == "shallow"
    assert result.run_metrics["documents_written_total"] == 17
    assert result.run_metrics["feeds"]["drugsfda"]["documents_seen"] == 12
    assert result.run_metrics["feeds"]["label"]["documents_seen"] == 7


def test_scan_routes_to_deep_sweep_on_sunday():
    fake_results = {
        "drugsfda": openfda_ingest.IngestRunResult(documents_seen=100,
                                                   documents_written=80),
        "label": openfda_ingest.IngestRunResult(documents_seen=200,
                                                documents_written=150),
    }
    cfg = MagicMock()
    env = {k: v for k, v in os.environ.items() if k != "OPENFDA_INGEST_MODE"}
    with patch.dict(os.environ, env, clear=True), \
         patch.object(openfda_corpus_ingest, "deep_sweep_openfda",
                      return_value=fake_results) as deep_mock, \
         patch("modal_workers.scanners.openfda_corpus_ingest.datetime") as dt_mock:
        dt_mock.now.return_value = datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc)  # Sunday
        result = openfda_corpus_ingest.scan(cfg)

    deep_mock.assert_called_once_with(days=DEEP_SWEEP_DAYS)
    assert result.run_metrics["mode"] == "deep"
    assert result.fetched_records == 300
    assert result.signals == []
