"""A0 offline point-in-time feature builder — THIN RE-EXPORT.

The implementation was promoted to the Modal-importable shared path
``modal_workers/shared/feature_builder_pit.py`` so the live weekly score worker
(``modal_workers/bc_score/run_weekly.py``) and this offline A0 study share ONE
feature substrate (Phase 1 §1.2 / reconciliation note §9.5; build-handoff
landmine §3). The shared module is byte-aligned to ``feature_assembly`` and
parity-tested, so A0's out-of-sample metrics transfer to the live scorer.

This shim preserves the prior ``analysis.bc_a0.feature_builder`` import surface
(``DrugsFDA``, ``build_features``, ``estimate_ref_date``, ``orig_submission``,
``parse_compact_date``, ``_shift``, ``_REVIEW_CLOCK_DAYS``) so the A0 consumers
(``build_cohort.py`` / ``score_and_metrics.py``) keep working unchanged.
``build_features`` here is the offline caller (submissions-API 8-K source,
no designation source) — identical behavior to before the promotion.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.shared.feature_builder_pit import (  # noqa: E402,F401
    _REVIEW_CLOCK_DAYS,
    _shift,
    DrugsFDA,
    appl_is_bla,
    build_features,
    estimate_ref_date,
    orig_submission,
    parse_compact_date,
)

__all__ = [
    "DrugsFDA",
    "appl_is_bla",
    "build_features",
    "estimate_ref_date",
    "orig_submission",
    "parse_compact_date",
    "_shift",
    "_REVIEW_CLOCK_DAYS",
]
