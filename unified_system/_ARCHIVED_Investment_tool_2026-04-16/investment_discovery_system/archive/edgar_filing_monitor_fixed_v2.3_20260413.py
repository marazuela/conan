"""
EDGAR Filing Monitor  (v2.3 — 2026-04-10)
==========================================
Scans SEC EDGAR full-text search (EFTS) for filings containing activist,
distress, M&A, and governance keywords. Produces standardized JSON signals
for the investment discovery pipeline.

Data Sources:
- EFTS: https://efts.sec.gov/LATEST/search-index (full-text search, free, no auth)
- Submissions: https://data.sec.gov/submissions/CIK{cik}.json (company metadata)
- Rate limit: 10 req/sec with User-Agent header (must include valid email)

Usage:
    python edgar_filing_monitor.py                    # Run full scan, output to signals/
    python edgar_filing_monitor.py --days 3            # Scan last 3 days
    python edgar_filing_monitor.py --category distress # Scan single category
    python edgar_filing_monitor.py --dry-run           # Print signals, don't save

API Response Fields (verified 2026-04-09):
    _source.ciks[]           — CIK numbers (array, use [0])
    _source.adsh             — accession number (e.g., "0001234567-26-000001")
    _source.display_names[]  — "Company Name  (CIK 0001234567)"
    _source.form             — form type (e.g., "10-K", "SC 13D")
    _source.file_date        — filing date "YYYY-MM-DD"
    _source.file_description — filing description
    _source.sics[]           — SIC codes
    No 'tickers' field — must resolve via data.sec.gov or OpenFIGI.
"""

import json
import os
import re
import sys
import time
import logging
import hashlib
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

import requests

# Try to import from tools.mcap_cache; fall back to local import if running standalone
try:
    from tools.mcap_cache import get_market_cap_cached as _get_market_cap
except ImportError:
    from mcap_cache import get_market_cap_cached as _get_market_cap

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

USER_AGENT = "InvestmentResearch research@example.com"  # SEC requires valid email
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

# Rate limiting
SEC_RATE_LIMIT = 8  # requests per second (conservative, SEC allows 10)
REQUEST_TIMEOUT = 10  # per-request timeout (reduced from 15 to prevent pipeline hangs)

# Wall-clock budget: stop scanning new keywords after this many seconds.
# Ensures the scanner completes within the Cowork bash timeout (45s).
# Set to 0 to disable (useful for standalone/subprocess execution).
WALL_CLOCK_BUDGET_S = 35  # Must finish before 45s bash timeout to avoid sandbox lock

# Market cap triage threshold
MARKET_CAP_FLOOR_MM = 215  # €200M ≈ $215M — minimum for liquidity

# Signal dedup window
DEDUP_WINDOW_DAYS = 45  # Reduced from 90 — filings are time-sensitive

# Output — default paths based on script location. Overridden by CLI main().
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SCRIPT_DIR)
SIGNALS_DIR = os.path.join(_PROJECT_DIR, "signals")
DEDUP_FILE = os.path.join(_PROJECT_DIR, "signals", "edgar_dedup.json")
ROTATION_FILE = os.path.join(_PROJECT_DIR, "signals", "edgar_rotation_state.json")

# Rotation order (prioritized by signal value)
ROTATION_ORDER = ["activist", "mna", "distress", "governance"]

# Logging
logger = logging.getLogger("edgar_monitor")

# ---------------------------------------------------------------------------
# Keyword dictionaries
# ---------------------------------------------------------------------------

SIGNAL_KEYWORDS = {
    "activist": [
        "strategic alternatives", "board representation", "maximize shareholder value",
        "undervalued", "change in control", "special committee", "proxy contest",
        "consent solicitation",
    ],
    "distress": [
        "going concern", "covenant breach", "waiver", "forbearance agreement",
        "material weakness", "restatement", "liquidity shortfall",
        "substantial doubt", "debtor-in-possession",
    ],
    "mna": [
        "merger agreement", "tender offer", "fairness opinion",
        "change of control", "break-up fee", "definitive agreement",
        "received indication of interest",
    ],
    "governance": [
        "poison pill", "rights plan", "bylaw amendment", "declassify board",
        "auditor resignation", "whistleblower", "internal investigation",
    ],
}

