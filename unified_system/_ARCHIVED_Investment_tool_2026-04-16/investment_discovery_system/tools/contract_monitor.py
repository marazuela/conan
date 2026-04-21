"""
Government Contract Award Monitor  (v1.1 — 2026-04-09)
========================================================
Monitors USAspending.gov for large federal contract awards to publicly listed
companies. Produces standardized JSON signals for the investment discovery
pipeline.

Data Source: https://api.usaspending.gov/api/v2/search/spending_by_award/
- Free, no API key, no auth
- POST-based REST API with filter/sort/pagination
- Returns: recipient name, award amount, awarding agency, description, dates
- Note: queries can be slow (15-40s) — use generous timeouts

Signal Logic:
1. Large award: contract >$50M to a public company
2. Very large award: contract >$250M
3. Mega award: contract >$1B
4. Known defense/IT contractor match via mapping table

Usage:
    python contract_monitor.py                 # Scan last 48 hours
    python contract_monitor.py --days 7        # Scan last 7 days
    python contract_monitor.py --dry-run       # Print without saving
"""

import json
import os
import re
import time
import logging
import hashlib
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple

import requests

# Try to import from tools.mcap_cache; fall back to local import if running standalone
try:
    from tools.mcap_cache import get_market_cap_cached as _get_market_cap
except ImportError:
    from mcap_cache import get_market_cap_cached as _get_market_cap

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

USASPENDING_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
REQUEST_TIMEOUT = 45  # USAspending can be slow
DAYS_BACK = 7         # Default: look back 7 days (awards appear slowly)
AWARD_FLOOR = 25_000_000  # $25M minimum (lowered from $50M to capture more signals)
MAX_PAGES = 15        # Pages to scan (10 results each) — increased from 5

# Triage
MARKET_CAP_FLOOR_MM = 300

# Dedup
DEDUP_WINDOW_DAYS = 30

# Output — default paths based on script location. Overridden by CLI main().
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SCRIPT_DIR)
SIGNALS_DIR = os.path.join(_PROJECT_DIR, "signals")
DEDUP_FILE = os.path.join(_PROJECT_DIR, "signals", "contract_dedup.json")

logger = logging.getLogger("contract_monitor")

# ---------------------------------------------------------------------------
# Contractor-to-Ticker Mapping
# ---------------------------------------------------------------------------
# Maps normalized recipient name fragments → ticker
# Matching is case-insensitive substring match
# Order matters: more specific matches should come first

CONTRACTOR_TICKER_MAP: Dict[str, str] = {
    # Defense primes
    "lockheed martin": "LMT",
    "raytheon": "RTX",
    "rtx": "RTX",
    "general dynamics": "GD",
    "gulfstream aerospace": "GD",  # GD subsidiary — appears separately in USAspending
    "northrop grumman": "NOC",
    "boeing": "BA",
    "l3harris": "LHX",
    "huntington ingalls": "HII",
    "bae systems": "BAESY",
    "textron": "TXT",
    "elbit": "ESLT",             # Elbit Systems; USAspending shows "ELBITAMERICA, INC."

    # IT / Cybersecurity / Federal services
    "palantir": "PLTR",
    "leidos": "LDOS",
    "saic": "SAIC",
    "booz allen": "BAH",
    "caci": "CACI",
    "science applications": "SAIC",
    "peraton": "LDOS",           # Peraton was acquired by Leidos parent
    "mantech": "BAH",            # ManTech acquired by Carlyle, some units to BAH
    "accenture": "ACN",
    "cgi federal": "GIB",        # CGI Group — major federal IT contractor
    "deloitte": None,             # Private
    "ibm": "IBM",
    "microsoft": "MSFT",
    "amazon": "AMZN",
    "oracle": "ORCL",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "cisco": "CSCO",
    "dell federal": "DELL",      # Dell Technologies federal arm
    "dell technologies": "DELL",
    "maximus": "MMS",            # Maximus Inc — major gov services contractor
    "unisys": "UIS",

    # Defense / Aerospace mid-caps
    "kratos": "KTOS",
    "mercury systems": "MRCY",
    "aerojet": "AJRD",
    "curtiss-wright": "CW",
    "heico": "HEI",
    "ducommun": "DCO",
    "parsons": "PSN",
    "vectrus": "VVX",
    "intuitive machines": "LUNR",  # NASA contractor, publicly traded

    # Healthcare / Pharma
    "veeva": "VEEV",
    "unitedhealth": "UNH",
    "humana": "HUM",
    "centene": "CNC",
    "cigna": "CI",
    "anthem": "ELV",
    "elevance": "ELV",
    "vericel": "VCEL",           # Vericel Corporation — gov healthcare contracts
    "dlh solutions": "DLH",     # DLH Holdings — VA/HHS contractor
    "dlh holdings": "DLH",

    # Telecom
    "at&t": "T",
    "verizon": "VZ",
    "t-mobile": "TMUS",
    "lumen": "LUMN",

    # Infrastructure / Construction
    "fluor": "FLR",
    "jacobs": "J",
    "aecom": "ACM",
    "quanta services": "PWR",
    "bechtel": None,              # Private
    "kbr": "KBR",

    # Corrections / Detention (DHS/DOJ contracts)
    "geo group": "GEO",
    "corecivic": "CXW",

    # Fire / Specialty chemicals
    "perimeter solutions": "PRM",  # Wildfire retardant — recurring USDA contracts

    # Energy / Industrial
    "general electric": "GE",
    "honeywell": "HON",
    "3m": "MMM",
}


