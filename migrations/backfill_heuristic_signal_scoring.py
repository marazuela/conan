"""Backfill heuristic scoring provenance and replay safe signal rows.

Dry-run:
    python3 migrations/backfill_heuristic_signal_scoring.py --dry-run

Live:
    SUPABASE_URL=https://... \
    SUPABASE_SERVICE_ROLE_KEY=sbp_... \
    python3 migrations/backfill_heuristic_signal_scoring.py

By default, candidate-linked rows are reported but not automatically re-queued,
because a promoted candidate may need human review before its thesis state is
reset. Pass `--include-candidate-linked` to rewrite those signals too.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modal_workers.shared.dim_estimator import estimate_dimensions
from modal_workers.shared.market_snapshot import load_market_snapshot
from modal_workers.shared.rubric_engine import build_scoring_meta, score_signal
from modal_workers.shared.supabase_client import SupabaseClient

REPORT_PATH = REPO_ROOT / "migrations" / "backfill_heuristic_signal_scoring_report.json"
TARGET_PROFILES = ("binary_catalyst", "short_positioning", "takeover_candidate")
PAGE_SIZE = 200
SCRIPT_VERSION = "v1"

MODE_ALL = "all"
MODE_CANDIDATE_LINKED_ONLY = "candidate-linked-only"
MODE_ORPHANS_ONLY = "orphans-only"
VALID_MODES = (MODE_ALL, MODE_CANDIDATE_LINKED_ONLY, MODE_ORPHANS_ONLY)


def _quoted_in(values: Iterable[str]) -> str:
    cleaned = [value for value in values if value]
    return ",".join(f'"{value}"' for value in cleaned)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_supabase_env() -> None:
    missing = [
        name for name in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        raise SystemExit(
            "Missing required env vars: "
            + ", ".join(missing)
            + ". Set them before running this backfill."
        )


def _fetch_scored_signals(
    client: SupabaseClient,
    *,
    limit: Optional[int] = None,
    signal_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    signal_id_filter = f"in.({_quoted_in(signal_ids)})" if signal_ids else None
    offset = 0
    while True:
        page_limit = PAGE_SIZE if limit is None else min(PAGE_SIZE, max(limit - len(rows), 0))
        if page_limit <= 0:
            break
        page = client._rest(
            "GET",
            "signals",
            params={
                "select": (
                    "signal_id,entity_id,scoring_profile,raw_payload,dimensions,extensions,score,band,"
                    "auto_caps_triggered,convergence_key,convergence_bonus,score_with_bonus,"
                    "band_with_bonus,convergence_evaluated_at"
                ),
                "scoring_profile": f"in.({','.join(TARGET_PROFILES)})",
                "score": "not.is.null",
                **({"signal_id": signal_id_filter} if signal_id_filter else {}),
                "order": "created_at.asc",
                "limit": str(page_limit),
                "offset": str(offset),
            },
        ) or []
        rows.extend(page)
        if len(page) < page_limit:
            break
        offset += page_limit
    return rows


def _fetch_thesis_jobs(
    client: SupabaseClient,
    signal_ids: List[str],
) -> Dict[str, Dict[str, Any]]:
    jobs: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(signal_ids), PAGE_SIZE):
        chunk = signal_ids[start:start + PAGE_SIZE]
        if not chunk:
            continue
        rows = client._rest(
            "GET",
            "thesis_jobs",
            params={
                "select": "id,signal_id,status,candidate_id",
                "signal_id": f"in.({_quoted_in(chunk)})",
            },
        ) or []
        for row in rows:
            jobs[row["signal_id"]] = row
    return jobs


def _fetch_entities(
    client: SupabaseClient,
    entity_ids: List[str],
) -> Dict[str, Dict[str, Any]]:
    entities: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(entity_ids), PAGE_SIZE):
        chunk = entity_ids[start:start + PAGE_SIZE]
        if not chunk:
            continue
        rows = client._rest(
            "GET",
            "entities",
            params={
                "select": "id,primary_ticker,primary_mic,name",
                "id": f"in.({_quoted_in(chunk)})",
            },
        ) or []
        for row in rows:
            entities[row["id"]] = row
    return entities


def _metrics_from_signals(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    scored_rows = [row for row in rows if row.get("score") is not None]
    exact_30 = sum(1 for row in scored_rows if float(row.get("score") or 0) == 30.0)
    scored_without_provenance = 0
    total_numeric_dims = 0
    total_threes = 0

    for row in scored_rows:
        dimensions = row.get("dimensions") if isinstance(row.get("dimensions"), dict) else {}
        if "_provenance" not in dimensions:
            scored_without_provenance += 1
        for key, value in dimensions.items():
            if key == "_provenance" or not isinstance(value, (int, float)):
                continue
            total_numeric_dims += 1
            if int(value) == 3:
                total_threes += 1

    return {
        "scored_rows": len(scored_rows),
        "exact_30_rows": exact_30,
        "pct_exact_30": round(100.0 * exact_30 / max(len(scored_rows), 1), 2),
        "scored_without_provenance": scored_without_provenance,
        "pct_scored_without_provenance": round(
            100.0 * scored_without_provenance / max(len(scored_rows), 1),
            2,
        ),
        "total_numeric_dims": total_numeric_dims,
        "numeric_dim_threes": total_threes,
        "pct_numeric_dim_threes": round(
            100.0 * total_threes / max(total_numeric_dims, 1),
            2,
        ),
    }


def _fetch_all_signal_metrics(client: SupabaseClient) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = client._rest(
            "GET",
            "signals",
            params={
                "select": "signal_id,score,dimensions",
                "order": "created_at.asc",
                "limit": str(PAGE_SIZE),
                "offset": str(offset),
            },
        ) or []
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return _metrics_from_signals(rows)


def _merge_extensions(
    existing: Any,
    scoring_meta: Dict[str, Any],
    *,
    batch_id: str,
    run_at: str,
) -> Dict[str, Any]:
    """Merge scoring_meta and stamp this run's backfill batch into extensions.

    The `backfill` key carries the UUID + run timestamp so replay paths and
    post-run MCP queries can find every row touched by a specific run without
    depending on the JSON report file (see --retry-reactor-failures-from).
    """
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged["scoring_meta"] = scoring_meta
    merged["backfill"] = {
        "batch_id": batch_id,
        "run_at": run_at,
        "script_version": SCRIPT_VERSION,
    }
    return merged


def _enriched_raw_payload(
    row: Dict[str, Any],
    entity: Optional[Dict[str, Any]],
    client: SupabaseClient,
) -> Dict[str, Any]:
    raw_payload = dict(row.get("raw_payload") or {})
    ticker = raw_payload.get("ticker")
    if not ticker and isinstance(raw_payload.get("tickers"), list):
        ticker_list = [value for value in raw_payload.get("tickers") or [] if isinstance(value, str)]
        ticker = ticker_list[0] if ticker_list else None
    if not ticker and entity:
        ticker = entity.get("primary_ticker")
    mic = entity.get("primary_mic") if entity else None
    if isinstance(ticker, str) and ticker:
        snapshot = load_market_snapshot(ticker, mic=mic, client=client)
        if snapshot:
            raw_payload.update(snapshot)
    return raw_payload


def _build_backfill_patch(
    row: Dict[str, Any],
    entity: Optional[Dict[str, Any]],
    client: SupabaseClient,
    *,
    batch_id: str,
    run_at: str,
) -> Dict[str, Any]:
    profile = row["scoring_profile"]
    raw_payload = _enriched_raw_payload(row, entity, client)
    estimate = estimate_dimensions(profile, raw_payload)

    if estimate is None:
        scoring_meta = build_scoring_meta(
            provenance="unscored",
            supported_dims=[],
            defaulted_dims=[],
            requires_resolution=True,
            missing_dimensions=[],
        )
        return {
            "dimensions": {},
            "extensions": _merge_extensions(
                row.get("extensions"),
                scoring_meta,
                batch_id=batch_id,
                run_at=run_at,
            ),
            "score": None,
            "band": None,
            "auto_caps_triggered": [],
            "requires_resolution": True,
        }

    scored = score_signal(
        {
            "scoring_profile": profile,
            "raw_data": {**raw_payload, "dimensions": estimate.dimensions},
        },
        provenance="heuristic",
    )
    return {
        "dimensions": estimate.with_provenance("heuristic"),
        "extensions": _merge_extensions(
            row.get("extensions"),
            estimate.scoring_meta("heuristic"),
            batch_id=batch_id,
            run_at=run_at,
        ),
        "score": scored["score"],
        "band": scored["band"],
        "auto_caps_triggered": scored["auto_caps_triggered"],
        "requires_resolution": estimate.requires_resolution,
    }


def _signals_patch(row: Dict[str, Any], recomputed: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "dimensions": recomputed["dimensions"],
        "extensions": recomputed["extensions"],
        "score": recomputed["score"],
        "band": recomputed["band"],
        "auto_caps_triggered": recomputed["auto_caps_triggered"],
        "convergence_key": None,
        "convergence_bonus": 0,
        "score_with_bonus": None,
        "band_with_bonus": None,
        "convergence_evaluated_at": None,
    }


def _row_needs_update(row: Dict[str, Any], patch: Dict[str, Any]) -> bool:
    for key, value in patch.items():
        if row.get(key) != value:
            return True
    return False


def _upsert_needs_scoring(client: SupabaseClient, signal_id: str) -> None:
    client._rest(
        "POST",
        "thesis_jobs",
        params={"on_conflict": "signal_id"},
        json_body={
            "signal_id": signal_id,
            "status": "needs_scoring",
            "started_at": None,
            "completed_at": None,
        },
        prefer="resolution=merge-duplicates,return=representation",
    )


def _replay_reactor(client: SupabaseClient, record: Dict[str, Any]) -> requests.Response:
    response = requests.post(
        f"{client.url}/functions/v1/reactor",
        json={
            "type": "INSERT",
            "table": "signals",
            "schema": "public",
            "record": record,
            "old_record": None,
        },
        headers={
            "Authorization": f"Bearer {client.service_key}",
            "apikey": client.service_key,
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    return response


def _fetch_batch_replay_candidates(
    client: SupabaseClient,
    batch_id: str,
) -> List[Dict[str, Any]]:
    """Find rows touched by a given backfill batch that still need convergence.

    Resumability criteria (4.2):
      - `extensions.backfill.batch_id == :batch_id`  — this batch touched it
      - `score IS NOT NULL`                           — heuristic produced a score
      - `extensions.scoring_meta.requires_resolution = false` — not provisional
      - `band_with_bonus IS NULL`                     — reactor never completed

    These rows are ready for reactor replay; failures from the original backfill
    run fall into this set automatically, even if the JSON report file is lost.
    """
    candidates: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = client._rest(
            "GET", "signals",
            params={
                "select": "*",
                "extensions->backfill->>batch_id": f"eq.{batch_id}",
                "score": "not.is.null",
                "extensions->scoring_meta->>requires_resolution": "eq.false",
                "band_with_bonus": "is.null",
                "order": "created_at.asc",
                "limit": str(PAGE_SIZE),
                "offset": str(offset),
            },
        ) or []
        candidates.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return candidates


def _retry_reactor_failures(
    *,
    report_path: Optional[Path] = None,
    batch_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Replay reactor for rows left without convergence by a prior backfill.

    Two resumption modes (4.2):
      - `batch_id`: DB-authoritative. Queries signals.extensions.backfill to
        rebuild the replay set — survives report-file loss.
      - `report_path`: legacy. Reads `reactor_failures` from the JSON report.
        Used for historical runs where batch_id wasn't stamped.

    Passing both is valid: the DB set is authoritative; the report list
    supplements with any signal_ids not captured by DB filters (e.g. rows
    from backfill runs before 4.1 landed).
    """
    if report_path is None and batch_id is None:
        raise SystemExit("must pass --batch-id or --retry-reactor-failures-from")

    _require_supabase_env()
    client = SupabaseClient()

    db_candidates: List[Dict[str, Any]] = []
    legacy_candidates: List[Dict[str, Any]] = []
    resolved_batch_id = batch_id

    # DB-authoritative path.
    if batch_id:
        db_candidates = _fetch_batch_replay_candidates(client, batch_id)

    # Legacy report-file path — used standalone, or as supplement.
    if report_path:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        # Pick up the batch_id from the report if we weren't given one — this
        # lets `--retry-reactor-failures-from report.json` still use the DB
        # query for reports produced after 4.1 shipped.
        if resolved_batch_id is None:
            resolved_batch_id = report.get("batch_id")
            if resolved_batch_id:
                db_candidates = _fetch_batch_replay_candidates(client, resolved_batch_id)
        seen_ids = {row["signal_id"] for row in db_candidates}
        failure_ids = [
            f.get("signal_id") for f in (report.get("reactor_failures") or [])
            if f.get("signal_id")
        ]
        for sid in failure_ids:
            if sid in seen_ids:
                continue
            rows = client._rest(
                "GET", "signals",
                params={"select": "*", "signal_id": f"eq.{sid}", "limit": "1"},
            ) or []
            if rows:
                legacy_candidates.append(rows[0])
                seen_ids.add(sid)

    all_candidates = db_candidates + legacy_candidates
    retried: List[Dict[str, Any]] = []
    for row in all_candidates:
        try:
            response = _replay_reactor(client, row)
            retried.append({
                "signal_id": row["signal_id"],
                "status": "ok" if response.ok else "error",
                "status_code": response.status_code,
                "body": response.text[:500] if not response.ok else None,
                "source": "db" if row in db_candidates else "legacy_report",
            })
        except requests.RequestException as exc:
            retried.append({
                "signal_id": row.get("signal_id"),
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "source": "db" if row in db_candidates else "legacy_report",
            })

    return {
        "ran_at_utc": _utc_now(),
        "mode": "retry_reactor_failures",
        "batch_id": resolved_batch_id,
        "source_report": str(report_path) if report_path else None,
        "db_state_candidates": len(db_candidates),
        "legacy_report_fallbacks": len(legacy_candidates),
        "retried": retried,
        "retried_ok": sum(1 for r in retried if r.get("status") == "ok"),
        "retried_error": sum(1 for r in retried if r.get("status") != "ok"),
    }


