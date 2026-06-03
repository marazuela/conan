"""Forward shadow-validation for the CRL rubric — the go/no-go readout.

Because the live DB has no app-numbered resolved history, the rubric can't be
back-tested against the past; it is validated FORWARD instead. With the Seam-2
override OFF (``FDA_CRL_OVERRIDE_ENABLED`` unset), every in-scope event records
``raw_inputs.crl.shadow_fair_probability`` (= 1 - crl_risk) alongside the live
base-rate ``fair_probability``. Once enough of those events RESOLVE, this report
asks the only question that matters for cutover:

    On the events the rubric covers, does ``shadow_fair_probability`` beat the
    base-rate ``fair_probability`` against realized outcomes?

It reuses the calibration engine's own outcome labeling (``label_from_row``) and
metrics (``brier_score`` / ``realized_ev``) so the verdict is consistent with the
existing backtest. Read-only; changes nothing. Until shadow data accumulates it
reports ``n=0`` / ``inconclusive``.

    python -m modal_workers.scripts.fda_crl_shadow_report --lookback-days 365
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Any, Dict, List, Mapping, Optional

from modal_workers.scripts.fda_calibration import label_from_row
from modal_workers.shared.fda_calibration_math import brier_score, realized_ev

logger = logging.getLogger("fda_crl_shadow_report")

# Cutover guardrails — mirror the calibration script's intent.
MIN_SAMPLE = 20            # resolved in-scope shadow events required for a verdict
MIN_REL_BRIER_GAIN = 0.02  # rubric must beat base-rate Brier by >=2% relative


def shadow_fair_probability(row: Mapping[str, Any]) -> Optional[float]:
    """Pull crl.shadow_fair_probability from a row, whether nested under
    raw_inputs.crl or already flattened onto the row."""
    val = row.get("shadow_fair_probability")
    if val is None:
        raw = row.get("raw_inputs")
        crl = raw.get("crl") if isinstance(raw, Mapping) else None
        if isinstance(crl, Mapping):
            val = crl.get("shadow_fair_probability")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def compare_rows(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    """Pure go/no-go comparison over loaded rows. Each row needs:
    catalyst_type, material_outcome, fair_probability, realized_price_move, and a
    shadow_fair_probability (nested or flat). Rows without a label or shadow value
    are dropped (and counted)."""
    base_p: List[float] = []
    shadow_p: List[float] = []
    outcomes: List[int] = []
    moves: List[float] = []
    dropped_no_label = 0
    dropped_no_shadow = 0
    dropped_no_baserate = 0

    for row in rows:
        label = label_from_row(row)
        if label is None:
            dropped_no_label += 1
            continue
        sp = shadow_fair_probability(row)
        if sp is None:
            dropped_no_shadow += 1
            continue
        bp = row.get("fair_probability")
        if bp is None:
            dropped_no_baserate += 1
            continue
        base_p.append(float(bp))
        shadow_p.append(sp)
        outcomes.append(label)
        moves.append(float(row.get("realized_price_move") or 0.0))

    n = len(outcomes)
    summary: Dict[str, Any] = {
        "n": n,
        "positives": sum(outcomes),
        "dropped_no_label": dropped_no_label,
        "dropped_no_shadow": dropped_no_shadow,
        "dropped_no_baserate": dropped_no_baserate,
        "thresholds": {"min_sample": MIN_SAMPLE, "min_rel_brier_gain": MIN_REL_BRIER_GAIN},
    }
    if n == 0:
        summary["verdict"] = "inconclusive"
        summary["reason"] = "no resolved in-scope shadow events yet"
        return summary

    brier_base = brier_score(base_p, outcomes)
    brier_shadow = brier_score(shadow_p, outcomes)
    rel_gain = (brier_base - brier_shadow) / brier_base if brier_base > 0 else 0.0
    summary.update(
        {
            "brier_base_rate": round(brier_base, 4),
            "brier_rubric_shadow": round(brier_shadow, 4),
            "brier_relative_gain": round(rel_gain, 4),
            "realized_ev_base": round(realized_ev(base_p, moves), 4),
            "realized_ev_rubric": round(realized_ev(shadow_p, moves), 4),
        }
    )
    if n < MIN_SAMPLE:
        summary["verdict"] = "insufficient_sample"
    elif brier_shadow < brier_base and rel_gain >= MIN_REL_BRIER_GAIN:
        summary["verdict"] = "go"  # rubric beats base-rate -> safe to enable override
    else:
        summary["verdict"] = "no_improvement"
    return summary


def load_rows(client: Any, lookback_days: int = 365) -> List[Dict[str, Any]]:
    """Thin loader: resolved calibration rows + per-event raw_inputs (for the
    shadow value). Reuses the existing ``fda_calibration_load`` RPC for the
    outcome set, then fetches raw_inputs by event_id. NOTE: confirm the RPC's
    request/response shape against prod before relying on the numbers."""
    base = client._rest(
        "POST", "rpc/fda_calibration_load", json_body={"lookback_days": str(lookback_days)}
    ) or []
    ids = [r["event_id"] for r in base if r.get("event_id")]
    raw_by_id: Dict[str, Any] = {}
    if ids:
        feats = client._rest(
            "GET", "fda_event_features",
            params={"event_id": f"in.({','.join(ids)})", "select": "event_id,raw_inputs"},
        ) or []
        raw_by_id = {f["event_id"]: f.get("raw_inputs") for f in feats}
    for r in base:
        r["raw_inputs"] = raw_by_id.get(r.get("event_id"))
    return base


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-days", type=int, default=365)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")

    from modal_workers.shared.supabase_client import SupabaseClient

    client = SupabaseClient(
        url=os.environ.get("SUPABASE_URL"),
        service_key=os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY"),
    )
    rows = load_rows(client, lookback_days=args.lookback_days)
    summary = compare_rows(rows)
    logger.info("CRL shadow vs base-rate:\n%s", json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
