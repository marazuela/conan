"""
SEDAR+ (Canada) scanner — Phase 4.

Endpoint strategy:
  SEDAR+ direct (sedarplus.ca) is blocked by a PerfDrive JavaScript challenge
  on raw HTTP requests. We cannot hit it from the pipeline without a headless
  browser. Instead we use the same universe-enumeration pattern we proved on
  ASX in Phase 3:

    1. Load tools/ca_universe.py (TSX + TSXV, market cap >= $300M USD).
    2. For each ticker, call yfinance.Ticker('<SYM>.TO' | '<SYM>.V').news
       to get the latest aggregator-syndicated headlines (StockStory, Zacks,
       Reuters, CP, Yahoo Finance).
    3. Classify each headline via SEDAR_TITLE_RULES, emit signals in the
       common schema with rubric_scores attached.

  A separate once-daily companion tool
  (tools/sedar_chrome_supplement.py — Claude-in-Chrome driven) is planned to
  catch non-syndicated SEDAR+ filings. This scanner handles the per-scan path.

yfinance news item shape (observed 2026-04-14 on SHOP.TO):
  {'uuid': '...', 'title': '...', 'publisher': '...', 'link': '...',
   'providerPublishTime': 1744599121, 'type': 'STORY', 'relatedTickers': [...]}

Signal_id: sha1(ticker + mic + pubtime + uuid) truncated to 32.

Rate limits: yfinance self-throttles. We add a small sleep per ticker to be
safe (0.3s default, same budget as ASX).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
WORKING = ROOT / "working"
WORKING.mkdir(parents=True, exist_ok=True)

REQUEST_THROTTLE_SECONDS = 0.3

# Pattern → (signal_type, strength_estimate, thesis_direction)
SEDAR_TITLE_RULES: list[tuple[re.Pattern, str, int, str]] = [
    # M&A
    (re.compile(r"\btake(-?over)?\s+bid\b", re.I), "takeover_bid_circular", 5, "long"),
    (re.compile(r"\bplan of arrangement\b", re.I), "plan_of_arrangement", 5, "long"),
    (re.compile(r"\bproposal\b.*\bacqui(re|sition)\b", re.I), "acquisition_proposal", 4, "long"),
    (re.compile(r"\b(merger|merging|combining)\b.*\b(agreement|deal)\b", re.I),
     "merger_agreement", 4, "unknown"),
    (re.compile(r"\b(definitive|amalgamation)\s+agreement\b", re.I),
     "merger_agreement", 4, "unknown"),
    (re.compile(r"\bdirectors?\s+circular\b", re.I), "directors_circular", 4, "unknown"),

    # Material change
    (re.compile(r"\bmaterial\s+change\s+report\b", re.I), "material_change_report", 4, "unknown"),
    (re.compile(r"\bmaterial\s+change\b", re.I), "material_change_report", 3, "unknown"),

    # Guidance
    (re.compile(r"\b(profit|earnings|revenue)\s+(warning|shortfall)\b", re.I),
     "guidance_downgrade", 5, "short"),
    (re.compile(r"\bguidance\b.*\b(lowered|cut|reduced|withdrawn)\b", re.I),
     "guidance_downgrade", 4, "short"),
    (re.compile(r"\bguidance\b.*\b(raised|upgraded|increased)\b", re.I),
     "guidance_upgrade", 4, "long"),
    (re.compile(r"\b(?:materially\s+)?(?:below|lower\s+than)\s+(?:guidance|consensus|expectations)\b", re.I),
     "guidance_downgrade", 4, "short"),

    # Early-warning / ownership
    (re.compile(r"\bearly\s+warning\s+report\b", re.I), "early_warning_10pct", 3, "long"),
    (re.compile(r"\b(10|twenty)%?\s+(?:or\s+more\s+)?ownership\b", re.I),
     "early_warning_10pct", 3, "long"),

    # Technical reports
    (re.compile(r"\bNI\s*43-?101\b|\bmineral\s+resource\s+estimate\b", re.I),
     "ni43101_technical_report", 3, "long"),
    (re.compile(r"\b(maiden|increased?|updated)\s+mineral\s+resource\b", re.I),
     "ni43101_technical_report", 4, "long"),
    (re.compile(r"\bNI\s*51-?101\b|\breserves?\s+(report|evaluation|estimate)\b", re.I),
     "ni51101_reserves", 3, "unknown"),

    # Cease trade / MCTO
    (re.compile(r"\bmanagement\s+cease\s+trade\s+order\b|\bMCTO\b", re.I),
     "mcto_management_cease_trade", 5, "short"),
    (re.compile(r"\bcease\s+trade\s+order\b", re.I), "cease_trade_order", 5, "short"),

    # Impairment / restatement
    (re.compile(r"\b(impair(ment)?|write-?down|write-?off)\b.*\b(charge|loss|asset|goodwill)\b", re.I),
     "impairment_loss", 4, "short"),
    (re.compile(r"\brestat(ement|ed)\b.*\b(accounts|results|financial)\b", re.I),
     "financial_restatement", 5, "short"),

    # Capital / financing
    (re.compile(r"\bbought\s+deal\s+(financing|offering)\b", re.I),
     "bought_deal", 3, "short"),
    (re.compile(r"\bprivate\s+placement\b", re.I), "private_placement", 3, "short"),
    (re.compile(r"\bequity\s+(financing|offering)\b", re.I), "equity_financing", 3, "short"),
    (re.compile(r"\b(normal\s+course|substantial\s+issuer|share)\s+(issuer\s+)?bid\b", re.I),
     "share_buyback", 3, "long"),
    (re.compile(r"\bshare\s+buy-?back\b", re.I), "share_buyback", 3, "long"),

    # Dividend
    (re.compile(r"\bspecial\s+dividend\b", re.I), "special_dividend", 3, "long"),
    (re.compile(r"\bdividend.*\b(suspended|cancell?ed|deferred|cut)\b", re.I),
     "dividend_cut", 4, "short"),

    # Going-concern / distress
    (re.compile(r"\bgoing\s+concern\b", re.I), "going_concern_warning", 5, "short"),
    (re.compile(r"\b(debt|covenant)\s+(breach|default)\b", re.I), "covenant_breach", 5, "short"),
    (re.compile(r"\bCCAA\b|\bcompanies'\s+creditors\s+arrangement\s+act\b", re.I),
     "ccaa_filing", 5, "short"),
    (re.compile(r"\b(receiver|receivership|bankruptc(y|ies))\b", re.I),
     "administration_or_receivership", 5, "short"),

    # Reporting cadence (lower-value but flagged)
    (re.compile(r"\bmanagement('s)?\s+discussion\s+and\s+analysis\b|\bMD&A\b", re.I),
     "interim_mda", 2, "unknown"),
]


def _classify(headline: str) -> Optional[tuple[str, int, str, str]]:
    for pat, stype, strength, direction in SEDAR_TITLE_RULES:
        if pat.search(headline):
            return stype, strength, direction, pat.pattern
    return None


def _fetch_news(yf, ticker: str, suffix: str) -> list[dict]:
    sym = f"{ticker}{suffix}"
    try:
        t = yf.Ticker(sym)
        news = t.news or []
    except Exception as e:
        log.debug("sedar_scanner: news fetch %s failed: %s", sym, e)
        return []
    return news


def _extract_fields(item: dict) -> tuple[str, str, datetime, str, str]:
    """Return (title, uuid, dt_utc, publisher, url) — normalizes yfinance news item shape."""
    title = (item.get("title") or "").strip()
    uuid = item.get("uuid") or item.get("id") or ""
    ts = item.get("providerPublishTime") or item.get("pubDate") or 0
    try:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            # ISO string
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        dt = datetime.now(timezone.utc)
    publisher = (item.get("publisher") or item.get("provider") or "").strip()
    url = (item.get("link") or item.get("url") or "").strip()
    return title, uuid, dt, publisher, url


def _make_signal_id(ticker: str, mic: str, dt: datetime, uuid: str) -> str:
    h = hashlib.sha1(f"{ticker}|{mic}|{dt.strftime('%Y-%m-%dT%H:%M:%SZ')}|{uuid}".encode("utf-8"))
    return h.hexdigest()[:32]


def _make_content_hash(ticker: str, uuid: str, title: str) -> str:
    return hashlib.sha1(f"sedar:{ticker}:{uuid}:{title}".encode()).hexdigest()


def _load_universe() -> list[dict]:
    try:
        from tools.ca_universe import load_universe
    except ImportError:
        import sys
        sys.path.insert(0, str(ROOT))
        from tools.ca_universe import load_universe  # type: ignore
    u = load_universe(auto_refresh=False)
    return u.get("tickers", [])


def _category_for(signal_type: str) -> str:
    if signal_type in ("takeover_bid_circular", "plan_of_arrangement",
                       "acquisition_proposal", "merger_agreement", "directors_circular"):
        return "takeover"
    if signal_type in ("guidance_upgrade", "guidance_downgrade", "guidance_revision",
                       "material_change_report", "impairment_loss", "financial_restatement"):
        return "results"
    if signal_type in ("equity_financing", "bought_deal", "private_placement",
                       "share_buyback"):
        return "capital_structure"
    if signal_type in ("early_warning_10pct",):
        return "ownership"
    if signal_type in ("ni43101_technical_report", "ni51101_reserves"):
        return "resources"
    if signal_type in ("cease_trade_order", "mcto_management_cease_trade"):
        return "trading_status"
    if signal_type in ("special_dividend", "dividend_cut"):
        return "capital_return"
    if signal_type in ("going_concern_warning", "covenant_breach",
                       "administration_or_receivership", "ccaa_filing"):
        return "distress"
    if signal_type in ("interim_mda", "annual_mda", "proxy_circular"):
        return "reporting"
    return "other"


def fetch_raw_signals(window_days: int = 7,
                      max_tickers: Optional[int] = None,
                      throttle_seconds: float = REQUEST_THROTTLE_SECONDS) -> list[dict]:
    """Public scanner API — called by pipeline_runner."""
    universe = _load_universe()
    if not universe:
        log.error("sedar_scanner: no Canada universe available — run tools/ca_universe.py first")
        return []

    if max_tickers:
        universe = universe[:max_tickers]

    import yfinance as yf
    import warnings
    warnings.filterwarnings("ignore")

    try:
        from tools.sedar_rubric import rubric_scores_sedar
    except ImportError:
        import sys
        sys.path.insert(0, str(ROOT))
        from tools.sedar_rubric import rubric_scores_sedar  # type: ignore

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    signals: list[dict] = []
    fetched = 0
    classified = 0
    t0 = time.time()

    for entry in universe:
        ticker = entry["ticker"]
        suffix = entry.get("suffix", ".TO")
        board = entry.get("board", "tsx")
        mic = entry.get("mic", "XTSE")

        news = _fetch_news(yf, ticker, suffix)
        fetched += 1
        if throttle_seconds:
            time.sleep(throttle_seconds)

        for item in news:
            title, uuid, dt, publisher, url = _extract_fields(item)
            if not title or dt < cutoff:
                continue
            cls = _classify(title)
            if not cls:
                continue
            signal_type, strength, direction, matched_pat = cls

            sig_id = _make_signal_id(ticker, mic, dt, uuid)
            content_hash = _make_content_hash(ticker, uuid, title)

            rubric = rubric_scores_sedar(
                strength=strength,
                signal_type=signal_type,
                market_cap_usd_mm=entry.get("market_cap_usd_mm"),
                board=board,
                filing_language="en",  # yfinance feed is English
                translation_confidence=None,
            )

            signals.append({
                "signal_id": sig_id,
                "source_content_hash": content_hash,
                "ticker_local": ticker,
                "mic": mic,
                "ticker_plus_mic": f"{ticker}.{mic}",
                "company_name_local": entry.get("name"),
                "company_name_en": entry.get("name"),
                "country": "CA",
                "exchange": "TSX" if board == "tsx" else "TSXV",
                "scanner": "sedar",
                "source_date": dt.strftime("%Y-%m-%d"),
                "source_timestamp": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source_url": url or None,
                "signal_type": signal_type,
                "signal_category": _category_for(signal_type),
                "strength_estimate": strength,
                "thesis_direction": direction,
                "translation_confidence": "n/a",
                "market_cap_usd_mm": entry.get("market_cap_usd_mm"),
                "gics_industry_group": None,
                "rubric_scores": rubric,
                "raw_data": {
                    "headline": title,
                    "publisher": publisher,
                    "uuid": uuid,
                    "board": board,
                    "suffix": suffix,
                    "matched_pattern": matched_pat,
                    "source_type": "yfinance_news_aggregator",
                },
                "scan_date": now_iso,
            })
            classified += 1

    elapsed = time.time() - t0
    log.info("sedar_scanner: fetched=%d classified=%d elapsed=%.1fs",
             fetched, classified, elapsed)
    return signals


import os


NAME = "sedar_plus_scanner"
OUT_FILE = Path(__file__).parent.parent / "signals" / f"{NAME}_output.json"
OUT_FILE.parent.mkdir(parents=True, exist_ok=True)


def _iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_signal_for_unified_envelope(s: dict) -> dict:
    """Promote a sedar_plus signal into the unified envelope shape.

    The scanner's internal signal dict uses the legacy Tool-2 schema
    (`ticker_local`, `scanner="sedar"`, no `scanner_source` /
    `upstream_scanner` / `scoring_profile` fields). The unified system
    expects `scanner_source`, `upstream_scanner`, `scoring_profile`, and
    `ticker` to be top-level. Mirror the keys so downstream ingestion
    works without having to touch the scanner's main loop.
    """
    sig = dict(s)
    sig.setdefault("scanner_source", NAME)
    sig.setdefault("upstream_scanner", NAME)
    sig_type = s.get("signal_type") or ""
    if not sig.get("scoring_profile"):
        if sig_type.startswith(("merger_", "tender_", "spinoff", "takeover", "going_private", "offer_")):
            sig["scoring_profile"] = "merger_arb"
        elif sig_type.startswith(("activist", "governance", "proxy", "board_", "auditor", "shareholder")):
            sig["scoring_profile"] = "activist_governance"
        elif sig_type.startswith(("lit", "lawsuit", "class_action", "securities_litigation")):
            sig["scoring_profile"] = "litigation"
        elif sig_type.startswith(("short_", "crowding", "sho_")):
            sig["scoring_profile"] = "short_positioning"
        elif sig_type.startswith(("fda_", "phase_", "pdufa", "clinical_", "approval", "trial_")):
            sig["scoring_profile"] = "binary_catalyst"
        else:
            sig["scoring_profile"] = "activist_governance"
    sig.setdefault("ticker", s.get("ticker_local"))
    return sig


def scan(window_days: int = 7,
         max_tickers: Optional[int] = None,
         throttle_seconds: float = 0.3) -> dict:
    """Wrapper around fetch_raw_signals that returns the unified envelope."""
    try:
        raw = fetch_raw_signals(
            window_days=window_days,
            max_tickers=max_tickers,
            throttle_seconds=throttle_seconds,
        )
    except Exception as e:
        return {
            "scanner": NAME,
            "ran_at_utc": _iso_utc(),
            "status": "error",
            "signals": [],
            "error": f"{type(e).__name__}: {e}",
        }
    normalized = [_normalize_signal_for_unified_envelope(s) for s in raw]
    return {
        "scanner": NAME,
        "ran_at_utc": _iso_utc(),
        "status": "ok",
        "signals": normalized,
        "fetched_records": len(raw),
        "in_window_records": len(raw),
        "unique_signals": len(normalized),
        "window_days": window_days,
    }


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, default=7)
    parser.add_argument("--max", type=int, default=None)
    parser.add_argument("--throttle", type=float, default=REQUEST_THROTTLE_SECONDS)
    args = parser.parse_args()
    result = scan(window_days=args.window, max_tickers=args.max,
                  throttle_seconds=args.throttle)
    tmp = OUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    os.replace(tmp, OUT_FILE)
    print(json.dumps({
        "signals": len(result["signals"]),
        "scanner": NAME,
        "status": result["status"],
        "fetched": result.get("fetched_records", 0),
        "in_window": result.get("in_window_records", 0),
    }, ensure_ascii=False))

# --- END OF FILE ---
