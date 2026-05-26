"""WI-5 — Q1 audit tests.

Pure-function coverage of confounder + coverage + assemble_verdict helpers.
No Supabase, no Polygon, no yfinance — every input is a literal so the
verdict ladder stays a regression target as we refine the audit.

Run: python -m pytest modal_workers/tests/test_audit_event_data_quality.py -v
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from modal_workers.scripts.audit_event_data_quality import (
    DEFAULT_EARNINGS_WINDOW_TD,
    LOW_VOLUME_DAY_PCT,
    MATERIAL_8K_ITEMS,
    Q1Verdict,
    T_PLUS_DAYS,
    _doc_has_material_8k_item,
    _extract_8k_items,
    _filter_material_8k_rows,
    assemble_verdict,
    check_earnings_within_window,
    check_fomc_day,
    check_low_volume_days_pct,
    check_material_8k_in_window,
    check_pre_window_delisting,
    check_spx_three_sigma,
    check_yfinance_window_gap,
)


REF_DATE = date(2026, 5, 15)


# ---------------------------------------------------------------------------
# Confounder: earnings within window
# ---------------------------------------------------------------------------


def test_earnings_within_window_triggers_on_in_window_date():
    result = check_earnings_within_window(
        ticker="AXSM", ref_date=REF_DATE,
        earnings_dates=[REF_DATE + timedelta(days=2)],
    )
    assert result["triggered"] is True
    assert result["evidence"]["hits"] == [(REF_DATE + timedelta(days=2)).isoformat()]


def test_earnings_within_window_no_hits_outside_window():
    far = REF_DATE + timedelta(days=60)
    result = check_earnings_within_window(
        ticker="AXSM", ref_date=REF_DATE,
        earnings_dates=[far],
    )
    assert result["triggered"] is False
    assert result["evidence"]["hits"] == []


def test_earnings_within_window_empty_list_doesnt_trigger():
    result = check_earnings_within_window(
        ticker="AXSM", ref_date=REF_DATE, earnings_dates=[],
    )
    assert result["triggered"] is False


# ---------------------------------------------------------------------------
# Confounder: FOMC day
# ---------------------------------------------------------------------------


def test_fomc_day_triggers_on_exact_date():
    result = check_fomc_day(ref_date=REF_DATE, fomc_dates=[REF_DATE])
    assert result["triggered"] is True


def test_fomc_day_triggers_plus_one_day():
    result = check_fomc_day(
        ref_date=REF_DATE, fomc_dates=[REF_DATE + timedelta(days=1)],
    )
    assert result["triggered"] is True


def test_fomc_day_doesnt_trigger_two_days_out():
    result = check_fomc_day(
        ref_date=REF_DATE, fomc_dates=[REF_DATE + timedelta(days=2)],
    )
    assert result["triggered"] is False


# ---------------------------------------------------------------------------
# Confounder: SPX 3σ
# ---------------------------------------------------------------------------


def test_spx_three_sigma_triggers_on_outlier_return():
    # 30 daily returns centered around 0 with one 5-σ tail.
    returns = [0.001] * 29 + [0.10]  # +10% on day 30 (well >3σ here)
    result = check_spx_three_sigma(spy_daily_returns=returns)
    assert result["triggered"] is True
    assert result["evidence"]["excess_returns"] == [0.1]


def test_spx_three_sigma_doesnt_trigger_on_quiet_series():
    returns = [0.002] * 30
    result = check_spx_three_sigma(spy_daily_returns=returns)
    # Zero variance → short-circuits to not triggered (no excess possible).
    assert result["triggered"] is False


def test_spx_three_sigma_empty_input_returns_no_spy_data():
    result = check_spx_three_sigma(spy_daily_returns=[])
    assert result["triggered"] is False
    assert result["evidence"]["reason"] == "no_spy_data"


# ---------------------------------------------------------------------------
# Confounder: material 8-K in window
# ---------------------------------------------------------------------------


def test_material_8k_triggers_when_count_positive():
    result = check_material_8k_in_window(
        ticker="AXSM", in_window_8k_count=2, items_observed=["1.01", "8.01"],
    )
    assert result["triggered"] is True
    assert result["evidence"]["count"] == 2
    assert result["evidence"]["items_observed"] == ["1.01", "8.01"]


def test_material_8k_doesnt_trigger_at_zero():
    result = check_material_8k_in_window(ticker="AXSM", in_window_8k_count=0)
    assert result["triggered"] is False
    assert result["evidence"]["items_observed"] == []


def test_material_8k_evidence_carries_material_item_set():
    result = check_material_8k_in_window(ticker="AXSM", in_window_8k_count=0)
    assert result["evidence"]["material_items"] == sorted(MATERIAL_8K_ITEMS)


# ---------------------------------------------------------------------------
# Pure 8-K item filtering (replaces the v1 stub)
# ---------------------------------------------------------------------------


def test_extract_8k_items_parses_string_list():
    assert _extract_8k_items({"items": ["1.01", "8.01"]}) == ["1.01", "8.01"]


def test_extract_8k_items_coerces_non_string_codes():
    # EFTS occasionally emits items as numerics; we normalize to str.
    assert _extract_8k_items({"items": [1.01, "8.01", None, ""]}) == ["1.01", "8.01"]


def test_extract_8k_items_handles_missing_or_malformed_extensions():
    assert _extract_8k_items(None) == []
    assert _extract_8k_items({}) == []
    assert _extract_8k_items({"items": "1.01"}) == []  # not a list
    assert _extract_8k_items({"items": []}) == []


def test_doc_has_material_8k_item_matches_each_material_code():
    for code in ("1.01", "2.02", "8.01"):
        assert _doc_has_material_8k_item({"items": [code]}) is True


def test_doc_has_material_8k_item_rejects_non_material_codes():
    # 5.02 = officer departure; 7.01 = Reg-FD; neither moves the stock the
    # way 1.01/2.02/8.01 do.
    assert _doc_has_material_8k_item({"items": ["5.02", "7.01"]}) is False
    assert _doc_has_material_8k_item({"items": []}) is False


def test_filter_material_8k_rows_counts_only_material_docs():
    rows = [
        {"documents": {"id": "d1", "extensions": {"items": ["1.01"]}}},  # material
        {"documents": {"id": "d2", "extensions": {"items": ["5.02"]}}},  # not material
        {"documents": {"id": "d3", "extensions": {"items": ["8.01", "1.01"]}}},  # material
        {"documents": {"id": "d4", "extensions": {"items": []}}},        # empty items
        {"documents": None},                                             # malformed
    ]
    count, observed = _filter_material_8k_rows(rows)
    assert count == 2
    assert observed == ["1.01", "8.01"]


def test_filter_material_8k_rows_excludes_source_docs():
    rows = [
        {"documents": {"id": "d1", "extensions": {"items": ["1.01"]}}},
        {"documents": {"id": "d2", "extensions": {"items": ["2.02"]}}},
    ]
    count, observed = _filter_material_8k_rows(rows, exclude_document_ids=["d1"])
    assert count == 1
    assert observed == ["2.02"]


def test_filter_material_8k_rows_deduplicates_observed_items():
    # Two material docs both carrying 1.01 should report ["1.01"] once.
    rows = [
        {"documents": {"id": "d1", "extensions": {"items": ["1.01"]}}},
        {"documents": {"id": "d2", "extensions": {"items": ["1.01", "2.02"]}}},
    ]
    count, observed = _filter_material_8k_rows(rows)
    assert count == 2
    assert observed == ["1.01", "2.02"]


def test_filter_material_8k_rows_empty_input_returns_zero():
    assert _filter_material_8k_rows([]) == (0, [])


# ---------------------------------------------------------------------------
# Coverage: yfinance window gap
# ---------------------------------------------------------------------------


def test_yfinance_window_gap_triggers_on_invalidated_t30():
    windows = [
        {"days": 30, "status": "invalidated"},
        {"days": 60, "status": "ok"},
    ]
    result = check_yfinance_window_gap(windows=windows)
    assert result["triggered"] is True


def test_yfinance_window_gap_triggers_when_t30_missing_entirely():
    result = check_yfinance_window_gap(windows=[{"days": 60, "status": "ok"}])
    assert result["triggered"] is True


def test_yfinance_window_gap_doesnt_trigger_when_t30_ok():
    result = check_yfinance_window_gap(
        windows=[{"days": 30, "status": "ok"}],
    )
    assert result["triggered"] is False


# ---------------------------------------------------------------------------
# Coverage: low volume days
# ---------------------------------------------------------------------------


def test_low_volume_check_pending_state_when_no_polygon_data():
    result = check_low_volume_days_pct()
    assert result["triggered"] is False
    assert result["evidence"]["state"] == "polygon_pending"


def test_low_volume_check_triggers_when_pct_exceeds_threshold():
    # 30 days; 7 low-volume → 23.3% > 20% threshold
    daily_volumes = [100.0] * 23 + [10.0] * 7
    result = check_low_volume_days_pct(
        daily_volumes=daily_volumes,
        trailing_90td_median=100.0,
    )
    assert result["triggered"] is True
    assert result["evidence"]["low_days"] == 7


def test_low_volume_check_doesnt_trigger_when_under_threshold():
    daily_volumes = [100.0] * 28 + [10.0] * 2  # 6.6% low → fine
    result = check_low_volume_days_pct(
        daily_volumes=daily_volumes,
        trailing_90td_median=100.0,
    )
    assert result["triggered"] is False


# ---------------------------------------------------------------------------
# Coverage: pre-window delisting
# ---------------------------------------------------------------------------


def test_pre_window_delisting_triggers_on_window_status():
    result = check_pre_window_delisting(
        issuer_status="active",
        windows=[{"days": 30, "status": "delisted"}],
    )
    assert result["triggered"] is True


def test_pre_window_delisting_triggers_on_issuer_status():
    result = check_pre_window_delisting(
        issuer_status="delisted",
        windows=[{"days": 30, "status": "ok"}],
    )
    assert result["triggered"] is True


def test_pre_window_delisting_doesnt_trigger_when_clean():
    result = check_pre_window_delisting(
        issuer_status="active",
        windows=[{"days": 30, "status": "ok"}],
    )
    assert result["triggered"] is False


# ---------------------------------------------------------------------------
# assemble_verdict — verdict ladder
# ---------------------------------------------------------------------------


def _confounders(**flags):
    return {k: {"triggered": v, "evidence": {}} for k, v in flags.items()}


def _coverage(**flags):
    return {k: {"triggered": v, "evidence": {}} for k, v in flags.items()}


def test_clean_path_when_all_checks_pass():
    v = assemble_verdict(
        tradeable_filter_pass=True,
        confounders=_confounders(earnings=False, fomc=False),
        coverage=_coverage(yfinance=False, delisting=False),
    )
    assert v.verdict == "clean"
    assert v.reasons == []


def test_tradeable_filter_failure_short_circuits_to_discard():
    v = assemble_verdict(
        tradeable_filter_pass=False,
        confounders=_confounders(),
        coverage=_coverage(),
    )
    assert v.verdict == "discard"
    assert v.reasons == ["tradeable_filter_failed"]


def test_coverage_failure_overrides_confounder_signal():
    # Both fire — coverage wins because we discard rather than just flag.
    v = assemble_verdict(
        tradeable_filter_pass=True,
        confounders=_confounders(earnings=True),
        coverage=_coverage(yfinance=True),
    )
    assert v.verdict == "discard"
    assert "yfinance" in v.reasons


def test_confounder_only_yields_confounded_verdict():
    v = assemble_verdict(
        tradeable_filter_pass=True,
        confounders=_confounders(earnings=True),
        coverage=_coverage(yfinance=False),
    )
    assert v.verdict == "confounded"
    assert "earnings" in v.reasons


def test_verdict_db_row_shape():
    v = Q1Verdict(verdict="clean", reasons=[], confounders={}, coverage={})
    row = v.as_db_row()
    # All five expected columns present + correct types.
    assert set(row) == {
        "q1_verdict", "q1_reasons", "q1_confounders",
        "q1_coverage", "q1_audited_at",
    }
    assert row["q1_verdict"] == "clean"


def test_constants_match_plan():
    assert DEFAULT_EARNINGS_WINDOW_TD == 5
    assert T_PLUS_DAYS == 30
    assert 0.0 < LOW_VOLUME_DAY_PCT < 1.0
