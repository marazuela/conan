"""bc_pipeline_runs — the reusable fail-loud open/close helper for the BC-FDA monitor.

THE liveness sink for every BC Light v4 cron (Phase 0/1/2/3). The fail-loud principle
(build handoff §1.3): every cron opens a ``bc_pipeline_runs`` row at the top of its work
and closes it in a ``finally`` — so "did today's run write (and close) its row?" is the
single liveness signal, with NO watchdog meta-system (v4's watchdog went dark and blinded
itself; see memory ``cowork_session_halt``).

Phase 0 (``bc_universe_pdufa``) is this helper's **first consumer** — it is built here on the
0→1 critical path, and Phase 1 (``bc_weekly_score``), Phase 2 (monitor), and Phase 3 (digest)
import the same two functions so the open/close contract is identical across the pipeline
(phase0 §5.1, phase1 §5/§0.7).

Contract (verbatim from phase1 §5)::

    open_run(client, *, pipeline_name, snapshot_date) -> run_id   # status='running'
    close_run(client, run_id, *, status, n_processed, n_failed, cost_usd=0, log, reason=None)
                                                                  # status ∈ {succeeded,partial,failed}

CHECK-safety (verified live 2026-06-04 against xvwvwbnxdsjpnealarkh via
``bc_pipeline_runs_status_check``): ``status`` ∈ {running, succeeded, failed, partial}. This
helper enforces that domain **in Python** (raising ``ValueError`` before the POST/PATCH) so a
mistyped status (``'ok'``/``'error'`` — the tokens that 23514-failed earlier drafts) fails
loudly at the call site instead of as an opaque DB 400. Put nuance (``killed_budget``,
``skipped_no_entitlement``, phase provenance) in ``reason``/``log``, never in the enum
(landmine §1).

Idempotency / cost note: ``cost_usd`` defaults to 0 (no LLM is on the universe/score path).
``open_run`` uses ``return=representation`` to read back the generated ``id``; ``close_run``
is a no-op when ``run_id`` is falsy (so a failure *before* the row was opened never raises a
second, masking error inside the ``finally``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("bc_pipeline_runs")

# Live CHECK domain (bc_pipeline_runs_status_check). 'running' is the open state;
# the close states are the terminal three.
_OPEN_STATUS = "running"
_CLOSE_STATUSES = frozenset({"succeeded", "partial", "failed"})
_ALL_STATUSES = _CLOSE_STATUSES | {_OPEN_STATUS}

_TABLE = "bc_pipeline_runs"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_run(client: Any, *, pipeline_name: str, snapshot_date: str) -> Optional[str]:
    """INSERT a ``bc_pipeline_runs`` row in the open (``'running'``) state and return its id.

    Args:
        client: a ``SupabaseClient``-shaped object exposing
            ``_rest_with_retry(method, path, *, json_body, prefer)``.
        pipeline_name: the cron's name (e.g. ``'bc_universe_pdufa'``, ``'bc_weekly_score'``);
            ``bc_pipeline_runs.pipeline_name`` is NOT NULL.
        snapshot_date: the run's snapshot date (ISO ``YYYY-MM-DD``).

    Returns:
        The generated row ``id`` (str), or ``None`` if the insert returned no representation
        (the caller treats a ``None`` run_id as "couldn't open" — ``close_run`` no-ops on it).
    """
    rows = client._rest_with_retry(
        "POST",
        _TABLE,
        json_body=[{
            "pipeline_name": pipeline_name,
            "status": _OPEN_STATUS,
            "snapshot_date": snapshot_date,
            "started_at": _now_iso(),
        }],
        prefer="return=representation",
    )
    if isinstance(rows, list) and rows:
        return rows[0].get("id")
    return None


def close_run(
    client: Any,
    run_id: Optional[str],
    *,
    status: str,
    n_processed: int,
    n_failed: int,
    cost_usd: float = 0,
    log: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
) -> None:
    """PATCH a ``bc_pipeline_runs`` row to a terminal status. Call this in a ``finally``.

    Args:
        run_id: the id from :func:`open_run`. **No-op when falsy** — a crash before the row
            was opened must not raise a second, masking error inside the ``finally``.
        status: terminal status ∈ {succeeded, partial, failed}. Any other value raises
            ``ValueError`` (CHECK-safety enforced in Python — never let ``'ok'``/``'error'``
            reach the DB as a 23514).
        n_processed / n_failed: row counts for the liveness/coverage signal.
        cost_usd: marginal $ cost (default 0 — no LLM on this path).
        log: jsonb diagnostics (coverage, stats, error tail). Defaults to ``{}``.
        reason: short free-text reason (esp. on ``failed``/``partial``); the place for nuance
            tokens the enum CHECK forbids.
    """
    if status not in _CLOSE_STATUSES:
        raise ValueError(
            f"close_run status {status!r} not in {sorted(_CLOSE_STATUSES)} "
            "(bc_pipeline_runs.status CHECK; put nuance in reason/log, not the enum)"
        )
    if not run_id:
        # Opened-failed (or never opened): nothing to close. Log so the gap is visible but
        # never raise — this runs inside the worker's finally.
        logger.warning(
            "close_run called with no run_id (status=%s, reason=%s) — row was never opened",
            status, reason,
        )
        return
    client._rest_with_retry(
        "PATCH",
        f"{_TABLE}?id=eq.{run_id}",
        json_body={
            "status": status,
            "finished_at": _now_iso(),
            "n_processed": n_processed,
            "n_failed": n_failed,
            "cost_usd": cost_usd,
            "log": log if log is not None else {},
            "reason": reason,
        },
        prefer="return=minimal",
    )
