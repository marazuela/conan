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
import time
from datetime import datetime, timezone
from typing import Any, Dict

from modal_workers.ingestion.openfda_ingest import (
    DEEP_SWEEP_DAYS,
    DEFAULT_WALL_CLOCK_BUDGET_S,
    IngestRunResult,
    deep_sweep_openfda,
    ingest_drug_label_recent,
    ingest_drugsfda_approvals,
)
from modal_workers.shared.scanner_base import ScannerResult
from modal_workers.shared.supabase_client import ScannerConfig

logger = logging.getLogger(__name__)


_VALID_MODES = {"deep", "shallow"}


def _resolve_mode(now: datetime) -> str:
    override = os.environ.get("OPENFDA_INGEST_MODE")
    if override:
        normalized = override.strip().lower()
        if normalized in _VALID_MODES:
            return normalized
        # Typo or unrecognised value — log + fall through to weekday default
        # rather than silently running shallow when the operator asked for deep.
        logger.warning(
            "OPENFDA_INGEST_MODE=%r not in %s; falling through to weekday default",
            override, sorted(_VALID_MODES),
        )
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
        "wall_clock_timeout_hit": r.wall_clock_timeout_hit,
    }


def scan(cfg: ScannerConfig) -> ScannerResult:
    """Entrypoint invoked by run_scanner. Emits no signals.

    Computes a wall-clock deadline ~60s under the Modal function timeout so
    paginated ingest exits gracefully (with metrics flushed and scanner_runs
    marked 'partial') instead of getting SIGKILL'd mid-write. The 900s
    SIGKILL on 2026-05-11 left 3408 dailymed labels half-ingested + the
    scanner_runs row as status='error' with fetched_records=null, which then
    fed the asset_linker rate-limit cascade.

    Override via OPENFDA_WALL_CLOCK_BUDGET_S env var (operator backfills may
    want to disable by setting it to a very large value).
    """
    now = datetime.now(timezone.utc)
    mode = _resolve_mode(now)

    budget_s = float(os.environ.get(
        "OPENFDA_WALL_CLOCK_BUDGET_S", DEFAULT_WALL_CLOCK_BUDGET_S))
    deadline = time.time() + budget_s

    if mode == "deep":
        results = deep_sweep_openfda(
            days=DEEP_SWEEP_DAYS, wall_clock_deadline=deadline,
        )
    else:
        results = {
            "drugsfda": ingest_drugsfda_approvals(wall_clock_deadline=deadline),
            "label": ingest_drug_label_recent(wall_clock_deadline=deadline),
        }

    total_seen = sum(r.documents_seen for r in results.values())
    total_written = sum(r.documents_written for r in results.values())
    total_errors = sum(r.errors for r in results.values())
    any_partial = any(r.wall_clock_timeout_hit for r in results.values())

    # Status precedence: 'error' for any actual error, else 'partial' if
    # wall-clock truncated, else 'ok'. 'partial' is a clean terminal state —
    # the row gets completed_at, metrics are persisted, the next cron tick
    # picks up where this one left off (via the sliding 30d window).
    if total_errors > 0:
        status = "partial" if any_partial else "error"
    elif any_partial:
        status = "partial"
    else:
        status = "ok"

    return ScannerResult(
        scanner="openfda_corpus_ingest",
        status=status,
        signals=[],
        fetched_records=total_seen,
        run_metrics={
            "mode": mode,
            "documents_written_total": total_written,
            "documents_errors_total": total_errors,
            "wall_clock_budget_s": budget_s,
            "wall_clock_truncated": any_partial,
            "feeds": {name: _ingest_to_dict(r) for name, r in results.items()},
        },
    )
