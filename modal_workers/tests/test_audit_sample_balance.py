"""WI-6 — Q2 sample-balance audit tests.

Pure-function coverage: cohort_hash determinism, Herfindahl math, per-axis
status, verdict ladder, gate_mode routing. No Supabase coupling.

Run: python -m pytest modal_workers/tests/test_audit_sample_balance.py -v
"""
from __future__ import annotations

import pytest

from modal_workers.scripts.audit_sample_balance import (
    DELISTED_STATUSES,
    HERFINDAHL_FAIL,
    HERFINDAHL_WARN,
    HIT_MISS_FAIL_LOW,
    HIT_MISS_WARN_LOW,
    SURVIVORSHIP_FAIL_N_FLOOR,
    SURVIVORSHIP_WARN_PCT,
    Q2Verdict,
    assemble_q2_verdict,
    compute_cohort_hash,
    herfindahl,
)


# ---------------------------------------------------------------------------
# cohort_hash determinism
# ---------------------------------------------------------------------------


def test_cohort_hash_stable_regardless_of_order():
    pairs_a = [("asset-1", "2026-01-15"), ("asset-2", "2026-02-20")]
    pairs_b = [("asset-2", "2026-02-20"), ("asset-1", "2026-01-15")]
    assert compute_cohort_hash(pairs_a) == compute_cohort_hash(pairs_b)


def test_cohort_hash_changes_when_member_changes():
    base = [("asset-1", "2026-01-15"), ("asset-2", "2026-02-20")]
    perturbed = [("asset-1", "2026-01-15"), ("asset-3", "2026-02-20")]
    assert compute_cohort_hash(base) != compute_cohort_hash(perturbed)


def test_cohort_hash_truncated_to_16_chars():
    h = compute_cohort_hash([("a", "2026-01-01")])
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# Herfindahl math
# ---------------------------------------------------------------------------


def test_herfindahl_empty_is_zero():
    assert herfindahl([]) == 0.0


def test_herfindahl_uniform_distribution_is_inverse_n():
    # 4 distinct categories → HHI = 4 * (1/4)^2 = 0.25
    assert pytest.approx(herfindahl(["a", "b", "c", "d"]), abs=1e-9) == 0.25


def test_herfindahl_single_category_is_one():
    assert herfindahl(["a", "a", "a", "a"]) == 1.0


def test_herfindahl_drops_nulls_and_empty_strings():
    # 1 valid category → HHI = 1.0 (we count only "a" twice).
    assert herfindahl([None, "a", "", "a"]) == 1.0


# ---------------------------------------------------------------------------
# assemble_q2_verdict — clean pass
# ---------------------------------------------------------------------------


def _diverse_cohort(n: int = 100):
    """Generate a deliberately diverse cohort that should pass all axes."""
    pairs = [(f"asset-{i}", f"2026-{(i % 12) + 1:02d}-15") for i in range(n)]
    years = [f"202{i % 5}" for i in range(n)]  # 5 years × 20 each → HHI 0.20
    sectors = [f"sector-{i % 5}" for i in range(n)]
    sponsors = [f"sponsor-{i % 5}" for i in range(n)]
    return pairs, years, sectors, sponsors


def test_pass_when_all_axes_diverse_and_balanced():
    pairs, years, sectors, sponsors = _diverse_cohort(n=100)
    v = assemble_q2_verdict(
        cohort_pairs=pairs,
        n_hits=50, n_total=100,
        years=years, sectors=sectors, sponsors=sponsors,
        n_delisted_etc=10,  # 10% delisted-etc → above warn floor
    )
    assert v.verdict == "pass"
    assert all(ar.status == "pass" for ar in v.axes.values())
    assert v.phase5_triggers == []
    assert v.cohort_size == 100


# ---------------------------------------------------------------------------
# Verdict ladder — warn / fail
# ---------------------------------------------------------------------------


def test_warn_when_one_axis_warns():
    # Sponsor-concentration in [warn, fail) range. 100 rows, 4 sponsors
    # with shares 0.4, 0.3, 0.2, 0.1 → HHI = 0.30 (warn but not fail).
    pairs, years, sectors, _ = _diverse_cohort(n=100)
    sponsors = ["sponsor-a"] * 40 + ["sponsor-b"] * 30 + ["sponsor-c"] * 20 + ["sponsor-d"] * 10
    v = assemble_q2_verdict(
        cohort_pairs=pairs,
        n_hits=50, n_total=100,
        years=years, sectors=sectors, sponsors=sponsors,
        n_delisted_etc=10,
    )
    assert v.verdict == "pass_with_warnings"
    assert v.axes["sponsor_concentration"].status == "warn"
    # No phase5 triggers on warn — those are only emitted for fail axes.
    assert v.phase5_triggers == []


