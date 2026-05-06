"""Backfill eval_harness.realized_outcome_data.realized_move_pct via Polygon.

Phase 0 close-out — D2. Curated rows have approval_or_crl_date set but no
realized stock-move data. This pass pulls daily closes around each resolution
date and computes signed % moves at T+1, T+7, T+30 (anchor: close on the
last trading day BEFORE resolution).

Convention:
  - T = resolution date (approval_or_crl_date)
  - anchor = close on the last trading day with date < T
  - t1 = first trading day with date >= T (i.e. announcement day or the next
    trading session if announcement was after market or on a non-trading day)
  - t7 = first trading day with date >= T + 7 calendar days
  - t30 = first trading day with date >= T + 30 calendar days
  - move_N = (close_N - anchor) / anchor * 100, rounded to 4 dp

Run:
  python3 -m modal_workers.scripts.backfill_realized_move [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)


@dataclass
class Stats:
    rows_seen: int = 0
    rows_already_filled: int = 0
    rows_no_ticker: int = 0
    rows_no_resolution_date: int = 0
    polygon_no_data: int = 0
    rows_updated: int = 0
    errors: int = 0


def _fetch_via_polygon(ticker: str, window_start: date,
                      window_end: date) -> List[Dict[str, Any]]:
    """Pull aggs from Polygon. Returns [] when POLYGON_API_KEY is unset or
    the API returns no data — caller falls back to yfinance."""
    try:
        from modal_workers.providers.polygon.base import PolygonClient
        client = PolygonClient()
    except RuntimeError:
        return []  # POLYGON_API_KEY unset
    path = (
        f"/v2/aggs/ticker/{ticker}/range/1/day/"
        f"{window_start.isoformat()}/{window_end.isoformat()}"
    )
    body = client.get(path, params={"adjusted": "true", "sort": "asc", "limit": 50000})
    if not body or not isinstance(body, dict):
        return []
    return body.get("results") or []


def _fetch_via_yfinance(ticker: str, window_start: date,
                       window_end: date) -> List[Dict[str, Any]]:
    """Fallback price source. Returns the same shape Polygon does
    ({t (ms), c, o, h, l, v}) so the rest of the script doesn't care."""
    import yfinance as yf  # imported lazily; only used in fallback path
    t = yf.Ticker(ticker)
    df = t.history(
        start=window_start.isoformat(),
        end=(window_end + timedelta(days=1)).isoformat(),
        auto_adjust=True,
    )
    if df is None or df.empty:
        return []
    out: List[Dict[str, Any]] = []
    for ts, row in df.iterrows():
        # yfinance returns tz-aware index; convert to ms epoch.
        epoch_ms = int(ts.to_pydatetime().timestamp() * 1000)
        out.append({
            "t": epoch_ms,
            "o": float(row["Open"]),
            "h": float(row["High"]),
            "l": float(row["Low"]),
            "c": float(row["Close"]),
            "v": int(row["Volume"]),
        })
    return out


def fetch_daily_closes(
    ticker: str,
    *,
    window_start: date,
    window_end: date,
) -> List[Dict[str, Any]]:
    """Try Polygon first; fall back to yfinance if Polygon is unavailable
    or returns no data. Returns aggregates sorted ascending by date."""
    closes = _fetch_via_polygon(ticker, window_start, window_end)
    if closes:
        return closes
    try:
        return _fetch_via_yfinance(ticker, window_start, window_end)
    except Exception as exc:  # noqa: BLE001
        logger.warning("yfinance fallback failed for %s: %s", ticker, exc)
        return []


def date_from_ms(ms: int) -> date:
    return datetime.utcfromtimestamp(ms / 1000.0).date()


def find_anchor_close(closes: List[Dict[str, Any]], resolution_d: date) -> Optional[Dict[str, Any]]:
    """Last close strictly BEFORE resolution_d."""
    pre = [c for c in closes if date_from_ms(c["t"]) < resolution_d]
    if not pre:
        return None
    return pre[-1]


def find_first_close_at_or_after(
    closes: List[Dict[str, Any]],
    target: date,
) -> Optional[Dict[str, Any]]:
    """First close with date >= target."""
    for c in closes:
        if date_from_ms(c["t"]) >= target:
            return c
    return None


def compute_move_pct(anchor_close: float, target_close: float) -> float:
    if anchor_close == 0:
        return 0.0
    return round((target_close - anchor_close) / anchor_close * 100.0, 4)


