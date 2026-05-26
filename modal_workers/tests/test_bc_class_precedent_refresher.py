"""WI-2 follow-up — bc_class_precedent_refresher tests.

Pure-function coverage of normalization, Wilson CI, bucketing, and
build_base_rate_rows. End-to-end `refresh` is driven through monkeypatched
fetch + upsert so the test stays Supabase-free.

Run: python3 -m pytest modal_workers/tests/test_bc_class_precedent_refresher.py -v
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from modal_workers.scripts.bc_class_precedent_refresher import (
    APPROVAL_TYPES,
    CRL_TYPES,
    DEFAULT_LOOKBACK_YEARS,
    BaseRateRow,
    ClassKey,
    bucket_decisions,
    build_base_rate_rows,
    normalize_class_field,
    refresh,
    wilson_ci,
)


# ---------------------------------------------------------------------------
# normalize_class_field
# ---------------------------------------------------------------------------


def test_normalize_lowercases_and_strips():
    assert normalize_class_field("  GLP-1 Agonist  ") == "glp-1 agonist"


def test_normalize_collapses_internal_whitespace():
    assert normalize_class_field("anti-VEGF\tmAb") == "anti-vegf mab"
    assert normalize_class_field("JAK   inhibitor") == "jak inhibitor"


def test_normalize_returns_empty_for_none_or_empty():
    assert normalize_class_field(None) == ""
    assert normalize_class_field("") == ""
    assert normalize_class_field("   ") == ""
    assert normalize_class_field(123) == ""  # non-string → empty


# ---------------------------------------------------------------------------
# wilson_ci
# ---------------------------------------------------------------------------


def test_wilson_ci_returns_none_for_zero_trials():
    assert wilson_ci(0, 0) == (None, None)


def test_wilson_ci_bounds_on_unanimous_success():
    low, high = wilson_ci(10, 10)
    # With 10/10 trials Wilson CI does NOT pin at 1.0 — it should leave headroom.
    assert low is not None and 0.7 < low < 1.0
    assert high == 1.0


def test_wilson_ci_bounds_on_unanimous_failure():
    low, high = wilson_ci(0, 10)
    assert low == 0.0
    assert high is not None and 0.0 < high < 0.3


def test_wilson_ci_centers_near_observed_rate():
    # 6/10 = 0.6; Wilson should give a sensible CI that contains 0.6.
    low, high = wilson_ci(6, 10)
    assert low is not None and high is not None
    assert low < 0.6 < high
    assert (high - low) > 0.3  # 10-sample interval is wide


def test_wilson_ci_narrows_with_more_data():
    _, high_10 = wilson_ci(6, 10)
    _, high_100 = wilson_ci(60, 100)
    assert high_100 is not None and high_10 is not None
    assert high_100 < high_10  # tighter at n=100


# ---------------------------------------------------------------------------
# bucket_decisions
# ---------------------------------------------------------------------------


def _row(*, moa: str, ind: str, etype: str) -> Dict[str, Any]:
    return {
        "event_type": etype,
        "event_status": "resolved",
        "fda_assets": {"mechanism": moa, "indication": ind},
    }


def test_bucket_groups_by_normalized_key():
    rows = [
        _row(moa="GLP-1 Agonist", ind="T2D", etype="approval"),
        _row(moa="glp-1 agonist", ind="t2d", etype="crl"),  # same bucket after normalize
        _row(moa="JAK inhibitor", ind="RA", etype="approval"),
    ]
    buckets = bucket_decisions(rows)
    assert len(buckets) == 2
    glp1_key = ClassKey(moa_canonical="glp-1 agonist", indication="t2d")
    assert buckets[glp1_key] == {"approvals": 1, "crls": 1}


def test_bucket_skips_rows_missing_moa_or_indication():
    rows = [
        _row(moa="", ind="T2D", etype="approval"),
        _row(moa="GLP-1", ind="", etype="crl"),
        {"event_type": "approval", "fda_assets": None},
        _row(moa="GLP-1", ind="T2D", etype="approval"),
    ]
    buckets = bucket_decisions(rows)
    assert len(buckets) == 1
    only_key = next(iter(buckets))
    assert only_key.moa_canonical == "glp-1"
    assert only_key.indication == "t2d"


def test_bucket_treats_presumed_crl_and_withdrawal_as_crls():
    rows = [
        _row(moa="X", ind="Y", etype="approval"),
        _row(moa="X", ind="Y", etype="presumed_crl"),
        _row(moa="X", ind="Y", etype="withdrawal"),
        _row(moa="X", ind="Y", etype="crl"),
    ]
    buckets = bucket_decisions(rows)
    only = buckets[ClassKey("x", "y")]
    assert only["approvals"] == 1
    assert only["crls"] == 3


def test_bucket_ignores_non_decision_event_types():
    rows = [
        _row(moa="X", ind="Y", etype="pdufa"),
        _row(moa="X", ind="Y", etype="adcom"),
        _row(moa="X", ind="Y", etype="phase3_readout"),
        _row(moa="X", ind="Y", etype="date_change"),
    ]
    assert bucket_decisions(rows) == {}


def test_decision_types_partition_correctly():
    # Constants are the contract; if someone adds a new event_type we want
    # the test to fail loudly so the bucket logic gets revisited.
    assert set(APPROVAL_TYPES) == {"approval"}
    assert set(CRL_TYPES) == {"crl", "presumed_crl", "withdrawal"}


# ---------------------------------------------------------------------------
# build_base_rate_rows
# ---------------------------------------------------------------------------


def test_build_skips_empty_buckets():
    buckets = {ClassKey("a", "b"): {"approvals": 0, "crls": 0}}
    assert build_base_rate_rows(buckets) == []


def test_build_emits_rate_and_ci_for_populated_bucket():
    buckets = {ClassKey("glp-1", "t2d"): {"approvals": 6, "crls": 4}}
    rows = build_base_rate_rows(buckets)
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, BaseRateRow)
    assert r.n_approvals == 6
    assert r.n_crls == 4
    assert r.approval_rate == 0.6
    assert r.ci_low is not None and r.ci_high is not None
    assert r.ci_low < 0.6 < r.ci_high
    assert r.lookback_years == DEFAULT_LOOKBACK_YEARS
    assert r.source == "fda_regulatory_events"


def test_build_respects_custom_lookback():
    buckets = {ClassKey("a", "b"): {"approvals": 1, "crls": 0}}
    rows = build_base_rate_rows(buckets, lookback_years=5)
    assert rows[0].lookback_years == 5


def test_build_row_db_shape_has_expected_columns():
    row = build_base_rate_rows(
        {ClassKey("a", "b"): {"approvals": 3, "crls": 1}}
    )[0]
    db = row.as_db_row()
    expected = {
        "moa_canonical", "indication", "n_approvals", "n_crls",
        "approval_rate", "ci_low", "ci_high", "lookback_years",
        "source", "refreshed_at",
    }
    assert set(db) == expected
    assert db["moa_canonical"] == "a"
    assert db["indication"] == "b"


# ---------------------------------------------------------------------------
# refresh (end-to-end, monkeypatched)
# ---------------------------------------------------------------------------


class _FakeSB:
    """Stand-in for SupabaseClient that records upsert calls."""

    def __init__(self, decisions: List[Dict[str, Any]]):
        self._decisions = decisions
        self.upsert_payloads: List[Any] = []

    def _rest_with_retry(self, method, path, **kwargs):
        if method == "GET" and path == "fda_regulatory_events":
            return self._decisions
        if method == "POST" and path == "fda_class_precedent_base_rates":
            self.upsert_payloads.append(kwargs.get("json_body"))
            return None
        raise AssertionError(f"unexpected call {method} {path}")


def test_refresh_dry_run_counts_without_upserting():
    sb = _FakeSB([
        _row(moa="GLP-1", ind="T2D", etype="approval"),
        _row(moa="GLP-1", ind="T2D", etype="crl"),
    ])
    result = refresh(sb, apply=False)
    assert result["decisions_fetched"] == 2
    assert result["class_buckets"] == 1
    assert result["rate_rows"] == 1
    assert result["written"] == 1
    assert sb.upsert_payloads == []  # nothing persisted


def test_refresh_apply_writes_one_payload_with_all_rows():
    sb = _FakeSB([
        _row(moa="GLP-1", ind="T2D", etype="approval"),
        _row(moa="GLP-1", ind="T2D", etype="crl"),
        _row(moa="JAK", ind="RA", etype="approval"),
    ])
    result = refresh(sb, apply=True)
    assert result["rate_rows"] == 2
    assert result["written"] == 2
    assert len(sb.upsert_payloads) == 1
    written = sb.upsert_payloads[0]
    assert len(written) == 2
    moas = sorted(r["moa_canonical"] for r in written)
    assert moas == ["glp-1", "jak"]


def test_refresh_with_no_decisions_skips_upsert():
    sb = _FakeSB([])
    result = refresh(sb, apply=True)
    assert result == {
        "decisions_fetched": 0,
        "class_buckets": 0,
        "rate_rows": 0,
        "written": 0,
    }
    assert sb.upsert_payloads == []
