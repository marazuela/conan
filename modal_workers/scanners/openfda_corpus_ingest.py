"""openfda_corpus_ingest — v3 RAG corpus ingestion (drugsfda + dailymed labels).

This scanner is signal-less: it writes to `documents` for the RAG corpus and
returns ScannerResult(signals=[]). It exists as a `modal_workers/scanners/`
shim so the standard `_run(scanner_name)` dispatcher (timing, error capture,
scanner_runs row) wraps the ingest the same way it wraps every other scanner.

Mode selection:
  - default ("shallow"): 30d window per feed, page-until-empty (the underlying
    ingest functions enforce MAX_PAGES_HARD_CAP).
  - "deep": 180d window. Triggered automatically on Sundays (UTC), or by setting
    `OPENFDA_INGEST_MODE=deep` in the environment. The Sunday auto-trigger keeps
    the registry to a single daily row while still giving us a weekly catch-up
    sweep for openFDA corrections that the 30d window slides past.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

from modal_workers.ingestion.openfda_ingest import (
    DEEP_SWEEP_DAYS,
    IngestRunResult,
    deep_sweep_openfda,
    ingest_drug_label_recent,
    ingest_drugsfda_approvals,
)
from modal_workers.shared.scanner_base import ScannerResult
from modal_workers.shared.supabase_client import ScannerConfig

logger = logging.getLogger(__name__)


def _resolve_mode(now: datetime) -> str:
    override = os.environ.get("OPENFDA_INGEST_MODE")
    if override:
        return override.strip().lower()
    # Sunday = 6 in datetime.weekday(). Sunday auto-deep folds the weekly
    # catch-up into the daily 06 UTC dispatch slot, so we don't need a second
    # registry row or a separate dispatcher cron.
    if now.weekday() == 6:
        return "deep"
    return "shallow"


def _ingest_to_dict(r: IngestRunResult) -> Dict[str, Any]:
    return {
        "documents_seen": r.documents_seen,
        "documents_written": r.documents_written,
        "documents_dedup_hit": r.documents_dedup_hit,
        "documents_skipped": r.documents_skipped,
        "errors": r.errors,
    }


def scan(cfg: ScannerConfig) -> ScannerResult:
    """Entrypoint invoked by run_scanner. Emits no signals."""
    now = datetime.now(timezone.utc)
    mode = _resolve_mode(now)

    if mode == "deep":
        results = deep_sweep_openfda(days=DEEP_SWEEP_DAYS)
    else:
        results = {
            "drugsfda": ingest_drugsfda_approvals(),
            "label": ingest_drug_label_recent(),
        }

    total_seen = sum(r.documents_seen for r in results.values())
    total_written = sum(r.documents_written for r in results.values())
    total_errors = sum(r.errors for r in results.values())

    status = "ok" if total_errors == 0 else "partial"
    return ScannerResult(
        scanner="openfda_corpus_ingest",
        status=status,
        signals=[],
        fetched_records=total_seen,
        run_metrics={
            "mode": mode,
            "documents_written_total": total_written,
            "documents_errors_total": total_errors,
            "feeds": {name: _ingest_to_dict(r) for name, r in results.items()},
        },
    )
