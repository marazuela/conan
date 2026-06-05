"""Unified FDA CRL scoring decision.

``score_crl`` routes a catalyst, runs the right model, and returns one
normalized decision consumed by both integration seams:

  - Seam 2 (source of truth): ``crl_risk`` (NDA calibrated P(CRL)) feeds
    ``fair_probability = 1 - crl_risk`` for in-scope originals.
  - Seam 1 (first filter): ``crl_scope`` + ``crl_confidence`` drive the
    reactor pre-gate's scope+coverage decline rule.

Output keys (all present, null where not applicable):
  crl_scope          'original' | 'efficacy_supplement' | 'refused'
  crl_risk           float P(CRL) for originals (NDA, calibrated); None otherwise
  crl_percentile     float 0..100 for efficacy supplements (sNDA rank); None otherwise
  crl_confidence     float 0..1 coverage/quality of the inputs
  crl_model_version  str model id used; None when refused
  crl_refusal_reason str|None
  crl_flags          str scorer confidence flags (NDA) or ''
"""

from __future__ import annotations

from typing import Optional

from modal_workers.bc_score._m14 import nda_scorer, snda_scorer
from modal_workers.bc_score._m14.percentile import to_percentile
from modal_workers.bc_score._m14.router import (
    EFFICACY_SUPPLEMENT,
    ORIGINAL,
    REFUSED,
    classify_scope,
)

# NDA confidence: multiplicative penalties per scorer flag.
_NDA_FLAG_FACTOR = {
    "standard": 1.0,
    "low_confidence_sponsor": 0.6,
    "moderate_confidence_no_edgar_signal": 0.8,
    "probability_extrapolation": 0.9,
}


def _nda_confidence(confidence_flag: str) -> float:
    factor = 1.0
    for flag in (confidence_flag or "").split(";"):
        factor *= _NDA_FLAG_FACTOR.get(flag.strip(), 1.0)
    return round(factor, 4)


def _refused(reason: str) -> dict:
    return {
        "crl_scope": REFUSED,
        "crl_risk": None,
        "crl_percentile": None,
        "crl_confidence": 0.0,
        "crl_model_version": None,
        "crl_refusal_reason": reason,
        "crl_flags": "",
    }


def score_crl(
    catalyst: dict,
    nda_features: Optional[dict] = None,
    snda_features: Optional[dict] = None,
) -> dict:
    """Route + score a catalyst into one normalized CRL decision.

    ``catalyst`` carries the routing metadata (application_type,
    submission_type, submission_class_code, flags). ``nda_features`` /
    ``snda_features`` are the assembled feature dicts (see feature_assembly,
    Phase 2). The scorer reads only the branch it needs.
    """
    route = classify_scope(catalyst)
    scope = route["scope"]

    if scope == REFUSED:
        return _refused(route["reason"] or "out_of_scope")

    if scope == ORIGINAL:
        scored = nda_scorer.score_nda(dict(nda_features or {}))
        if scored["confidence_flag"] == "refused":
            # Router said original but the scorer's own guardrail refused
            # (e.g. cycle_type/biosimilar leaked through) — honor it.
            return _refused(scored["refusal_reason"] or "nda_scorer_refused")
        return {
            "crl_scope": ORIGINAL,
            "crl_risk": float(scored["p_crl"]),
            "crl_percentile": None,
            "crl_confidence": _nda_confidence(scored["confidence_flag"]),
            "crl_model_version": scored["model_version"],
            "crl_refusal_reason": None,
            "crl_flags": scored["confidence_flag"],
        }

    if scope == EFFICACY_SUPPLEMENT:
        scored = snda_scorer.score_snda(dict(snda_features or {}))
        return {
            "crl_scope": EFFICACY_SUPPLEMENT,
            "crl_risk": None,  # rank-only; never surface the uncalibrated prob
            "crl_percentile": to_percentile(scored["raw_score"]),
            "crl_confidence": round(scored["coverage"], 4),
            "crl_model_version": scored["model_version"],
            "crl_refusal_reason": None,
            "crl_flags": "",
        }

    return _refused("unclassifiable_catalyst")
