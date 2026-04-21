"""
Live price refresh for the publish path (v1.0 — 2026-04-20)
===========================================================

Fetches the latest daily close for each active curated candidate and rewrites
the `price_targets` block so executive summary / dossiers display current
numbers instead of the original anchor price captured by the curator.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

REPO = Path(__file__).parent.parent
WORKING = REPO / "working"
WORKING.mkdir(exist_ok=True)
OVERLAY_PATH = WORKING / "_live_prices_overlay.json"
OVERLAY_MAX_AGE_HOURS = 6


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fetch_live_price(ticker: str) -> Optional[Tuple[float, str]]:
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return None
    try:
        history = yf.Ticker(ticker).history(period="5d", auto_adjust=False)
        if history is None or len(history) == 0:
            return None
        closes = history["Close"].tolist()
        dates = [str(value.date()) for value in history.index]
        return float(closes[-1]), dates[-1]
    except Exception:
        return None


def _parse_price_range(value: str) -> Optional[Tuple[float, float]]:
    if not isinstance(value, str):
        return None
    match = re.search(r"\$?(\d+\.?\d*)\s*[-–—]\s*\$?(\d+\.?\d*)", value)
    if match:
        try:
            return float(match.group(1)), float(match.group(2))
        except ValueError:
            return None
    match = re.search(r"\$(\d+\.?\d*)", value)
    if match:
        try:
            parsed = float(match.group(1))
            return parsed, parsed
        except ValueError:
            return None
    return None


def _fmt_pct(percent: float) -> str:
    sign = "+" if percent >= 0 else "-"
    return f"{sign}{abs(percent):.0f}%"


def _recompute_pct_in_string(value: str, new_ref: float) -> str:
    if not isinstance(value, str) or new_ref <= 0:
        return value
    parsed = _parse_price_range(value)
    if not parsed:
        return value
    low, high = parsed
    pct_low = (low / new_ref - 1.0) * 100.0
    pct_high = (high / new_ref - 1.0) * 100.0
    new_group = f"({_fmt_pct(pct_low)} to {_fmt_pct(pct_high)})"
    pattern = re.compile(r"\([+\-−]?\s*\d+\.?\d*%[^)]*\)")
    if pattern.search(value):
        return pattern.sub(new_group, value, count=1)
    return value


def refresh_all(curated: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(curated, dict):
        return {"_live_refresh": {"error": "curated was not a dict", "refreshed_at": _now().isoformat().replace("+00:00", "Z")}}
    out = json.loads(json.dumps(curated))
    meta = {
        "refreshed_at": _now().isoformat().replace("+00:00", "Z"),
        "tickers_refreshed": [],
        "tickers_stale": [],
        "tickers_skipped": [],
    }
    for ticker, entry in out.items():
        if ticker.startswith("_") or not isinstance(entry, dict):
            continue
        price_targets = entry.get("price_targets")
        if not isinstance(price_targets, dict):
            meta["tickers_skipped"].append(ticker)
            continue
        live = _fetch_live_price(ticker)
        if live is None:
            original = str(price_targets.get("reference_price", ""))
            if not original.startswith("[STALE"):
                price_targets["reference_price"] = f"[STALE — live refresh failed] {original}".strip()
            meta["tickers_stale"].append(ticker)
            continue
        close, date_str = live
        price_targets["reference_price"] = f"${close:.2f} ({date_str} live)"
        for key in ("upside_base", "upside_best", "downside"):
            if key in price_targets and isinstance(price_targets[key], str):
                price_targets[key] = _recompute_pct_in_string(price_targets[key], close)
        meta["tickers_refreshed"].append(ticker)
    out["_live_refresh"] = meta
    return out


def write_overlay(overlay: Dict[str, Any]) -> Path:
    tmp = OVERLAY_PATH.with_suffix(OVERLAY_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(overlay, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
    except Exception:
        pass
    os.replace(tmp, OVERLAY_PATH)
    return OVERLAY_PATH


def load_overlay_if_fresh() -> Optional[Dict[str, Any]]:
    if not OVERLAY_PATH.exists():
        return None
    try:
        data = json.loads(OVERLAY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    meta = data.get("_live_refresh") or {}
    stamp = meta.get("refreshed_at")
    if not stamp:
        return None
    try:
        ts = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except Exception:
        return None
    age_hours = (_now() - ts).total_seconds() / 3600.0
    if age_hours > OVERLAY_MAX_AGE_HOURS:
        return None
    return data


def refresh_and_write(curated_path: Optional[Path] = None) -> Dict[str, Any]:
    if curated_path is None:
        curated_path = REPO / "candidates" / "_curated_rationales.json"
    try:
        curated = json.loads(Path(curated_path).read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": f"failed to load curated: {exc}"}
    overlay = refresh_all(curated)
    write_overlay(overlay)
    return overlay.get("_live_refresh", {})


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Refresh live reference prices for publish")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print, do not write overlay")
    args = parser.parse_args()
    curated_path = REPO / "candidates" / "_curated_rationales.json"
    try:
        curated = json.loads(curated_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(json.dumps({"error": f"failed to load curated: {exc}"}))
        return
    overlay = refresh_all(curated)
    meta = overlay.get("_live_refresh", {})
    print(json.dumps(meta, indent=2))
    diff: Dict[str, Any] = {}
    for ticker, entry in overlay.items():
        if ticker.startswith("_") or not isinstance(entry, dict):
            continue
        price_targets = entry.get("price_targets") or {}
        diff[ticker] = {
            "reference_price": price_targets.get("reference_price"),
            "downside": price_targets.get("downside"),
            "upside_base": price_targets.get("upside_base"),
            "upside_best": price_targets.get("upside_best"),
        }
    print(json.dumps(diff, indent=2, ensure_ascii=False))
    if not args.dry_run:
        path = write_overlay(overlay)
        print(f"\nwrote overlay -> {path}")


if __name__ == "__main__":
    main()
