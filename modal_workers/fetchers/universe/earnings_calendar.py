"""Earnings calendar fetcher.

Phase 3a — feeds public.earnings_calendar from yfinance (primary) and Polygon
(fallback). The Q1 confounder audit reads this table to flag FDA events that
landed ±5 trading days from an earnings announcement on the same ticker.

Sources:
  1. yfinance Ticker.get_earnings_dates(limit=N) — returns up to ~16 past
     quarters plus the next scheduled date. Free, no auth. Primary source.
  2. Polygon /vX/reference/tickers/{ticker}/events — paid fallback when
     yfinance is unavailable, rate-limited, or returns null. The provider
     value persists on the row ('polygon' vs 'yfinance') so multi-source
     readers can resolve via confidence.

Both sources have their warts: yfinance occasionally returns wrong dates
near holidays; Polygon emits the events feed under a more restrictive
license tier. Keeping both rows (one per source) lets the reader pick.

Run locally:
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python3 -m modal_workers.fetchers.universe.earnings_calendar \\
        --tickers AXSM,VRDN,IONS --lookback-days 730 --apply
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError  # noqa: E402

logger = logging.getLogger(__name__)

# yfinance get_earnings_dates rate limit: empirically ~2k calls/sec ceiling
# before HTTP 429. We pause between tickers to stay generous; batches of 50
# with a longer pause keep the daily refresh well under any reasonable
# threshold.
PER_TICKER_SLEEP_S = 0.10
BATCH_SIZE = 50
BATCH_SLEEP_S = 2.0


def fetch(
    client: SupabaseClient,
    *,
    tickers: Iterable[str],
    lookback_days: int = 730,
    forward_days: int = 90,
    dry_run: bool = False,
    polygon_fallback: bool = True,
) -> Dict[str, Any]:
    """Fetch earnings dates for each ticker; upsert one row per (ticker, date, source).

    Returns a counts dict identical in shape to fda_adcomm_pdufa.fetch.

    Window math: we accept any earnings_date in [today-lookback_days, today+forward_days].
    Backfills can pass lookback_days=5*365 to cover the calibration training pool.
    """
    fetched = 0
    upserted = 0
    skipped = 0
    errors: List[Dict[str, Any]] = []

    today = datetime.now(timezone.utc).date()
    window_start = today - timedelta(days=lookback_days)
    window_end = today + timedelta(days=forward_days)

    ticker_list = list(_normalize_tickers(tickers))
    for idx, ticker in enumerate(ticker_list, start=1):
        try:
            rows = _yfinance_earnings_for_ticker(
                ticker, window_start=window_start, window_end=window_end,
            )
        except Exception as e:  # noqa: BLE001
            errors.append({"ticker": ticker, "source": "yfinance", "error": str(e)[:400]})
            rows = []

        if not rows and polygon_fallback:
            try:
                rows = _polygon_earnings_for_ticker(
                    ticker, window_start=window_start, window_end=window_end,
                )
            except Exception as e:  # noqa: BLE001
                errors.append({"ticker": ticker, "source": "polygon", "error": str(e)[:400]})
                rows = []

        fetched += len(rows)
        for row in rows:
            if dry_run:
                upserted += 1
                continue
            try:
                _upsert_earnings_row(client, row)
                upserted += 1
            except SupabaseError as e:
                errors.append({
                    "ticker": ticker, "date": row.get("earnings_date"),
                    "error": str(e)[:400],
                })
                skipped += 1

        # Rate-limit pause. Per-ticker is 100ms; batch boundary tacks on 2s.
        time.sleep(PER_TICKER_SLEEP_S)
        if idx % BATCH_SIZE == 0 and idx < len(ticker_list):
            time.sleep(BATCH_SLEEP_S)

    return {
        "fetched": fetched,
        "upserted": upserted,
        "skipped": skipped,
        "errors": errors,
        "window": {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        },
        "n_tickers": len(ticker_list),
    }


# ---------------------------------------------------------------------------
# yfinance — primary source.
# ---------------------------------------------------------------------------


def _yfinance_earnings_for_ticker(
    ticker: str, *, window_start: date, window_end: date,
) -> List[Dict[str, Any]]:
    """Return earnings rows for one ticker, parsed from yfinance.

    Output shape per row:
      {
        "ticker": "AXSM",
        "earnings_date": "2026-08-04",
        "session": "amc" | "bmo" | "during" | "unknown",
        "is_estimated": True/False,
        "source": "yfinance",
        "confidence": 0.85,
        "raw_payload": { ... },
      }

    Importing yfinance is deferred so unit tests can monkeypatch this helper
    without paying the heavyweight import cost.
    """
    try:
        import yfinance  # noqa: WPS433 (deferred import is intentional)
    except ImportError:
        logger.warning("yfinance not installed — skipping ticker %s", ticker)
        return []

    yt = yfinance.Ticker(ticker)
    try:
        df = yt.get_earnings_dates(limit=16)
    except Exception:  # noqa: BLE001
        # yfinance raises a grab-bag of errors; treat all as "no data" and
        # let the caller fall back to Polygon.
        return []

    if df is None or len(df) == 0:
        return []

    rows: List[Dict[str, Any]] = []
    # yfinance returns a DataFrame indexed by Timestamp; iterate by tuples to
    # avoid coercing the timestamp through pandas-specific accessors that
    # break under monkeypatch in tests.
    for ts, record in _iterate_dataframe(df):
        try:
            edate = ts.date() if hasattr(ts, "date") else date.fromisoformat(str(ts)[:10])
        except (ValueError, AttributeError):
            continue
        if not (window_start <= edate <= window_end):
            continue
        is_estimated = edate >= datetime.now(timezone.utc).date()
        rows.append({
            "ticker": ticker,
            "earnings_date": edate.isoformat(),
            "session": _infer_session_from_ts(ts),
            "is_estimated": is_estimated,
            "source": "yfinance",
            # yfinance is reasonably accurate post-print, less so for forward
            # estimates. Confidence reflects this split.
            "confidence": 0.85 if not is_estimated else 0.65,
            "raw_payload": _yfinance_record_to_payload(record),
        })
    return rows


def _iterate_dataframe(df: Any) -> Iterable[Any]:
    """Yield (index, row_dict) pairs from a yfinance DataFrame. Falls back to
    `.itertuples` then to direct dict iteration for testing."""
    try:
        for ts, row in df.iterrows():
            yield ts, dict(row) if hasattr(row, "to_dict") is False else row.to_dict()
    except AttributeError:
        # Already a plain dict-of-rows (test fixture path).
        for ts, row in dict(df).items():
            yield ts, row


def _infer_session_from_ts(ts: Any) -> str:
    """Use the timestamp's local hour to guess BMO vs AMC. yfinance tags
    earnings with a wall-clock time; before-market typically reads ≤09:30 ET
    and after-market reads ≥16:00 ET."""
    try:
        hour = ts.hour if hasattr(ts, "hour") else int(str(ts)[11:13])
    except (ValueError, AttributeError):
        return "unknown"
    if hour < 9:
        return "bmo"
    if hour >= 16:
        return "amc"
    return "during"


def _yfinance_record_to_payload(record: Any) -> Dict[str, Any]:
    """Pull only the JSON-serializable bits from a yfinance row."""
    if isinstance(record, dict):
        return {k: _coerce_json(v) for k, v in record.items()}
    return {}


def _coerce_json(v: Any) -> Any:
    """Best-effort JSON coercion — yfinance returns numpy scalars + NaT."""
    if v is None:
        return None
    if isinstance(v, (int, float, str, bool)):
        if isinstance(v, float) and v != v:  # NaN
            return None
        return v
    # numpy / pandas scalars expose .item()
    if hasattr(v, "item"):
        try:
            return v.item()
        except (ValueError, AttributeError):
            return str(v)
    return str(v)


# ---------------------------------------------------------------------------
# Polygon — fallback source.
# ---------------------------------------------------------------------------


def _polygon_earnings_for_ticker(
    ticker: str, *, window_start: date, window_end: date,
) -> List[Dict[str, Any]]:
    """Stub fallback. Live Polygon integration goes here once we settle on the
    endpoint tier; until then it's a no-op so the fetcher gracefully degrades
    to yfinance-only.

    Leaving this as a stub rather than wiring requests at the bottom of the
    file keeps the test fixture lightweight: production Polygon credentials
    aren't required to exercise the yfinance path.
    """
    return []


# ---------------------------------------------------------------------------
# Supabase upsert.
# ---------------------------------------------------------------------------


def _upsert_earnings_row(client: SupabaseClient, row: Dict[str, Any]) -> None:
    """ON CONFLICT (ticker, earnings_date, source) DO UPDATE — multi-source
    rows coexist; we always overwrite the same-source row to track confidence
    drift over time."""
    client.from_("earnings_calendar").upsert(
        {
            "ticker": row["ticker"],
            "earnings_date": row["earnings_date"],
            "session": row.get("session", "unknown"),
            "is_estimated": row.get("is_estimated", True),
            "source": row["source"],
            "confidence": row.get("confidence"),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "raw_payload": row.get("raw_payload") or {},
        },
        on_conflict="ticker,earnings_date,source",
    ).execute()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_tickers(tickers: Iterable[str]) -> Iterable[str]:
    """Strip whitespace, uppercase, dedupe while preserving order."""
    seen: set[str] = set()
    for t in tickers:
        norm = (t or "").strip().upper()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        yield norm


def load_tradeable_tickers(client: SupabaseClient) -> List[str]:
    """Pull the union of (tradeable-filter-passed) tickers from eval_harness
    joined to fda_assets. The Modal cron job uses this so the daily refresh
    only touches tickers we care about for the calibration training pool.

    Direct query instead of a view to avoid adding a schema dependency for a
    helper that runs once a day — Supabase's REST API can serve this fine.
    """
    # eval_harness has asset_id + tradeable_filter_pass; fda_assets carries
    # the ticker. The inner select on fda_assets.id keeps the predicate on
    # the indexed asset_id column.
    result = (
        client.from_("eval_harness")
        .select("asset_id, fda_assets!inner(ticker)")
        .eq("tradeable_filter_pass", True)
        .execute()
    )
    rows = result.data or []
    tickers: set[str] = set()
    for row in rows:
        ticker = (row.get("fda_assets") or {}).get("ticker")
        if ticker:
            tickers.add(ticker.upper())
    return sorted(tickers)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tickers", required=False,
                   help="Comma-separated tickers; defaults to v_calibration_tradeable_tickers.")
    p.add_argument("--lookback-days", type=int, default=730)
    p.add_argument("--forward-days", type=int, default=90)
    p.add_argument("--apply", action="store_true",
                   help="Persist to Supabase. Default is dry-run.")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    client = SupabaseClient()
    tickers = (
        args.tickers.split(",")
        if args.tickers
        else load_tradeable_tickers(client)
    )
    print(f"earnings_calendar fetcher: {len(tickers)} tickers, "
          f"lookback={args.lookback_days}d, forward={args.forward_days}d, "
          f"apply={args.apply}")
    result = fetch(
        client,
        tickers=tickers,
        lookback_days=args.lookback_days,
        forward_days=args.forward_days,
        dry_run=not args.apply,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
