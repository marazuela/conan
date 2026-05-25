"""Tests for curate_tradeable_filter_pass — D-105 backfill script.

Covers the deterministic decision logic in isolation, then exercises
`curate_row` and `run` with stubbed yfinance + SupabaseClient.

The contract under test (D-105 / migration
`20260507000000_v3_d105_eval_harness_extracted_facts_amendments.sql`):

  tradeable_filter_pass = true IFF at reference_assessment_date:
    - market cap >= $215M USD
    - 90-day ADV >= $500K USD
    - listed on NYSE / NASDAQ / AMEX / LSE family

  issuer_status ∈ {'active', 'delisted'} via the initial heuristic
  (acquired/bankrupt not auto-classified — left for downstream refinement).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from modal_workers.scripts.curate_tradeable_filter_pass import (
    ALLOWED_EXCHANGES,
    MIN_ADV_USD,
    MIN_MARKET_CAP_USD,
    FilterDecision,
    classify_exchange_allowed,
    compute_adv_usd,
    curate_row,
    decide_filter,
    needs_patch,
    pick_close_on_or_before,
    run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agg(d: date, close: float, volume: int) -> Dict[str, Any]:
    """Build a Polygon-shaped daily aggregate for a given date."""
    ts_ms = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    return {"t": ts_ms, "o": close, "h": close, "l": close, "c": close, "v": volume}


def _aggs_for_window(start: date, n_days: int, close: float, volume: int) -> List[Dict[str, Any]]:
    """Synthesize N daily aggregates with constant close + volume."""
    return [_agg(start + timedelta(days=i), close, volume) for i in range(n_days)]


# ---------------------------------------------------------------------------
# Pure-function tests — compute_adv_usd
# ---------------------------------------------------------------------------


def test_compute_adv_usd_returns_none_on_empty():
    assert compute_adv_usd([]) is None


def test_compute_adv_usd_averages_close_times_volume():
    closes = _aggs_for_window(date(2024, 1, 2), n_days=4, close=10.0, volume=100_000)
    # mean(10 * 100_000) = 1_000_000
    assert compute_adv_usd(closes) == pytest.approx(1_000_000.0)


def test_compute_adv_usd_skips_rows_with_missing_fields():
    closes = [
        _agg(date(2024, 1, 2), 10.0, 100_000),
        {"t": 12345, "c": None, "v": None},  # missing fields — ignored
        _agg(date(2024, 1, 3), 20.0, 50_000),
    ]
    # mean(1_000_000, 1_000_000) = 1_000_000
    assert compute_adv_usd(closes) == pytest.approx(1_000_000.0)


# ---------------------------------------------------------------------------
# Pure-function tests — pick_close_on_or_before
# ---------------------------------------------------------------------------


def test_pick_close_on_or_before_returns_last_before_target():
    closes = [
        _agg(date(2024, 1, 1), 5.0, 100),
        _agg(date(2024, 1, 5), 7.0, 100),
        _agg(date(2024, 1, 10), 9.0, 100),
    ]
    assert pick_close_on_or_before(closes, date(2024, 1, 8)) == 7.0


def test_pick_close_on_or_before_returns_exact_target():
    closes = [_agg(date(2024, 1, 5), 7.5, 100)]
    assert pick_close_on_or_before(closes, date(2024, 1, 5)) == 7.5


def test_pick_close_on_or_before_returns_none_when_all_after_target():
    closes = [_agg(date(2024, 1, 10), 9.0, 100)]
    assert pick_close_on_or_before(closes, date(2024, 1, 5)) is None


# ---------------------------------------------------------------------------
# Pure-function tests — classify_exchange_allowed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(ALLOWED_EXCHANGES))
def test_classify_exchange_allowed_admits_d105_allowlist(code: str):
    assert classify_exchange_allowed(code) is True
    # case-insensitive
    assert classify_exchange_allowed(code.lower()) is True


@pytest.mark.parametrize("code", ["PNK", "OTC", "OTCQB", "OTCQX", "TSX", "TSE"])
def test_classify_exchange_allowed_rejects_otc_and_non_majors(code: str):
    assert classify_exchange_allowed(code) is False


def test_classify_exchange_allowed_rejects_none_and_empty():
    assert classify_exchange_allowed(None) is False
    assert classify_exchange_allowed("") is False


# ---------------------------------------------------------------------------
# Pure-function tests — decide_filter (all four outcomes)
# ---------------------------------------------------------------------------


def _passing_inputs() -> Dict[str, Any]:
    """Baseline inputs above all three D-105 thresholds; tweak fields per
    test to isolate failure modes."""
    return {
        "closes_for_adv": _aggs_for_window(
            date(2024, 1, 2), n_days=90, close=50.0, volume=100_000,  # ADV = $5M
        ),
        "close_at_ref": 50.0,
        "shares_outstanding": 10_000_000,   # mcap = 50 * 10M = $500M
        "exchange_code": "NMS",
        "has_future_history": True,
    }


def test_decide_filter_all_thresholds_pass():
    decision = decide_filter(**_passing_inputs())
    assert decision.tradeable_filter_pass is True
    assert decision.issuer_status == "active"
    assert decision.rationale == "pass"
    assert decision.market_cap_usd == pytest.approx(500_000_000.0)
    assert decision.adv_usd == pytest.approx(5_000_000.0)
    assert decision.exchange == "NMS"


def test_decide_filter_fails_when_market_cap_below_threshold():
    inputs = _passing_inputs()
    inputs["shares_outstanding"] = 100_000  # mcap = 50 * 100K = $5M
    decision = decide_filter(**inputs)
    assert decision.tradeable_filter_pass is False
    assert decision.issuer_status == "active"
    assert "mcap" in decision.rationale


def test_decide_filter_fails_when_adv_below_threshold():
    inputs = _passing_inputs()
    inputs["closes_for_adv"] = _aggs_for_window(
        date(2024, 1, 2), n_days=90, close=1.0, volume=100,  # ADV = $100/day
    )
    inputs["close_at_ref"] = 1.0
    decision = decide_filter(**inputs)
    assert decision.tradeable_filter_pass is False
    assert "adv" in decision.rationale


def test_decide_filter_fails_when_exchange_not_in_allowlist():
    inputs = _passing_inputs()
    inputs["exchange_code"] = "PNK"
    decision = decide_filter(**inputs)
    assert decision.tradeable_filter_pass is False
    assert "exchange" in decision.rationale


def test_decide_filter_short_circuits_to_delisted_when_no_price_at_ref():
    inputs = _passing_inputs()
    inputs["close_at_ref"] = None
    decision = decide_filter(**inputs)
    assert decision.tradeable_filter_pass is False
    assert decision.issuer_status == "delisted"


def test_decide_filter_marks_delisted_when_no_future_history():
    inputs = _passing_inputs()
    inputs["has_future_history"] = False
    decision = decide_filter(**inputs)
    # Filter can still pass (issuer was tradeable at ref); status flags
    # delisted for survivorship-bias audit.
    assert decision.tradeable_filter_pass is True
    assert decision.issuer_status == "delisted"


def test_decide_filter_fails_when_shares_outstanding_unknown():
    inputs = _passing_inputs()
    inputs["shares_outstanding"] = None
    decision = decide_filter(**inputs)
    assert decision.tradeable_filter_pass is False
    assert "mcap_unknown" in decision.rationale


# ---------------------------------------------------------------------------
# curate_row — integration of fetch + decide
# ---------------------------------------------------------------------------


def _stub_fetchers(
    *,
    history_at_ref: Optional[List[Dict[str, Any]]] = None,
    history_at_future: Optional[List[Dict[str, Any]]] = None,
    info: Optional[Dict[str, Any]] = None,
):
    """Build fake fetch_history + fetch_info callables that return synthetic
    data. `history_at_ref` is returned for the ADV window; `history_at_future`
    for the +365d probe."""

    history_at_ref = history_at_ref or []
    history_at_future = history_at_future if history_at_future is not None else []

    calls = []

    def _fetch_history(ticker, *, window_start, window_end):
        calls.append(("history", ticker, window_start, window_end))
        # Heuristic: the +365d probe asks for a narrow window > ref_date.
        # The ADV window asks for ref_date - ~130 days through ref_date.
        if window_end - window_start <= timedelta(days=30):
            return list(history_at_future)
        return list(history_at_ref)

    def _fetch_info(ticker):
        calls.append(("info", ticker))
        return dict(info or {})

    return _fetch_history, _fetch_info, calls


def test_curate_row_returns_none_when_ticker_missing():
    row = {"reference_assessment_date": "2024-01-15", "fda_assets": {}}
    assert curate_row(row, fetch_history=lambda *a, **k: [],
                      fetch_info=lambda *a, **k: {}) is None


def test_curate_row_returns_none_when_ref_date_unparseable():
    row = {"reference_assessment_date": "not-a-date",
           "fda_assets": {"ticker": "AAA"}}
    assert curate_row(row, fetch_history=lambda *a, **k: [],
                      fetch_info=lambda *a, **k: {}) is None


def test_curate_row_full_passing_path():
    ref = date(2024, 1, 15)
    fh, fi, _ = _stub_fetchers(
        history_at_ref=_aggs_for_window(
            ref - timedelta(days=90), n_days=90, close=20.0, volume=100_000,
        ),
        history_at_future=_aggs_for_window(
            ref + timedelta(days=365), n_days=5, close=22.0, volume=80_000,
        ),
        info={"sharesOutstanding": 30_000_000, "exchange": "NMS"},
    )
    row = {
        "reference_assessment_date": ref.isoformat(),
        "fda_assets": {"ticker": "AAA"},
    }
    decision = curate_row(row, fetch_history=fh, fetch_info=fi,
                          today=ref + timedelta(days=400))
    assert decision is not None
    assert decision.tradeable_filter_pass is True
    assert decision.issuer_status == "active"


def test_curate_row_marks_delisted_when_no_future_history():
    ref = date(2022, 6, 1)
    fh, fi, _ = _stub_fetchers(
        history_at_ref=_aggs_for_window(
            ref - timedelta(days=90), n_days=90, close=15.0, volume=200_000,
        ),
        history_at_future=[],  # delisted before ref+365
        info={"sharesOutstanding": 20_000_000, "exchange": "NMS"},
    )
    row = {
        "reference_assessment_date": ref.isoformat(),
        "fda_assets": {"ticker": "BBB"},
    }
    decision = curate_row(row, fetch_history=fh, fetch_info=fi,
                          today=date(2026, 1, 1))
    assert decision is not None
    assert decision.issuer_status == "delisted"


def test_curate_row_skips_future_probe_when_anchor_in_future():
    """A row whose reference_assessment_date is recent (ref+365d still in
    the future) should treat the issuer as active without calling the
    future-history fetcher."""
    ref = date.today() - timedelta(days=30)
    fh, fi, calls = _stub_fetchers(
        history_at_ref=_aggs_for_window(
            ref - timedelta(days=90), n_days=90, close=10.0, volume=100_000,
        ),
        info={"sharesOutstanding": 50_000_000, "exchange": "NMS"},
    )
    row = {
        "reference_assessment_date": ref.isoformat(),
        "fda_assets": {"ticker": "CCC"},
    }
    decision = curate_row(row, fetch_history=fh, fetch_info=fi)
    assert decision is not None
    assert decision.issuer_status == "active"
    # Only one history call (ADV window); no +365d probe.
    history_calls = [c for c in calls if c[0] == "history"]
    assert len(history_calls) == 1


# ---------------------------------------------------------------------------
# needs_patch — idempotency
# ---------------------------------------------------------------------------


def test_needs_patch_false_when_stored_matches_decision():
    row = {"tradeable_filter_pass": True, "issuer_status": "active"}
    decision = FilterDecision(
        tradeable_filter_pass=True, issuer_status="active", rationale="pass",
    )
    assert needs_patch(row, decision) is False


def test_needs_patch_true_when_pass_differs():
    row = {"tradeable_filter_pass": False, "issuer_status": "active"}
    decision = FilterDecision(
        tradeable_filter_pass=True, issuer_status="active", rationale="pass",
    )
    assert needs_patch(row, decision) is True


def test_needs_patch_true_when_status_differs():
    row = {"tradeable_filter_pass": True, "issuer_status": None}
    decision = FilterDecision(
        tradeable_filter_pass=True, issuer_status="active", rationale="pass",
    )
    assert needs_patch(row, decision) is True


# ---------------------------------------------------------------------------
# run() — end-to-end with stubbed SupabaseClient
# ---------------------------------------------------------------------------


class _StubSb:
    """Minimal SupabaseClient stand-in for the curate script's _rest calls."""

    def __init__(self, *, rows: List[Dict[str, Any]]):
        self.rows = rows
        self.calls: List[Dict[str, Any]] = []

    def _rest(self, method, table, *, params=None, json_body=None, prefer=None,
              headers=None, json=None):
        self.calls.append({
            "method": method, "table": table, "params": params or {},
            "json_body": json_body, "prefer": prefer,
        })
        if method == "GET" and table == "eval_harness":
            return list(self.rows)
        return []


