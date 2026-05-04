"""
Polygon options data: chain snapshot, IV, straddle-implied move, event-window
liquidity.

Endpoints used:
  - /v3/snapshot/options/{underlying}     paginated chain with per-contract IV,
                                          bid/ask, open interest, last trade.

A "straddle implied move" is computed from the ATM call+put expiring on or
just after the event_date: implied_move_pct = (call_mid + put_mid) / underlying.
This is a rough proxy — accurate for short-dated, near-ATM options on
single-event names. For binary FDA events the straddle is dominated by the
event payoff, so it's a usable signal.

Returns None when the chain is illiquid (fewer than `min_liquid_contracts`
total, or no contracts within the event window). The bridge degrades to
non-options market_implied_probability in that case.

ContractSnapshot shape (subset we care about):
  {
    "details": {"contract_type": "call"|"put", "strike_price": 50.0,
                "expiration_date": "2026-09-19", "ticker": "O:..."},
    "implied_volatility": 0.85,
    "open_interest": 1234,
    "last_quote": {"bid": 1.05, "ask": 1.15, "midpoint": 1.10},
    "underlying_asset": {"price": 50.50},
  }
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Protocol

from modal_workers.providers.polygon.base import PolygonClient

logger = logging.getLogger(__name__)

DEFAULT_EVENT_WINDOW_DAYS = 30
MIN_LIQUID_CONTRACTS = 5


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _midpoint(quote: Dict[str, Any]) -> Optional[float]:
    if not quote:
        return None
    mid = quote.get("midpoint")
    if mid is not None:
        return float(mid)
    bid = quote.get("bid")
    ask = quote.get("ask")
    if bid is None or ask is None:
        return None
    return (float(bid) + float(ask)) / 2.0


class OptionsDataProvider(Protocol):
    def get_chain(self, ticker: str, expiry: Optional[date] = None) -> Optional[List[Dict[str, Any]]]: ...
    def get_iv(self, ticker: str, strike: float, expiry: date) -> Optional[float]: ...
    def get_straddle_implied_move(self, ticker: str, event_date: date) -> Optional[Dict[str, Any]]: ...
    def get_event_window_liquidity(self, ticker: str, event_date: date) -> Optional[Dict[str, Any]]: ...


class PolygonOptionsData:
    def __init__(
        self,
        client: PolygonClient,
        *,
        event_window_days: int = DEFAULT_EVENT_WINDOW_DAYS,
        min_liquid_contracts: int = MIN_LIQUID_CONTRACTS,
    ):
        self.client = client
        self.event_window_days = event_window_days
        self.min_liquid_contracts = min_liquid_contracts

    # --------------------------------------------------------------
    # Chain snapshot
    # --------------------------------------------------------------

    def get_chain(self, ticker: str, expiry: Optional[date] = None) -> Optional[List[Dict[str, Any]]]:
        params: Dict[str, Any] = {"limit": 250}
        if expiry is not None:
            params["expiration_date"] = expiry.isoformat()
        contracts: List[Dict[str, Any]] = []
        for page in self.client.paginate(f"/v3/snapshot/options/{ticker}", params=params):
            if not isinstance(page, dict):
                break
            results = page.get("results") or []
            contracts.extend(results)
        if not contracts:
            return None
        return contracts

    # --------------------------------------------------------------
    # IV for a specific (strike, expiry) — returns the call IV by default
    # --------------------------------------------------------------

    def get_iv(self, ticker: str, strike: float, expiry: date) -> Optional[float]:
        contracts = self.get_chain(ticker, expiry=expiry) or []
        for c in contracts:
            details = c.get("details") or {}
            if details.get("contract_type") != "call":
                continue
            if abs(float(details.get("strike_price") or 0) - strike) < 1e-6:
                iv = c.get("implied_volatility")
                return float(iv) if iv is not None else None
        return None

    # --------------------------------------------------------------
    # Straddle implied move
    # --------------------------------------------------------------

    def get_straddle_implied_move(self, ticker: str, event_date: date) -> Optional[Dict[str, Any]]:
        contracts = self.get_chain(ticker) or []
        if len(contracts) < self.min_liquid_contracts:
            logger.info("polygon options: %s chain too small (%d) — illiquid", ticker, len(contracts))
            return None

        # Underlying spot from the first snapshot's underlying_asset.price
        underlying = None
        for c in contracts:
            ua = c.get("underlying_asset") or {}
            if ua.get("price"):
                underlying = float(ua["price"])
                break
        if not underlying:
            return None

        # Pick the smallest expiry on or after event_date.
        def _expiry(c: Dict[str, Any]) -> Optional[date]:
            return _parse_date((c.get("details") or {}).get("expiration_date"))

        eligible = [
            c for c in contracts
            if (_expiry(c) is not None and _expiry(c) >= event_date)
        ]
        if not eligible:
            return None
        target_expiry = min(_expiry(c) for c in eligible)  # type: ignore[type-var]
        chain = [c for c in eligible if _expiry(c) == target_expiry]

        calls = [c for c in chain if (c.get("details") or {}).get("contract_type") == "call"]
        puts = [c for c in chain if (c.get("details") or {}).get("contract_type") == "put"]
        if not calls or not puts:
            return None

        atm_call = min(
            calls,
            key=lambda c: abs(float((c.get("details") or {}).get("strike_price") or 0) - underlying),
        )
        atm_put = min(
            puts,
            key=lambda c: abs(float((c.get("details") or {}).get("strike_price") or 0) - underlying),
        )
        call_mid = _midpoint(atm_call.get("last_quote") or {})
        put_mid = _midpoint(atm_put.get("last_quote") or {})
        if call_mid is None or put_mid is None:
            return None

        straddle = call_mid + put_mid
        if straddle <= 0:
            return None
        implied_move_pct = (straddle / underlying) * 100.0
        return {
            "underlying_price": underlying,
            "expiry": target_expiry.isoformat(),
            "call_strike": float((atm_call.get("details") or {}).get("strike_price") or 0),
            "put_strike": float((atm_put.get("details") or {}).get("strike_price") or 0),
            "call_mid": call_mid,
            "put_mid": put_mid,
            "straddle_price": straddle,
            "implied_move_pct": implied_move_pct,
            "call_iv": atm_call.get("implied_volatility"),
            "put_iv": atm_put.get("implied_volatility"),
        }

    # --------------------------------------------------------------
    # Event-window liquidity score (0..5)
    # --------------------------------------------------------------

    def get_event_window_liquidity(self, ticker: str, event_date: date) -> Optional[Dict[str, Any]]:
        contracts = self.get_chain(ticker) or []
        if not contracts:
            return None
        window_end = event_date + timedelta(days=self.event_window_days)
        in_window = [
            c for c in contracts
            if (
                _parse_date((c.get("details") or {}).get("expiration_date"))
                and event_date <= _parse_date((c.get("details") or {}).get("expiration_date")) <= window_end  # type: ignore[operator]
            )
        ]
        total_oi = sum(float(c.get("open_interest") or 0) for c in in_window)
        contract_count = len(in_window)
        # Score: 5=deep, 0=none. Calibrated to crude OI thresholds.
        if contract_count == 0 or total_oi == 0:
            return {
                "contract_count": contract_count,
                "total_open_interest": total_oi,
                "liquidity_score": 0.0,
            }
        if total_oi >= 10000 and contract_count >= 20:
            score = 5.0
        elif total_oi >= 5000 and contract_count >= 12:
            score = 4.0
        elif total_oi >= 1000 and contract_count >= 6:
            score = 3.0
        elif total_oi >= 250 and contract_count >= 3:
            score = 2.0
        else:
            score = 1.0
        return {
            "contract_count": contract_count,
            "total_open_interest": total_oi,
            "liquidity_score": score,
        }
