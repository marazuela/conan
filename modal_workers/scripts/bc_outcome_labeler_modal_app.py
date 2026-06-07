"""Standalone Modal app for the BC outcome labeler (Phase 3 §4–§5; LOGGING ONLY).

Vendored P4 worker. When a watched PDUFA resolves, this records what actually
happened — the regulatory verdict (CRL Transparency > Drugs@FDA approval/withdrawal
> PDUFA extension) + the t+1/7/30 equity price reaction — paired with the band/p_crl
shown at prediction time, into ``bc_prediction_outcomes`` (3 idempotent rows/app).
It is LOGGING ONLY: it NEVER reads/writes ``bc_refit_log`` / ``l7.*`` (no refit loop;
the endorsed v4-light direction cut L7 to logging — §4.0). ``p_crl`` is read only here
(paired into ``scored_p_crl``) and NEVER surfaced.

Kept SEPARATE from ``modal_workers/app.py`` (like ``bc_score_modal_app.py`` /
``phase0_modal_app.py``) so deploying the labeler does NOT validate or redeploy the
rest of the orchestrator fleet (its deploy is separately gated).

────────────────────────────────────────────────────────────────────────────────
DEPLOY (OPERATOR-GATED — do NOT run from this build):

    modal deploy modal_workers/scripts/bc_outcome_labeler_modal_app.py

  After deploy, copy the printed ``bc-outcome-labeler`` web-endpoint URL into
  Supabase so the @22:00-UTC pg_cron ``bc-outcome-labeler-daily`` can invoke it::

    UPDATE public.internal_config
       SET value = '<https://…modal.run/…bc-outcome-labeler>'
     WHERE key = 'modal_url_bc_outcome_labeler';

  (Cron defined in supabase/migrations/20260620000020_bc_digest_outcome_crons.sql:
  it POSTs ``{"source":"pg_cron"}`` with ``x-conan-compute-secret`` +
  ``Authorization: Bearer <compute_secret>`` and exits clean if the URL is unset.)

EPHEMERAL VALIDATION (``modal run`` — no cron, nothing scheduled):

    # DRY-RUN: resolve + price the live universe, write NOTHING.
    modal run modal_workers/scripts/bc_outcome_labeler_modal_app.py::bc_outcome_labeler_dryrun

    # GATED APPLY: write bc_prediction_outcomes + open/close a bc_pipeline_runs row.
    modal run modal_workers/scripts/bc_outcome_labeler_modal_app.py::bc_outcome_labeler_apply

────────────────────────────────────────────────────────────────────────────────
Secrets:
  scanner-secrets   — OPENFDA_API_KEY (Drugs@FDA submissions; absent => unauthenticated,
                      fine for the small pending universe), SEC_USER_AGENT (unused here).
  supabase-secrets  — SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (REST writes).
  compute-auth      — CONAN_COMPUTE_SECRET (HTTP-endpoint bearer; matches Supabase
                      internal_config.compute_secret). Endpoint-only.
  POLYGON_API_KEY   — the Polygon price source (PolygonClient reads it from env);
                      provided in whichever secret carries it in this workspace. When
                      unset the labeler records verdicts with price null (graceful).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import modal
from fastapi import Header, HTTPException

app = modal.App("conan-bc-outcome-labeler")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("requests>=2.31", "fastapi[standard]")
    .add_local_python_source("modal_workers")
)

scanner_secrets = modal.Secret.from_name("scanner-secrets")
supabase_secrets = modal.Secret.from_name("supabase-secrets")
# CONAN_COMPUTE_SECRET — shared with Supabase internal_config.compute_secret; the
# bearer the pg_cron 'bc-outcome-labeler-daily' presents. Endpoint-only secret.
compute_auth_secrets = modal.Secret.from_name("compute-auth")


# --------------------------------------------------------------------------- #
# compute-secret auth (mirrors modal_workers/app.py::_verify_compute_secret;
# re-implemented locally so this standalone app does not import the fleet app).
# --------------------------------------------------------------------------- #
def _verify_compute_secret(provided: Optional[str]) -> None:
    """Raise HTTPException unless `provided` matches CONAN_COMPUTE_SECRET.

    401 on bad/missing header, 500 on server misconfiguration. Constant-time
    compare so an attacker can't learn the prefix byte-by-byte."""
    import hmac

    expected = os.environ.get("CONAN_COMPUTE_SECRET", "")
    if not expected:
        raise HTTPException(
            status_code=500,
            detail={"error": "server misconfiguration: CONAN_COMPUTE_SECRET not set"},
        )
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid or missing x-conan-compute-secret"},
        )


