"""v3 closed feedback loop — post-mortem runner (Stream 2, D-104 + Phase 8).

Drains rows from `post_mortem_queue` where `outcome_window_end < now()` and
`status='pending'`. For each:

  1. Looks up the convergence_assessment + fda_asset for context.
  2. Calls D-116's label_forward_returns.label_event() to compute the realized
     outcome (T+30/60/90/180 returns, HIT/MISS verdict).
  3. If no clean outcome (`hit is None`, e.g. delisted, halted, no SPY,
     unparseable): updates status='no_outcome' and skips post-mortem text.
  4. Otherwise:
     - Computes prediction_error (signed pp delta between conviction_pct
       and realized outcome score).
     - Calls Haiku 4.5 to generate a 200-word retrospective.
     - Updates post_mortem_queue: status='post_mortem_complete',
       realized_outcome jsonb, post_mortem_text, prediction_error, realized_at.
     - Refits reference_class_base_rates UPSERT (Wilson interval CI).
     - Appends a "Resolved post-mortems" entry to the asset memory file
       per Contract C5.

The runner is idempotent: rows already at status='post_mortem_complete' or
'no_outcome' are skipped. Modal scheduling lives in feedback_loop_app.py.

CLI: python -m modal_workers.shared.post_mortem_runner --dry-run --limit 10
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import math
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from modal_workers.shared.supabase_client import SupabaseClient

# Reuse D-116 forward-return labeling — single source of truth for HIT/MISS
# verdicts and per-window returns. We import lazily so this module is
# importable without yfinance installed (e.g. in a synthetic-data test).
def _import_label_event():
    from modal_workers.scripts.label_forward_returns import label_event, ForwardReturnLabel  # noqa: E402
    return label_event, ForwardReturnLabel

logger = logging.getLogger(__name__)

# Anthropic Haiku for post-mortem text. Keep cheap — these are diagnostic
# narratives, not user-facing analysis.
DEFAULT_HAIKU_MODEL = os.environ.get(
    "POSTMORTEM_HAIKU_MODEL", "claude-haiku-4-5-20251001"
)
DEFAULT_HAIKU_MAX_TOKENS = 600  # ~200 words + slack

# v3 FDA assets are scored as binary_catalyst for forward-return labeling.
DEFAULT_PROFILE_FOR_LABELING = "binary_catalyst"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ResolvedOutcome:
    """One drained post_mortem_queue row's resolution."""
    queue_id: str
    assessment_id: str
    asset_id: str
    predicted_conviction_pct: float
    predicted_direction: str
    status: str  # 'post_mortem_complete' | 'no_outcome' | 'skipped'
    skipped_reason: Optional[str] = None
    realized_outcome: Optional[Dict[str, Any]] = None
    prediction_error: Optional[float] = None
    post_mortem_text: Optional[str] = None
    reference_class: Optional[str] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def drain_resolved_queue(
    *,
    now: Optional[datetime] = None,
    batch_size: int = 50,
    dry_run: bool = False,
    skip_text_generation: bool = False,
    sb: Optional[SupabaseClient] = None,
    label_event_fn: Optional[Any] = None,
    write_text_fn: Optional[Any] = None,
) -> List[ResolvedOutcome]:
    """Main entry point — drain at most `batch_size` resolved queue rows.

    Parameters
    ----------
    now : optional datetime (default: utcnow). Override for testability.
    batch_size : max rows to process per invocation.
    dry_run : when True, computes outcomes + post-mortem text but writes nothing.
    skip_text_generation : when True, skips the Haiku call (cheap testability).
    sb : injectable Supabase client; defaults to env-provided service-role.
    label_event_fn : injectable label_event(); defaults to D-116 helper.
    write_text_fn : injectable Anthropic text writer for post-mortem narrative.
    """
    sb = sb or SupabaseClient()
    label_event_fn = label_event_fn or _import_label_event()[0]
    if not skip_text_generation and write_text_fn is None:
        write_text_fn = _default_post_mortem_writer

    now = now or datetime.now(timezone.utc)
    results: List[ResolvedOutcome] = []

    queue_rows = _fetch_pending_queue(sb, now, limit=batch_size)
    if not queue_rows:
        logger.info("post_mortem_runner: no pending rows ready (now=%s)", now.isoformat())
        return results

    for q in queue_rows:
        try:
            outcome = _resolve_one(
                sb,
                q,
                label_event_fn=label_event_fn,
                write_text_fn=(None if skip_text_generation else write_text_fn),
                dry_run=dry_run,
            )
            results.append(outcome)
        except Exception as exc:  # noqa: BLE001 — drainer must keep going
            logger.exception("post_mortem_runner: row %s failed: %s",
                             q.get("id"), exc)
            results.append(ResolvedOutcome(
                queue_id=q.get("id", ""),
                assessment_id=q.get("assessment_id", ""),
                asset_id=q.get("asset_id", ""),
                predicted_conviction_pct=q.get("predicted_conviction_pct", 0.0),
                predicted_direction=q.get("predicted_direction", ""),
                status="skipped",
                skipped_reason=f"exception:{type(exc).__name__}:{str(exc)[:120]}",
            ))
    return results