def _match_contractor(recipient_name: str) -> Tuple[Optional[str], str]:
    """Match recipient name to ticker.

    Returns (ticker_or_None, matched_fragment).
    """
    name_lower = recipient_name.lower()

    for fragment, ticker in CONTRACTOR_TICKER_MAP.items():
        if fragment in name_lower:
            return ticker, fragment

    return None, ""


# ---------------------------------------------------------------------------
# USAspending API Client
# ---------------------------------------------------------------------------

def fetch_awards(days_back: int = DAYS_BACK,
                 min_amount: int = AWARD_FLOOR,
                 max_pages: int = MAX_PAGES) -> List[dict]:
    """Fetch recent contract awards from USAspending.gov.

    Returns list of award dicts with normalized fields.

    IMPORTANT: The USAspending time_period filter matches on *action date*
    (when any transaction/modification occurred), NOT on when the base contract
    started. Sorting by 'Award Amount' surfaces massive legacy contracts with
    recent modifications (e.g. DOE contracts from 2000 worth $30B+). We sort
    by 'Start Date' descending and post-filter to keep only contracts whose
    Start Date falls within a generous lookback window (6 months). This ensures
    we surface genuinely new awards, not decades-old contracts with routine mods.
    """
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")
    start_date = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
    # Post-filter cutoff: keep awards whose Start Date is within 6 months
    # (generous window because large contracts may take weeks to appear)
    start_date_cutoff = (today - timedelta(days=180)).strftime("%Y-%m-%d")

    all_awards = []

    for page in range(1, max_pages + 1):
        payload = {
            "filters": {
                "time_period": [{"start_date": start_date, "end_date": today_str}],
                "award_type_codes": ["A", "B", "C", "D"],  # Contract types
                "award_amounts": [{"lower_bound": min_amount}],
            },
            "fields": [
                "Award ID", "Recipient Name", "Award Amount",
                "Awarding Agency", "Description", "Start Date",
                "Award Type",
            ],
            "sort": "Start Date",
            "order": "desc",
            "limit": 10,
            "page": page,
        }

        try:
            resp = requests.post(
                USASPENDING_URL, json=payload, timeout=REQUEST_TIMEOUT
            )
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error(f"USAspending API error on page {page}: {e}")
            break

        data = resp.json()
        results = data.get("results", [])

        if not results:
            logger.info(f"No more results on page {page}")
            break

        stale_count = 0
        for r in results:
            raw_start = r.get("Start Date", "")
            # Post-filter: skip awards whose Start Date is before our cutoff
            # (these are old contracts with recent modifications, not new awards)
            if raw_start and raw_start < start_date_cutoff:
                stale_count += 1
                continue

            award = {
                "award_id": r.get("Award ID", ""),
                "recipient_name": (r.get("Recipient Name") or "").strip(),
                "award_amount": r.get("Award Amount", 0),
                "awarding_agency": (r.get("Awarding Agency") or "").strip(),
                "description": (r.get("Description") or "").strip()[:200],
                "start_date": raw_start,
                "award_type": r.get("Award Type", ""),
                "internal_id": r.get("generated_internal_id", ""),
            }
            all_awards.append(award)

        if stale_count > 0:
            logger.info(f"Page {page}: filtered {stale_count} stale awards (Start Date before {start_date_cutoff})")

        total = data.get("page_metadata", {}).get("total", 0)
        logger.info(f"Page {page}: {len(results)} raw, {len(results) - stale_count} kept (total available: {total})")

        # If ALL results on this page were stale, stop — sorted desc means
        # subsequent pages will be even older
        if stale_count == len(results):
            logger.info("All results on page are stale — stopping pagination")
            break

        if page * 10 >= total:
            break

        if page < max_pages:
            time.sleep(1)  # Be polite

    logger.info(f"Total awards fetched: {len(all_awards)} (after stale filtering)")
    return all_awards


