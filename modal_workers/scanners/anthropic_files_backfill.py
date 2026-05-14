"""anthropic_files_backfill — daily drain of unuploaded eligible documents.

Signal-less scanner that wraps the existing one-shot backfill script
(modal_workers/scripts/backfill_anthropic_files.py) so the standard dispatch
machinery (scanner_runs row, timing, error capture, cron registration) governs
it the same as every other scanner.

Rationale: the at-ingest path in DocumentWriter handles new documents, but the
backfill catches:
  - documents that predate the at-ingest wiring,
  - rows where the at-ingest upload transiently failed,
  - rows ingested by paths that don't pass upload_to_anthropic=True (operator
    scripts, manual imports).

Cost shape: limited to BACKFILL_DAILY_LIMIT rows/day so the ~11k existing
backlog drains over ~3 weeks at predictable cost. The actual byte-level cost
limit lives in modal_workers/shared/cost_budget.py (FILES_API_24H_HARD_USD).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from modal_workers.scripts.backfill_anthropic_files import run as backfill_run
from modal_workers.shared.cost_budget import check_files_api_hard_halt
from modal_workers.shared.scanner_base import ScannerResult
from modal_workers.shared.supabase_client import ScannerConfig, SupabaseClient

logger = logging.getLogger(__name__)

# Daily cap. Anthropic Files uploads are cheap individually but unbounded
# concurrency would let a bad day exhaust budget. 500/day drains 11k in ~3w.
# Tunable via env so operators can throttle without redeploying.
BACKFILL_DAILY_LIMIT = int(
    os.environ.get("ANTHROPIC_FILES_BACKFILL_DAILY_LIMIT", "500")
)


def scan(cfg: ScannerConfig) -> ScannerResult:
    """Entrypoint invoked by run_scanner. Emits no signals."""
    # Pre-flight: bail before any upload work if the 24h notional budget is
    # exhausted. The hard-halt opens an operator_flag automatically.
    sb = SupabaseClient()
    halt_state = check_files_api_hard_halt(sb)
    if halt_state["halt"]:
        logger.warning(
            "anthropic_files_backfill: HARD halt active "
            "(uploads_24h=%s notional_usd=%s); skipping run",
            halt_state["uploads_24h"], halt_state["total_24h_usd"],
        )
        return ScannerResult(
            scanner="anthropic_files_backfill",
            status="ok",  # halt is a budget signal, not a scanner failure
            signals=[],
            fetched_records=0,
            run_metrics={
                "skipped_reason": "files_api_24h_hard_halt",
                "daily_limit": BACKFILL_DAILY_LIMIT,
                **halt_state,
            },
        )

    summary: Dict[str, int] = backfill_run(limit=BACKFILL_DAILY_LIMIT)

    failed = summary.get("failed", 0)
    uploaded = summary.get("uploaded", 0)
    status = "ok"
    if summary.get("skipped_no_key"):
        status = "error"
    elif failed and not uploaded:
        status = "error"
    elif failed:
        status = "partial"

    return ScannerResult(
        scanner="anthropic_files_backfill",
        status=status,
        signals=[],
        fetched_records=summary.get("processed", 0),
        run_metrics={
            "daily_limit": BACKFILL_DAILY_LIMIT,
            **summary,
        },
    )