def _metrics_delta(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    delta: Dict[str, Any] = {}
    for key in (
        "scored_rows", "exact_30_rows", "scored_without_provenance",
        "total_numeric_dims", "numeric_dim_threes",
    ):
        delta[key] = (after.get(key) or 0) - (before.get(key) or 0)
    return delta


def _write_metrics_snapshot_flag(
    client: SupabaseClient,
    *,
    batch_id: str,
    run_at: str,
    dry_run: bool,
    mode: str,
    metrics_before: Dict[str, Any],
    metrics_after: Dict[str, Any],
    summary: Dict[str, Any],
) -> None:
    """Persist before/after metrics into operator_flags so the delta is
    retrievable without the JSON report file (4.4 contract)."""
    try:
        client._rest(
            "POST", "operator_flags",
            json_body={
                "severity": "info",
                "source": "backfill_heuristic_signal_scoring",
                "kind": "backfill_metrics_snapshot",
                "title": (
                    f"heuristic backfill {'(dry-run)' if dry_run else ''} "
                    f"batch={batch_id[:8]} mode={mode}"
                ),
                "evidence": {
                    "batch_id": batch_id,
                    "run_at": run_at,
                    "dry_run": dry_run,
                    "mode": mode,
                    "summary": summary,
                    "metrics_before": metrics_before,
                    "metrics_after": metrics_after,
                    "metrics_delta": _metrics_delta(metrics_before, metrics_after),
                },
            },
            prefer="return=minimal",
        )
    except Exception as e:  # noqa: BLE001
        # Metric-flag write must not break the backfill — the JSON report is
        # still the primary record; the flag is a convenience surface.
        print(f"warning: failed to write backfill_metrics_snapshot flag: {e}")


def backfill(
    *,
    dry_run: bool,
    include_candidate_linked: bool,
    limit: Optional[int],
    signal_ids: Optional[List[str]] = None,
    mode: str = MODE_ALL,
) -> Dict[str, Any]:
    if mode not in VALID_MODES:
        raise SystemExit(f"--mode must be one of {VALID_MODES}, got {mode!r}")
    _require_supabase_env()
    client = SupabaseClient()
    batch_id = str(uuid.uuid4())
    run_at = _utc_now()

    rows = _fetch_scored_signals(client, limit=limit, signal_ids=signal_ids)
    thesis_jobs = _fetch_thesis_jobs(client, [row["signal_id"] for row in rows])
    entity_ids = [row["entity_id"] for row in rows if row.get("entity_id")]
    entities = _fetch_entities(client, entity_ids)
    metrics_before = _fetch_all_signal_metrics(client)

    summary = {
        "batch_id": batch_id,
        "run_at": run_at,
        "mode": mode,
        "rows_examined": len(rows),
        "rows_updated": 0,
        "rows_unchanged": 0,
        "rows_replayed": 0,
        "rows_queued_for_scoring": 0,
        "rows_skipped_candidate_linked": 0,
        "rows_skipped_orphan_by_mode": 0,
        "rows_candidate_linked_seen": 0,
        "reactor_failures": 0,
    }
    skipped_candidate_linked: List[str] = []
    skipped_orphan_by_mode: List[str] = []
    # 4.3: split the changed-signals audit trail so operators can see how much
    # of the run touched live-candidate rows (higher blast radius) vs orphan
    # signals (safe churn).
    candidate_linked_rewrites: List[Dict[str, Any]] = []
    orphan_rewrites: List[Dict[str, Any]] = []
    reactor_failures: List[Dict[str, Any]] = []

    for row in rows:
        signal_id = row["signal_id"]
        thesis_job = thesis_jobs.get(signal_id)
        thesis_status = thesis_job.get("status") if thesis_job else None
        candidate_linked = bool(
            thesis_job and (
                thesis_job.get("candidate_id") is not None or
                thesis_status == "promoted"
            )
        )
        if candidate_linked:
            summary["rows_candidate_linked_seen"] += 1

        # Mode filter (4.3): `candidate-linked-only` drops orphans,
        # `orphans-only` drops candidate-linked, `all` defers to
        # include_candidate_linked.
        if mode == MODE_CANDIDATE_LINKED_ONLY and not candidate_linked:
            summary["rows_skipped_orphan_by_mode"] += 1
            skipped_orphan_by_mode.append(signal_id)
            continue
        if mode == MODE_ORPHANS_ONLY and candidate_linked:
            summary["rows_skipped_candidate_linked"] += 1
            skipped_candidate_linked.append(signal_id)
            continue
        if mode == MODE_ALL and candidate_linked and not include_candidate_linked:
            summary["rows_skipped_candidate_linked"] += 1
            skipped_candidate_linked.append(signal_id)
            continue

        recomputed = _build_backfill_patch(
            row,
            entities.get(row.get("entity_id")),
            client,
            batch_id=batch_id,
            run_at=run_at,
        )
        patch = _signals_patch(row, recomputed)
        if not _row_needs_update(row, patch):
            summary["rows_unchanged"] += 1
            continue

        change_entry = {
            "signal_id": signal_id,
            "scoring_profile": row["scoring_profile"],
            "score_before": row.get("score"),
            "score_after": patch["score"],
            "band_before": row.get("band"),
            "band_after": patch["band"],
            "requires_resolution": recomputed["requires_resolution"],
            "candidate_linked": candidate_linked,
            "queue_action": "needs_scoring" if recomputed["requires_resolution"] else "reactor_replay",
        }
        (candidate_linked_rewrites if candidate_linked else orphan_rewrites).append(change_entry)

        if dry_run:
            summary["rows_updated"] += 1
            if recomputed["requires_resolution"]:
                summary["rows_queued_for_scoring"] += 1
            else:
                summary["rows_replayed"] += 1
            continue

        updated = client._rest(
            "PATCH",
            "signals",
            params={"signal_id": f"eq.{signal_id}"},
            json_body=patch,
            prefer="return=representation",
        ) or []
        summary["rows_updated"] += 1
        updated_row = updated[0] if updated else {**row, **patch}

        if recomputed["requires_resolution"]:
            _upsert_needs_scoring(client, signal_id)
            summary["rows_queued_for_scoring"] += 1
            continue

        response = _replay_reactor(client, updated_row)
        if response.ok:
            summary["rows_replayed"] += 1
        else:
            summary["reactor_failures"] += 1
            reactor_failures.append(
                {
                    "signal_id": signal_id,
                    "status_code": response.status_code,
                    "body": response.text[:500],
                }
            )

    metrics_after = metrics_before if dry_run else _fetch_all_signal_metrics(client)

    _write_metrics_snapshot_flag(
        client,
        batch_id=batch_id,
        run_at=run_at,
        dry_run=dry_run,
        mode=mode,
        metrics_before=metrics_before,
        metrics_after=metrics_after,
        summary=summary,
    )

    result = {
        "ran_at_utc": run_at,
        "batch_id": batch_id,
        "dry_run": dry_run,
        "mode": mode,
        "candidate_linked_policy": (
            "include_promoted_and_candidate_linked"
            if include_candidate_linked or mode == MODE_CANDIDATE_LINKED_ONLY
            else "skip_promoted_or_candidate_linked"
        ),
        "include_candidate_linked": include_candidate_linked,
        "limit": limit,
        "signal_ids": signal_ids,
        "profiles": list(TARGET_PROFILES),
        "metrics_before": metrics_before,
        "metrics_after": metrics_after,
        "metrics_delta": _metrics_delta(metrics_before, metrics_after),
        "summary": summary,
        "candidate_linked_rewrites": candidate_linked_rewrites,
        "orphan_rewrites": orphan_rewrites,
        "changed_signals_sample": (candidate_linked_rewrites + orphan_rewrites)[:50],
        "skipped_candidate_linked": skipped_candidate_linked,
        "skipped_orphan_by_mode": skipped_orphan_by_mode,
        "reactor_failures": reactor_failures,
    }
    REPORT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["report_path"] = str(REPORT_PATH)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill heuristic scoring provenance for live signals")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--include-candidate-linked",
        action="store_true",
        help="Also rewrite rows whose thesis job is already linked to a candidate. "
             "Ignored when --mode is candidate-linked-only or orphans-only.",
    )
    parser.add_argument(
        "--mode",
        choices=VALID_MODES,
        default=MODE_ALL,
        help=(
            "Row selection mode. 'all' (default) mirrors the legacy behavior. "
            "'candidate-linked-only' processes only rows tied to live candidates "
            "(higher blast radius — use for targeted remediation). "
            "'orphans-only' processes only signals with no candidate link (safe churn)."
        ),
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--signal-ids",
        help="Optional comma-separated signal_id subset for staged or resumed runs.",
    )
    parser.add_argument(
        "--retry-reactor-failures-from",
        help="Path to a prior backfill report JSON. Replays only the recorded reactor failures.",
    )
    parser.add_argument(
        "--retry-batch",
        help=(
            "UUID of a prior backfill run (signals.extensions.backfill.batch_id). "
            "Replays every row touched by that batch which still needs convergence — "
            "survives loss of the JSON report file. Prefer this over "
            "--retry-reactor-failures-from for batches produced after 4.1 shipped."
        ),
    )
    args = parser.parse_args()

    if args.retry_reactor_failures_from or args.retry_batch:
        result = _retry_reactor_failures(
            report_path=Path(args.retry_reactor_failures_from) if args.retry_reactor_failures_from else None,
            batch_id=args.retry_batch,
        )
        print(json.dumps(result, indent=2))
        return

    signal_ids = [value.strip() for value in (args.signal_ids or "").split(",") if value.strip()]

    result = backfill(
        dry_run=args.dry_run,
        include_candidate_linked=args.include_candidate_linked,
        limit=args.limit,
        signal_ids=signal_ids or None,
        mode=args.mode,
    )
    print(json.dumps(result, indent=2))
    print(f"Full report: {result['report_path']}")


if __name__ == "__main__":
    main()
