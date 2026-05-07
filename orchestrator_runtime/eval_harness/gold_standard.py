"""Eval-harness gold-standard loader.

Reads `eval_harness` rows from Supabase. Each row encodes a resolved historical
FDA signal: the asset, the reference assessment date (the "as of" date when the
orchestrator would have run), the realized outcome (approved/CRL/etc + market
move), and the document_set (uuids of documents available as of reference date).

Curation is a Phase 0 deliverable: ~50 historical PDUFAs from 2023-2025 nominated
by the operator, each with realized outcome + snapshotted document set. This
loader doesn't curate; it loads what's been curated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

from modal_workers.shared.supabase_client import SupabaseClient


@dataclass
class HarnessCase:
    id: str
    asset_id: str
    reference_assessment_date: date
    realized_outcome: str                # 'approved','crl','withdrawn','adcom_positive','adcom_negative', etc.
    realized_outcome_data: Dict[str, Any]
    document_set: List[str]              # document uuids available as of reference date
    is_holdout: bool
    difficulty: Optional[str]
    notes: Optional[str]


def load_holdout_set(client: Optional[SupabaseClient] = None) -> List[HarnessCase]:
    """Load all rows where is_holdout=true. This is the gated set used by CI.
    Non-holdout rows (is_holdout=false) are training/development examples."""
    return _load(client, holdout_filter=True)


def load_dev_set(client: Optional[SupabaseClient] = None) -> List[HarnessCase]:
    """Load is_holdout=false rows for prompt iteration without contaminating
    the gating set."""
    return _load(client, holdout_filter=False)


def load_all(client: Optional[SupabaseClient] = None) -> List[HarnessCase]:
    return _load(client, holdout_filter=None)


def _load(client: Optional[SupabaseClient], holdout_filter: Optional[bool]) -> List[HarnessCase]:
    sb = client or SupabaseClient()
    params: Dict[str, str] = {
        "select": ",".join([
            "id", "asset_id", "reference_assessment_date",
            "realized_outcome", "realized_outcome_data",
            "document_set", "is_holdout", "difficulty", "notes",
        ]),
    }
    if holdout_filter is True:
        params["is_holdout"] = "is.true"
    elif holdout_filter is False:
        params["is_holdout"] = "is.false"

    rows = sb._rest("GET", "eval_harness", params=params) or []
    return [_to_case(r) for r in rows]


def _to_case(row: Dict[str, Any]) -> HarnessCase:
    return HarnessCase(
        id=row["id"],
        asset_id=row["asset_id"],
        reference_assessment_date=date.fromisoformat(row["reference_assessment_date"]),
        realized_outcome=row["realized_outcome"],
        realized_outcome_data=row.get("realized_outcome_data") or {},
        document_set=row.get("document_set") or [],
        is_holdout=bool(row.get("is_holdout", True)),
        difficulty=row.get("difficulty"),
        notes=row.get("notes"),
    )


# ---------------------------------------------------------------------------
# Outcome → direction-correctness mapping
# ---------------------------------------------------------------------------
# The orchestrator emits a `thesis_direction` (long/short/neutral/straddle).
# To compute Brier we need a binary "direction was right" signal, which depends
# on the realized outcome and the predicted direction.
#
# The mapping below is the canonical one. Long is correct on approval, short
# on CRL/withdrawn, etc. Straddle is correct when the realized move exceeds
# the implied move (irrespective of sign).
# ---------------------------------------------------------------------------

POSITIVE_OUTCOMES = {"approved", "adcom_positive", "label_expansion_approved"}
NEGATIVE_OUTCOMES = {"crl", "adcom_negative", "withdrawn", "label_warning_added"}


def is_direction_correct(predicted_direction: str, case: HarnessCase) -> bool:
    """Returns True if the orchestrator's predicted direction matched the
    realized outcome. Handles long/short/neutral/straddle."""
    outcome = case.realized_outcome
    realized_move_pct = float(case.realized_outcome_data.get("realized_move_pct", 0.0))
    implied_move_pct = float(case.realized_outcome_data.get("implied_move_pct", 0.0))

    if predicted_direction == "long":
        return outcome in POSITIVE_OUTCOMES
    if predicted_direction == "short":
        return outcome in NEGATIVE_OUTCOMES
    if predicted_direction == "neutral":
        # Neutral is "correct" when realized move is small relative to implied.
        return abs(realized_move_pct) < max(implied_move_pct * 0.5, 3.0)
    if predicted_direction == "straddle":
        # Straddle pays when realized move exceeds implied (either direction).
        return abs(realized_move_pct) > implied_move_pct
    raise ValueError(f"Unknown thesis_direction: {predicted_direction!r}")