# --------------------------------------------------------------------------- #
# Run body — delegate to the Modal-free entry (the single source of truth for the
# input sourcing + run_labeler call, shared with the Cowork task / CLI). Keeping
# the sourcing OUT of this Modal app means the Cowork trigger and this dormant
# Modal path can never drift.
# --------------------------------------------------------------------------- #
def _run(apply: bool) -> Dict[str, Any]:
    from modal_workers.bc_outcome_labeler.entry import run_once

    result = run_once(apply=apply)
    _print_report(result, apply=apply)
    return {"status": result["status"], "stats": result["stats"]}


@app.function(image=image, timeout=600, secrets=[scanner_secrets, supabase_secrets])
def bc_outcome_labeler_dryrun() -> Dict[str, Any]:
    """DRY-RUN: resolve + price the live universe, write NOTHING (no bc_pipeline_runs
    row, no bc_prediction_outcomes upsert). Returns the per-app result + stats."""
    return _run(apply=False)


@app.function(image=image, timeout=600, secrets=[scanner_secrets, supabase_secrets])
def bc_outcome_labeler_apply() -> Dict[str, Any]:
    """GATED APPLY: write bc_prediction_outcomes (3 idempotent rows/resolved app) +
    open/close a bc_pipeline_runs(pipeline_name='bc_outcome_labeler') row. Idempotent
    (merge-upsert on the UNIQUE (application_number, horizon_days); null-omitting body
    so a partial row is filled, never clobbered)."""
    return _run(apply=True)


@app.function(
    image=image,
    timeout=600,
    secrets=[scanner_secrets, supabase_secrets, compute_auth_secrets],
)
@modal.fastapi_endpoint(method="POST", label="bc-outcome-labeler")
def bc_outcome_labeler_endpoint(
    payload: Optional[dict] = None,
    x_conan_compute_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """compute-secret-gated HTTP endpoint for the pg_cron 'bc-outcome-labeler-daily'.

    The cron POSTs ``{"source":"pg_cron"}`` with header ``x-conan-compute-secret``
    (and a Bearer Authorization carrying the same value). We verify the header, then
    run the labeler with apply=True. ``run_labeler`` opens/closes the fail-loud
    bc_pipeline_runs row itself, so this wrapper adds nothing to the write path."""
    _verify_compute_secret(x_conan_compute_secret)
    return _run(apply=True)


def _print_report(result: Dict[str, Any], *, apply: bool) -> None:
    stats = result.get("stats", {})
    print("\n===== bc_outcome_labeler " + ("--apply" if apply else "DRY-RUN") + " (modal) =====")
    print(f"  status: {result.get('status')}")
    for k in ("n_candidate_apps", "n_resolved", "n_wrote", "n_failed",
              "horizons", "grace_days", "crl_source_available",
              "stale_pending_unresolved"):
        if k in stats:
            print(f"  {k}: {stats[k]}")
    print("\n--- per-app (p_crl is INTERNAL — paired into scored_p_crl, never surfaced) ---")
    for r in result.get("results", []):
        if r.get("error"):
            print(f"  {r.get('application_number')}: ERROR {r['error']}")
            continue
        rets = r.get("returns") or {}
        print(
            f"  {str(r.get('application_number')):16s} "
            f"outcome={r.get('regulatory_outcome') or '-':9s} "
            f"src={r.get('source') or '-':14s} "
            f"band={r.get('band') or '-':8s} "
            f"hypo={r.get('hypothesis_outcome') or '-':24s} "
            f"ret={ {k: (round(v, 2) if v is not None else None) for k, v in rets.items()} } "
            f"wrote={r.get('wrote')} "
            f"price_log={r.get('price_log') or '-'} resolve_log={r.get('resolve_log') or '-'}"
        )
