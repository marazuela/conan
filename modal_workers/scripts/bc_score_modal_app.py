"""Standalone Modal app for the BC weekly-score worker live validation (Phase 1).

Kept separate from ``modal_workers/app.py`` (like ``phase0_modal_app.py``) so the
Phase-1 dry-run / gated apply does NOT validate or deploy the rest of the fleet —
this is ``modal run`` (ephemeral container), NOT ``modal deploy`` (no cron added,
nothing scheduled). The real cron wiring (``bc_weekly_score_once`` in app.py +
the ``public.scanners`` row) is a separate, later step — NOT done here.

Run via::

    # DRY-RUN: score the live universe, NO DB writes. Prints the band distribution
    # + per-name risk_band/p_crl/coverage. The key reviewable output.
    modal run modal_workers/scripts/bc_score_modal_app.py::bc_score_dryrun

    # Gated APPLY: write bc_rubric_scores + bc_application_features cols + refresh
    # bc_candidates (idempotent). --limit caps the names; omit for the full set.
    modal run modal_workers/scripts/bc_score_modal_app.py::bc_score_apply --limit=3
    modal run modal_workers/scripts/bc_score_modal_app.py::bc_score_apply

Secrets:
  scanner-secrets   — SEC_USER_AGENT (8-K EFTS), OPENFDA_API_KEY (Drugs@FDA;
                      absent => unauthenticated, fine for ~18 names)
  supabase-secrets  — SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
"""

from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any, Dict, Optional

import modal

app = modal.App("conan-bc-weekly-score")

# The scorer + percentile reference are DATA files (.json/.csv) — they are NOT
# shipped by add_local_python_source (which copies .py only). Mount the models
# dir explicitly so score_nda's MODEL_PATH and the locked-2025 reference resolve
# in-container at /root/modal_workers/bc_score/_m14/models/ (the bc_-owned M14
# scorer, re-vendored out of the torn-down shared/fda_crl/).
_MODELS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bc_score", "_m14", "models",
)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("requests>=2.31")
    .add_local_python_source("modal_workers")
    .add_local_dir(_MODELS_DIR, remote_path="/root/modal_workers/bc_score/_m14/models")
)

scanner_secrets = modal.Secret.from_name("scanner-secrets")
supabase_secrets = modal.Secret.from_name("supabase-secrets")


def _shaped(result: Dict[str, Any]) -> Dict[str, Any]:
    """JSON-able view of the run result (Modal serializes the return)."""
    return {
        "status": result["status"],
        "stats": result["stats"],
        "scored": [asdict(r) for r in result["scored"]],
    }


@app.function(image=image, timeout=1200, secrets=[scanner_secrets, supabase_secrets])
def bc_score_dryrun(
    limit: Optional[int] = None,
    application_number: Optional[str] = None,
    openfda_sleep_s: float = 0.2,
) -> Dict[str, Any]:
    """DRY-RUN: build + score the live Phase-0 universe, write NOTHING. Returns the
    full per-name result + band distribution + coverage. Reads live Drugs@FDA
    (priority/class/n_prior) + EFTS-by-CIK (8-K count) + the read-only universe."""
    from modal_workers.bc_score.run_weekly import run_weekly
    from modal_workers.shared.supabase_client import SupabaseClient

    result = run_weekly(
        SupabaseClient(), apply=False, limit=limit,
        application_number=application_number, openfda_sleep_s=openfda_sleep_s,
    )
    shaped = _shaped(result)
    _print_report(shaped, apply=False)
    return shaped


@app.function(image=image, timeout=1200, secrets=[scanner_secrets, supabase_secrets])
def bc_score_apply(
    limit: Optional[int] = None,
    application_number: Optional[str] = None,
    openfda_sleep_s: float = 0.2,
) -> Dict[str, Any]:
    """GATED APPLY: write bc_rubric_scores + bc_application_features M14 columns,
    refresh bc_candidates, open/close a bc_pipeline_runs row. Idempotent (single
    per-run scored_at; same-week re-run merges in place)."""
    from modal_workers.bc_score.run_weekly import run_weekly
    from modal_workers.shared.supabase_client import SupabaseClient

    result = run_weekly(
        SupabaseClient(), apply=True, limit=limit,
        application_number=application_number, openfda_sleep_s=openfda_sleep_s,
    )
    shaped = _shaped(result)
    _print_report(shaped, apply=True)
    return shaped


def _print_report(shaped: Dict[str, Any], *, apply: bool) -> None:
    stats = shaped["stats"]
    print("\n===== bc_weekly_score " + ("--apply" if apply else "DRY-RUN") + " (modal) =====")
    print(f"  status: {shaped['status']}")
    for k in ("n_in_universe", "n_scored", "n_failed", "n_refused", "n_low_coverage",
              "band_distribution", "coverage_hist", "matview_refreshed",
              "scored_snapshot_date", "scorer_version"):
        if k in stats:
            print(f"  {k}: {stats[k]}")
    print("\n--- per-name (p_crl is INTERNAL — operator review only) ---")
    rows = sorted(shaped["scored"], key=lambda r: (r.get("p_crl") if r.get("p_crl") is not None else -1.0), reverse=True)
    for r in rows:
        pc = f"{r['p_crl']:.4f}" if r.get("p_crl") is not None else "  -   "
        pct = f"{r['oof_percentile_rank']:5.1f}" if r.get("oof_percentile_rank") is not None else "  -  "
        cov = f"{r['coverage']:.2f}" if r.get("coverage") is not None else " -  "
        print(
            f"  {r.get('ticker') or '?':6s} {r.get('appl_type') or '?':3s} "
            f"pdufa={r.get('pdufa_date')} band={r.get('risk_band') or '-':8s} p_crl={pc} "
            f"pct={pct} cov={cov} fq={r.get('feature_quality') or '-':8s} "
            f"8k={r.get('n_8ks_30_180_clean')} ref={r.get('ref_date_source')} "
            f"flag={r.get('confidence_flag') or '-'}"
            f"{' REFUSED' if r.get('refusal_reason') else ''}{' ERR' if r.get('error') else ''} "
            f"appno={r.get('application_number')}"
        )
