"""
ASX universe enumerator — Phase 3.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
WORKING = ROOT / "working"
WORKING.mkdir(parents=True, exist_ok=True)

UNIVERSE_PATH = WORKING / "asx_universe.json"
UNIVERSE_TTL_SECONDS = 7 * 24 * 3600
MCAP_FLOOR_USD_MM = 300.0

CSV_URL = "https://www.asx.com.au/asx/research/ASXListedCompanies.csv"

_AUD_USD_DEFAULT = 0.66
_aud_usd_runtime: Optional[float] = None


def _get_aud_usd() -> float:
    global _aud_usd_runtime
    if _aud_usd_runtime is not None:
        return _aud_usd_runtime
    try:
        import yfinance as yf
        import warnings
        warnings.filterwarnings("ignore")
        t = yf.Ticker("AUDUSD=X")
        info = t.info or {}
        rate = info.get("regularMarketPrice") or info.get("previousClose")
        if rate and 0.3 < rate < 1.5:
            _aud_usd_runtime = float(rate)
            return _aud_usd_runtime
    except Exception:
        pass
    _aud_usd_runtime = _AUD_USD_DEFAULT
    return _aud_usd_runtime


def _fetch_listed_csv() -> list[dict]:
    import urllib.request
    req = urllib.request.Request(CSV_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read().decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(data))
    rows = []
    for r in reader:
        if len(r) < 3:
            continue
        name, ticker, gics = r[0], r[1], r[2]
        if not ticker or not ticker.strip():
            continue
        t_up = ticker.strip().upper()
        if t_up in ("ASX CODE", "ASX_CODE") or t_up.startswith("ASX LISTED"):
            continue
        rows.append({"name": name.strip(), "ticker": t_up, "gics": gics.strip()})
    return rows


def _fetch_mcap(ticker, yf):
    sym = f"{ticker}.AX"
    try:
        info = yf.Ticker(sym).info or {}
    except Exception:
        return None, None
    mc = info.get("marketCap")
    currency = info.get("currency") or "AUD"
    if mc and isinstance(mc, (int, float)) and mc > 0:
        return float(mc), currency
    return None, currency


def refresh_universe(force=False, throttle_seconds=0.2, max_tickers=None):
    if UNIVERSE_PATH.exists() and not force:
        try:
            cached = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
            asof = datetime.fromisoformat(cached["as_of"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - asof).total_seconds()
            if age < UNIVERSE_TTL_SECONDS:
                return cached
        except Exception:
            pass

    import yfinance as yf
    import warnings
    warnings.filterwarnings("ignore")

    rows = _fetch_listed_csv()
    if max_tickers:
        rows = rows[:max_tickers]

    aud_usd = _get_aud_usd()
    above_floor = []
    below_floor_count = 0
    unresolved_count = 0
    for i, row in enumerate(rows, 1):
        mc_aud, ccy = _fetch_mcap(row["ticker"], yf)
        if mc_aud is None:
            unresolved_count += 1
        else:
            mc_usd = mc_aud if ccy == "USD" else mc_aud * aud_usd
            mc_usd_mm = round(mc_usd / 1e6, 1)
            if mc_usd_mm >= MCAP_FLOOR_USD_MM:
                above_floor.append({
                    "ticker": row["ticker"],
                    "name": row["name"],
                    "gics": row["gics"],
                    "market_cap_usd_mm": mc_usd_mm,
                    "market_cap_aud_mm": round(mc_aud / 1e6, 1),
                    "currency": ccy,
                })
            else:
                below_floor_count += 1
        if throttle_seconds:
            time.sleep(throttle_seconds)

    universe = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": CSV_URL,
        "total_listed": len(rows),
        "above_floor": len(above_floor),
        "below_floor": below_floor_count,
        "unresolved": unresolved_count,
        "aud_usd_rate": aud_usd,
        "tickers": sorted(above_floor, key=lambda x: -x["market_cap_usd_mm"]),
    }
    UNIVERSE_PATH.write_text(json.dumps(universe, ensure_ascii=False, indent=2), encoding="utf-8")
    return universe


def load_universe(auto_refresh=True):
    if UNIVERSE_PATH.exists():
        try:
            cached = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
        except Exception:
            cached = None
        if cached is not None:
            if not auto_refresh:
                return cached
            try:
                asof = datetime.fromisoformat(cached["as_of"].replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - asof).total_seconds()
                if age < UNIVERSE_TTL_SECONDS:
                    return cached
            except Exception:
                pass
    if not auto_refresh:
        return {"tickers": []}
    return refresh_universe()


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--max", type=int, default=None)
    parser.add_argument("--throttle", type=float, default=0.2)
    args = parser.parse_args()
    u = refresh_universe(force=args.force, throttle_seconds=args.throttle, max_tickers=args.max)
    print(f"Universe: {u['above_floor']} tickers above $300M USD floor")
