"""
ASX (Australia) announcement scanner — Phase 3 (concurrent + checkpointed).

Source:
  https://asx.api.markitdigital.com/asx-research/1.0/companies/{TICKER}/announcements

2026-04-20 rewrite (Q-036 fix):
  - Use ThreadPoolExecutor with MAX_CONCURRENT_REQUESTS=10 (was serial).
  - Checkpoint last-scanned index to signals/asx_rotation_state.json so
    each run resumes where the last left off (if budget exhausted).
  - Wall-clock budget 95s (hard kill is 120s per scanner_registry).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
WORKING = ROOT / "working"
WORKING.mkdir(parents=True, exist_ok=True)

API_TEMPLATE = "https://asx.api.markitdigital.com/asx-research/1.0/companies/{ticker}/announcements"

REQUEST_THROTTLE_SECONDS = 0.3
REQUEST_TIMEOUT_SECONDS = 10
MAX_RETRIES = 2
MAX_CONCURRENT_REQUESTS = 10
WALL_CLOCK_BUDGET_S = 95
ROTATION_FILE = ROOT / "signals" / "asx_rotation_state.json"


ASX_TITLE_RULES: list[tuple[re.Pattern, str, int, str]] = [
    (re.compile(r"\btakeover\b.*\b(offer|bid)\b", re.I), "takeover_bid", 5, "long"),
    (re.compile(r"\bscheme of arrangement\b", re.I), "scheme_of_arrangement", 5, "long"),
    (re.compile(r"\bproposal\b.*\bacqui(re|sition)\b", re.I), "acquisition_proposal", 4, "long"),
    (re.compile(r"\b(merger|merging)\b", re.I), "merger_agreement", 4, "unknown"),
    (re.compile(r"\b(profit|earnings)\s+(upgrade|guidance).*\b(above|beat|exceed|strong|materially higher)\b", re.I),
     "guidance_upgrade", 4, "long"),
    (re.compile(r"\b(profit|earnings)\s+(downgrade|warning)\b", re.I),
     "guidance_downgrade", 4, "short"),
    (re.compile(r"\b(?:materially\s+)?(?:below|lower\s+than)\s+(?:guidance|consensus|expectations)", re.I),
     "guidance_downgrade", 4, "short"),
    (re.compile(r"\brevised\s+(?:guidance|outlook)\b", re.I), "guidance_revision", 3, "unknown"),
    (re.compile(r"\bitems impacting\b", re.I), "results_items_impacting", 4, "short"),
    (re.compile(r"\bimpairment\b.*\b(charge|loss|write-?down)\b", re.I),
     "impairment_loss", 4, "short"),
    (re.compile(r"\b(goodwill|asset)\s+(impairment|write-?down)\b", re.I),
     "impairment_loss", 4, "short"),
    (re.compile(r"\brestat(ement|ed)\b.*\b(accounts|results|financial)\b", re.I),
     "financial_restatement", 5, "short"),
    (re.compile(r"\b(preliminary final report|appendix\s*4e)\b", re.I),
     "preliminary_final_report", 3, "unknown"),
    (re.compile(r"\b(half year|half-year|appendix\s*4d)\b.*\bresults?\b", re.I),
     "half_year_report", 3, "unknown"),
    (re.compile(r"\b(placement|institutional placement)\b", re.I),
     "equity_placement", 3, "short"),
    (re.compile(r"\bentitlement offer\b|\brights issue\b", re.I),
     "rights_issue", 3, "short"),
    (re.compile(r"\bshare purchase plan\b|\bspp\b", re.I),
     "share_purchase_plan", 2, "unknown"),
    (re.compile(r"\bcapital raising\b", re.I), "capital_raising", 3, "short"),
    (re.compile(r"\bon-?market\s+buy-?back\b|\bshare\s+buy-?back\b", re.I),
     "share_buyback", 3, "long"),
    (re.compile(r"\bbecoming\s+a\s+substantial\s+holder\b|\bform\s*603\b", re.I),
     "substantial_holder_initial", 3, "long"),
    (re.compile(r"\bceasing\s+to\s+be\s+a\s+substantial\s+holder\b|\bform\s*605\b", re.I),
     "substantial_holder_ceasing", 3, "short"),
    (re.compile(r"\bchange\s+(?:in|of)\s+substantial\s+holder\b|\bform\s*604\b", re.I),
     "substantial_holder_change", 2, "unknown"),
    (re.compile(r"\btrading\s+halt\b", re.I), "trading_halt", 3, "unknown"),
    (re.compile(r"\btrading\s+suspension\b|\bsuspended\s+from\s+quotation\b", re.I),
     "trading_suspension", 4, "short"),
    (re.compile(r"\b(jorc|drill(?:ing)?\s+results)\b", re.I),
     "jorc_drilling_results", 2, "unknown"),
    (re.compile(r"\bresource\s+(?:upgrade|update|estimate)\b", re.I),
     "jorc_resource_update", 3, "long"),
    (re.compile(r"\bappendix\s*4c\b", re.I), "appendix_4c_cashflow", 2, "unknown"),
    (re.compile(r"\bspecial\s+dividend\b", re.I), "special_dividend", 3, "long"),
    (re.compile(r"\bdividend\s+(cut|reduction|suspension)\b", re.I),
     "dividend_cut", 4, "short"),
    (re.compile(r"\bgoing concern\b", re.I), "going_concern_warning", 5, "short"),
    (re.compile(r"\bcovenant\s+(breach|waiver)\b", re.I), "covenant_breach", 5, "short"),
    (re.compile(r"\b(administration|receivership|voluntary administrator)\b", re.I),
     "administration_or_receivership", 5, "short"),
]


def _classify(headline: str, announcement_type: str, is_price_sensitive: bool
              ) -> Optional[tuple[str, int, str, str]]:
    if not headline:
        return None
    for pat, stype, strength, direction in ASX_TITLE_RULES:
        if pat.search(headline):
            if is_price_sensitive and strength < 5:
                strength += 1
            return stype, strength, direction, pat.pattern
    return None


def _fetch_announcements(ticker: str) -> Optional[dict]:
    import urllib.request
    import urllib.error
    url = API_TEMPLATE.format(ticker=ticker)
    for attempt in range(MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                data = resp.read()
            return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            return None
        except Exception:
            if attempt < MAX_RETRIES:
                time.sleep(0.5)
                continue
            return None
    return None


def _parse_date(iso: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None


def _make_signal_id(ticker: str, doc_key: str, date_str: str) -> str:
    return hashlib.sha1(f"{ticker}|XASX|{date_str}|{doc_key}".encode("utf-8")).hexdigest()[:32]


def _make_content_hash(ticker: str, doc_key: str) -> str:
    return hashlib.sha1(f"asx:{ticker}:{doc_key}".encode()).hexdigest()


def _load_universe() -> list[dict]:
    try:
        from tools.asx_universe import load_universe
    except ImportError:
        import sys
        sys.path.insert(0, str(ROOT))
        from tools.asx_universe import load_universe  # type: ignore
    u = load_universe(auto_refresh=False)
    return u.get("tickers", [])


def _load_rotation_state() -> dict:
    if not ROTATION_FILE.exists():
        return {"last_index": 0, "universe_size": 0}
    try:
        return json.loads(ROTATION_FILE.read_text())
    except Exception:
        return {"last_index": 0, "universe_size": 0}


def _save_rotation_state(last_index: int, universe_size: int) -> None:
    ROTATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ROTATION_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "last_index": last_index,
        "universe_size": universe_size,
        "updated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }, indent=2))
    os.replace(tmp, ROTATION_FILE)


def fetch_raw_signals(window_days: int = 7,
                      max_tickers: Optional[int] = None,
                      throttle_seconds: float = REQUEST_THROTTLE_SECONDS,
                      use_concurrency: bool = True,
                      wall_clock_budget_s: float = WALL_CLOCK_BUDGET_S) -> list[dict]:
    universe = _load_universe()
    if not universe:
        log.error("asx_scanner: no universe available — run tools/asx_universe.py first")
        return []

    if max_tickers:
        universe = universe[:max_tickers]

    rotation = _load_rotation_state()
    start_idx = 0
    if rotation.get("universe_size") == len(universe) and isinstance(rotation.get("last_index"), int):
        start_idx = rotation["last_index"] % len(universe)
    rotated = universe[start_idx:] + universe[:start_idx]

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    signals: list[dict] = []
    fetched = 0
    classified = 0
    t0 = time.time()

    results: list[tuple] = []

    if use_concurrency:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        last_completed_idx = 0
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS) as ex:
            future_to_idx = {}
            for idx, entry in enumerate(rotated):
                if time.time() - t0 > wall_clock_budget_s - 5:
                    break
                fut = ex.submit(_fetch_announcements, entry["ticker"])
                future_to_idx[fut] = (idx, entry)

            for fut in as_completed(future_to_idx):
                idx, entry = future_to_idx[fut]
                try:
                    doc = fut.result(timeout=REQUEST_TIMEOUT_SECONDS + 2)
                except Exception:
                    doc = None
                fetched += 1
                last_completed_idx = max(last_completed_idx, idx)
                if doc is not None:
                    results.append((idx, entry, doc))
                if time.time() - t0 > wall_clock_budget_s:
                    log.warning("asx_scanner: wall-clock budget hit at idx=%d", last_completed_idx)
                    break

        next_start = (start_idx + last_completed_idx + 1) % len(universe)
        _save_rotation_state(next_start, len(universe))
    else:
        for idx, entry in enumerate(rotated):
            if time.time() - t0 > wall_clock_budget_s:
                break
            ticker = entry["ticker"]
            doc = _fetch_announcements(ticker)
            fetched += 1
            if throttle_seconds:
                time.sleep(throttle_seconds)
            if doc is not None:
                results.append((idx, entry, doc))
        next_start = (start_idx + fetched) % len(universe)
        _save_rotation_state(next_start, len(universe))

    for _, entry, doc in results:
        ticker = entry["ticker"]
        items = (doc.get("data") or {}).get("items") or []
        display_name = (doc.get("data") or {}).get("displayName") or entry.get("name")

        for ann in items:
            date_str = ann.get("date", "")
            dt = _parse_date(date_str)
            if not dt or dt < cutoff:
                continue
            headline = ann.get("headline") or ""
            ann_type = ann.get("announcementType") or ""
            is_ps = bool(ann.get("isPriceSensitive"))

            cls = _classify(headline, ann_type, is_ps)
            if not cls:
                continue
            signal_type, strength, direction, matched_pat = cls

            doc_key = ann.get("documentKey") or ""
            sig_id = _make_signal_id(ticker, doc_key, date_str)
            content_hash = _make_content_hash(ticker, doc_key)

            try:
                from tools.asx_rubric import rubric_scores_asx
            except ImportError:
                import sys
                sys.path.insert(0, str(ROOT))
                from tools.asx_rubric import rubric_scores_asx  # type: ignore
            rubric = rubric_scores_asx(
                strength=strength,
                signal_type=signal_type,
                is_price_sensitive=is_ps,
                market_cap_usd_mm=entry.get("market_cap_usd_mm"),
            )

            signals.append({
                "signal_id": sig_id,
                "source_content_hash": content_hash,
                "ticker_local": ticker,
                "mic": "XASX",
                "ticker_plus_mic": f"{ticker}.XASX",
                "company_name_local": display_name,
                "company_name_en": display_name,
                "country": "AU",
                "exchange": "ASX",
                "scanner": "asx",
                "source_date": dt.strftime("%Y-%m-%d"),
                "source_timestamp": date_str,
                "source_url": None,
                "signal_type": signal_type,
                "signal_category": _category_for(signal_type),
                "strength_estimate": strength,
                "thesis_direction": direction,
                "translation_confidence": "n/a",
                "market_cap_usd_mm": entry.get("market_cap_usd_mm"),
                "gics_industry_group": entry.get("gics"),
                "rubric_scores": rubric,
                "raw_data": {
                    "headline": headline,
                    "announcement_type": ann_type,
                    "document_key": doc_key,
                    "is_price_sensitive": is_ps,
                    "file_size": ann.get("fileSize"),
                    "num_pages": ann.get("numPages"),
                    "matched_pattern": matched_pat,
                },
                "scan_date": now_iso,
            })
            classified += 1

    elapsed = time.time() - t0
    log.info("asx_scanner: fetched=%d classified=%d elapsed=%.1fs", fetched, classified, elapsed)
    return signals


def _category_for(signal_type: str) -> str:
    if signal_type in ("takeover_bid", "scheme_of_arrangement", "acquisition_proposal", "merger_agreement"):
        return "takeover"
    if signal_type in ("guidance_upgrade", "guidance_downgrade", "guidance_revision",
                       "results_items_impacting", "impairment_loss", "financial_restatement",
                       "preliminary_final_report", "half_year_report"):
        return "results"
    if signal_type in ("equity_placement", "rights_issue", "share_purchase_plan",
                       "capital_raising", "share_buyback"):
        return "capital_structure"
    if signal_type in ("substantial_holder_initial", "substantial_holder_ceasing",
                       "substantial_holder_change"):
        return "ownership"
    if signal_type in ("trading_halt", "trading_suspension"):
        return "trading_status"
    if signal_type in ("jorc_drilling_results", "jorc_resource_update"):
        return "resources"
    if signal_type in ("appendix_4c_cashflow",):
        return "cash_flow"
    if signal_type in ("special_dividend", "dividend_cut"):
        return "capital_return"
    if signal_type in ("going_concern_warning", "covenant_breach",
                       "administration_or_receivership"):
        return "distress"
    return "other"


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, default=7)
    parser.add_argument("--max", type=int, default=None)
    parser.add_argument("--throttle", type=float, default=REQUEST_THROTTLE_SECONDS)
    parser.add_argument("--no-concurrency", action="store_true")
    parser.add_argument("--budget", type=float, default=WALL_CLOCK_BUDGET_S)
    args = parser.parse_args()
    sigs = fetch_raw_signals(
        window_days=args.window,
        max_tickers=args.max,
        throttle_seconds=args.throttle,
        use_concurrency=not args.no_concurrency,
        wall_clock_budget_s=args.budget,
    )
    print(f"\nFetched {len(sigs)} classified ASX signals over {args.window}d window")
    for s in sigs[:20]:
        print(f"  {s['ticker_local']:6s} {s['signal_type']:30s} {s['thesis_direction']:8s}  "
              f"{s['raw_data']['headline'][:80]}")

# --- END OF FILE ---