# ---------------------------------------------------------------------------
# Market cap triage
# ---------------------------------------------------------------------------

# DEPRECATED — use mcap_cache.get_market_cap_cached() instead
# def _get_market_cap(ticker: str) -> Optional[float]:
#     """Get market cap in millions via yfinance."""
#     if not ticker:
#         return None
#     try:
#         import yfinance as yf
#         stock = yf.Ticker(ticker)
#         info = stock.info
#         mcap = info.get("marketCap")
#         if mcap:
#             return mcap / 1_000_000
#     except Exception as e:
#         logger.debug(f"Market cap lookup failed for {ticker}: {e}")
#     return None


# ---------------------------------------------------------------------------
# Signal Builder
# ---------------------------------------------------------------------------

def _signal_hash(recipient: str, award_id: str) -> str:
    return hashlib.md5(f"{recipient}|{award_id}".encode()).hexdigest()


def _classify_award_size(amount: float) -> Tuple[str, int]:
    """Classify award by size, return (label, base_strength)."""
    if amount >= 1_000_000_000:
        return "mega_award", 5
    elif amount >= 250_000_000:
        return "very_large_award", 4
    elif amount >= 50_000_000:
        return "large_award", 3
    return "award", 2


def _build_signal(award: dict, signal_type: str, strength: int,
                  ticker: str = "", market_cap_mm: float = 0,
                  matched_name: str = "") -> dict:
    """Build a signal in common pipeline format."""
    raw_data = {
        "recipient_name": award.get("recipient_name", ""),
        "award_amount": award.get("award_amount", 0),
        "awarding_agency": award.get("awarding_agency", ""),
        "description": award.get("description", ""),
        "start_date": award.get("start_date", ""),
        "award_type": award.get("award_type", ""),
        "award_id": award.get("award_id", ""),
        "matched_contractor": matched_name,
    }

    amount = award.get("award_amount", 0)
    amount_str = (
        f"${amount / 1e9:.1f}B" if amount >= 1e9 else f"${amount / 1e6:.0f}M"
    )

    return {
        "ticker": ticker or "",
        "isin": None,
        "company_name": award.get("recipient_name", ""),
        "market_cap_mm": round(market_cap_mm, 1) if market_cap_mm else None,
        "signal_type": f"contract_{signal_type}",
        "signal_category": "government_contract",
        "strength_estimate": strength,
        "source_url": f"https://www.usaspending.gov/award/{award.get('internal_id', '')}",
        "source_date": award.get("start_date", ""),
        "scan_date": datetime.now().strftime("%Y-%m-%d"),
        "raw_data": raw_data,
    }


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

