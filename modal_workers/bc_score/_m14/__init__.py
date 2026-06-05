"""Empirical FDA Complete Response Letter (CRL) risk models.

Source-of-truth regulatory-risk scorers for FDA binary catalysts:

- ``nda_scorer.score_nda``   — first-cycle original NDA/BLA, calibrated P(CRL).
- ``snda_scorer.score_snda`` — efficacy supplement, rank-only (use percentile).
- ``percentile.to_percentile`` — map an sNDA raw score to a triage percentile.
- ``router.classify_scope``  — original / efficacy_supplement / refused.
- ``score.score_crl``        — unified decision both seams consume.

Feature assembly (DB-coupled) lands in Phase 2.
"""

from __future__ import annotations

from modal_workers.bc_score._m14.nda_scorer import NDA_MODEL_VERSION, score_nda
from modal_workers.bc_score._m14.percentile import to_percentile
from modal_workers.bc_score._m14.router import classify_scope
from modal_workers.bc_score._m14.score import score_crl
from modal_workers.bc_score._m14.snda_scorer import score_snda

__all__ = [
    "score_crl",
    "classify_scope",
    "score_nda",
    "score_snda",
    "to_percentile",
    "NDA_MODEL_VERSION",
]
