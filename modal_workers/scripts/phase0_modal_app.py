"""Modal app dedicated to Phase 0 close-out backfill scripts.

Kept separate from the main `modal_workers/app.py` so the Phase 0 close-out
doesn't depend on validating the rest of the fleet (e.g. legacy timeout=5 on
older app.py functions trips Modal's modern 10s floor).

Run via:
  modal run modal_workers/scripts/phase0_modal_app.py::phase0_curate_crl_from_edgar
  modal run modal_workers/scripts/phase0_modal_app.py::phase0_backfill_indications
  modal run modal_workers/scripts/phase0_modal_app.py::phase0_backfill_realized_move
  modal run modal_workers/scripts/phase0_modal_app.py::phase0_backfill_document_set
  modal run modal_workers/scripts/phase0_modal_app.py::phase0_run_baseline_eval

Optional kwargs (all have sensible defaults):
  --since=YYYY-MM-DD --until=YYYY-MM-DD --limit=N --dry-run=True

Secrets:
  scanner-secrets   — SEC_USER_AGENT, POLYGON_API_KEY
  supabase-secrets  — SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
"""

from __future__ import annotations

import modal

app = modal.App("conan-phase0-backfill")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "requests>=2.31",
        "beautifulsoup4>=4.12",
        "yfinance>=0.2",  # fallback price source for D2 when POLYGON_API_KEY unset
    )
    .add_local_python_source("modal_workers")
)

scanner_secrets = modal.Secret.from_name("scanner-secrets")
supabase_secrets = modal.Secret.from_name("supabase-secrets")


@app.function(image=image, timeout=3600,
              secrets=[scanner_secrets, supabase_secrets])
def phase0_curate_crl_from_edgar(
    since: str = "2023-01-01",
    until: str = "2024-12-31",
    dry_run: bool = False,
) -> int:
    from modal_workers.scripts.curate_crl_from_edgar import main
    argv = ["--since", since, "--until", until]
    if dry_run:
        argv.append("--dry-run")
    return main(argv)


@app.function(image=image, timeout=1800,
              secrets=[scanner_secrets, supabase_secrets])
def phase0_backfill_indications(limit: int = 200, dry_run: bool = False) -> int:
    from modal_workers.scripts.backfill_indications import main
    argv = ["--limit", str(limit)]
    if dry_run:
        argv.append("--dry-run")
    return main(argv)


@app.function(image=image, timeout=1800,
              secrets=[scanner_secrets, supabase_secrets])
def phase0_backfill_realized_move(limit: int = 500, dry_run: bool = False) -> int:
    from modal_workers.scripts.backfill_realized_move import main
    argv = ["--limit", str(limit)]
    if dry_run:
        argv.append("--dry-run")
    return main(argv)


@app.function(image=image, timeout=3600,
              secrets=[scanner_secrets, supabase_secrets])
def phase0_backfill_document_set(limit: int = 200, dry_run: bool = False) -> int:
    from modal_workers.scripts.backfill_document_set import main
    argv = ["--limit", str(limit)]
    if dry_run:
        argv.append("--dry-run")
    return main(argv)


@app.function(image=image, timeout=300, secrets=[supabase_secrets])
def phase0_run_baseline_eval(dry_run: bool = False) -> int:
    from modal_workers.scripts.run_baseline_eval import main
    argv = []
    if dry_run:
        argv.append("--dry-run")
    return main(argv)
