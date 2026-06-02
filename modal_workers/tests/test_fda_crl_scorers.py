"""Phase-0 port-fidelity tests for the vendored FDA CRL scorers."""

from __future__ import annotations

import csv
import math
from pathlib import Path

from modal_workers.shared.fda_crl import nda_scorer, percentile, snda_scorer
from modal_workers.shared.fda_crl import router, score as crl_score

TESTDATA = Path(__file__).resolve().parent.parent / "shared" / "fda_crl" / "testdata"

_SCORE_FIELDS = [
    "p_crl",
    "raw_p_uncalibrated",
    "ci_low",
    "ci_high",
    "risk_band",
    "confidence_flag",
    "refusal_reason",
    "model_version",
]


def _read_csv(path: Path) -> list:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# --------------------------------------------------------------------------
# NDA — exact reproduction of the bundled example_input -> example_output
# --------------------------------------------------------------------------


def test_nda_reproduces_bundled_fixture_exactly():
    inputs = _read_csv(TESTDATA / "example_input.csv")
    expected = {r["ApplNo"]: r for r in _read_csv(TESTDATA / "example_output.csv")}
    assert inputs, "fixture inputs missing"

    for row in inputs:
        got = nda_scorer.score_nda(dict(row))
        exp = expected[row["ApplNo"]]
        for field in _SCORE_FIELDS:
            assert got[field] == exp[field], (
                f"ApplNo={row['ApplNo']} field={field}: got {got[field]!r} != {exp[field]!r}"
            )


def test_nda_refuses_supplement():
    out = nda_scorer.score_nda(
        {"ApplType": "NDA", "cycle_type": "supplemental", "n_prior_filings": "5"}
    )
    assert out["confidence_flag"] == "refused"
    assert out["p_crl"] == ""
    assert "first_cycle_orig" in out["refusal_reason"]


def test_nda_refuses_biosimilar_bla():
    out = nda_scorer.score_nda(
        {"ApplType": "BLA", "cycle_type": "first_cycle_orig", "is_biosimilar_bla": "1"}
    )
    assert out["confidence_flag"] == "refused"
    assert "biosimilar" in out["refusal_reason"].lower()


def test_nda_priority_alias_normalization():
    # 'PR' / 'Priority Review' / 'P' must all read as priority=1 (lowers p_crl).
    base = {"ApplType": "NDA", "cycle_type": "first_cycle_orig", "n_prior_filings": "4", "n_8ks_30_180_clean": "1"}
    std = nda_scorer.score_nda({**base, "ReviewPriority": "Standard Review"})
    for alias in ("PR", "Priority Review", "P", "PRIORITY"):
        pri = nda_scorer.score_nda({**base, "ReviewPriority": alias})
        assert float(pri["p_crl"]) < float(std["p_crl"]), f"alias {alias!r} not treated as priority"


def test_nda_deterministic():
    row = {"ApplType": "BLA", "cycle_type": "first_cycle_orig", "n_prior_filings": "4", "n_8ks_30_180_clean": "2"}
    assert nda_scorer.score_nda(dict(row)) == nda_scorer.score_nda(dict(row))


# --------------------------------------------------------------------------
# sNDA — golden math (rank-only), coverage, percentile
# --------------------------------------------------------------------------


def test_snda_mean_vector_returns_sigmoid_intercept():
    model = snda_scorer.load_model()
    # A row equal to the training means standardizes to 0 -> z = intercept.
    mean_row = dict(model["standardize_mu"])
    out = snda_scorer.score_snda(mean_row)
    expected = 1.0 / (1.0 + math.exp(-model["intercept"]))
    assert math.isclose(out["raw_score"], expected, rel_tol=1e-12)
    assert out["coverage"] == 1.0
    assert out["calibrated"] is False


def test_snda_single_feature_offset_matches_hand_computation():
    model = snda_scorer.load_model()
    feat = "sponsor_has_prior_crl"
    # Set one feature to mean + 1 SD (standardized +1); others absent (neutral).
    row = {feat: model["standardize_mu"][feat] + model["standardize_sd"][feat]}
    out = snda_scorer.score_snda(row)
    z = model["intercept"] + model["coefficients"][feat] * 1.0
    expected = 1.0 / (1.0 + math.exp(-z))
    assert math.isclose(out["raw_score"], expected, rel_tol=1e-12)
    assert out["n_features_present"] == 1


