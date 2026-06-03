"""Unit tests for the CRL forward shadow-validation comparison core (no DB)."""

from __future__ import annotations

import pytest

from modal_workers.scripts import fda_crl_shadow_report as sr


def _row(kind, base, shadow, move=0.0, nested=True):
    """kind: 'approve' -> label 1 (fda_approval/yes); 'crl' -> label 0 (fda_crl)."""
    ct = "fda_approval" if kind == "approve" else "fda_crl"
    mat = "yes" if kind == "approve" else "no"
    row = {"catalyst_type": ct, "material_outcome": mat, "fair_probability": base,
           "realized_price_move": move}
    if shadow is not None:
        if nested:
            row["raw_inputs"] = {"crl": {"shadow_fair_probability": shadow}}
        else:
            row["shadow_fair_probability"] = shadow
    return row


def test_shadow_field_extraction_nested_and_flat():
    assert sr.shadow_fair_probability({"raw_inputs": {"crl": {"shadow_fair_probability": 0.7}}}) == 0.7
    assert sr.shadow_fair_probability({"shadow_fair_probability": 0.4}) == 0.4
    assert sr.shadow_fair_probability({"raw_inputs": {"crl": {}}}) is None
    assert sr.shadow_fair_probability({"raw_inputs": None}) is None


def test_verdict_go_when_rubric_beats_base_rate():
    rows = [_row("approve", 0.5, 0.9) for _ in range(13)] + [_row("crl", 0.5, 0.1) for _ in range(12)]
    out = sr.compare_rows(rows)
    assert out["n"] == 25
    assert out["brier_rubric_shadow"] < out["brier_base_rate"]
    assert out["brier_relative_gain"] >= 0.02
    assert out["verdict"] == "go"


def test_verdict_no_improvement_when_rubric_worse():
    # base-rate is well-calibrated (0.9/0.1); rubric shadow is uninformative (0.5).
    rows = [_row("approve", 0.9, 0.5) for _ in range(13)] + [_row("crl", 0.1, 0.5) for _ in range(12)]
    out = sr.compare_rows(rows)
    assert out["n"] == 25
    assert out["brier_rubric_shadow"] > out["brier_base_rate"]
    assert out["verdict"] == "no_improvement"


def test_verdict_insufficient_sample():
    rows = [_row("approve", 0.5, 0.95), _row("crl", 0.5, 0.05)] * 3  # n=6 < MIN_SAMPLE
    out = sr.compare_rows(rows)
    assert out["n"] == 6
    assert out["verdict"] == "insufficient_sample"


def test_no_rows_is_inconclusive():
    out = sr.compare_rows([])
    assert out["n"] == 0
    assert out["verdict"] == "inconclusive"


def test_drops_unlabeled_and_missing_shadow():
    rows = [
        _row("approve", 0.5, 0.9),                       # counted
        {"catalyst_type": "weird", "material_outcome": "", "fair_probability": 0.5,
         "raw_inputs": {"crl": {"shadow_fair_probability": 0.9}}},  # no label -> dropped
        _row("crl", 0.5, None),                          # no shadow -> dropped
    ]
    out = sr.compare_rows(rows)
    assert out["n"] == 1
    assert out["dropped_no_label"] == 1
    assert out["dropped_no_shadow"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
