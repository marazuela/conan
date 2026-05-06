"""Compute the Phase 0 baseline Brier score against eval_harness.

Phase 0 close-out — D5. The replay loader scaffold for Phase 2+ orchestrator
evaluation. Right now there's no orchestrator to evaluate, so we anchor the
calibration target with a class-frequency prior baseline. Every row gets the
same prediction p_approved = n_approved / n_total. Per-row Brier is
(p_predicted - y)² where y=1 for approved, 0 for CRL.

Analytically, mean Brier under a class-frequency prior = p * (1-p) where
p = p_approved. For balanced 30/20 split (p=0.6) → Brier = 0.24, well within
the plan's 0.10-0.30 acceptance band.

Inserts one row into eval_runs with orchestrator_version='baseline_class_prior_v0'.
Idempotent — re-running upserts if a row with the same orchestrator_version
already exists.

Run:
  python3 -m modal_workers.scripts.run_baseline_eval [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)


ORCHESTRATOR_VERSION = "baseline_class_prior_v0"
PROMPT_HASH = "n/a"
SANITY_BRIER_FLOOR = 0.10
SANITY_BRIER_CEILING = 0.30


@dataclass
class BaselineResult:
    n_total: int
    n_approved: int
    n_crl: int
    p_approved: float
    brier_score: float
    per_assessment_results: List[Dict[str, Any]]
    passed_gate: bool


def compute_baseline(rows: List[Dict[str, Any]]) -> BaselineResult:
    n_total = len(rows)
    if n_total == 0:
        return BaselineResult(
            n_total=0, n_approved=0, n_crl=0, p_approved=0.0,
            brier_score=0.0, per_assessment_results=[], passed_gate=False,
        )
    approved = [r for r in rows if r.get("realized_outcome") == "approved"]
    crl = [r for r in rows if r.get("realized_outcome") == "crl"]
    n_approved = len(approved)
    n_crl = len(crl)
    if n_approved + n_crl == 0:
        return BaselineResult(
            n_total=n_total, n_approved=0, n_crl=0, p_approved=0.0,
            brier_score=0.0, per_assessment_results=[], passed_gate=False,
        )
    p_approved = n_approved / (n_approved + n_crl)

    per_assessment_results: List[Dict[str, Any]] = []
    sse = 0.0
    for r in rows:
        outcome = r.get("realized_outcome")
        if outcome not in ("approved", "crl"):
            continue
        y = 1 if outcome == "approved" else 0
        err_sq = (p_approved - y) ** 2
        sse += err_sq
        per_assessment_results.append({
            "eval_id": r["id"],
            "asset_id": r.get("asset_id"),
            "realized_outcome": outcome,
            "p_predicted": round(p_approved, 6),
            "y": y,
            "brier_squared_error": round(err_sq, 6),
        })

    brier = sse / max(1, n_approved + n_crl)
    passed_gate = SANITY_BRIER_FLOOR <= brier <= SANITY_BRIER_CEILING

    return BaselineResult(
        n_total=n_approved + n_crl,
        n_approved=n_approved,
        n_crl=n_crl,
        p_approved=p_approved,
        brier_score=round(brier, 4),
        per_assessment_results=per_assessment_results,
        passed_gate=passed_gate,
    )


def upsert_eval_run(result: BaselineResult, client: SupabaseClient) -> str:
    note = (
        f"Phase 0 baseline anchor: class-frequency prior. "
        f"n={result.n_total} (approved={result.n_approved}, crl={result.n_crl}). "
        f"p_approved={result.p_approved:.4f}. "
        f"Brier sanity band [{SANITY_BRIER_FLOOR},{SANITY_BRIER_CEILING}]; "
        f"observed {result.brier_score}; passed_gate={result.passed_gate}."
    )

    # Check for an existing run with this version + prompt_hash; PATCH if so.
    existing = client._rest(
        "GET", "eval_runs",
        params={
            "select": "id",
            "orchestrator_version": f"eq.{ORCHESTRATOR_VERSION}",
            "prompt_hash": f"eq.{PROMPT_HASH}",
            "limit": "1",
        },
    ) or []

    body: Dict[str, Any] = {
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "prompt_hash": PROMPT_HASH,
        "brier_score": result.brier_score,
        "calibration_curve": {
            "type": "class_frequency_prior",
            "p_approved": round(result.p_approved, 6),
        },
        "ranking_auc": None,
        "per_assessment_results": result.per_assessment_results,
        "passed_gate": result.passed_gate,
        "notes": note,
    }

    if existing:
        run_id = existing[0]["id"]
        client._rest(
            "PATCH", "eval_runs",
            params={"id": f"eq.{run_id}"},
            json_body=body,
            prefer="return=minimal",
        )
        return run_id

    rows = client._rest(
        "POST", "eval_runs",
        json_body=body,
        prefer="return=representation",
    )
    return rows[0]["id"] if rows else ""


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="run_baseline_eval")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute and print the baseline but don't insert eval_runs row")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    client = SupabaseClient()

    rows = client._rest(
        "GET", "eval_harness",
        params={
            "select": "id,asset_id,realized_outcome,realized_outcome_data",
            "is_holdout": "eq.true",
            "realized_outcome": "in.(approved,crl)",
            "limit": "5000",
        },
    ) or []

    result = compute_baseline(rows)

    logger.info(
        "Baseline class-frequency prior on eval_harness: n_total=%d "
        "n_approved=%d n_crl=%d p_approved=%.4f Brier=%.4f passed_gate=%s",
        result.n_total, result.n_approved, result.n_crl,
        result.p_approved, result.brier_score, result.passed_gate,
    )

    if not result.passed_gate:
        logger.warning(
            "Brier score %.4f outside sanity band [%.2f, %.2f] — investigate",
            result.brier_score, SANITY_BRIER_FLOOR, SANITY_BRIER_CEILING,
        )

    if args.dry_run:
        return 0 if result.passed_gate else 1

    run_id = upsert_eval_run(result, client)
    logger.info("Wrote eval_runs row id=%s", run_id)
    return 0 if result.passed_gate else 1


if __name__ == "__main__":
    sys.exit(main())