# Filing types that are themselves signals
SIGNAL_FILING_TYPES = {
    "activist_ownership": ["SC 13D", "SC 13D/A"],
    "late_filings": ["NT 10-K", "NT 10-K/A", "NT 10-Q", "NT 10-Q/A"],
}

# Filing types to SKIP during keyword scanning — these contain boilerplate
# language that triggers false positive keyword matches (D-042: AMT ARS false positive)
KEYWORD_SKIP_FORMS = {
    "ARS",           # Annual Report to Shareholders — boilerplate governance language
    "DEF 14A",       # Definitive proxy (routine governance boilerplate)
    "DEFA14A",       # Additional definitive proxy soliciting material
    "DEFM14A",       # Definitive proxy for merger (handled by M&A filing types)
    "PRE 14A",       # Preliminary proxy
    "N-CSR",         # Certified shareholder report (fund, not corporate)
    "N-CSRS",        # Semi-annual fund shareholder report
    "497",           # Definitive materials (fund prospectus)
    "497K",          # Summary prospectus
    "NPORT-P",       # Monthly portfolio report
}

# Per-category form whitelists (Q-009, Q-010, D-030, D-031).
# If a category has an entry here, ONLY these form types are accepted.
# If a category is absent, all forms (minus KEYWORD_SKIP_FORMS) are accepted.
# This dramatically reduces SPAC/proxy-season noise.
CATEGORY_FORM_WHITELIST = {
    "distress": {"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A"},
    "activist": {"8-K", "8-K/A", "SC 13D", "SC 13D/A", "SC 14D9",
                 "PRER14A", "DFAN14A"},
}

# Additional form blacklist for SPAC/de-SPAC/IPO noise (Q-010).
# These forms are excluded from ALL keyword categories.
SPAC_IPO_FORM_BLACKLIST = {
    "S-1", "S-1/A", "S-4", "S-4/A", "F-1", "F-1/A", "F-4", "F-4/A",
    "DRS", "DRS/A", "SB-2", "SB-2/A",
    "425",           # Prospectus communications (deal/SPAC flow)
    "SC TO-C",       # Tender offer commentary
    "SC TO-C/A",
    "424B3", "424B4", "424B5",  # Prospectus supplements
}


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class _SECRateLimiter:
    """Ensures we don't exceed SEC's 10 req/sec limit."""
    def __init__(self, max_per_sec: int = SEC_RATE_LIMIT):
        self.max_per_sec = max_per_sec
        self._timestamps: List[float] = []

    def wait(self):
        now = time.time()
        self._timestamps = [t for t in self._timestamps if now - t < 1.0]
        if len(self._timestamps) >= self.max_per_sec:
            sleep_time = 1.0 - (now - self._timestamps[0]) + 0.05
            if sleep_time > 0:
                time.sleep(sleep_time)
        self._timestamps.append(time.time())


_rate_limiter = _SECRateLimiter()


# ---------------------------------------------------------------------------
# EFTS API functions
# ---------------------------------------------------------------------------

def _efts_search(query: str, date_from: str, date_to: str,
                 form_type: str = "", max_results: int = 50) -> List[dict]:
    """Query EDGAR EFTS and return raw hit sources.

    Returns list of dicts with keys: cik, adsh, company_raw, form, file_date,
    file_description, sics.
    """
    params = {
        "q": query,
        "dateRange": "custom",
        "startdt": date_from,
        "enddt": date_to,
    }
    if form_type:
        params["forms"] = form_type

    headers = {"User-Agent": USER_AGENT}

    _rate_limiter.wait()
    try:
        resp = requests.get(EFTS_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"EFTS query failed for '{query}': {e}")
        return []

    results = []
    for hit in data.get("hits", {}).get("hits", [])[:max_results]:
        src = hit.get("_source", {})
        cik = src.get("ciks", [""])[0] if src.get("ciks") else ""
        adsh = src.get("adsh", "")

        # Parse company name from display_names (format: "Company Name  (CIK 0001234567)")
        raw_name = src.get("display_names", [""])[0] if src.get("display_names") else ""
        company_name = re.sub(r"\s*\(CIK\s+\d+\)\s*$", "", raw_name).strip()

        # Build filing URL
        filing_url = ""
        if cik and adsh:
            cik_stripped = cik.lstrip("0") or "0"
            adsh_clean = adsh.replace("-", "")
            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{adsh_clean}"

        results.append({
            "cik": cik,
            "adsh": adsh,
            "company_name": company_name,
            "company_raw": raw_name,
            "form": src.get("form", ""),
            "file_date": src.get("file_date", ""),
            "file_description": src.get("file_description", ""),
            "filing_url": filing_url,
            "sics": src.get("sics", []),
        })

    return results