def test_snda_empty_features_is_neutral_zero_coverage():
    model = snda_scorer.load_model()
    out = snda_scorer.score_snda({})
    expected = 1.0 / (1.0 + math.exp(-model["intercept"]))
    assert math.isclose(out["raw_score"], expected, rel_tol=1e-12)
    assert out["coverage"] == 0.0


def test_percentile_against_explicit_reference():
    ref = [0.1, 0.2, 0.3]
    assert percentile.to_percentile(0.05, ref) == 0.0
    assert percentile.to_percentile(0.25, ref) == 100.0 * 2 / 3
    assert percentile.to_percentile(0.99, ref) == 100.0


def test_percentile_bundled_reference_monotonic():
    lo = percentile.to_percentile(0.01)
    hi = percentile.to_percentile(0.99)
    assert 0.0 <= lo <= hi <= 100.0
    assert hi > lo


# --------------------------------------------------------------------------
# Router — scope classification
# --------------------------------------------------------------------------


def test_router_original_nda():
    r = router.classify_scope({"application_type": "NDA", "submission_type": "ORIG"})
    assert r["scope"] == router.ORIGINAL


def test_router_efficacy_supplement():
    r = router.classify_scope(
        {"application_type": "sNDA", "submission_type": "SUPPL", "submission_class_code": "TYPE 6 - NEW INDICATION"}
    )
    assert r["scope"] == router.EFFICACY_SUPPLEMENT


def test_router_refuses_biosimilar():
    r = router.classify_scope({"application_type": "BLA", "is_biosimilar": True})
    assert r["scope"] == router.REFUSED
    assert "biosimilar" in r["reason"]


def test_router_refuses_cmc_supplement():
    r = router.classify_scope(
        {"submission_type": "SUPPL", "submission_class_code": "MANUF (CMC)"}
    )
    assert r["scope"] == router.REFUSED
    assert "non_efficacy" in r["reason"]


def test_router_refuses_resubmission():
    r = router.classify_scope({"application_type": "NDA", "submission_type": "RESUBMISSION"})
    assert r["scope"] == router.REFUSED


# --------------------------------------------------------------------------
# Unified score_crl — both seams' decision
# --------------------------------------------------------------------------


def test_score_crl_original_sets_crl_risk():
    out = crl_score.score_crl(
        {"application_type": "BLA", "submission_type": "ORIG"},
        nda_features={"ApplType": "BLA", "cycle_type": "first_cycle_orig", "n_prior_filings": "4", "n_8ks_30_180_clean": "2"},
    )
    assert out["crl_scope"] == "original"
    assert isinstance(out["crl_risk"], float)
    assert out["crl_percentile"] is None
    assert 0.0 < out["crl_confidence"] <= 1.0
    assert out["crl_model_version"]


def test_score_crl_supplement_sets_percentile_not_risk():
    out = crl_score.score_crl(
        {"submission_type": "SUPPL", "submission_class_code": "EFFICACY"},
        snda_features={"sponsor_has_prior_crl": 1, "priority": 1},
    )
    assert out["crl_scope"] == "efficacy_supplement"
    assert out["crl_risk"] is None  # never surface uncalibrated prob
    assert 0.0 <= out["crl_percentile"] <= 100.0


def test_score_crl_refused_is_terminal():
    out = crl_score.score_crl({"application_type": "BLA", "is_biosimilar": True})
    assert out["crl_scope"] == "refused"
    assert out["crl_risk"] is None and out["crl_percentile"] is None
    assert out["crl_confidence"] == 0.0
    assert out["crl_refusal_reason"]


def test_score_crl_low_coverage_lowers_confidence():
    # No EDGAR signal + thin sponsor history -> NDA flags -> confidence < 1.
    out = crl_score.score_crl(
        {"application_type": "NDA", "submission_type": "ORIG"},
        nda_features={"ApplType": "NDA", "cycle_type": "first_cycle_orig", "n_prior_filings": "0"},
    )
    assert out["crl_scope"] == "original"
    assert out["crl_confidence"] < 1.0
