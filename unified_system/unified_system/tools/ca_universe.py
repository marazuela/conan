"""
Canada universe enumerator — Phase 4.

Builds a TSX + TSXV issuer list, filters to market cap >= $300M USD.

Source strategy:
  TMX / TSX does not expose a clean public CSV of all listed issuers, but:
    https://www.tsx.com/json/company-directory/search/tsx/<letter>
    https://www.tsx.com/json/company-directory/search/tsxv/<letter>
  returns JSON with {"results":[{"symbol":"ABC","name":"..."}]}. We iterate
  A-Z + 0-9 per board, dedup, then enrich market cap via yfinance using the
  "<symbol>.TO" (TSX) or "<symbol>.V" (TSXV) suffix.

CAD->USD conversion via yfinance "CADUSD=X"; fallback 0.72.

Cache: working/ca_universe.json, TTL 7 days.
"""
from __future__ import annotations

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

UNIVERSE_PATH = WORKING / "ca_universe.json"
UNIVERSE_TTL_SECONDS = 7 * 24 * 3600
MCAP_FLOOR_USD_MM = 300.0

# TMX company directory endpoints — return JSON keyed by first character.
TSX_DIRECTORY_TEMPLATE = "https://www.tsx.com/json/company-directory/search/tsx/{char}"
TSXV_DIRECTORY_TEMPLATE = "https://www.tsx.com/json/company-directory/search/tsxv/{char}"

_CAD_USD_DEFAULT = 0.72
_cad_usd_runtime: Optional[float] = None


def _get_cad_usd() -> float:
    global _cad_usd_runtime
    if _cad_usd_runtime is not None:
        return _cad_usd_runtime
    try:
        import yfinance as yf
        import warnings
        warnings.filterwarnings("ignore")
        t = yf.Ticker("CADUSD=X")
        info = t.info or {}
        rate = info.get("regularMarketPrice") or info.get("previousClose")
        if rate and 0.3 < rate < 1.5:
            _cad_usd_runtime = float(rate)
            return _cad_usd_runtime
    except Exception:
        pass
    _cad_usd_runtime = _CAD_USD_DEFAULT
    return _cad_usd_runtime


def _fetch_directory(board: str) -> list[dict]:
    """Fetch TSX or TSXV directory, iterate A-Z + 0-9, return list of {ticker, name}.

    board: "tsx" or "tsxv"
    """
    import urllib.request

    if board == "tsx":
        template = TSX_DIRECTORY_TEMPLATE
    elif board == "tsxv":
        template = TSXV_DIRECTORY_TEMPLATE
    else:
        raise ValueError(f"unknown board {board}")

    out: list[dict] = []
    seen: set[str] = set()
    chars = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    for ch in chars:
        url = template.format(char=ch)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as e:
            log.debug("ca_universe: directory %s char=%s failed: %s", board, ch, e)
            continue
        results = data.get("results") or []
        for r in results:
            sym = (r.get("symbol") or "").strip().upper()
            name = (r.get("name") or "").strip()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            out.append({"ticker": sym, "name": name, "board": board})
    return out


def _fetch_mcap(ticker: str, suffix: str, yf):
    sym = f"{ticker}{suffix}"
    try:
        info = yf.Ticker(sym).info or {}
    except Exception:
        return None, None
    mc = info.get("marketCap")
    currency = info.get("currency") or "CAD"
    if mc and isinstance(mc, (int, float)) and mc > 0:
        return float(mc), currency
    return None, currency


def refresh_universe(force: bool = False,
                    throttle_seconds: float = 0.2,
                    max_tickers: Optional[int] = None,
                    boards: tuple[str, ...] = ("tsx", "tsxv")) -> dict:
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

    rows: list[dict] = []
    for board in boards:
        rows.extend(_fetch_directory(board))

    if max_tickers:
        rows = rows[:max_tickers]

    cad_usd = _get_cad_usd()
    above_floor = []
    below_floor_count = 0
    unresolved_count = 0

    for i, row in enumerate(rows, 1):
        suffix = ".TO" if row["board"] == "tsx" else ".V"
        mic = "XTSE" if row["board"] == "tsx" else "XTSX"
        mc_native, ccy = _fetch_mcap(row["ticker"], suffix, yf)
        if mc_native is None:
            unresolved_count += 1
        else:
            # Yahoo returns marketCap in native currency for .TO/.V (CAD).
            mc_usd = mc_native if ccy == "USD" else mc_native * cad_usd
            mc_usd_mm = round(mc_usd / 1e6, 1)
            if mc_usd_mm >= MCAP_FLOOR_USD_MM:
                above_floor.append({
                    "ticker": row["ticker"],
                    "name": row["name"],
                    "board": row["board"],
                    "mic": mic,
                    "suffix": suffix,
                    "market_cap_usd_mm": mc_usd_mm,
                    "market_cap_cad_mm": round(mc_native / 1e6, 1),
                    "currency": ccy,
                })
            else:
                below_floor_count += 1
        if throttle_seconds:
            time.sleep(throttle_seconds)

    universe = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "https://www.tsx.com/json/company-directory/search/{board}/{letter}",
        "boards": list(boards),
        "total_listed": len(rows),
        "above_floor": len(above_floor),
        "below_floor": below_floor_count,
        "unresolved": unresolved_count,
        "cad_usd_rate": cad_usd,
        "tickers": sorted(above_floor, key=lambda x: -x["market_cap_usd_mm"]),
    }
    UNIVERSE_PATH.write_text(json.dumps(universe, ensure_ascii=False, indent=2), encoding="utf-8")
    return universe


def load_universe(auto_refresh: bool = True) -> dict:
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
    parser.add_argument("--boards", default="tsx,tsxv",
                        help="comma-separated: tsx, tsxv")
    args = parser.parse_args()
    boards = tuple(b.strip() for b in args.boards.split(",") if b.strip())
    u = refresh_universe(force=args.force, throttle_seconds=args.throttle,
                         max_tickers=args.max, boards=boards)
    print(f"Canada universe: {u['above_floor']} tickers above $300M USD floor "
          f"(TSX+TSXV), CAD/USD={u['cad_usd_rate']:.4f}")

# --- END OF FILE ---