def _get_company_tickers(cik: str) -> Tuple[List[str], Optional[str]]:
    """Look up tickers and exchange for a CIK via data.sec.gov submissions API.

    Returns (tickers_list, exchange_name).
    """
    if not cik:
        return [], None

    cik_padded = cik.zfill(10)
    url = SUBMISSIONS_URL.format(cik=cik_padded)
    headers = {"User-Agent": USER_AGENT}

    _rate_limiter.wait()
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            tickers = data.get("tickers", [])
            exchanges = data.get("exchanges", [])
            exchange = exchanges[0] if exchanges else None
            return tickers, exchange
    except Exception as e:
        logger.debug(f"Failed to get tickers for CIK {cik}: {e}")

    return [], None


# ---------------------------------------------------------------------------
# Market cap triage
# ---------------------------------------------------------------------------

# DEPRECATED — use mcap_cache.get_market_cap_cached() instead (D-044)
# def _get_market_cap(ticker: str) -> Optional[float]:
#     """Get market cap in millions via yfinance. Returns None on failure."""
#     if not ticker:
#         return None
#     try:
#         import yfinance as yf
#         stock = yf.Ticker(ticker)
#         info = stock.info
#         mcap = info.get("marketCap")
#         if mcap:
#             return mcap / 1_000_000  # Convert to millions
#     except Exception as e:
#         logger.debug(f"yfinance market cap lookup failed for {ticker}: {e}")
#     return None


# ---------------------------------------------------------------------------
# Signal dedup
# ---------------------------------------------------------------------------

