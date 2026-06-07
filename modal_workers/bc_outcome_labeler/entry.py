"""Modal-free entry for the BC outcome labeler — so it runs IDENTICALLY from a
Cowork scheduled task, a CLI, or the Modal app. Sources the two inputs the
labeler needs (Polygon equity market data + Drugs@FDA submissions), then calls
``run_labeler``. LOGGING ONLY — it never touches ``bc_refit_log`` / ``l7.*``.

The ACTIVE trigger in v1 is the Cowork scheduled task ``bc-outcome-labeler``
($0, no Modal web-function slot). The Modal app + the pg_cron
``bc-outcome-labeler-daily`` job are a DORMANT alternative path (the cron exits
clean while ``internal_config.modal_url_bc_outcome_labeler`` is unset) — kept
ready for a future Modal-plan upgrade. Run exactly ONE of the two; if the Modal
path is ever lit, disable the Cowork task.

CLI:
    python3 -m modal_workers.bc_outcome_labeler.entry            # DRY-RUN (no writes)
    python3 -m modal_workers.bc_outcome_labeler.entry --apply    # persist outcomes
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def build_submissions_by_app(
    client: Any,
    *,
    today_iso: str,
    openfda_sleep_s: float = 0.2,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return ``{application_number: [submission rows]}`` for resolved/maturing
    candidates (pdufa_date <= today), via the cached Drugs@FDA client the scorer
    uses. Surrogate ``EDGAR8K:`` appnos have no Drugs@FDA record and are skipped
    (the resolver then degrades to the PDUFA-extension branch — never crashes)."""
    from modal_workers.shared.feature_builder_pit import DrugsFDA, appl_is_bla

    rows = client.select(
        "bc_candidates",
        params={
            "select": "application_number,pdufa_date",
            "pdufa_date": f"lte.{today_iso}",
            "order": "pdufa_date.desc",
        },
    ) or []

    dfda = DrugsFDA(sleep_s=openfda_sleep_s)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        appno = str(r.get("application_number") or "").strip()
        if not appno or appl_is_bla(appno) is None:
            continue
        rec = dfda.application(appno)
        if not rec:
            continue
        subs = rec.get("submissions") or []
        if subs:
            out[appno] = subs
    return out


def run_once(apply: bool, *, openfda_sleep_s: float = 0.2) -> Dict[str, Any]:
    """Build inputs + run the labeler. ``run_labeler`` reads the candidate universe,
    pairs the pre-PDUFA score, and (when apply) opens/closes the fail-loud
    bc_pipeline_runs row itself. ``crl_records=None`` -> degrade to Drugs@FDA per
    design. market_data is None when POLYGON_API_KEY is unset (prices logged null)."""
    from modal_workers.bc_outcome_labeler.run_labeler import _build_market_data, run_labeler
    from modal_workers.shared.supabase_client import SupabaseClient

    client = SupabaseClient()
    today_iso = datetime.now(timezone.utc).date().isoformat()

    market_data = _build_market_data()
    submissions_by_app = build_submissions_by_app(
        client, today_iso=today_iso, openfda_sleep_s=openfda_sleep_s
    )
    return run_labeler(
        client,
        apply=apply,
        market_data=market_data,
        crl_records=None,
        submissions_by_app=submissions_by_app,
    )


def main(argv: Any = None) -> int:
    ap = argparse.ArgumentParser(description="BC outcome labeler (logging only)")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="persist bc_prediction_outcomes (default: dry-run, no DB writes)",
    )
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = run_once(apply=args.apply)
    print(json.dumps({"status": result.get("status"), "stats": result.get("stats")}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