def test_fail_when_sponsor_concentration_exceeds_fail_floor():
    # Single dominant sponsor → HHI ≈ 1.0 → fail.
    pairs, years, sectors, _ = _diverse_cohort(n=100)
    sponsors = ["dominant-sponsor"] * 100
    v = assemble_q2_verdict(
        cohort_pairs=pairs,
        n_hits=50, n_total=100,
        years=years, sectors=sectors, sponsors=sponsors,
        n_delisted_etc=10,
    )
    assert v.verdict == "fail"
    assert v.axes["sponsor_concentration"].status == "fail"
    assert "broaden_sponsor_coverage" in v.phase5_triggers


def test_fail_when_hit_miss_ratio_at_extreme():
    pairs, years, sectors, sponsors = _diverse_cohort(n=100)
    v = assemble_q2_verdict(
        cohort_pairs=pairs,
        n_hits=95, n_total=100,  # 95% HIT rate — way above 0.80 fail
        years=years, sectors=sectors, sponsors=sponsors,
        n_delisted_etc=10,
    )
    assert v.verdict == "fail"
    assert v.axes["hit_miss_ratio"].status == "fail"
    assert "rebalance_hit_miss_distribution" in v.phase5_triggers


def test_fail_when_zero_delisted_in_large_cohort():
    pairs, years, sectors, sponsors = _diverse_cohort(n=100)
    v = assemble_q2_verdict(
        cohort_pairs=pairs,
        n_hits=50, n_total=100,
        years=years, sectors=sectors, sponsors=sponsors,
        n_delisted_etc=0,  # zero in n>50 → survivorship fail
    )
    assert v.verdict == "fail"
    assert v.axes["survivorship"].status == "fail"
    assert "run_survivorship_audit" in v.phase5_triggers


def test_small_cohort_zero_delisted_doesnt_fail_survivorship():
    # Below SURVIVORSHIP_FAIL_N_FLOOR, zero delisted isn't suspicious —
    # we just don't have enough sample to detect bias.
    pairs, years, sectors, sponsors = _diverse_cohort(n=30)
    v = assemble_q2_verdict(
        cohort_pairs=pairs,
        n_hits=15, n_total=30,
        years=years, sectors=sectors, sponsors=sponsors,
        n_delisted_etc=0,
    )
    assert v.axes["survivorship"].status != "fail"


def test_warn_count_at_least_one_triggers_pass_with_warnings():
    # Sponsor concentration warn-zone HHI = 0.30
    pairs, years, sectors, _ = _diverse_cohort(n=100)
    sponsors = ["sponsor-a"] * 40 + ["sponsor-b"] * 30 + ["sponsor-c"] * 20 + ["sponsor-d"] * 10
    v = assemble_q2_verdict(
        cohort_pairs=pairs,
        n_hits=50, n_total=100,
        years=years, sectors=sectors, sponsors=sponsors,
        n_delisted_etc=10,
    )
    # One warn axis (sponsor) — verdict pass_with_warnings.
    assert v.verdict == "pass_with_warnings"


# ---------------------------------------------------------------------------
# Empty cohort — fails hit_miss with n=0
# ---------------------------------------------------------------------------


def test_empty_cohort_fails():
    v = assemble_q2_verdict(
        cohort_pairs=[], n_hits=0, n_total=0,
        years=[], sectors=[], sponsors=[], n_delisted_etc=0,
    )
    assert v.verdict == "fail"
    assert v.cohort_size == 0


# ---------------------------------------------------------------------------
# Db-row shape
# ---------------------------------------------------------------------------


def test_verdict_db_row_shape():
    pairs, years, sectors, sponsors = _diverse_cohort(n=20)
    v = assemble_q2_verdict(
        cohort_pairs=pairs,
        n_hits=10, n_total=20,
        years=years, sectors=sectors, sponsors=sponsors,
        n_delisted_etc=2,
    )
    row = v.as_db_row()
    assert set(row) == {
        "cohort_hash", "cohort_size", "audit_date",
        "verdict", "axes", "phase5_triggers",
    }
    assert row["verdict"] in ("pass", "pass_with_warnings", "fail")
    # axes serialized as dict-of-dicts (not dataclass).
    assert isinstance(row["axes"], dict)
    assert all(isinstance(v, dict) for v in row["axes"].values())


# ---------------------------------------------------------------------------
# Constants regression
# ---------------------------------------------------------------------------


def test_thresholds_match_plan():
    assert HIT_MISS_WARN_LOW == 0.30
    assert HIT_MISS_FAIL_LOW == 0.20
    assert HERFINDAHL_WARN == 0.25
    assert HERFINDAHL_FAIL == 0.40
    assert SURVIVORSHIP_WARN_PCT == 0.05
    assert SURVIVORSHIP_FAIL_N_FLOOR == 50
    assert set(DELISTED_STATUSES) == {"delisted", "acquired", "bankrupt"}