def _load_dedup_log(filepath: str) -> Dict[str, str]:
    """Load dedup log: maps signal_hash -> date_first_seen."""
    if filepath and os.path.exists(filepath):
        try:
            with open(filepath) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_dedup_log(filepath: str, log: dict):
    """Save dedup log."""
    if filepath:
        try:
            with open(filepath, "w") as f:
                json.dump(log, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save dedup log: {e}")


def _signal_hash(cik: str, keyword: str, category: str) -> str:
    """Deterministic hash for dedup: same company + same keyword + same category."""
    raw = f"{cik}|{keyword}|{category}"
    return hashlib.md5(raw.encode()).hexdigest()


def _is_novel(cik: str, keyword: str, category: str,
              dedup_log: dict, window_days: int = DEDUP_WINDOW_DAYS) -> bool:
    """Check if this signal is novel (not seen in the dedup window)."""
    h = _signal_hash(cik, keyword, category)
    if h in dedup_log:
        first_seen = dedup_log[h]
        try:
            first_date = datetime.strptime(first_seen, "%Y-%m-%d")
            if (datetime.now() - first_date).days < window_days:
                return False  # Not novel — seen recently
        except ValueError:
            pass
    return True


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------

def _build_signal(hit: dict, keyword: str, category: str,
                  ticker: str = "", market_cap_mm: float = 0) -> dict:
    """Build a signal dict in the common pipeline format."""
    # Estimate strength based on category and form type
    strength = 2  # default baseline
    form = hit.get("form", "")

    if category == "activist" and "13D" in form:
        strength = 4  # SC 13D is a strong activist signal
    elif category == "distress" and keyword in ("going concern", "substantial doubt"):
        strength = 4
    elif category == "mna":
        if keyword in ("definitive agreement", "merger agreement"):
            # 8-K with merger agreement = new deal announcement (high strength)
            # SC TO-T, PREM14A, S-4, SC TO-C, SC 13E3 = ongoing deal paperwork (lower)
            ongoing_forms = ("SC TO-T", "SC TO-C", "PREM14A", "DEFM14A",
                             "S-4", "SC 13E3", "SC TO-I", "SC TO-T/A",
                             "SC TO-C/A", "S-4/A", "SC 13E3/A", "SC TO-I/A",
                             "DFAN14A", "DEFA14A")
            if any(form.upper().startswith(f) for f in ongoing_forms):
                strength = 2  # ongoing deal filing — info already public
            else:
                strength = 5  # likely new announcement (8-K, etc.)
        elif keyword in ("tender offer",):
            # SC TO-T = initiating tender, SC TO-C = commenting on tender
            if "SC TO-T" in form.upper() and "/A" not in form.upper():
                strength = 4  # new tender offer filing
            else:
                strength = 2  # amendment or commentary — ongoing
        else:
            strength = 3  # other MNA keywords
    elif category == "governance" and keyword in ("poison pill", "rights plan"):
        strength = 3

    return {
        "ticker": ticker,
        "isin": None,
        "company_name": hit.get("company_name", ""),
        "market_cap_mm": round(market_cap_mm, 1) if market_cap_mm else None,
        "signal_type": f"{category}_keyword",
        "signal_category": "edgar",
        "strength_estimate": strength,
        "source_url": hit.get("filing_url", ""),
        "source_date": hit.get("file_date", ""),
        "scan_date": datetime.now().strftime("%Y-%m-%d"),
        "raw_data": {
            "keyword": keyword,
            "filing_type": form,
            "cik": hit.get("cik", ""),
            "adsh": hit.get("adsh", ""),
            "file_description": hit.get("file_description", ""),
            "company_raw": hit.get("company_raw", ""),
        },
    }


# ---------------------------------------------------------------------------
# Main scan functions
# ---------------------------------------------------------------------------

def _load_rotation_state() -> dict:
    """Load category rotation state from persistent file."""
    if os.path.exists(ROTATION_FILE):
        try:
            with open(ROTATION_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"rotation_index": -1, "scan_history": {}}


def _save_rotation_state(state: dict) -> None:
    """Save category rotation state to persistent file."""
    try:
        os.makedirs(os.path.dirname(ROTATION_FILE), exist_ok=True)
        with open(ROTATION_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save rotation state: {e}")


def get_next_rotation_category() -> str:
    """Get the next keyword category to scan based on rotation state.

    Returns the next category in ROTATION_ORDER, advancing the index.
    If state is corrupted or missing, starts from "activist" (index 0).
    """
    state = _load_rotation_state()
    current_idx = state.get("rotation_index", -1)
    next_idx = (current_idx + 1) % len(ROTATION_ORDER)
    category = ROTATION_ORDER[next_idx]

    # Update state
    state["rotation_index"] = next_idx
    state["last_category"] = category
    state["last_scan_ts"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    if "scan_history" not in state:
        state["scan_history"] = {}
    state["scan_history"][category] = state["last_scan_ts"]
    _save_rotation_state(state)

    logger.info(f"Rotation: scanning category '{category}' (index {next_idx}/{len(ROTATION_ORDER)})")
    return category


def scan_keywords(categories: Optional[List[str]] = None,
                  days_back: int = 2,
                  market_cap_filter: bool = True) -> List[dict]:
    """Run keyword scan across specified categories.

    Args:
        categories: List of keyword categories to scan (default: all)
        days_back: How many days back to search
        market_cap_filter: Whether to apply $300M market cap floor

    Returns:
        List of signal dicts in common format
    """
    if categories is None:
        categories = list(SIGNAL_KEYWORDS.keys())

    date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = datetime.now().strftime("%Y-%m-%d")

    # Load dedup log
    dedup_log = _load_dedup_log(DEDUP_FILE)

    all_signals = []
    seen_adsh = set()  # Dedup within this scan
    scan_start = time.time()
    budget_exceeded = False

    for category in categories:
        if budget_exceeded:
            break
        keywords = SIGNAL_KEYWORDS.get(category, [])
        logger.info(f"Scanning category '{category}' ({len(keywords)} keywords)")

        for keyword in keywords:
            # Check wall-clock budget before each keyword query
            if WALL_CLOCK_BUDGET_S > 0 and (time.time() - scan_start) > WALL_CLOCK_BUDGET_S:
                logger.warning(f"Wall-clock budget ({WALL_CLOCK_BUDGET_S}s) exceeded — "
                               f"stopping keyword scan early. Processed {len(seen_adsh)} hits so far.")
                budget_exceeded = True
                break

            hits = _efts_search(
                query=f'"{keyword}"',
                date_from=date_from,
                date_to=date_to,
                max_results=30,
            )

            for hit in hits:
                adsh = hit.get("adsh", "")
                cik = hit.get("cik", "")

                # Skip duplicates within this scan
                dedup_key = f"{adsh}|{keyword}"
                if dedup_key in seen_adsh:
                    continue
                seen_adsh.add(dedup_key)

                # Skip boilerplate filing types (D-042: ARS, proxies, fund reports)
                form_type = hit.get("form", "").strip()
                if form_type in KEYWORD_SKIP_FORMS:
                    logger.debug(f"Skipping boilerplate form '{form_type}': "
                                 f"{hit.get('company_name')} / {keyword}")
                    continue

                # Skip SPAC/IPO forms (Q-010: de-SPAC noise filter)
                if any(form_type.upper().startswith(bl) for bl in SPAC_IPO_FORM_BLACKLIST):
                    logger.debug(f"Skipping SPAC/IPO form '{form_type}': "
                                 f"{hit.get('company_name')} / {keyword}")
                    continue

                # Per-category form whitelist (Q-009, D-030, D-031)
                if category in CATEGORY_FORM_WHITELIST:
                    whitelist = CATEGORY_FORM_WHITELIST[category]
                    if not any(form_type.upper().startswith(wl) for wl in whitelist):
                        logger.debug(f"Skipping non-whitelisted form '{form_type}' "
                                     f"for category '{category}': "
                                     f"{hit.get('company_name')} / {keyword}")
                        continue

                # Novelty check (90-day dedup)
                if not _is_novel(cik, keyword, category, dedup_log):
                    logger.debug(f"Skipping non-novel signal: {hit.get('company_name')} / {keyword}")
                    continue

                # Resolve ticker via data.sec.gov
                tickers, exchange = _get_company_tickers(cik)
                ticker = tickers[0] if tickers else ""

                # Market cap triage
                market_cap_mm = 0
                if ticker and market_cap_filter:
                    market_cap_mm = _get_market_cap(ticker) or 0
                    if market_cap_mm < MARKET_CAP_FLOOR_MM and market_cap_mm > 0:
                        logger.debug(f"Below market cap floor: {ticker} ${market_cap_mm:.0f}M < ${MARKET_CAP_FLOOR_MM}M")
                        continue
                    elif market_cap_mm == 0:
                        # Could not determine market cap — keep but flag
                        logger.debug(f"Market cap unknown for {ticker or cik}, keeping signal")

                signal = _build_signal(hit, keyword, category, ticker, market_cap_mm)
                all_signals.append(signal)

                # Update dedup log
                h = _signal_hash(cik, keyword, category)
                if h not in dedup_log:
                    dedup_log[h] = date_to

    # Save updated dedup log
    _save_dedup_log(DEDUP_FILE, dedup_log)

    logger.info(f"Keyword scan complete: {len(all_signals)} signals from {len(seen_adsh)} unique hits")
    return all_signals


def scan_filing_types(days_back: int = 2,
                      market_cap_filter: bool = True) -> List[dict]:
    """Scan for specific filing types that are themselves signals (SC 13D, NT 10-K).

    Returns list of signal dicts in common format.
    """
    date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = datetime.now().strftime("%Y-%m-%d")

    all_signals = []
    seen_adsh = set()

    for signal_type, form_types in SIGNAL_FILING_TYPES.items():
        for form_type in form_types:
            logger.info(f"Scanning filing type: {form_type}")
            hits = _efts_search(
                query="*",
                date_from=date_from,
                date_to=date_to,
                form_type=form_type,
                max_results=50,
            )

            for hit in hits:
                adsh = hit.get("adsh", "")
                if adsh in seen_adsh:
                    continue
                seen_adsh.add(adsh)

                cik = hit.get("cik", "")
                tickers, exchange = _get_company_tickers(cik)
                ticker = tickers[0] if tickers else ""

                market_cap_mm = 0
                if ticker and market_cap_filter:
                    market_cap_mm = _get_market_cap(ticker) or 0
                    if 0 < market_cap_mm < MARKET_CAP_FLOOR_MM:
                        continue

                signal = _build_signal(
                    hit, keyword=form_type, category=signal_type,
                    ticker=ticker, market_cap_mm=market_cap_mm,
                )
                # Boost strength for 13D filings
                if "13D" in form_type:
                    signal["strength_estimate"] = max(signal["strength_estimate"], 4)
                elif "NT 10" in form_type:
                    signal["strength_estimate"] = max(signal["strength_estimate"], 3)

                all_signals.append(signal)

    logger.info(f"Filing type scan complete: {len(all_signals)} signals")
    return all_signals


def run_full_scan(days_back: int = 2,
                  market_cap_filter: bool = True,
                  save_signals: bool = True) -> List[dict]:
    """Run complete EDGAR scan: keywords + filing types.

    Args:
        days_back: How many days back to search
        market_cap_filter: Apply $300M market cap floor
        save_signals: Whether to write signals to signals/ directory

    Returns:
        List of all signals found
    """
    all_signals = []

    # Keyword scan
    keyword_signals = scan_keywords(days_back=days_back, market_cap_filter=market_cap_filter)
    all_signals.extend(keyword_signals)

    # Filing type scan
    type_signals = scan_filing_types(days_back=days_back, market_cap_filter=market_cap_filter)
    all_signals.extend(type_signals)

    # Save signals to JSON
    if save_signals and SIGNALS_DIR and all_signals:
        os.makedirs(SIGNALS_DIR, exist_ok=True)
        output_file = os.path.join(
            SIGNALS_DIR,
            f"edgar_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        with open(output_file, "w") as f:
            json.dump(all_signals, f, indent=2)
        logger.info(f"Saved {len(all_signals)} signals to {output_file}")

    return all_signals


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="EDGAR Filing Monitor")
    parser.add_argument("--days", type=int, default=2, help="Days to look back (default: 2)")
    parser.add_argument("--category", type=str, default=None,
                        help="Single category to scan (activist, distress, mna, governance)")
    parser.add_argument("--no-market-cap", action="store_true",
    parser.add_argument("--dry-run", action="store_true",
                        help="Print signals without saving")
    parser.add_argument("--rotate", action="store_true",
                        help="Use category rotation — scans next category in rotation order")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Set up paths relative to script location
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    signals_dir = os.path.join(project_dir, "signals")

    # Determine category
    if args.rotate:
        categories = list(SIGNAL_KEYWORDS.keys())
        rotation_file = os.path.join(signals_dir, "edgar_rotation_state.json")
        try:
            with open(rotation_file) as f:
                state = json.load(f)
            idx = (state.get("rotation_index", 0) + 1) % len(categories)
        except (FileNotFoundError, json.JSONDecodeError):
            idx = 0
        category = categories[idx]
        state = {
            "rotation_index": idx,
            "scan_history": {},
            "last_category": category,
            "last_scan_ts": datetime.utcnow().isoformat() + "Z",
        }
        try:
            with open(rotation_file) as f:
                old_state = json.load(f)
            state["scan_history"] = old_state.get("scan_history", {})
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        state["scan_history"][category] = state["last_scan_ts"]
        with open(rotation_file, "w") as f:
            json.dump(state, f, indent=2)
        logger.info(f"Rotation: scanning category '{category}' (index {idx}/{len(categories)})")
        categories_to_scan = [category]
    elif args.category:
        categories_to_scan = [args.category]
    else:
        categories_to_scan = None

    signals = scan_keywords(
        categories=categories_to_scan,
        days_back=args.days,
        market_cap_filter=not args.no_market_cap,
        signals_dir=signals_dir,
    )

    if not args.dry_run and signals:
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        out_file = os.path.join(signals_dir, f"edgar_{ts}.json")
        with open(out_file, "w") as f:
            json.dump(signals, f, indent=2)
        logger.info(f"Saved {len(signals)} signals to {out_file}")
    elif args.dry_run and signals:
        for s in signals:
            print(json.dumps(s, indent=2))
    else:
        logger.info(f"Keyword scan complete: 0 signals")


if __name__ == "__main__":
    main()