def _load_dedup(filepath: str) -> dict:
    if filepath and os.path.exists(filepath):
        try:
            with open(filepath) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_dedup(filepath: str, log: dict):
    if filepath:
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, "w") as f:
                json.dump(log, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save dedup: {e}")


# ---------------------------------------------------------------------------
# Main Scan
# ---------------------------------------------------------------------------

def run_scan(days_back: int = DAYS_BACK,
             market_cap_filter: bool = True,
             save_signals: bool = True) -> List[dict]:
    """Run full government contract scan.

    1. Fetch recent large awards from USAspending.gov
    2. Match recipient names to public company tickers
    3. Classify award size
    4. Apply market cap triage
    5. Build and save signals

    Returns list of signal dicts.
    """
    # Fetch awards
    awards = fetch_awards(days_back=days_back)
    if not awards:
        logger.warning("No awards found")
        return []

    # Load dedup
    dedup = _load_dedup(DEDUP_FILE)

    all_signals = []

    for award in awards:
        recipient = award["recipient_name"]
        award_id = award["award_id"]
        amount = award.get("award_amount", 0)

        if amount < AWARD_FLOOR:
            continue

        # Dedup
        h = _signal_hash(recipient, award_id)
        if h in dedup:
            continue

        # Match to ticker
        ticker, matched_name = _match_contractor(recipient)

        # Skip private companies (ticker is None, not "")
        if ticker is None and matched_name:
            logger.debug(f"Private company match: {recipient} ({matched_name})")
            continue

        # Skip unmatched recipients (no fragment matched at all)
        if not ticker and not matched_name:
            # Log at INFO for unmatched awards >$50M so scheduled sessions
            # can spot mapping gaps and expand CONTRACTOR_TICKER_MAP
            if amount >= 50_000_000:
                logger.info(f"UNMATCHED HIGH-VALUE: {recipient} | ${amount/1e6:.0f}M | "
                            f"{award.get('awarding_agency', '')} — consider adding to CONTRACTOR_TICKER_MAP")
            else:
                logger.debug(f"No ticker match: {recipient}")
            continue

        # Classify award size
        size_label, base_strength = _classify_award_size(amount)

        # Market cap triage
        market_cap_mm = 0
        if market_cap_filter and ticker:
            market_cap_mm = _get_market_cap(ticker) or 0
            if 0 < market_cap_mm < MARKET_CAP_FLOOR_MM:
                logger.debug(f"Below market cap floor: {ticker} ${market_cap_mm:.0f}M")
                continue

        # Build signal
        signal = _build_signal(
            award, size_label, base_strength,
            ticker=ticker, market_cap_mm=market_cap_mm,
            matched_name=matched_name,
        )
        all_signals.append(signal)

        # Update dedup
        dedup[h] = datetime.now().strftime("%Y-%m-%d")

    # Save dedup
    _save_dedup(DEDUP_FILE, dedup)

    # Save signals
    if save_signals and SIGNALS_DIR and all_signals:
        os.makedirs(SIGNALS_DIR, exist_ok=True)
        output_file = os.path.join(
            SIGNALS_DIR,
            f"contract_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
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

    parser = argparse.ArgumentParser(description="Government Contract Monitor")
    parser.add_argument("--days", type=int, default=DAYS_BACK,
                        help=f"Days to look back (default: {DAYS_BACK})")
    parser.add_argument("--no-market-cap", action="store_true",
                        help="Disable market cap filtering")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print signals without saving")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    global SIGNALS_DIR, DEDUP_FILE
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    SIGNALS_DIR = os.path.join(project_dir, "signals")
    DEDUP_FILE = os.path.join(project_dir, "signals", "contract_dedup.json")

    signals = run_scan(
        days_back=args.days,
        market_cap_filter=not args.no_market_cap,
        save_signals=not args.dry_run,
    )

    print(f"\n{'=' * 70}")
    print(f"Government Contract Scan — {len(signals)} signals found")
    print(f"{'=' * 70}")

    for s in signals:
        ticker = s.get("ticker") or "N/A"
        rd = s["raw_data"]
        amount = rd.get("award_amount", 0)
        amt_str = f"${amount/1e9:.1f}B" if amount >= 1e9 else f"${amount/1e6:.0f}M"
        agency = rd.get("awarding_agency", "")[:25]
        recipient = rd.get("recipient_name", "")[:30]
        strength = s.get("strength_estimate", 0)
        stype = s.get("signal_type", "")
        print(f"  [{strength}] {ticker:6s} | {recipient:30s} | {amt_str:>8s} | {agency:25s} | {stype}")


if __name__ == "__main__":
    main()
