"""One-shot Modal app for the Phase 4B eval_harness ETL.

Wraps `modal_workers.scripts.seed_eval_harness_db_etl.run_etl` so the
binary_catalyst staging file can be loaded into the live `eval_harness`
table from a Modal container that already has supabase-secrets mounted.

The staging file lives at `data/eval_harness_staging/binary_catalyst.json`
locally; the image mounts it at `/root/binary_catalyst.json` so the
function reads from a fixed path.

Deploy:  modal deploy modal_workers/eval_harness_etl_app.py
Run:     modal run modal_workers/eval_harness_etl_app.py::run_seed --apply
Backfill existing seeded document sets:
         modal run modal_workers/eval_harness_etl_app.py::run_seed \
           --apply --backfill-existing-document-set

Apart from the file mount and the secrets attachment, this is a thin
wrapper — all logic is in `seed_eval_harness_db_etl`.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import modal

app = modal.App("conan-v3-eval-harness-etl")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "requests>=2.31",
        "pydantic>=2",
    )
    .add_local_python_source("modal_workers")
    .add_local_file(
        "data/eval_harness_staging/binary_catalyst.json",
        remote_path="/root/binary_catalyst.json",
    )
)

supabase_secrets = modal.Secret.from_name("supabase-secrets")


@app.function(image=image, timeout=900, secrets=[supabase_secrets])
def run_seed(
    multi_asset: str = "newest",
    limit: Optional[int] = None,
    apply: bool = False,
    backfill_existing_document_set: bool = False,
) -> Dict[str, Any]:
    """Execute the Phase 4B subset ETL. Returns the EtlSummary as a dict."""
    from pathlib import Path
    from dataclasses import asdict

    from modal_workers.scripts.seed_eval_harness_db_etl import run_etl

    summary = run_etl(
        staging_path=Path("/root/binary_catalyst.json"),
        multi_asset=multi_asset,
        limit=limit,
        apply=apply,
        backfill_existing_document_set=backfill_existing_document_set,
    )
    return asdict(summary)
