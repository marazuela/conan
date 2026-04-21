"""
SEDAR+ Chrome supplement — once-daily pass to catch non-syndicated filings.

Why this exists:
  The primary sedar_scanner uses yfinance Ticker.news, which is an aggregator
  feed. It picks up most large-cap TSX filings that get syndicated to
  Reuters/CP/StockStory, but will miss:
    - French-only material change reports from Québec-domiciled issuers
    - Mid/small-cap NI 43-101 technical reports that don't get picked up by
      English wire services
    - Early warning reports from lower-profile institutional filers

  This supplement is designed to run once per day (not per scan) via
  Claude-in-Chrome, where a real browser session defeats the SEDAR+
  PerfDrive challenge.

Planned flow (operator-driven, not auto-scheduled yet):
  1. Operator navigates Claude-in-Chrome to SEDAR+ "today's filings" view.
  2. Scrapes filing table into JSON rows:
       [{"company": "...", "filing_type": "...", "date": "...", "pdf_url": "..."}]
  3. This module ingests that JSON, filters to filing_types we care about,
     looks up market cap via yfinance on '<TICKER>.TO|.V',
     drops below-floor issuers, classifies headline against SEDAR_TITLE_RULES,
     emits signals in the common schema with
       raw_data.source_type = 'sedar_chrome_supplement'.

Status: placeholder. Interface is stable; the Chrome-side capture step is
manual until we build a Cowork shortcut for it.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
WORKING = ROOT / "working"
INBOX_PATH = WORKING / "sedar_chrome_inbox.json"  # operator drops scraped rows here


def ingest_chrome_inbox(inbox_path: Path = INBOX_PATH) -> list[dict]:
    """Read a JSON array of rows captured by a Claude-in-Chrome SEDAR+ session.

    Expected row shape (minimum fields):
      {
        "company": "Company Name",
        "ticker": "SHOP",
        "board": "tsx",            # or "tsxv"
        "filing_type": "Material Change Report",
        "headline": "...",         # optional; falls back to filing_type
        "date": "2026-04-14",
        "pdf_url": "https://...",
        "language": "en"           # or "fr"
      }
    """
    if not inbox_path.exists():
        log.info("sedar_chrome_supplement: no inbox at %s — nothing to ingest", inbox_path)
        return []
    try:
        rows = json.loads(inbox_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("sedar_chrome_supplement: bad inbox JSON: %s", e)
        return []
    if not isinstance(rows, list):
        log.error("sedar_chrome_supplement: inbox must be a JSON array")
        return []
    return rows


def rows_to_signals(rows: list[dict], window_days: int = 7) -> list[dict]:
    """Convert Chrome-scraped SEDAR+ rows into common-schema signals.

    Lazy-imports scanner helpers so tests don't need yfinance loaded.
    """
    try:
        from tools.sedar_scanner import (_classify, _category_for,
                                         _make_signal_id, _make_content_hash)
        from tools.sedar_rubric import rubric_scores_sedar
        from tools.ca_universe import load_universe
    except ImportError:
        import sys
        sys.path.insert(0, str(ROOT))
        from tools.sedar_scanner import (_classify, _category_for,
                                         _make_signal_id, _make_content_hash)  # type: ignore
        from tools.sedar_rubric import rubric_scores_sedar  # type: ignore
        from tools.ca_universe import load_universe  # type: ignore

    uni = load_universe(auto_refresh=False).get("tickers", [])
    mcap_by_ticker = {e["ticker"]: e for e in uni}

    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    signals: list[dict] = []
    for row in rows:
        ticker = (row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        board = (row.get("board") or "tsx").lower()
        mic = "XTSE" if board == "tsx" else "XTSX"

        uni_entry = mcap_by_ticker.get(ticker) or {}
        mcap = uni_entry.get("market_cap_usd_mm")
        # If not in universe cache, still emit — triage will drop if below floor
        date_str = row.get("date") or ""
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = datetime.now(timezone.utc)
        if dt < cutoff:
            continue

        headline = (row.get("headline") or row.get("filing_type") or "").strip()
        if not headline:
            continue
        cls = _classify(headline)
        if not cls:
            continue
        signal_type, strength, direction, matched_pat = cls

        lang = (row.get("language") or "en").lower()
        rubric = rubric_scores_sedar(
            strength=strength,
            signal_type=signal_type,
            market_cap_usd_mm=mcap,
            board=board,
            filing_language=lang,
            translation_confidence=None if lang == "en" else 0.85,
        )

        uuid_src = row.get("pdf_url") or f"{ticker}-{date_str}-{headline[:40]}"
        uuid_hash = hashlib.sha1(uuid_src.encode()).hexdigest()[:16]
        sig_id = _make_signal_id(ticker, mic, dt, uuid_hash)
        content_hash = _make_content_hash(ticker, uuid_hash, headline)

        signals.append({
            "signal_id": sig_id,
            "source_content_hash": content_hash,
            "ticker_local": ticker,
            "mic": mic,
            "ticker_plus_mic": f"{ticker}.{mic}",
            "company_name_local": row.get("company"),
            "company_name_en": row.get("company"),
            "country": "CA",
            "exchange": "TSX" if board == "tsx" else "TSXV",
            "scanner": "sedar_chrome",
            "source_date": dt.strftime("%Y-%m-%d"),
            "source_timestamp": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_url": row.get("pdf_url"),
            "signal_type": signal_type,
            "signal_category": _category_for(signal_type),
            "strength_estimate": strength,
            "thesis_direction": direction,
            "translation_confidence": "n/a" if lang == "en" else 0.85,
            "market_cap_usd_mm": mcap,
            "gics_industry_group": None,
            "rubric_scores": rubric,
            "raw_data": {
                "headline": headline,
                "filing_type": row.get("filing_type"),
                "pdf_url": row.get("pdf_url"),
                "language": lang,
                "board": board,
                "matched_pattern": matched_pat,
                "source_type": "sedar_chrome_supplement",
            },
            "scan_date": now_iso,
        })
    return signals


def fetch_raw_signals(window_days: int = 7, **kwargs) -> list[dict]:
    """Pipeline-compatible entrypoint — reads whatever is sitting in the inbox."""
    rows = ingest_chrome_inbox()
    return rows_to_signals(rows, window_days=window_days)


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, default=7)
    args = parser.parse_args()
    sigs = fetch_raw_signals(window_days=args.window)
    print(f"Chrome supplement produced {len(sigs)} signals")

# --- END OF FILE ---