def merge_realized_move(
    existing_data: Dict[str, Any],
    moves: Dict[str, Any],
    asof_date_iso: str,
) -> Dict[str, Any]:
    out = deepcopy(existing_data) if existing_data else {}
    out["realized_move_pct"] = moves
    out["realized_move_backfilled_at"] = asof_date_iso
    out["realized_move_source"] = "polygon_aggs_v0.1"
    return out


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="backfill_realized_move")
    p.add_argument("--limit", type=int, default=500,
                   help="Max eval_harness rows to backfill in one run")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch + compute but don't PATCH")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    client = SupabaseClient()
    stats = Stats()

    # Pull all eval_harness rows joined with fda_assets via PostgREST embedding.
    rows = client._rest(
        "GET", "eval_harness",
        params={
            "select": "id,realized_outcome,realized_outcome_data,fda_assets(id,ticker)",
            "limit": str(args.limit),
        },
    ) or []
    stats.rows_seen = len(rows)
    logger.info("Inspecting %d eval_harness rows", len(rows))

    for r in rows:
        eval_id = r["id"]
        outcome_data = r.get("realized_outcome_data") or {}
        # Skip rows already backfilled (idempotent re-run).
        if outcome_data.get("realized_move_pct") and isinstance(
                outcome_data["realized_move_pct"], dict):
            stats.rows_already_filled += 1
            continue

        asset = r.get("fda_assets") or {}
        ticker = (asset.get("ticker") or "").strip()
        if not ticker:
            stats.rows_no_ticker += 1
            continue

        resolution_iso = outcome_data.get("approval_or_crl_date")
        if not resolution_iso:
            stats.rows_no_resolution_date += 1
            continue
        try:
            resolution_d = datetime.strptime(resolution_iso, "%Y-%m-%d").date()
        except ValueError:
            stats.rows_no_resolution_date += 1
            continue

        # Window: 5 trading days before to 45 calendar days after.
        window_start = resolution_d - timedelta(days=10)
        window_end = resolution_d + timedelta(days=45)
        # Bound by today — Polygon won't have future data.
        if window_end > date.today():
            window_end = date.today()
        if window_start >= window_end:
            stats.polygon_no_data += 1
            continue

        try:
            closes = fetch_daily_closes(ticker,
                                        window_start=window_start,
                                        window_end=window_end)
        except Exception as exc:  # noqa: BLE001
            logger.warning("price fetch failed for %s %s: %s",
                           ticker, resolution_iso, exc)
            stats.errors += 1
            continue

        if not closes:
            logger.info("polygon: no data for %s in %s..%s",
                        ticker, window_start, window_end)
            stats.polygon_no_data += 1
            continue

        anchor = find_anchor_close(closes, resolution_d)
        if not anchor:
            stats.polygon_no_data += 1
            continue

        anchor_close = float(anchor["c"])
        anchor_d = date_from_ms(anchor["t"])

        t1_target = resolution_d
        t7_target = resolution_d + timedelta(days=7)
        t30_target = resolution_d + timedelta(days=30)

        t1_close = find_first_close_at_or_after(closes, t1_target)
        t7_close = find_first_close_at_or_after(closes, t7_target)
        t30_close = find_first_close_at_or_after(closes, t30_target)

        moves: Dict[str, Any] = {
            "anchor_date": anchor_d.isoformat(),
            "anchor_close": anchor_close,
            "t1": compute_move_pct(anchor_close, float(t1_close["c"])) if t1_close else None,
            "t1_date": date_from_ms(t1_close["t"]).isoformat() if t1_close else None,
            "t7": compute_move_pct(anchor_close, float(t7_close["c"])) if t7_close else None,
            "t7_date": date_from_ms(t7_close["t"]).isoformat() if t7_close else None,
            "t30": compute_move_pct(anchor_close, float(t30_close["c"])) if t30_close else None,
            "t30_date": date_from_ms(t30_close["t"]).isoformat() if t30_close else None,
        }

        if args.dry_run:
            logger.info(
                "[dry-run] %s %s anchor=%s/%.2f t1=%s t7=%s t30=%s",
                eval_id[:8], ticker, anchor_d, anchor_close,
                moves["t1"], moves["t7"], moves["t30"],
            )
            continue

        merged = merge_realized_move(outcome_data, moves,
                                     asof_date_iso=date.today().isoformat())
        try:
            client._rest(
                "PATCH", "eval_harness",
                params={"id": f"eq.{eval_id}"},
                json_body={"realized_outcome_data": merged},
                prefer="return=minimal",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("PATCH eval_harness failed for %s: %s", eval_id, exc)
            stats.errors += 1
            continue
        stats.rows_updated += 1

    logger.info(
        "realized_move backfill summary: rows=%d already=%d no_ticker=%d "
        "no_date=%d polygon_empty=%d updated=%d errors=%d",
        stats.rows_seen, stats.rows_already_filled, stats.rows_no_ticker,
        stats.rows_no_resolution_date, stats.polygon_no_data,
        stats.rows_updated, stats.errors,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
