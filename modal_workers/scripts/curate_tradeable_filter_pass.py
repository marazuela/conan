"""curate_tradeable_filter_pass — D-105 backfill for eval_harness.

For every `eval_harness` row, computes `tradeable_filter_pass` and
`issuer_status` at `reference_assessment_date` per D-105
(migration `20260507000000_v3_d105_eval_harness_extracted_facts_amendments.sql`).

Contract:

  tradeable_filter_pass = true IFF at reference_assessment_date:
    - market cap     >= $215M USD
    - 90-day ADV     >= $500K USD
    - listed on NYSE / NASDAQ / AMEX / LSE

  issuer_status ∈ {'active', 'acquired', 'delisted', 'bankrupt'}:
    - 'active'    — price history exists at reference_assessment_date AND at
                    reference_assessment_date + 365d.
    - 'delisted'  — price history exists at reference_assessment_date but is
                    empty when extended to reference_assessment_date + 365d
                    (subsumes acquired/bankrupt for the initial backfill;
                    finer discrimination is a downstream refinement task).
    - 'acquired' / 'bankrupt' — not auto-classified today; left for future
                    work that joins against an M&A / bankruptcy ledger.

NO SURVIVORSHIP BIAS: rows that fail the tradeable filter (microcap, OTC,
illiquid) stay in eval_harness — they're flagged with
`tradeable_filter_pass=false` for downstream filtering, not deleted.

Historical-point-in-time caveat: yfinance only exposes today's
`sharesOutstanding`. The script uses today's share count × close at
reference_assessment_date as a proxy market cap. For biotechs whose share
count grew via dilution, this OVER-estimates historical mcap. Acceptable
for a binary calibration filter; document refinements as needed.

Usage:

  python3 -m modal_workers.scripts.curate_tradeable_filter_pass \\
      [--dry-run]   # default: print plan, no PATCH
      [--apply]     # actually PATCH
      [--limit N]   # cap row count (debugging)
      [--only-empty] # only rows where issuer_status is NULL (default true)

Idempotent: a row whose stored (tradeable_filter_pass, issuer_status)
already matches the computed values is skipped (no PATCH).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# D-105 thresholds (frozen from migration comments + DECISIONS D-105)
# ---------------------------------------------------------------------------

MIN_MARKET_CAP_USD = 215_000_000.0  # $215M
MIN_ADV_USD = 500_000.0             # $500K / day, averaged over 90 trading days
ADV_WINDOW_TRADING_DAYS = 90

# yfinance `info['exchange']` codes that count as NYSE/NASDAQ/AMEX/LSE.
# NMS = NASDAQ Global Select / National Market; NCM = NASDAQ Capital Market;
# NGM = NASDAQ Global Market; NYQ = NYSE; PCX = NYSE Arca; ASE = NYSE American
# (formerly AMEX). LSE family codes ('LSE', 'LON') for FCA-listed UK issuers.
ALLOWED_EXCHANGES = {
    "NMS", "NCM", "NGM",        # NASDAQ tiers
    "NYQ",                       # NYSE
    "PCX", "ARCA",               # NYSE Arca
    "ASE", "AMEX",               # NYSE American (ex-AMEX)
    "LSE", "LON",                # London Stock Exchange
}


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class FilterDecision:
    tradeable_filter_pass: bool
    issuer_status: str
    rationale: str
    market_cap_usd: Optional[float] = None
    adv_usd: Optional[float] = None
    exchange: Optional[str] = None


@dataclass
class Stats:
    rows_seen: int = 0
    rows_skipped_idempotent: int = 0
    rows_updated: int = 0
    rows_error: int = 0
    by_status: Dict[str, int] = field(default_factory=dict)
    by_pass: Dict[bool, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Price + metadata helpers (yfinance-backed, with caching)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=512)
def _fetch_yf_info(ticker: str) -> Dict[str, Any]:
    """Fetch yfinance `.info` dict. Cached per-process — many eval_harness
    rows share tickers."""
    try:
        import yfinance as yf  # lazy import
    except ImportError:
        logger.warning("yfinance not installed; install via `pip install yfinance`")
        return {}
    try:
        return dict(yf.Ticker(ticker).info or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("yfinance info fetch failed for %s: %s", ticker, exc)
        return {}


def fetch_history_window(
    ticker: str,
    *,
    window_start: date,
    window_end: date,
) -> List[Dict[str, Any]]:
    """Reuse the codebase's existing Polygon→yfinance fetcher.

    Returns a list of daily aggregate dicts with keys {t (ms), o, h, l, c, v}
    in ascending date order. Empty list when the ticker has no data in the
    window (delisted, pre-IPO, or hard yfinance failure)."""
    from modal_workers.scripts.backfill_realized_move import fetch_daily_closes
    return fetch_daily_closes(ticker, window_start=window_start, window_end=window_end)


# ---------------------------------------------------------------------------
# Core curation logic (pure functions, no I/O — testable in isolation)
# ---------------------------------------------------------------------------

def compute_adv_usd(closes: List[Dict[str, Any]]) -> Optional[float]:
    """Average daily dollar volume = mean(close × volume) over the supplied
    aggregates. Returns None if `closes` is empty."""
    if not closes:
        return None
    products = [
        float(c.get("c") or 0.0) * float(c.get("v") or 0.0)
        for c in closes
        if c.get("c") is not None and c.get("v") is not None
    ]
    if not products:
        return None
    return sum(products) / len(products)


def pick_close_on_or_before(
    closes: List[Dict[str, Any]],
    target: date,
) -> Optional[float]:
    """Last available close at or before target date. None if no such row."""
    candidates = []
    for c in closes:
        ts_ms = c.get("t")
        if ts_ms is None:
            continue
        d = datetime.utcfromtimestamp(int(ts_ms) / 1000.0).date()
        if d <= target:
            candidates.append((d, float(c.get("c") or 0.0)))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


def classify_exchange_allowed(exchange_code: Optional[str]) -> bool:
    """True iff `exchange_code` is in the D-105 allowlist."""
    if not exchange_code:
        return False
    return exchange_code.upper() in ALLOWED_EXCHANGES


def decide_filter(
    *,
    closes_for_adv: List[Dict[str, Any]],
    close_at_ref: Optional[float],
    shares_outstanding: Optional[float],
    exchange_code: Optional[str],
    has_future_history: bool,
) -> FilterDecision:
    """Apply the three D-105 thresholds + issuer_status rule.

    All inputs are pre-fetched so this function is pure + unit-testable.
    """
    # issuer_status — derive first; if 'delisted', short-circuit the filter.
    if close_at_ref is None:
        # No price at reference_assessment_date — issuer either pre-IPO or
        # not on a covered exchange. Mark delisted (a misnomer but the only
        # available bucket for "not tradeable then"); the filter is false.
        return FilterDecision(
            tradeable_filter_pass=False,
            issuer_status="delisted",
            rationale="no price history at reference_assessment_date",
            exchange=exchange_code,
        )
    issuer_status = "active" if has_future_history else "delisted"

    # Market cap proxy: today's sharesOutstanding × historical close.
    mcap = None
    if shares_outstanding and shares_outstanding > 0:
        mcap = float(shares_outstanding) * float(close_at_ref)

    adv = compute_adv_usd(closes_for_adv)
    exchange_ok = classify_exchange_allowed(exchange_code)

    reasons = []
    if mcap is None:
        reasons.append("mcap_unknown")
    elif mcap < MIN_MARKET_CAP_USD:
        reasons.append(f"mcap<${MIN_MARKET_CAP_USD/1e6:.0f}M")
    if adv is None:
        reasons.append("adv_unknown")
    elif adv < MIN_ADV_USD:
        reasons.append(f"adv<${MIN_ADV_USD/1e3:.0f}K")
    if not exchange_ok:
        reasons.append(f"exchange={exchange_code or 'unknown'}")

    passed = (
        mcap is not None and mcap >= MIN_MARKET_CAP_USD
        and adv is not None and adv >= MIN_ADV_USD
        and exchange_ok
    )

    return FilterDecision(
        tradeable_filter_pass=passed,
        issuer_status=issuer_status,
        rationale="pass" if passed else ",".join(reasons),
        market_cap_usd=mcap,
        adv_usd=adv,
        exchange=exchange_code,
    )


# ---------------------------------------------------------------------------
# Per-row curation
# ---------------------------------------------------------------------------

def curate_row(
    eval_row: Dict[str, Any],
    *,
    fetch_history=fetch_history_window,
    fetch_info=_fetch_yf_info,
    today: Optional[date] = None,
) -> Optional[FilterDecision]:
    """Compose a FilterDecision for one eval_harness row. Returns None when
    the row lacks the required (ticker, reference_assessment_date)."""
    fa = eval_row.get("fda_assets") or {}
    ticker = fa.get("ticker")
    ref_iso = eval_row.get("reference_assessment_date")
    if not ticker or not ref_iso:
        return None
    try:
        ref_d = datetime.strptime(ref_iso, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None

    today = today or date.today()

    # ADV window: 90 trading days ≈ ~130 calendar days back.
    adv_window_start = ref_d - timedelta(days=130)
    closes_for_adv = [
        c for c in fetch_history(
            ticker, window_start=adv_window_start, window_end=ref_d,
        )
        if datetime.utcfromtimestamp(int(c.get("t") or 0) / 1000.0).date() <= ref_d
    ]
    # Trim to the trailing 90 trading days.
    closes_for_adv = closes_for_adv[-ADV_WINDOW_TRADING_DAYS:]

    close_at_ref = pick_close_on_or_before(closes_for_adv, ref_d)

    # has_future_history: does the ticker still trade ~1y after ref_d?
    # Skip the query if ref_d+365 is in the future (treat as 'active').
    future_anchor = ref_d + timedelta(days=365)
    if future_anchor >= today:
        has_future_history = True
    else:
        future = fetch_history(
            ticker,
            window_start=future_anchor - timedelta(days=10),
            window_end=future_anchor + timedelta(days=10),
        )
        has_future_history = bool(future)

    info = fetch_info(ticker)
    shares_outstanding = info.get("sharesOutstanding") if info else None
    exchange_code = info.get("exchange") if info else None

    return decide_filter(
        closes_for_adv=closes_for_adv,
        close_at_ref=close_at_ref,
        shares_outstanding=shares_outstanding,
        exchange_code=exchange_code,
        has_future_history=has_future_history,
    )


def needs_patch(eval_row: Dict[str, Any], decision: FilterDecision) -> bool:
    """True iff the row's stored (tradeable_filter_pass, issuer_status) does
    not already match `decision` — drives idempotency."""
    return (
        bool(eval_row.get("tradeable_filter_pass")) != bool(decision.tradeable_filter_pass)
        or (eval_row.get("issuer_status") or None) != decision.issuer_status
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(
    *,
    sb: Optional[SupabaseClient] = None,
    apply: bool,
    limit: int,
    only_empty: bool,
) -> Stats:
    sb = sb or SupabaseClient()
    stats = Stats()

    params: Dict[str, str] = {
        "select": "id,asset_id,reference_assessment_date,"
                  "tradeable_filter_pass,issuer_status,"
                  "fda_assets(id,ticker,drug_name)",
        "limit": str(limit),
    }
    if only_empty:
        params["issuer_status"] = "is.null"
    rows = sb._rest("GET", "eval_harness", params=params) or []
    stats.rows_seen = len(rows)
    logger.info("inspecting %d eval_harness rows", len(rows))

    for r in rows:
        try:
            decision = curate_row(r)
        except Exception as exc:  # noqa: BLE001
            logger.warning("curate failed for %s: %s", r.get("id"), exc)
            stats.rows_error += 1
            continue
        if decision is None:
            stats.rows_error += 1
            continue

        stats.by_pass[decision.tradeable_filter_pass] = (
            stats.by_pass.get(decision.tradeable_filter_pass, 0) + 1
        )
        stats.by_status[decision.issuer_status] = (
            stats.by_status.get(decision.issuer_status, 0) + 1
        )

        logger.info(
            "%s ticker=%s pass=%s status=%s mcap=%s adv=%s exch=%s reason=%s",
            (r.get("id") or "")[:8],
            ((r.get("fda_assets") or {}).get("ticker")),
            decision.tradeable_filter_pass,
            decision.issuer_status,
            f"${decision.market_cap_usd/1e6:.1f}M" if decision.market_cap_usd else "?",
            f"${decision.adv_usd/1e3:.0f}K" if decision.adv_usd else "?",
            decision.exchange or "?",
            decision.rationale,
        )

        if not needs_patch(r, decision):
            stats.rows_skipped_idempotent += 1
            continue

        if not apply:
            continue

        try:
            sb._rest(
                "PATCH", "eval_harness",
                params={"id": f"eq.{r['id']}"},
                json_body={
                    "tradeable_filter_pass": decision.tradeable_filter_pass,
                    "issuer_status": decision.issuer_status,
                },
                prefer="return=minimal",
            )
            stats.rows_updated += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("PATCH failed for %s: %s", r.get("id"), exc)
            stats.rows_error += 1

    logger.info(
        "curation summary: seen=%d updated=%d idempotent_skip=%d errors=%d "
        "by_pass=%s by_status=%s",
        stats.rows_seen, stats.rows_updated, stats.rows_skipped_idempotent,
        stats.rows_error, dict(stats.by_pass), dict(stats.by_status),
    )
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="curate_tradeable_filter_pass")
    p.add_argument("--dry-run", action="store_true",
                   help="print plan without PATCHing; this is the default.")
    p.add_argument("--apply", action="store_true",
                   help="commit PATCHes; default is dry-run.")
    p.add_argument("--limit", type=int, default=500,
                   help="cap row count (debugging).")
    p.add_argument("--only-empty", action="store_true", default=True,
                   help="restrict to rows where issuer_status IS NULL.")
    p.add_argument("--all", action="store_true",
                   help="override --only-empty; revisit every row.")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    only_empty = args.only_empty and not args.all
    stats = run(apply=args.apply, limit=args.limit, only_empty=only_empty)
    return 0 if stats.rows_error == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