# ---------------------------------------------------------------------------
# Per-row resolution
# ---------------------------------------------------------------------------

def _resolve_one(
    sb: SupabaseClient,
    queue_row: Dict[str, Any],
    *,
    label_event_fn: Any,
    write_text_fn: Optional[Any],
    dry_run: bool,
) -> ResolvedOutcome:
    queue_id = queue_row["id"]
    assessment_id = queue_row["assessment_id"]
    asset_id = queue_row["asset_id"]
    predicted_pct = float(queue_row["predicted_conviction_pct"])
    predicted_dir = queue_row["predicted_direction"]

    # Load assessment + asset for filed_at + ticker + reference_class.
    assessment = _fetch_one(sb, "convergence_assessments", assessment_id,
                            select="id,asset_id,reference_class,reference_class_base_rate,thesis_direction,thesis_summary,cited_prose_blocks,created_at,document_window_end")
    asset = _fetch_one(sb, "fda_assets", asset_id,
                       select="id,asset_name,brand_name,sponsor,indication,indication_normalized,program_status,reference_class_signature,primary_ticker")
    if assessment is None or asset is None:
        return ResolvedOutcome(
            queue_id=queue_id, assessment_id=assessment_id, asset_id=asset_id,
            predicted_conviction_pct=predicted_pct, predicted_direction=predicted_dir,
            status="skipped",
            skipped_reason=f"assessment_or_asset_missing:assessment={assessment is not None},asset={asset is not None}",
        )

    ticker = asset.get("primary_ticker")
    if not ticker or ticker in ("?", "PRIVATE_DISCARD", "UNRESOLVABLE"):
        # No ticker — can't label forward returns. Mark no_outcome and persist.
        outcome = ResolvedOutcome(
            queue_id=queue_id, assessment_id=assessment_id, asset_id=asset_id,
            predicted_conviction_pct=predicted_pct, predicted_direction=predicted_dir,
            status="no_outcome",
            skipped_reason=f"unresolved_ticker:{ticker!r}",
            reference_class=assessment.get("reference_class"),
        )
        if not dry_run:
            _persist_no_outcome(sb, outcome, miss_reason=outcome.skipped_reason)
        return outcome

    # Filed_at = the convergence_assessment's created_at (when we made the call).
    filed_at = assessment["created_at"]

    label = label_event_fn(
        ticker=ticker,
        filed_at=filed_at,
        profile=DEFAULT_PROFILE_FOR_LABELING,
        event_id=assessment_id,
    )
    label_dict = label.as_dict() if hasattr(label, "as_dict") else dict(label)

    if label_dict.get("hit") is None:
        # No clean outcome — delisted, halted, no SPY, unparseable, etc.
        outcome = ResolvedOutcome(
            queue_id=queue_id, assessment_id=assessment_id, asset_id=asset_id,
            predicted_conviction_pct=predicted_pct, predicted_direction=predicted_dir,
            status="no_outcome",
            skipped_reason=label_dict.get("miss_reason") or "no_outcome",
            realized_outcome=label_dict,
            reference_class=assessment.get("reference_class"),
        )
        if not dry_run:
            _persist_no_outcome(sb, outcome, miss_reason=outcome.skipped_reason or "no_outcome")
        return outcome

    # Resolved: HIT/MISS available. Compute prediction_error.
    realized_score = realized_outcome_score(
        hit=bool(label_dict["hit"]),
        predicted_direction=predicted_dir,
    )
    prediction_error = round(predicted_pct - realized_score, 2)

    # Generate post-mortem text (skip if testability flag set).
    post_mortem_text: Optional[str] = None
    if write_text_fn is not None:
        try:
            post_mortem_text = write_text_fn(
                asset=asset,
                assessment=assessment,
                label=label_dict,
                predicted_pct=predicted_pct,
                predicted_dir=predicted_dir,
                realized_score=realized_score,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("post_mortem_runner: text generation failed for %s: %s",
                           queue_id, exc)
            post_mortem_text = (
                f"[auto-fallback] Predicted {predicted_pct:.0f}% {predicted_dir}; "
                f"realized hit={label_dict.get('hit')} ({label_dict.get('miss_reason') or 'OK'})."
            )

    outcome = ResolvedOutcome(
        queue_id=queue_id, assessment_id=assessment_id, asset_id=asset_id,
        predicted_conviction_pct=predicted_pct, predicted_direction=predicted_dir,
        status="post_mortem_complete",
        realized_outcome=label_dict,
        prediction_error=prediction_error,
        post_mortem_text=post_mortem_text,
        reference_class=assessment.get("reference_class"),
    )

    if not dry_run:
        _persist_complete(sb, outcome)
        if outcome.reference_class:
            _refit_reference_class(sb, outcome.reference_class)
        # Memory file append is best-effort; failure logs but doesn't gate persistence.
        try:
            _append_memory_file(sb, asset, assessment, outcome)
        except Exception as exc:  # noqa: BLE001
            logger.warning("post_mortem_runner: memory file append failed for asset %s: %s",
                           asset_id, exc)

    return outcome


# ---------------------------------------------------------------------------
# Pure helpers (testable)
# ---------------------------------------------------------------------------

def realized_outcome_score(*, hit: bool, predicted_direction: str) -> float:
    """Map (hit, direction) → 0..100 realized score for prediction_error.

    Convention: a HIT (forward return ≥ +20% in 30d) means the *long* thesis
    was correct. So:
      - direction='long' + hit=True  → 100 (correct, conviction validated)
      - direction='long' + hit=False → 0   (long was wrong)
      - direction='short' + hit=True → 0   (HIT means stock went UP — short was wrong)
      - direction='short' + hit=False → 100 (no upside move — short partially validated;
                                             absence of evidence ≈ evidence of absence
                                             over the window)
      - direction='neutral'/'straddle' + hit=True → 50 (one-sided event; neutral
                                                         was wrong about magnitude)
      - direction='neutral'/'straddle' + hit=False → 50 (no big move — neutral OK)
    Returns float in [0, 100].
    """
    d = (predicted_direction or "").lower()
    if d == "long":
        return 100.0 if hit else 0.0
    if d == "short":
        return 0.0 if hit else 100.0
    # neutral / straddle / unknown — middle of road.
    return 50.0


def wilson_interval(successes: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score interval for binomial proportion (95% CI by default).
    Returns (lower, upper) bounded to [0, 1]. Returns (0.0, 1.0) on n=0 to
    signal "no data" without divide-by-zero.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    halfwidth = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    lo = max(0.0, (centre - halfwidth) / denom)
    hi = min(1.0, (centre + halfwidth) / denom)
    return (round(lo, 4), round(hi, 4))


def median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


# ---------------------------------------------------------------------------
# DB I/O
# ---------------------------------------------------------------------------

def _fetch_pending_queue(
    sb: SupabaseClient, now: datetime, *, limit: int,
) -> List[Dict[str, Any]]:
    rows = sb._rest("GET", "post_mortem_queue", params={
        "select": "id,assessment_id,asset_id,predicted_conviction_pct,predicted_direction,predicted_outcome,outcome_window_end,status",
        "status": "eq.pending",
        "outcome_window_end": f"lt.{now.isoformat()}",
        "order": "outcome_window_end.asc",
        "limit": str(limit),
    })
    return rows or []


def _fetch_one(
    sb: SupabaseClient, table: str, row_id: str, *, select: str = "*",
) -> Optional[Dict[str, Any]]:
    rows = sb._rest("GET", table, params={
        "id": f"eq.{row_id}",
        "select": select,
        "limit": "1",
    })
    return rows[0] if rows else None


def _persist_no_outcome(
    sb: SupabaseClient, outcome: ResolvedOutcome, *, miss_reason: str,
) -> None:
    body = {
        "status": "no_outcome",
        "realized_at": datetime.now(timezone.utc).isoformat(),
        "realized_outcome": outcome.realized_outcome or {"miss_reason": miss_reason},
    }
    sb._rest_with_retry("PATCH", "post_mortem_queue",
                        params={"id": f"eq.{outcome.queue_id}"},
                        json_body=body, prefer="return=minimal")


def _persist_complete(sb: SupabaseClient, outcome: ResolvedOutcome) -> None:
    body = {
        "status": "post_mortem_complete",
        "realized_at": datetime.now(timezone.utc).isoformat(),
        "realized_outcome": outcome.realized_outcome,
        "prediction_error": outcome.prediction_error,
        "post_mortem_text": outcome.post_mortem_text,
    }
    sb._rest_with_retry("PATCH", "post_mortem_queue",
                        params={"id": f"eq.{outcome.queue_id}"},
                        json_body=body, prefer="return=minimal")


def _refit_reference_class(sb: SupabaseClient, reference_class: str) -> None:
    """UPSERT reference_class_base_rates from all resolved post-mortems sharing
    the same reference_class. Safe under low-n: writes the row even with n<5
    so the orchestrator's Stage 4 can render an "n=X (low)" disclaimer.
    """
    # Pull resolved rows for this class.
    rows = sb._rest("GET", "convergence_assessments", params={
        "select": "id,reference_class",
        "reference_class": f"eq.{reference_class}",
        "limit": "10000",
    }) or []
    assessment_ids = [r["id"] for r in rows]
    if not assessment_ids:
        return

    # Get the resolved post_mortem_queue rows for these assessments.
    in_filter = f"in.({','.join(assessment_ids)})"
    pms = sb._rest("GET", "post_mortem_queue", params={
        "select": "assessment_id,realized_outcome,predicted_direction,status",
        "assessment_id": in_filter,
        "status": "eq.post_mortem_complete",
        "limit": "10000",
    }) or []

    if not pms:
        return

    n = 0
    successes = 0
    realized_moves: List[float] = []
    for pm in pms:
        ro = pm.get("realized_outcome") or {}
        hit = ro.get("hit")
        if hit is None:
            continue
        n += 1
        # "approval" base rate semantic: HIT count as a long-thesis-validated
        # outcome. Reference-class is direction-agnostic; we record the raw
        # hit-rate over the class.
        if hit:
            successes += 1
        # Pull T+30 return for median computation.
        for w in (ro.get("windows") or []):
            if w.get("days") == 30 and w.get("status") == "ok" and w.get("return_pct") is not None:
                realized_moves.append(float(w["return_pct"]))
                break

    if n == 0:
        return

    rate = successes / n
    lo, hi = wilson_interval(successes, n)
    median_move = median(realized_moves)

    upsert_body = {
        "reference_class": reference_class,
        "n_cases": n,
        "approval_rate": round(rate, 4),
        "approval_rate_ci_low": lo,
        "approval_rate_ci_high": hi,
        "median_realized_move_pct": round(median_move, 2) if median_move is not None else None,
        "refit_at": datetime.now(timezone.utc).isoformat(),
    }
    sb._rest_with_retry(
        "POST", "reference_class_base_rates",
        json_body=upsert_body,
        prefer="resolution=merge-duplicates,return=minimal",
    )


def _append_memory_file(
    sb: SupabaseClient,
    asset: Dict[str, Any],
    assessment: Dict[str, Any],
    outcome: ResolvedOutcome,
) -> None:
    """Append a 'Resolved post-mortems' entry to the per-asset memory file
    per Contract C5. Storage path: memory_files/asset_<asset_id>.md.

    Writes via Supabase Storage REST API. Existing file is read first, the new
    entry is inserted into the "Resolved post-mortems" section (newest-first),
    and the result is uploaded with upsert=true. memory_files index row is
    UPSERTed so the dashboard knows about the file.
    """
    asset_id = asset["id"]
    storage_path = f"asset_{asset_id}.md"
    bucket = "memory_files"
    base_url = f"{sb.url}/storage/v1/object/{bucket}/{storage_path}"

    # Read existing file (404 = doesn't exist; we'll create).
    existing: Optional[str] = None
    r = sb._session.get(base_url, timeout=sb.timeout)
    if r.status_code == 200:
        existing = r.text
    elif r.status_code != 404:
        raise RuntimeError(f"memory file read {r.status_code}: {r.text[:200]}")

    new_content = _merge_memory_file(existing, asset, assessment, outcome)

    # Upload (POST with x-upsert=true).
    headers = {
        "Content-Type": "text/markdown; charset=utf-8",
        "x-upsert": "true",
    }
    r2 = sb._session.post(base_url, data=new_content.encode("utf-8"),
                          headers=headers, timeout=sb.timeout)
    if r2.status_code >= 400:
        raise RuntimeError(f"memory file upload {r2.status_code}: {r2.text[:200]}")

    # Update memory_files index (UPSERT on (scope, scope_id)).
    sb._rest_with_retry(
        "POST", "memory_files",
        json_body={
            "scope": "asset",
            "scope_id": str(asset_id),
            "storage_path": storage_path,
            "size_bytes": len(new_content.encode("utf-8")),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        prefer="resolution=merge-duplicates,return=minimal",
    )


def _merge_memory_file(
    existing: Optional[str],
    asset: Dict[str, Any],
    assessment: Dict[str, Any],
    outcome: ResolvedOutcome,
) -> str:
    """Pure helper — produce the new memory file text given existing + outcome.
    Append-only newest-first under '## Resolved post-mortems'. Idempotent: if
    a section entry for this assessment_id already exists, skip insertion.
    """
    ticker = asset.get("primary_ticker") or "?"
    asset_name = asset.get("asset_name") or asset.get("brand_name") or "?"
    indication = asset.get("indication_normalized") or asset.get("indication") or "?"

    ro = outcome.realized_outcome or {}
    label_hit = ro.get("hit")
    miss_reason = ro.get("miss_reason") or ""
    w30 = next((w for w in (ro.get("windows") or []) if w.get("days") == 30), {})
    return_pct = w30.get("return_pct")
    return_str = f"{return_pct:+.2f}%" if return_pct is not None else "n/a"
    hit_str = "HIT" if label_hit else "MISS"

    entry_marker = f"<!-- assessment:{outcome.assessment_id} -->"
    if existing and entry_marker in existing:
        return existing  # idempotent — already recorded

    entry = (
        f"### {datetime.now(timezone.utc).date().isoformat()} · "
        f"predicted {outcome.predicted_conviction_pct:.0f}% {outcome.predicted_direction} "
        f"→ realized {return_str} ({hit_str}, T+30)\n"
        f"{entry_marker}\n"
        f"Prediction error: {outcome.prediction_error:+.1f} pp · "
        f"reference class: `{outcome.reference_class or 'unset'}`\n\n"
        + (f"{outcome.post_mortem_text}\n\n" if outcome.post_mortem_text else "")
        + (f"Note: {miss_reason}\n\n" if miss_reason else "")
        + "---\n\n"
    )

    if not existing:
        header = (
            f"# {ticker} · {asset_name} ({indication})\n\n"
            f"> updated_at: {datetime.now(timezone.utc).isoformat()}\n\n"
            f"## Active hypotheses\n\n_(populated by orchestrator Stream 3)_\n\n"
            f"## Resolved post-mortems\n\n"
        )
        return header + entry + "## Open uncertainties\n\n_(populated by orchestrator Stream 3)_\n\n## Recent assessments\n\n_(populated by orchestrator Stream 3)_\n"

    section_header = "## Resolved post-mortems"
    if section_header not in existing:
        # Inject section before "## Open uncertainties" if present, else at end.
        if "## Open uncertainties" in existing:
            return existing.replace(
                "## Open uncertainties",
                f"{section_header}\n\n{entry}## Open uncertainties",
                1,
            )
        return existing + f"\n\n{section_header}\n\n{entry}"

    # Insert new entry at top of "Resolved post-mortems" section.
    return existing.replace(
        f"{section_header}\n\n",
        f"{section_header}\n\n{entry}",
        1,
    )


# ---------------------------------------------------------------------------
# Post-mortem text generation (Haiku)
# ---------------------------------------------------------------------------

def _default_post_mortem_writer(
    *,
    asset: Dict[str, Any],
    assessment: Dict[str, Any],
    label: Dict[str, Any],
    predicted_pct: float,
    predicted_dir: str,
    realized_score: float,
) -> str:
    """Call Haiku to generate a 200-word retrospective. Returns the text body.

    Imported lazily so the module loads without anthropic SDK present (e.g.
    in unit tests with skip_text_generation=True).
    """
    import anthropic  # noqa: WPS433  (lazy import is intentional)

    client = anthropic.Anthropic()
    ticker = asset.get("primary_ticker") or "?"
    indication = asset.get("indication_normalized") or asset.get("indication") or "?"
    sponsor = asset.get("sponsor") or "?"
    asset_name = asset.get("asset_name") or asset.get("brand_name") or "?"

    w30 = next((w for w in (label.get("windows") or []) if w.get("days") == 30), {})
    return_pct = w30.get("return_pct")
    hit_str = "HIT" if label.get("hit") else "MISS"
    realized_summary = f"T+30 return {return_pct:+.2f}% ({hit_str})" if return_pct is not None else f"{hit_str}"

    cited_blocks = assessment.get("cited_prose_blocks") or []
    citations_summary = ""
    if isinstance(cited_blocks, list):
        snippets = []
        for c in cited_blocks[:3]:
            if isinstance(c, dict):
                snip = c.get("snippet") or c.get("text") or ""
                if snip:
                    snippets.append(snip[:160])
        if snippets:
            citations_summary = "\nKey cited evidence:\n- " + "\n- ".join(snippets)

    user_prompt = f"""You are writing a concise post-mortem for an FDA asset thesis.

Asset: {ticker} · {asset_name} · {sponsor} · {indication}
Predicted: {predicted_pct:.0f}% {predicted_dir} (realized score {realized_score:.0f})
Realized: {realized_summary}
Reference class: {assessment.get('reference_class') or 'unset'}
Thesis summary (predicted): {assessment.get('thesis_summary') or '(unavailable)'}{citations_summary}

In 150–200 words, write a retrospective covering:
1. What evidence was overweighted (if MISS) or correctly weighted (if HIT).
2. What contrary evidence was minimized.
3. The reference-class implication (does this update the base rate?).

Do NOT speculate beyond what the data supports. Plain prose, no bullet lists, no headers."""

    msg = client.messages.create(
        model=DEFAULT_HAIKU_MODEL,
        max_tokens=DEFAULT_HAIKU_MAX_TOKENS,
        system="You are a calibrated equity analyst writing post-mortems on FDA asset theses. Be terse and specific.",
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Drain post_mortem_queue.")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-text", action="store_true",
                        help="Skip Haiku post-mortem generation (testability).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    results = drain_resolved_queue(
        batch_size=args.limit,
        dry_run=args.dry_run,
        skip_text_generation=args.skip_text,
    )
    by_status: Dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    logger.info("post_mortem_runner: drained %d rows → %s",
                len(results), dict(by_status))
    if args.dry_run:
        for r in results:
            print(f"[{r.status}] queue={r.queue_id} assessment={r.assessment_id} "
                  f"err={r.prediction_error} reason={r.skipped_reason or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
