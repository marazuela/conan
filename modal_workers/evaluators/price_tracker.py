"""Daily ticker price tracker.

Producer for `signal_price_snapshots` and the `outcomes.realized_move_{1d,7d,30d}`
columns. Runs once per day via the `evaluate_ticker_movement` Modal function
(scheduled in `modal_workers/app.py`, 23:30 UTC ≈ 18:30 ET, post-US-close).

For each tracked subject (candidates ∪ unpromoted watchlist/immediate signals),
we compute the close-to-close move at horizons 1/7/30 days from the subject's
created_at, sign-flip for short direction, and write one row per horizon to
`signal_price_snapshots`. When the subject is a candidate AND it has an
`outcomes` row (i.e., already transitioned to delivered/killed/expired), we
also mirror the signed value into `outcomes.realized_move_{Nd}`.

Anchor and horizon dates are *trading-day forward-filled*: a Saturday anchor
picks Monday's close. yfinance returns rows only for trading days, so a `start`
date that falls on a non-trading day yields the next available row when we
call `history(start, end)`.

Thesis-direction handling:
  - 'long'  → signed_move_pct = raw_move_pct
  - 'short' → signed_move_pct = -raw_move_pct
  - 'neutral' → no signed comparison; row stamped fetch_status='neutral_skipped',
                signed_move_pct = NULL.

Idempotency: re-running the same day produces UPSERTs against partial unique
indexes (signal_id, horizon_days) and (candidate_id, horizon_days). Values
overwrite, so a re-run on the same data is a no-op behavioral change.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from modal_workers.shared.market_snapshot import fetch_close_on_date
from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError

LOG = logging.getLogger("price_tracker")

HORIZONS = (1, 7, 30)


def _parse_anchor(created_at: str) -> Optional[date]:
    """Parse a Supabase `created_at` ISO string to a UTC date."""
    if not created_at:
        return None
    try:
        return datetime.fromisoformat(str(created_at).replace("Z", "+00:00")).astimezone(timezone.utc).date()
    except ValueError:
        return None


def _due_horizons(anchor: date, today: date) -> List[int]:
    """Return horizons whose end date is at-or-before `today`."""
    return [h for h in HORIZONS if anchor + timedelta(days=h) <= today]


def _percent(numerator: float, denominator: float) -> float:
    return round((numerator - denominator) / denominator * 100.0, 4)


def _sign_for_direction(direction: str) -> Optional[int]:
    if direction == "long":
        return 1
    if direction == "short":
        return -1
    return None  # neutral or unknown


def _build_row(
    subject: Dict[str, Any],
    horizon: int,
    anchor: date,
    anchor_close: Optional[float],
    horizon_close: Optional[float],
) -> Dict[str, Any]:
    direction = subject.get("thesis_direction") or "long"
    sign = _sign_for_direction(direction)

    raw_pct: Optional[float] = None
    signed_pct: Optional[float] = None
    if anchor_close is not None and anchor_close > 0 and horizon_close is not None:
        raw_pct = _percent(horizon_close, anchor_close)
        if sign is not None:
            signed_pct = round(raw_pct * sign, 4)

    if direction == "neutral":
        status = "neutral_skipped"
    elif anchor_close is None or horizon_close is None:
        status = "no_data" if anchor_close is None else "stale_anchor"
    else:
        status = "ok"

    return {
        "signal_id": subject.get("signal_id"),
        "candidate_id": subject.get("candidate_id"),
        "ticker": subject["ticker"],
        "mic": subject.get("mic"),
        "thesis_direction": direction,
        "anchor_date": anchor.isoformat(),
        "horizon_days": horizon,
        "anchor_close": anchor_close,
        "horizon_close": horizon_close,
        "raw_move_pct": raw_pct,
        "signed_move_pct": signed_pct,
        "fetch_status": status,
    }


def _process_subject(
    client: SupabaseClient,
    subject: Dict[str, Any],
    today: date,
) -> Dict[str, int]:
    counts = {"ok": 0, "no_data": 0, "neutral_skipped": 0, "stale_anchor": 0, "errors": 0}
    anchor = _parse_anchor(subject.get("created_at", ""))
    if anchor is None:
        counts["errors"] += 1
        return counts

    ticker = subject["ticker"]
    mic = subject.get("mic")
    horizons_due = _due_horizons(anchor, today)
    if not horizons_due:
        return counts

    anchor_close = fetch_close_on_date(ticker, mic, anchor)

    for horizon in horizons_due:
        end = anchor + timedelta(days=horizon)
        horizon_close = fetch_close_on_date(ticker, mic, end) if anchor_close is not None else None
        row = _build_row(subject, horizon, anchor, anchor_close, horizon_close)
        try:
            client.upsert_price_snapshot(row)
        except SupabaseError as e:
            LOG.warning("upsert_price_snapshot failed (subject=%s horizon=%s): %s",
                        subject.get("candidate_id") or subject.get("signal_id"), horizon, e)
            counts["errors"] += 1
            continue

        counts[row["fetch_status"]] = counts.get(row["fetch_status"], 0) + 1

        if row["fetch_status"] == "ok" and subject.get("candidate_id"):
            try:
                client.update_outcome_realized_move(
                    subject["candidate_id"], horizon, row["signed_move_pct"],
                )
            except SupabaseError as e:
                LOG.warning("outcomes mirror failed (candidate=%s horizon=%s): %s",
                            subject["candidate_id"], horizon, e)
                counts["errors"] += 1

    return counts


def run_price_tracker(
    *,
    client: Optional[SupabaseClient] = None,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """Entry point invoked by the Modal scheduled function.

    Returns a flat envelope counting subjects processed and snapshots written
    by status, suitable for log + scanner_runs visibility.
    """
    client = client or SupabaseClient()
    today = today or datetime.now(timezone.utc).date()

    subjects = client.load_price_tracking_subjects()
    totals = {"ok": 0, "no_data": 0, "neutral_skipped": 0, "stale_anchor": 0, "errors": 0}

    for subject in subjects:
        counts = _process_subject(client, subject, today)
        for k, v in counts.items():
            totals[k] = totals.get(k, 0) + v

    envelope = {
        "subjects": len(subjects),
        "snapshots_written": totals["ok"] + totals["no_data"] + totals["neutral_skipped"] + totals["stale_anchor"],
        **totals,
        "date": today.isoformat(),
    }
    LOG.info("price_tracker complete: %s", envelope)
    return envelope
