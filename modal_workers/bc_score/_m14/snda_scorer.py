"""sNDA efficacy-failure CRL-risk scorer (efficacy supplements).

Full-fit standardized ridge-logistic from the pooled time-series-CV model
(`models/snda_pooled.json`, id `M_SNDA_EFFFAIL_POOLED_TSCV_v1`).

IMPORTANT — this model is RANK-ONLY. Its raw sigmoid output is NOT a
calibrated probability (calibration slope ~0.18) and the discrimination is
fragile (AUC 0.52-0.72 depending on reconstructed labels). Never surface the
raw score as a probability; convert it to a percentile via ``percentile.py``
and use the rank for triage only. See memory `fda-crl-risk-rubrics-external`.

Public entrypoint: ``score_snda(features: dict) -> dict``.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Optional

MODEL_PATH = Path(__file__).resolve().parent / "models" / "snda_pooled.json"


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


@lru_cache(maxsize=1)
def load_model(path: str = str(MODEL_PATH)) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _as_float(value: object) -> Optional[float]:
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def score_snda(features: dict, model: Optional[dict] = None) -> dict:
    """Score an efficacy supplement.

    ``features`` maps the 13 sNDA feature names to numeric values. A missing
    or non-numeric feature is treated as the training mean (standardized 0 =
    neutral) and counted against coverage. Returns the raw rank score plus a
    coverage fraction; the caller maps ``raw_score`` to a percentile.
    """
    model = model or load_model()
    mu = model["standardize_mu"]
    sd = model["standardize_sd"]
    coef = model["coefficients"]

    z = float(model["intercept"])
    n_present = 0
    for feat in model["features"]:
        raw = _as_float(features.get(feat))
        if raw is None:
            std = 0.0  # absent -> mean -> neutral
        else:
            n_present += 1
            denom = sd[feat] if abs(sd[feat]) > 1e-8 else 1.0
            std = (raw - mu[feat]) / denom
        z += coef[feat] * std

    raw_score = _sigmoid(z)
    coverage = n_present / len(model["features"])
    return {
        "raw_score": raw_score,
        "coverage": coverage,
        "n_features_present": n_present,
        "model_version": model["model_version"],
        "calibrated": False,
    }
