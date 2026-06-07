"""bcfda.outcomes.price_returns — t+1 / t+7 / t+30 price returns around a PDUFA.

Phase 3 §5.2. For a resolved PDUFA, compute the split/dividend-adjusted price
return at N trading days post-decision vs the pre-PDUFA close:

    base    = the adjusted close on the LAST trading day STRICTLY BEFORE pdufa_date
    close_N = the adjusted close on the Nth TRADING DAY at/after pdufa_date
    price_return_pct = (close_N / base - 1) * 100

Trading-day counting is done on the returned Polygon daily aggregates, which
contain only trading days, so weekends/holidays are skipped naturally (a market
halt that drops a bar is also skipped — the Nth *available* bar is used, and a
horizon with no available bar yet returns None + a log token, §9 risk 4).

The pure functions here (``compute_return_for_horizon`` / ``compute_returns``)
take the already-fetched bar list, so they are unit-testable with fixture bars
and NO network. ``fetch_returns`` is the thin Polygon wrapper (uses
``PolygonMarketData.get_historical_prices`` — the §0.8 reuse pattern); it is the
only function that touches the network.

INVARIANT: this module reads PRICES only. Polygon options/IV is irrelevant to the
labeler (band-only v1; the labeler logs realized price reaction, not implied move).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bcfda.outcomes.price_returns")


def _bar_date(bar: Dict[str, Any]) -> Optional[date]:
    """Polygon daily aggregate timestamp ``t`` is epoch-MILLIS at 00:00 of the
    trading day (exchange tz). Convert to a UTC date for trading-day ordering.
    Returns None if the bar has no usable timestamp."""
    t = bar.get("t")
    if t is None:
        return None
    try:
        return datetime.fromtimestamp(float(t) / 1000.0, tz=timezone.utc).date()
    except (TypeError, ValueError, OverflowError):
        return None


def _close(bar: Dict[str, Any]) -> Optional[float]:
    c = bar.get("c")
    if c is None:
        return None
    try:
        return float(c)
    except (TypeError, ValueError):
        return None


def _parse_pdufa(pdufa_date: Any) -> Optional[date]:
    if isinstance(pdufa_date, date):
        return pdufa_date
    if not pdufa_date:
        return None
    try:
        return datetime.strptime(str(pdufa_date)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def split_bars_around_pdufa(
    bars: List[Dict[str, Any]],
    pdufa_date: Any,
) -> Dict[str, Any]:
    """Split a sorted-or-unsorted daily-bar list into (pre, post) around PDUFA.

    Returns ``{"base": <float|None>, "post": [closes...]}``:
      - ``base``: the close on the last trading day STRICTLY BEFORE pdufa_date.
      - ``post``: the closes on each trading day AT/AFTER pdufa_date, in
        chronological order (so ``post[N-1]`` is the t+N close, 1-indexed).

    Bars missing a date or close are dropped (so a halted/empty day is skipped,
    not counted as a horizon). Returns ``base=None`` when no pre-PDUFA bar exists.
    """
    pdufa = _parse_pdufa(pdufa_date)
    if pdufa is None:
        return {"base": None, "post": []}

    usable: List[tuple[date, float]] = []
    for b in bars or []:
        d = _bar_date(b)
        c = _close(b)
        if d is None or c is None:
            continue
        usable.append((d, c))
    usable.sort(key=lambda x: x[0])

    base: Optional[float] = None
    post: List[float] = []
    for d, c in usable:
        if d < pdufa:
            base = c  # keep advancing => the LAST strictly-before close
        else:
            post.append(c)
    return {"base": base, "post": post}


def compute_return_for_horizon(
    bars: List[Dict[str, Any]],
    pdufa_date: Any,
    horizon_days: int,
) -> Optional[float]:
    """``(close_{t+N} / base - 1) * 100`` for a single horizon, or None.

    Returns None when there is no pre-PDUFA base close, or when the Nth post-PDUFA
    trading bar has not matured yet (so an immature horizon stays null + the row
    still records the verdict — §5.3 partial-friendly).
    """
    split = split_bars_around_pdufa(bars, pdufa_date)
    base = split["base"]
    post: List[float] = split["post"]
    if base is None or base == 0:
        return None
    if horizon_days < 1 or len(post) < horizon_days:
        return None  # not mature yet (or invalid horizon)
    close_n = post[horizon_days - 1]  # t+N is the Nth post-PDUFA trading day (1-indexed)
    return (close_n / base - 1.0) * 100.0


def compute_returns(
    bars: List[Dict[str, Any]],
    pdufa_date: Any,
    horizons: List[int],
) -> Dict[int, Optional[float]]:
    """Compute each horizon's return from the SAME bar list (pure, no network).

    Returns ``{horizon_days: price_return_pct|None}``. An immature/missing horizon
    is None; the caller omits None from the upsert body so a later (matured) run
    merges it without clobbering (§5.3 null-omitting upsert)."""
    return {h: compute_return_for_horizon(bars, pdufa_date, h) for h in horizons}


def fetch_returns(
    market_data: Any,
    ticker: str,
    pdufa_date: Any,
    horizons: List[int],
    *,
    lookback_days: int = 45,
) -> Dict[str, Any]:
    """Fetch Polygon daily bars bracketing PDUFA and compute the horizon returns.

    Uses ``market_data.get_historical_prices(ticker, days)`` (the §0.8 reuse;
    adjusted=true, sort=asc daily OHLC). The only network function in this module.

    Returns ``{"returns": {h: pct|None}, "base": <float|None>, "n_bars": int,
    "log": <token|None>}``. A no-ticker / no-bars case yields all-None returns and
    a log token so the caller records the verdict with price null (§5.2 ``no_ticker``
    / ``no_bars``).
    """
    if not ticker:
        return {"returns": {h: None for h in horizons}, "base": None, "n_bars": 0, "log": "no_ticker"}

    # Window: ~lookback_days before PDUFA through the longest horizon after it.
    # max horizon trading days ~= 1.5x calendar days; pad generously.
    max_h = max(horizons) if horizons else 30
    days = lookback_days + int(max_h * 1.6) + 7
    try:
        bars = market_data.get_historical_prices(ticker, days)
    except Exception as exc:  # noqa: BLE001 — price fetch is advisory; record verdict regardless
        logger.warning("polygon get_historical_prices failed for %s: %s", ticker, exc)
        return {"returns": {h: None for h in horizons}, "base": None, "n_bars": 0, "log": f"price_fetch_error:{type(exc).__name__}"}

    if not bars:
        return {"returns": {h: None for h in horizons}, "base": None, "n_bars": 0, "log": "no_bars"}

    split = split_bars_around_pdufa(bars, pdufa_date)
    returns = compute_returns(bars, pdufa_date, horizons)
    log_token = None
    if split["base"] is None:
        log_token = "no_pre_pdufa_bar"
    return {"returns": returns, "base": split["base"], "n_bars": len(bars), "log": log_token}
