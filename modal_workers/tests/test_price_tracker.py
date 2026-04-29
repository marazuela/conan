"""Unit tests for the daily price tracker.

Covers:
  1. Direction sign-flip (long / short / neutral).
  2. Horizon eligibility based on (anchor, today).
  3. Weekend anchor — first trading day at-or-after `target` is returned by
     fetch_close_on_date.
  4. Missing-data handling — fetch_close_on_date None propagates to fetch_status
     and suppresses the outcomes mirror.
  5. Idempotency — re-running on the same day issues UPSERT calls with the
     same row key.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import pytest

from modal_workers.evaluators import price_tracker
from modal_workers.shared import market_snapshot


class FakeClient:
    def __init__(self, subjects: List[Dict[str, Any]]):
        self.subjects = subjects
        self.snapshots: List[Dict[str, Any]] = []
        self.outcome_updates: List[Tuple[str, int, Optional[float]]] = []

    def load_price_tracking_subjects(self) -> List[Dict[str, Any]]:
        return list(self.subjects)

    def upsert_price_snapshot(self, row: Dict[str, Any]) -> None:
        self.snapshots.append(row)

    def update_outcome_realized_move(
        self, candidate_id: str, horizon_days: int, signed_move_pct: Optional[float]
    ) -> None:
        self.outcome_updates.append((candidate_id, horizon_days, signed_move_pct))


def _patch_close(monkeypatch, prices: Dict[Tuple[str, str], Optional[float]]) -> None:
    """Patch fetch_close_on_date with a lookup table keyed on (ticker, iso_date)."""
    def fake(ticker: str, mic: Optional[str], target: date, *, forward_window_days: int = 5):
        return prices.get((ticker, target.isoformat()))
    monkeypatch.setattr(price_tracker, "fetch_close_on_date", fake)


# ---------------------------------------------------------------------------
# 1. direction sign-flip
# ---------------------------------------------------------------------------

def test_long_direction_keeps_raw_sign(monkeypatch):
    _patch_close(monkeypatch, {
        ("ACME", "2026-04-01"): 100.0,
        ("ACME", "2026-04-02"): 110.0,
    })
    client = FakeClient([{
        "kind": "candidate", "signal_id": None, "candidate_id": "c1",
        "ticker": "ACME", "mic": None, "thesis_direction": "long",
        "created_at": "2026-04-01T12:00:00Z",
    }])
    out = price_tracker.run_price_tracker(client=client, today=date(2026, 4, 30))
    one_day = next(s for s in client.snapshots if s["horizon_days"] == 1)
    assert one_day["raw_move_pct"] == pytest.approx(10.0)
    assert one_day["signed_move_pct"] == pytest.approx(10.0)
    assert one_day["fetch_status"] == "ok"
    assert ("c1", 1, pytest.approx(10.0)) in client.outcome_updates
    assert out["ok"] >= 1


def test_short_direction_flips_sign(monkeypatch):
    _patch_close(monkeypatch, {
        ("BEAR", "2026-04-01"): 100.0,
        ("BEAR", "2026-04-02"): 110.0,
    })
    client = FakeClient([{
        "kind": "signal", "signal_id": "sig-1", "candidate_id": None,
        "ticker": "BEAR", "mic": None, "thesis_direction": "short",
        "created_at": "2026-04-01T12:00:00Z",
    }])
    price_tracker.run_price_tracker(client=client, today=date(2026, 4, 30))
    one_day = next(s for s in client.snapshots if s["horizon_days"] == 1)
    assert one_day["raw_move_pct"] == pytest.approx(10.0)
    assert one_day["signed_move_pct"] == pytest.approx(-10.0)
    assert one_day["fetch_status"] == "ok"
    # signal-only (no candidate_id) => no outcomes mirror
    assert client.outcome_updates == []


def test_neutral_direction_skips_signed_value(monkeypatch):
    _patch_close(monkeypatch, {
        ("FLAT", "2026-04-01"): 100.0,
        ("FLAT", "2026-04-02"): 110.0,
    })
    client = FakeClient([{
        "kind": "candidate", "signal_id": None, "candidate_id": "c2",
        "ticker": "FLAT", "mic": None, "thesis_direction": "neutral",
        "created_at": "2026-04-01T12:00:00Z",
    }])
    price_tracker.run_price_tracker(client=client, today=date(2026, 4, 30))
    one_day = next(s for s in client.snapshots if s["horizon_days"] == 1)
    assert one_day["fetch_status"] == "neutral_skipped"
    assert one_day["signed_move_pct"] is None
    # neutral never mirrors to outcomes
    assert client.outcome_updates == []


# ---------------------------------------------------------------------------
# 2. horizon eligibility
# ---------------------------------------------------------------------------

def test_anchor_today_emits_no_horizons(monkeypatch):
    _patch_close(monkeypatch, {})
    client = FakeClient([{
        "kind": "candidate", "signal_id": None, "candidate_id": "c1",
        "ticker": "ACME", "mic": None, "thesis_direction": "long",
        "created_at": "2026-04-30T12:00:00Z",
    }])
    out = price_tracker.run_price_tracker(client=client, today=date(2026, 4, 30))
    assert client.snapshots == []
    assert out["snapshots_written"] == 0


def test_anchor_one_day_ago_emits_only_1d_horizon(monkeypatch):
    _patch_close(monkeypatch, {
        ("ACME", "2026-04-29"): 50.0,
        ("ACME", "2026-04-30"): 55.0,
    })
    client = FakeClient([{
        "kind": "candidate", "signal_id": None, "candidate_id": "c1",
        "ticker": "ACME", "mic": None, "thesis_direction": "long",
        "created_at": "2026-04-29T00:00:00Z",
    }])
    price_tracker.run_price_tracker(client=client, today=date(2026, 4, 30))
    horizons = sorted(s["horizon_days"] for s in client.snapshots)
    assert horizons == [1]


def test_anchor_thirty_days_ago_emits_all_horizons(monkeypatch):
    prices = {
        ("ACME", "2026-04-01"): 100.0,
        ("ACME", "2026-04-02"): 101.0,   # 1d
        ("ACME", "2026-04-08"): 105.0,   # 7d
        ("ACME", "2026-05-01"): 120.0,   # 30d
    }
    _patch_close(monkeypatch, prices)
    client = FakeClient([{
        "kind": "candidate", "signal_id": None, "candidate_id": "c1",
        "ticker": "ACME", "mic": None, "thesis_direction": "long",
        "created_at": "2026-04-01T00:00:00Z",
    }])
    price_tracker.run_price_tracker(client=client, today=date(2026, 5, 5))
    horizons = sorted(s["horizon_days"] for s in client.snapshots)
    assert horizons == [1, 7, 30]
    by_h = {s["horizon_days"]: s for s in client.snapshots}
    assert by_h[7]["signed_move_pct"] == pytest.approx(5.0)
    assert by_h[30]["signed_move_pct"] == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# 3. weekend anchor (fetch_close_on_date forward-fills)
# ---------------------------------------------------------------------------

class _FakeHistory:
    """Stand-in for the DataFrame yfinance returns; supports `"Close" in df` and
    df["Close"].dropna().iloc[0]."""

    def __init__(self, closes):
        self._closes = closes

    def __contains__(self, key):
        return key == "Close" and bool(self._closes)

    def __getitem__(self, key):
        assert key == "Close"
        return _FakeSeries(self._closes)


class _FakeSeries:
    def __init__(self, values):
        self._values = list(values)

    def dropna(self):
        return _FakeSeries([v for v in self._values if v is not None])

    def __len__(self):
        return len(self._values)

    @property
    def iloc(self):
        outer = self
        class _ILoc:
            def __getitem__(self, idx):
                return outer._values[idx]
        return _ILoc()


def test_weekend_anchor_picks_next_trading_day(monkeypatch):
    """A Saturday `target` should yield Monday's close because yfinance returns
    rows only for trading days within [start, end)."""
    market_snapshot._CLOSE_MEMO.clear()

    class _FakeYf:
        class Ticker:
            def __init__(self, symbol):
                self.symbol = symbol

            def history(self, start, end, auto_adjust):
                # Saturday=2026-04-04 → Monday=2026-04-06 close.
                # We only return Monday's row, simulating yfinance's behavior
                # of skipping weekend rows.
                assert start == "2026-04-04"
                return _FakeHistory([42.5])

    import sys
    monkeypatch.setitem(sys.modules, "yfinance", _FakeYf)
    close = market_snapshot.fetch_close_on_date("ACME", None, date(2026, 4, 4))
    assert close == 42.5


# ---------------------------------------------------------------------------
# 4. missing-data handling
# ---------------------------------------------------------------------------

def test_missing_anchor_close_marks_no_data_and_skips_outcomes(monkeypatch):
    _patch_close(monkeypatch, {
        # No anchor close for ACME on 2026-04-01.
        ("ACME", "2026-04-02"): 110.0,
    })
    client = FakeClient([{
        "kind": "candidate", "signal_id": None, "candidate_id": "c1",
        "ticker": "ACME", "mic": None, "thesis_direction": "long",
        "created_at": "2026-04-01T12:00:00Z",
    }])
    price_tracker.run_price_tracker(client=client, today=date(2026, 4, 30))
    statuses = {s["fetch_status"] for s in client.snapshots}
    assert "no_data" in statuses
    # No outcomes UPDATE issued because nothing reached fetch_status='ok'.
    assert client.outcome_updates == []


def test_missing_horizon_close_marks_stale_anchor(monkeypatch):
    _patch_close(monkeypatch, {
        ("ACME", "2026-04-01"): 100.0,
        # Horizon close for 1d is missing — yfinance gap.
        ("ACME", "2026-04-08"): 105.0,
        ("ACME", "2026-05-01"): 120.0,
    })
    client = FakeClient([{
        "kind": "candidate", "signal_id": None, "candidate_id": "c1",
        "ticker": "ACME", "mic": None, "thesis_direction": "long",
        "created_at": "2026-04-01T00:00:00Z",
    }])
    price_tracker.run_price_tracker(client=client, today=date(2026, 5, 5))
    by_h = {s["horizon_days"]: s for s in client.snapshots}
    assert by_h[1]["fetch_status"] == "stale_anchor"
    assert by_h[1]["signed_move_pct"] is None
    assert by_h[7]["fetch_status"] == "ok"
    assert by_h[30]["fetch_status"] == "ok"
    # Only the OK horizons should mirror.
    horizons_mirrored = {h for (_, h, _) in client.outcome_updates}
    assert horizons_mirrored == {7, 30}


# ---------------------------------------------------------------------------
# 5. idempotency
# ---------------------------------------------------------------------------

def test_rerun_same_day_produces_same_row_keys(monkeypatch):
    _patch_close(monkeypatch, {
        ("ACME", "2026-04-29"): 50.0,
        ("ACME", "2026-04-30"): 55.0,
    })
    subject = {
        "kind": "candidate", "signal_id": None, "candidate_id": "c1",
        "ticker": "ACME", "mic": None, "thesis_direction": "long",
        "created_at": "2026-04-29T00:00:00Z",
    }
    client = FakeClient([subject])
    price_tracker.run_price_tracker(client=client, today=date(2026, 4, 30))

    client2 = FakeClient([subject])
    price_tracker.run_price_tracker(client=client2, today=date(2026, 4, 30))

    # Row keys identify the conflict target — (signal_id|candidate_id, horizon_days).
    def key(row):
        return (row.get("signal_id"), row.get("candidate_id"), row["horizon_days"])

    assert {key(r) for r in client.snapshots} == {key(r) for r in client2.snapshots}
    # Values are deterministic under same inputs:
    assert [r["signed_move_pct"] for r in client.snapshots] == [
        r["signed_move_pct"] for r in client2.snapshots
    ]