def test_run_dry_run_makes_no_patches(monkeypatch):
    sb = _StubSb(rows=[{
        "id": "11111111-2222-3333-4444-555555555555",
        "reference_assessment_date": "2024-01-15",
        "tradeable_filter_pass": False,
        "issuer_status": None,
        "fda_assets": {"ticker": "AAA"},
    }])
    # Stub out the I/O helpers at module level.
    import modal_workers.scripts.curate_tradeable_filter_pass as mod
    monkeypatch.setattr(mod, "fetch_history_window",
                        lambda *a, **k: _aggs_for_window(
                            date(2023, 10, 1), 90, 30.0, 100_000))
    monkeypatch.setattr(mod, "_fetch_yf_info",
                        lambda t: {"sharesOutstanding": 20_000_000, "exchange": "NMS"})

    stats = run(sb=sb, apply=False, limit=10, only_empty=True)
    assert stats.rows_seen == 1
    assert stats.rows_updated == 0
    # No PATCH attempted.
    assert all(c["method"] != "PATCH" for c in sb.calls)


def test_run_apply_patches_rows_with_drift(monkeypatch):
    sb = _StubSb(rows=[{
        "id": "11111111-2222-3333-4444-555555555555",
        "reference_assessment_date": "2024-01-15",
        "tradeable_filter_pass": False,
        "issuer_status": None,
        "fda_assets": {"ticker": "AAA"},
    }])
    import modal_workers.scripts.curate_tradeable_filter_pass as mod
    monkeypatch.setattr(mod, "fetch_history_window",
                        lambda *a, **k: _aggs_for_window(
                            date(2023, 10, 1), 90, 30.0, 100_000))
    monkeypatch.setattr(mod, "_fetch_yf_info",
                        lambda t: {"sharesOutstanding": 20_000_000, "exchange": "NMS"})

    stats = run(sb=sb, apply=True, limit=10, only_empty=True)
    assert stats.rows_seen == 1
    assert stats.rows_updated == 1
    patches = [c for c in sb.calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    body = patches[0]["json_body"]
    assert body["tradeable_filter_pass"] is True
    assert body["issuer_status"] == "active"


def test_run_idempotent_skip_when_row_already_matches(monkeypatch):
    sb = _StubSb(rows=[{
        "id": "11111111-2222-3333-4444-555555555555",
        "reference_assessment_date": "2024-01-15",
        "tradeable_filter_pass": True,   # already matches expected outcome
        "issuer_status": "active",
        "fda_assets": {"ticker": "AAA"},
    }])
    import modal_workers.scripts.curate_tradeable_filter_pass as mod
    monkeypatch.setattr(mod, "fetch_history_window",
                        lambda *a, **k: _aggs_for_window(
                            date(2023, 10, 1), 90, 30.0, 100_000))
    monkeypatch.setattr(mod, "_fetch_yf_info",
                        lambda t: {"sharesOutstanding": 20_000_000, "exchange": "NMS"})

    stats = run(sb=sb, apply=True, limit=10, only_empty=False)
    assert stats.rows_seen == 1
    assert stats.rows_updated == 0
    assert stats.rows_skipped_idempotent == 1
    assert all(c["method"] != "PATCH" for c in sb.calls)
