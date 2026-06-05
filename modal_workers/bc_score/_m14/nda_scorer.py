"""NDA M14-adjusted CRL-risk scorer (first-cycle original NDA/BLA).

Faithful port of the externally-validated stdlib scorer
(`score_m14_adjusted.py`, model id `M14_ADJUSTED_L1_ORIG_v1_1_2026-05-30`).
The coefficients + Platt calibration live in `models/nda_m14_adjusted.json`;
the scoring math is byte-reproducible against the bundled fidelity fixture
(`testdata/example_input.csv` -> `testdata/example_output.csv`).

Scope: first-cycle ORIGINAL NDA/BLA only. Refuses supplements, resubmissions,
and biosimilar BLAs (returns confidence_flag='refused' with a reason). The
calibrated probability is trustworthy for p < 0.35; above that it extrapolates.

Public entrypoint: ``score_nda(row: dict) -> dict``.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Optional

MODEL_PATH = Path(__file__).resolve().parent / "models" / "nda_m14_adjusted.json"
NDA_MODEL_VERSION = "M14_ADJUSTED_L1_ORIG_v1_1_2026-05-30"


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def logit(p: float) -> float:
    p = min(max(float(p), 1e-9), 1 - 1e-9)
    return math.log(p / (1 - p))


@lru_cache(maxsize=1)
def load_model(path: str = str(MODEL_PATH)) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def as_int(row: dict, key: str, default: int = 0) -> int:
    val = row.get(key, default)
    if val in ("", None):
        return default
    try:
        return int(float(val))
    except Exception:
        return default


def as_float(row: dict, key: str, default: float = 0.0) -> float:
    val = row.get(key, default)
    if val in ("", None):
        return default
    try:
        return float(val)
    except Exception:
        return default


def norm_text(value: object) -> str:
    return str(value or "").strip().upper().replace("-", " ").replace("_", " ")


def as_binary(row: dict, keys: tuple, default: int = 0) -> int:
    for key in keys:
        if key not in row or row.get(key) in ("", None):
            continue
        val = row.get(key)
        if isinstance(val, str):
            txt = norm_text(val)
            if txt in {"1", "TRUE", "T", "YES", "Y"}:
                return 1
            if txt in {"0", "FALSE", "F", "NO", "N"}:
                return 0
        try:
            return int(float(val) != 0.0)
        except Exception:
            continue
    return default


def first_float(row: dict, keys: tuple, default: float = 0.0) -> float:
    for key in keys:
        if key in row and row.get(key) not in ("", None):
            return as_float(row, key, default)
    return default


def is_priority_review(row: dict) -> int:
    if "priority" in row and row.get("priority") not in ("", None):
        return as_binary(row, ("priority",), 0)
    review_priority = norm_text(row.get("ReviewPriority", row.get("review_priority", "")))
    priority_tokens = {
        "PRIORITY",
        "PRIORITY REVIEW",
        "PR",
        "P",
        "EXPEDITED PRIORITY",
    }
    return int(review_priority in priority_tokens or "PRIORITY" in review_priority)


def risk_band(p: float) -> str:
    if p < 0.08:
        return "low"
    if p < 0.15:
        return "moderate"
    if p < 0.25:
        return "elevated"
    return "high"


def score_row(row: dict, model: Optional[dict] = None) -> dict:
    model = model or load_model()
    cycle_type = norm_text(row.get("cycle_type", "first_cycle_orig") or "first_cycle_orig").lower()
    cycle_type = cycle_type.replace(" ", "_")
    is_biosimilar_bla = as_binary(row, ("is_biosimilar_bla", "biosimilar", "is_biosimilar"), 0)
    if cycle_type != "first_cycle_orig":
        return refused(row, f"M14 adjusted trained only on first_cycle_orig; got cycle_type={cycle_type}", model)
    if is_biosimilar_bla == 1:
        return refused(row, "M14 adjusted excludes biosimilar BLAs", model)

    appl_type = norm_text(row.get("ApplType", row.get("appl_type", "")))
    submission_class = norm_text(row.get("SubmissionClassCode", row.get("submission_class", "")))
    n_prior = int(first_float(row, ("n_prior_filings", "sponsor_history", "n_prior_filing_events"), 0.0))
    n_inspections = first_float(row, ("n_drug_inspections_5y_fix", "n_drug_inspections_5y", "n_drug_inspections"), 0.0)

    feats = {
        "n_prior_filings_log": first_float(row, ("n_prior_filings_log",), math.log1p(max(0, n_prior))),
        "priority": is_priority_review(row),
        "is_bla": as_binary(row, ("is_bla",), int(appl_type == "BLA")),
        "type5_or_3": as_binary(row, ("type5_or_3",), int(submission_class in ("TYPE 3", "TYPE 5", "TYPE 3 4", "TYPE 5 6"))),
        "sponsor_has_orphan_history": as_binary(row, ("sponsor_has_orphan_history",), 0),
        "sponsor_has_warning": as_binary(row, ("sponsor_has_warning", "sponsor_warning", "has_warning_letter"), 0),
        "n_drug_inspections_log": first_float(row, ("n_drug_inspections_log",), math.log1p(max(0.0, n_inspections))),
        "has_bt": as_binary(row, ("has_bt", "breakthrough", "breakthrough_therapy"), 0),
        "has_ft": as_binary(row, ("has_ft", "fast_track"), 0),
        "has_aa": as_binary(row, ("has_aa", "accelerated_approval"), 0),
        "n_8ks_30_180_clean": first_float(row, ("n_8ks_30_180_clean", "n_8ks_30_180", "edgar_8k_count_30_180"), 0.0),
        "ctgov_failed_primary": as_binary(row, ("ctgov_failed_primary", "failed_primary"), 0),
        "ctgov_any_randomized": as_binary(row, ("ctgov_any_randomized", "ctgov_any_randomized_pre_event", "any_randomized"), 0),
    }

    z = model["lr_intercept"]
    for feature in model["features"]:
        z += model["lr_coefficients"][feature] * feats[feature]
    p_raw = sigmoid(z)
    cal = model["platt_calibration"]
    p_cal = sigmoid(cal["a"] * logit(p_raw) + cal["b"])

    width = 0.15
    flags = []
    edgar_count = feats["n_8ks_30_180_clean"]
    if n_prior < 2:
        width = 0.22
        flags.append("low_confidence_sponsor")
    if edgar_count <= 0:
        width = max(width, 0.18)
        flags.append("moderate_confidence_no_edgar_signal")
    if p_cal > 0.35:
        width += 0.05
        flags.append("probability_extrapolation")
    if not flags:
        flags.append("standard")

    out = dict(row)
    out.update(
        {
            "p_crl": f"{p_cal:.8f}",
            "raw_p_uncalibrated": f"{p_raw:.8f}",
            "ci_low": f"{max(0.0, p_cal - width):.8f}",
            "ci_high": f"{min(1.0, p_cal + width):.8f}",
            "risk_band": risk_band(p_cal),
            "confidence_flag": ";".join(flags),
            "refusal_reason": "",
            "model_version": model["model_version"],
        }
    )
    return out


def refused(row: dict, reason: str, model: Optional[dict] = None) -> dict:
    out = dict(row)
    out.update(
        {
            "p_crl": "",
            "raw_p_uncalibrated": "",
            "ci_low": "",
            "ci_high": "",
            "risk_band": "",
            "confidence_flag": "refused",
            "refusal_reason": reason,
            "model_version": (model or {}).get("model_version", NDA_MODEL_VERSION),
        }
    )
    return out


# Public alias — the pipeline calls this name.
score_nda = score_row
