"""Modal wrapper for the RAG contextual augmenter (Phase 4.5 follow-up).

Runs `_run_augmenter_pass` (Haiku 4.5 contextual augmentation over chunks
where `contextual_prefix IS NULL`) as a server-side Modal function so the
key lives in the `anthropic-orchestrator` secret, NOT in the operator's
local shell. Spun out from orchestrator_app.py to avoid edit conflicts
with the other agent's actively-modified file.

Why separate from the local backfill_rag_corpus.py CLI:
  - The CLI's --augment runs locally and reads ANTHROPIC_API_KEY from the
    operator's shell. If the local key is rotated mid-run (per Pedro's
    rotate-after-share workflow), the augment phase fails on first call.
  - Modal-side: secret stays server-side, current API key is whatever's in
    the Modal secret regardless of local shell state. Safer + cheaper.

Cost: ~$15 for the full 3,149-doc corpus per the augmenter docstring math
($0.49/doc with prompt caching). Trigger via:

    modal run modal_workers/rag_augment_app.py::rag_augment_run

Idempotent: skips chunks that already have a contextual_prefix. Safe to
re-run incrementally.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import modal

app = modal.App("conan-v3-rag-augment")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "requests>=2.31",
        "anthropic>=0.50",
    )
    .add_local_python_source("modal_workers")
)

anthropic_secrets = modal.Secret.from_name("anthropic-orchestrator")
supabase_secrets = modal.Secret.from_name("supabase-secrets")


@app.function(
    image=image,
    timeout=14400,  # 4h cap; full 3,149-doc augment ≈ 30–60 min in practice
    secrets=[anthropic_secrets, supabase_secrets],
)
def rag_augment_run(
    limit: int = 4000,
    source: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run the contextual augmenter pass over chunks missing
    `contextual_prefix`.

    Parameters
    ----------
    limit : int
        Max chunks to process this run. The augmenter walks docs in
        document_chunks order and augments per-chunk.
    source : optional str
        Restrict to chunks from documents with this `source` value
        (e.g. 'edgar', 'clinicaltrials').
    dry_run : bool
        If True, walk + log work; do not write to chunks.contextual_prefix.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from modal_workers.shared.supabase_client import SupabaseClient

    sb = SupabaseClient()

    if dry_run:
        # Just count candidates without invoking Haiku.
        rows = sb._rest("GET", "document_chunks", params={
            "select": "id,document_id",
            "contextual_prefix": "is.null",
            "limit": str(limit),
        }) or []
        unique_docs = len({r.get("document_id") for r in rows if r.get("document_id")})
        return {
            "dry_run": True,
            "candidate_chunks": len(rows),
            "candidate_docs": unique_docs,
        }

    # Use the script's existing _run_augmenter_pass — it walks doc_ids in
    # batches, loads each doc's full text, and augments all of its chunks
    # in one cache-warm session.
    from modal_workers.scripts.backfill_rag_corpus import _run_augmenter_pass

    summary = _run_augmenter_pass(sb, limit=limit, source=source)
    return summary


@app.function(
    image=image,
    timeout=600,
    secrets=[supabase_secrets],
)
def rag_augment_status() -> Dict[str, Any]:
    """Quick state probe — counts chunks with vs without contextual_prefix
    so an operator can decide whether another augment pass is needed.
    """
    from modal_workers.shared.supabase_client import SupabaseClient
    sb = SupabaseClient()

    def _count(params: Dict[str, str]) -> int:
        params = {**params, "select": "id", "limit": "1"}
        # Use HEAD-style count via PostgREST `Prefer: count=exact`.
        # SupabaseClient doesn't expose that today; fall back to a body fetch
        # with limit=1 and read the Content-Range from the session.
        url = f"{sb.url}/rest/v1/document_chunks"
        import requests
        r = requests.get(
            url, params={**params, "limit": "1"},
            headers={
                **sb._session.headers,
                "Prefer": "count=exact",
            },
            timeout=sb.timeout,
        )
        cr = r.headers.get("content-range") or ""
        # Format: "0-0/<total>" or "*/<total>" when no rows.
        if "/" in cr:
            return int(cr.split("/")[-1])
        return 0

    total = _count({})
    augmented = _count({"contextual_prefix": "not.is.null"})
    pending = total - augmented
    return {
        "total_chunks": total,
        "augmented_chunks": augmented,
        "pending_augment_chunks": pending,
        "pct_augmented": round(100 * augmented / total, 1) if total > 0 else 0.0,
    }
