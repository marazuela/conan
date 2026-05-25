"""Phase 3a — tests for the earnings_calendar fetcher.

Focuses on the pure helpers that ship as part of the daily refresh:
ticker normalization, session inference, JSON coercion, and the
fetch() flow with the yfinance helper monkeypatched so tests don't
need yfinance installed.

Run: python -m pytest modal_workers/tests/test_earnings_calendar.py -v
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from modal_workers.fetchers.universe import earnings_calendar as ec


# ---------------------------------------------------------------------------
# _normalize_tickers — dedupe + uppercase + strip
# ---------------------------------------------------------------------------


def test_normalize_tickers_dedupes_and_uppercases():
    out = list(ec._normalize_tickers([" axsm ", "AXSM", "vrdn", "VRDN"]))
    assert out == ["AXSM", "VRDN"]


def test_normalize_tickers_drops_empties():
    out = list(ec._normalize_tickers(["", None, " ", "AXSM"]))
    assert out == ["AXSM"]


def test_normalize_tickers_preserves_input_order():
    out = list(ec._normalize_tickers(["ZTS", "AAPL", "MSFT"]))
    assert out == ["ZTS", "AAPL", "MSFT"]


# ---------------------------------------------------------------------------
# _infer_session_from_ts — BMO/AMC inference from local hour
# ---------------------------------------------------------------------------


class _FakeTs:
    def __init__(self, hour: int) -> None:
        self.hour = hour


def test_infer_session_before_market():
    assert ec._infer_session_from_ts(_FakeTs(hour=7)) == "bmo"


def test_infer_session_after_market():
    assert ec._infer_session_from_ts(_FakeTs(hour=16)) == "amc"
    assert ec._infer_session_from_ts(_FakeTs(hour=20)) == "amc"


def test_infer_session_during_market():
    assert ec._infer_session_from_ts(_FakeTs(hour=12)) == "during"


def test_infer_session_falls_back_to_unknown_on_garbage():
    class _Bad:  # no .hour, no parseable string repr
        def __str__(self) -> str:
            return "garbage"

    assert ec._infer_session_from_ts(_Bad()) == "unknown"


# ---------------------------------------------------------------------------
# _coerce_json — strips numpy scalars + NaN
# ---------------------------------------------------------------------------


def test_coerce_json_passes_through_primitives():
    assert ec._coerce_json("hello") == "hello"
    assert ec._coerce_json(42) == 42
    assert ec._coerce_json(True) is True
    assert ec._coerce_json(None) is None


def test_coerce_json_drops_nan():
    assert ec._coerce_json(float("nan")) is None


def test_coerce_json_unpacks_item_protocol():
    class _NpScalar:
        def item(self) -> int:
            return 7

    assert ec._coerce_json(_NpScalar()) == 7


def test_coerce_json_falls_back_to_str():
    class _Weird:
        def __str__(self) -> str:
            return "weird-thing"

    assert ec._coerce_json(_Weird()) == "weird-thing"


# ---------------------------------------------------------------------------
# fetch() with monkeypatched yfinance helper
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_client(monkeypatch):
    """Stand-in for SupabaseClient — we only need _upsert_earnings_row to
    succeed without hitting Postgres."""
    monkeypatch.setattr(ec, "_upsert_earnings_row", lambda _client, _row: None)
    return object()


def test_fetch_dry_run_does_not_call_upsert(fake_client, monkeypatch):
    today = datetime.now(timezone.utc).date()

    def fake_yfinance(ticker, *, window_start, window_end):
        return [{
            "ticker": ticker,
            "earnings_date": today.isoformat(),
            "session": "amc",
            "is_estimated": False,
            "source": "yfinance",
            "confidence": 0.85,
            "raw_payload": {},
        }]

    monkeypatch.setattr(ec, "_yfinance_earnings_for_ticker", fake_yfinance)
    monkeypatch.setattr(ec, "PER_TICKER_SLEEP_S", 0)
    monkeypatch.setattr(ec, "BATCH_SLEEP_S", 0)

    out = ec.fetch(fake_client, tickers=["AXSM"], dry_run=True)
    assert out["fetched"] == 1
    assert out["upserted"] == 1
    assert out["errors"] == []


def test_fetch_yfinance_empty_falls_back_to_polygon(monkeypatch, fake_client):
    captured: Dict[str, Any] = {"polygon_called": False}

    def empty_yf(*_a, **_kw): return []

    def polygon_returns_row(ticker, *, window_start, window_end):
        captured["polygon_called"] = True
        return [{
            "ticker": ticker,
            "earnings_date": "2026-08-01",
            "session": "amc",
            "is_estimated": False,
            "source": "polygon",
            "confidence": 0.80,
            "raw_payload": {"src": "polygon"},
        }]

    monkeypatch.setattr(ec, "_yfinance_earnings_for_ticker", empty_yf)
    monkeypatch.setattr(ec, "_polygon_earnings_for_ticker", polygon_returns_row)
    monkeypatch.setattr(ec, "PER_TICKER_SLEEP_S", 0)
    monkeypatch.setattr(ec, "BATCH_SLEEP_S", 0)

    out = ec.fetch(fake_client, tickers=["AXSM"], dry_run=True)
    assert captured["polygon_called"] is True
    assert out["fetched"] == 1


def test_fetch_yfinance_exception_records_error_and_continues(
    monkeypatch, fake_client,
):
    def boom(*_a, **_kw):
        raise RuntimeError("yfinance flaked")

    monkeypatch.setattr(ec, "_yfinance_earnings_for_ticker", boom)
    monkeypatch.setattr(ec, "_polygon_earnings_for_ticker", lambda *_a, **_kw: [])
    monkeypatch.setattr(ec, "PER_TICKER_SLEEP_S", 0)
    monkeypatch.setattr(ec, "BATCH_SLEEP_S", 0)

    out = ec.fetch(fake_client, tickers=["AXSM", "VRDN"], dry_run=True)
    assert out["fetched"] == 0
    # Two errors (one per ticker), neither tracked under upserted.
    assert len(out["errors"]) == 2
    assert all(e["source"] == "yfinance" for e in out["errors"])


def test_fetch_polygon_fallback_disabled(monkeypatch, fake_client):
    polygon_called = {"hit": False}

    def empty_yf(*_a, **_kw): return []

    def polygon_should_not_fire(*_a, **_kw):
        polygon_called["hit"] = True
        return []

    monkeypatch.setattr(ec, "_yfinance_earnings_for_ticker", empty_yf)
    monkeypatch.setattr(ec, "_polygon_earnings_for_ticker", polygon_should_not_fire)
    monkeypatch.setattr(ec, "PER_TICKER_SLEEP_S", 0)
    monkeypatch.setattr(ec, "BATCH_SLEEP_S", 0)

    ec.fetch(fake_client, tickers=["AXSM"], dry_run=True, polygon_fallback=False)
    assert polygon_called["hit"] is False
