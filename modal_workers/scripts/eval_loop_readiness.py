"""Report whether the eval loop is ready for AI-vs-v4 comparisons."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

from modal_workers.shared.supabase_client import SupabaseClient


TARGET_FIELDS = ("target_type", "horizon_days", "label_rule")


def _rows(sb: SupabaseClient, table: str, params: Dict[str, str]) -> List[Dict[str, Any]]:
    return sb._rest("GET", table, params=params) or []


def build_report(sb: SupabaseClient, *, min_resolved: int) -> Dict[str, Any]:
    eval_rows = _rows(
        sb,
        "eval_harness",
        {
            "select": "id,asset_id,realized_outcome,is_holdout,document_set",
            "limit": "10000",
        },
    )
    resolved = [
        r for r in eval_rows
        if r.get("realized_outcome") not in (None, "", "pending")
    ]
    assessments = _rows(
        sb,
        "convergence_assessments",
        {
            "select": "id,target_type,horizon_days,label_rule,event_anchor",
            "orchestrator_version_v4": "is.true",
            "limit": "10000",
        },
    )
    missing_target = [
        r["id"] for r in assessments
        if any(r.get(field) is None for field in TARGET_FIELDS)
    ]
    target_distribution = Counter(
        f"{r.get('target_type')}:{r.get('label_rule')}"
        for r in assessments
        if all(r.get(field) is not None for field in TARGET_FIELDS)
    )
    return {
        "n_eval_rows": len(eval_rows),
        "n_resolved": len(resolved),
        "min_resolved_required": min_resolved,
        "resolved_gate_pass": len(resolved) >= min_resolved,
        "n_v4_assessments_checked": len(assessments),
        "n_missing_prediction_target": len(missing_target),
        "missing_prediction_target_ids": missing_target[:50],
        "target_distribution": dict(target_distribution),
        "ready": len(resolved) >= min_resolved and not missing_target,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="eval_loop_readiness")
    parser.add_argument("--min-resolved", type=int, default=200)
    args = parser.parse_args(argv)
    report = build_report(SupabaseClient(), min_resolved=args.min_resolved)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
