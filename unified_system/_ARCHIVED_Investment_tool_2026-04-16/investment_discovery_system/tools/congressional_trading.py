"""
Congressional Trading Scanner  (v2.0 — 2026-04-09)
===================================================
Scans US congressional stock trades via Capitol Trades HTML scraping for
unusual activity, particularly committee-aligned trades that may reflect
non-public legislative intelligence.

Data Source:
- Capitol Trades: https://www.capitoltrades.com/trades
- Free, no auth required
- Paginated HTML table (12 trades per page)
- Fields: Politician (name, party, chamber, state), Issuer (name, ticker),
  Published date, Traded date, Filed After, Owner, Type (buy/sell),
  Size range, Price

History:
- v1.0: Quiver Quantitative API (now requires auth — see D-013)
- v2.0: Capitol Trades HTML scraping (free, no auth)

Usage:
    python congressional_trading.py                  # Run full scan
    python congressional_trading.py --days 14        # Scan last 14 days
    python congressional_trading.py --min-amount 50  # Min $50K trades
    python congressional_trading.py --pages 10       # Scrape 10 pages
    python congressional_trading.py --dry-run        # Print, don't save
"""

import json
import os
import re
import time
import logging
import hashlib
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

import requests
from bs4 import BeautifulSoup

# Try to import from tools.mcap_cache; fall back to local import if running standalone
try:
    from tools.mcap_cache import get_market_cap_cached as _get_market_cap
except ImportError:
    from mcap_cache import get_market_cap_cached as _get_market_cap

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CAPITOL_TRADES_URL = "https://www.capitoltrades.com/trades"
REQUEST_TIMEOUT = 15
SCRAPE_DELAY = 1.0  # seconds between pages — be polite
MAX_PAGES = 30       # default pages to scrape (12 trades/page = ~360 trades)
USER_AGENT = "Mozilla/5.0 (compatible; InvestmentResearchBot/1.0; contact: research@example.com)"

# Triage thresholds
MARKET_CAP_FLOOR_MM = 215    # €200M ≈ $215M — minimum for liquidity
MIN_TRADE_AMOUNT = 5000      # $5K minimum (lowered to capture more signals)
UNUSUAL_SIZE_THRESHOLD = 25000  # $25K — flags "unusual size" signal

# Dedup
DEDUP_WINDOW_DAYS = 14  # Short window — trades should surface quickly

# Output — default paths based on script location. Overridden by CLI main().
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SCRIPT_DIR)
SIGNALS_DIR = os.path.join(_PROJECT_DIR, "signals")
DEDUP_FILE = os.path.join(_PROJECT_DIR, "signals", "congressional_dedup.json")

# Logging
logger = logging.getLogger("congressional_trading")


# ---------------------------------------------------------------------------
# Committee-Sector Mapping (Static Lookup)
# ---------------------------------------------------------------------------

COMMITTEE_SECTOR_MAP = {
    "Armed Services": ["defense", "aerospace", "military"],
    "Health": ["pharma", "biotech", "healthcare", "medical"],
    "HELP": ["pharma", "biotech", "healthcare", "medical", "education"],
    "Banking": ["banking", "financial", "fintech", "insurance"],
    "Finance": ["banking", "financial", "tax", "insurance"],
    "Energy": ["oil", "gas", "energy", "utilities", "renewable", "solar", "nuclear"],
    "Commerce": ["tech", "telecom", "communications", "internet", "ai"],
    "Agriculture": ["agriculture", "food", "farming", "agribusiness"],
    "Judiciary": ["legal", "litigation", "prison", "law enforcement"],
    "Appropriations": [],
    "Intelligence": ["defense", "cybersecurity", "surveillance"],
    "Ways and Means": ["tax", "financial", "trade"],
    "Financial Services": ["banking", "financial", "fintech", "insurance", "crypto"],
    "Science": ["tech", "ai", "biotech", "space"],
    "Homeland Security": ["defense", "cybersecurity", "border"],
    "Veterans": ["healthcare", "medical"],
    "Transportation": ["transport", "infrastructure", "airline", "railroad"],
}

SECTOR_TICKER_MAP = {
    "defense": ["LMT", "RTX", "GD", "NOC", "BA", "LHX", "HII", "LDOS", "SAIC",
                 "BAH", "CACI", "KTOS", "PLTR", "BWXT"],
    "aerospace": ["LMT", "RTX", "BA", "NOC", "LHX", "HII", "AJRD", "BWXT"],
    "pharma": ["PFE", "JNJ", "MRK", "ABBV", "LLY", "BMY", "AZN", "NVS", "GSK",
               "AMGN", "GILD", "REGN", "VRTX", "BIIB"],
    "biotech": ["AMGN", "GILD", "REGN", "VRTX", "BIIB", "MRNA", "BNTX", "SGEN",
                "ALNY", "BMRN", "IONS"],
    "healthcare": ["UNH", "CVS", "CI", "HUM", "ELV", "HCA", "THC", "CNC", "MOH"],
    "banking": ["JPM", "BAC", "WFC", "C", "GS", "MS", "USB", "PNC", "TFC", "SCHW"],
    "financial": ["JPM", "BAC", "GS", "MS", "BLK", "SCHW", "CME", "ICE", "NDAQ"],
    "fintech": ["SQ", "PYPL", "SOFI", "AFRM", "COIN", "HOOD", "NU"],
    "tech": ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AVGO", "CRM",
             "ORCL", "IBM", "INTC", "AMD"],
    "ai": ["NVDA", "MSFT", "GOOGL", "META", "AMD", "PLTR", "AI", "PATH"],
    "telecom": ["T", "VZ", "TMUS", "CMCSA", "CHTR", "LUMN"],
    "oil": ["XOM", "CVX", "COP", "EOG", "SLB", "OXY", "MPC", "PSX", "VLO", "DVN"],
    "gas": ["XOM", "CVX", "COP", "EOG", "SLB", "OXY", "LNG", "EQT"],
    "energy": ["XOM", "CVX", "NEE", "DUK", "SO", "AES", "D", "EXC"],
    "renewable": ["NEE", "ENPH", "SEDG", "FSLR", "RUN", "PLUG", "BE"],
    "solar": ["ENPH", "SEDG", "FSLR", "RUN", "SPWR"],
    "cybersecurity": ["CRWD", "PANW", "FTNT", "ZS", "NET", "S", "OKTA"],
    "insurance": ["BRK.B", "PGR", "ALL", "TRV", "MET", "AFL", "AIG"],
    "crypto": ["COIN", "MSTR", "MARA", "RIOT", "CLSK"],
}

# Known high-profile members and their committees
# Key: politician name (lowercase) -> list of committee names
MEMBER_COMMITTEES_BY_NAME = {
    # Senate — active traders
    "nancy pelosi": ["Appropriations"],
    "tommy tuberville": ["Armed Services", "Agriculture", "Veterans"],
    "bernie sanders": ["HELP", "Energy"],
    "mark warner": ["Finance", "Intelligence", "Banking"],
    "ted cruz": ["Commerce", "Judiciary"],
    "lindsey graham": ["Appropriations", "Judiciary"],
    "susan collins": ["Appropriations", "Intelligence", "HELP"],
    "john hoeven": ["Appropriations", "Energy"],
    "john hickenlooper": ["Commerce", "HELP", "Energy"],
    "mark kelly": ["Armed Services", "Energy", "Commerce"],
    "rick scott": ["Armed Services", "Banking", "Commerce"],
    "roger marshall": ["HELP", "Agriculture"],
    "cynthia lummis": ["Banking", "Commerce"],
    "bill hagerty": ["Banking", "Appropriations"],
    "sheldon whitehouse": ["Judiciary", "Finance"],
    "gary peters": ["Armed Services", "Commerce", "Homeland Security"],
    "jerry moran": ["Appropriations", "Commerce", "Veterans"],
    "bill cassidy": ["Finance", "HELP", "Energy"],
    "john cornyn": ["Finance", "Intelligence", "Judiciary"],
    "thom tillis": ["Armed Services", "Banking", "Judiciary"],
    "mike crapo": ["Finance", "Banking"],
    "pat toomey": ["Banking", "Finance"],
    "dan sullivan": ["Armed Services", "Commerce"],
    "pete ricketts": ["Armed Services", "Banking"],
    "markwayne mullin": ["Armed Services", "HELP"],
    "tim scott": ["Banking", "Finance", "HELP"],
    "katie britt": ["Appropriations", "Banking"],
    "john fetterman": ["Banking", "HELP"],
    "jon ossoff": ["Banking", "Homeland Security", "Judiciary"],
    # House — most active traders
    "dan crenshaw": ["Energy", "Intelligence"],
    "josh gottheimer": ["Financial Services"],
    "ro khanna": ["Armed Services", "Commerce"],
    "michael mccaul": ["Armed Services", "Commerce"],
    "marjorie taylor greene": ["Homeland Security"],
    "thomas kean jr": ["Financial Services"],
    "kevin hern": ["Ways and Means"],
    "michael garcia": ["Armed Services", "Homeland Security"],
    "diana harshbarger": ["Energy", "Homeland Security"],
    "john james": ["Armed Services"],
    "troy nehls": ["Transportation"],
    "french hill": ["Financial Services", "Intelligence"],
    "john curtis": ["Energy", "Commerce"],
    "maria elvira salazar": ["Financial Services"],
    "pat fallon": ["Armed Services"],
    "virginia foxx": ["HELP"],
    "greg steube": ["Judiciary", "Armed Services"],
    "lois frankel": ["Appropriations"],
    "suzan delbene": ["Ways and Means"],
    "dean phillips": ["Financial Services"],
    "nicole malliotakis": ["Ways and Means"],
    "mark green": ["Armed Services", "Homeland Security"],
    "earl blumenauer": ["Ways and Means"],
    "brian higgins": ["Ways and Means"],
    "debbie wasserman schultz": ["Appropriations"],
    "ann wagner": ["Financial Services"],
    "austin scott": ["Armed Services", "Agriculture"],
    "mike gallagher": ["Armed Services", "Intelligence"],
    "jake auchincloss": ["Armed Services", "Transportation"],
    "alan lowenthal": ["Transportation", "Science"],
    "katherine clark": ["Appropriations"],
    "tony gonzales": ["Appropriations"],
}


# ---------------------------------------------------------------------------
# Capitol Trades Scraper
# ---------------------------------------------------------------------------

SIZE_RANGE_MAP = {
    "1K–15K": (1000, 15000),
    "15K–50K": (15000, 50000),
    "50K–100K": (50000, 100000),
    "100K–250K": (100000, 250000),
    "250K–500K": (250000, 500000),
    "500K–1M": (500000, 1000000),
    "1M–5M": (1000000, 5000000),
    "5M–25M": (5000000, 25000000),
    "25M–50M": (25000000, 50000000),
    "50M+": (50000000, 100000000),
    "Over $50,000,000": (50000000, 100000000),
}


def _parse_size_range(size_str: str) -> Tuple[float, float]:
    """Parse Capitol Trades size range into (low, high) dollars."""
    if not size_str:
        return (0, 0)
    size_str = size_str.strip()
    # Try exact match first
    if size_str in SIZE_RANGE_MAP:
        return SIZE_RANGE_MAP[size_str]
    # Try cleaned match (remove unicode dashes, spaces)
    cleaned = size_str.replace("\u2013", "–").replace(" ", "").strip()
    if cleaned in SIZE_RANGE_MAP:
        return SIZE_RANGE_MAP[cleaned]
    # Try regex fallback: "100K–250K" style
    m = re.match(r"(\d+(?:\.\d+)?)\s*([KMB]?)\s*[–\-]\s*(\d+(?:\.\d+)?)\s*([KMB]?)", cleaned)
    if m:
        def _to_num(val, suffix):
            n = float(val)
            if suffix == "K":
                return n * 1000
            elif suffix == "M":
                return n * 1000000
            elif suffix == "B":
                return n * 1000000000
            return n
        return (_to_num(m.group(1), m.group(2)), _to_num(m.group(3), m.group(4)))
    return (0, 0)


def _midpoint_amount(size_str: str) -> float:
    low, high = _parse_size_range(size_str)
    return (low + high) / 2


def _parse_trade_date(date_text: str) -> Optional[str]:
    """Parse Capitol Trades date string like '30 Mar2026' or '6 Apr2026' to YYYY-MM-DD."""
    if not date_text:
        return None
    # Clean up: the text often runs together like "30 Mar2026"
    cleaned = date_text.strip()
    # Try to insert space before year if missing
    cleaned = re.sub(r"(\w{3})(\d{4})", r"\1 \2", cleaned)
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _extract_ticker(issuer_text: str) -> Optional[str]:
    """Extract US ticker from issuer cell text like 'PepsiCo IncPEP:US'."""
    if not issuer_text:
        return None
    # Look for TICKER:US pattern
    m = re.search(r"([A-Z]{1,5})(?:\.[A-Z])?:US", issuer_text)
    if m:
        return m.group(1)
    # Look for TICKER:XX pattern (non-US — still useful)
    m = re.search(r"([A-Z]{1,5}):([A-Z]{2})", issuer_text)
    if m:
        return m.group(1)
    return None


def _extract_issuer_name(issuer_text: str) -> str:
    """Extract company name from issuer cell text like 'PepsiCo IncPEP:US'."""
    if not issuer_text:
        return ""
    # Remove ticker:exchange suffix
    cleaned = re.sub(r"[A-Z]{1,6}:[A-Z]{2}\s*$", "", issuer_text).strip()
    # Remove trailing N/A
    cleaned = re.sub(r"N/A\s*$", "", cleaned).strip()
    return cleaned


def _parse_politician_cell(cell_text: str) -> Dict[str, str]:
    """Parse politician cell like 'Sheldon WhitehouseDemocratSenateRI'."""
    result = {"name": "", "party": "", "chamber": "", "state": ""}
    if not cell_text:
        return result
    text = cell_text.strip()

    # Extract state (last 2 chars, uppercase)
    m = re.search(r"([A-Z]{2})\s*$", text)
    if m:
        result["state"] = m.group(1)
        text = text[:m.start()].strip()

    # Extract chamber
    for chamber in ["Senate", "House"]:
        if chamber in text:
            result["chamber"] = chamber
            text = text.replace(chamber, "", 1).strip()
            break

    # Extract party
    for party in ["Democrat", "Republican", "Independent"]:
        if party in text:
            result["party"] = party
            text = text.replace(party, "", 1).strip()
            break

    result["name"] = text.strip()
    return result


def fetch_trades_from_capitol(max_pages: int = MAX_PAGES) -> List[dict]:
    """Scrape trades from Capitol Trades HTML table.

    Returns list of normalized trade dicts. Pages are sorted by publication
    date (newest first), NOT by trade date — so we cannot use early exit
    based on trade dates. The scan_trades() function handles date filtering.
    """
    all_trades = []
    headers = {"User-Agent": USER_AGENT}

    for page in range(1, max_pages + 1):
        url = f"{CAPITOL_TRADES_URL}?page={page}"
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to fetch page {page}: {e}")
            break

        soup = BeautifulSoup(resp.text, "lxml")
        table = soup.find("table")
        if not table:
            logger.warning(f"No table found on page {page}")
            break

        rows = table.find_all("tr")[1:]  # Skip header row
        if not rows:
            logger.info(f"No more rows on page {page}")
            break

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 8:
                continue

            # Parse each cell
            politician_text = cells[0].get_text(strip=True) if cells[0] else ""
            issuer_text = cells[1].get_text(strip=True) if cells[1] else ""
            # Published date (cells[2]) — less useful for filtering
            traded_date_text = cells[3].get_text(strip=True) if cells[3] else ""
            # Filed after (cells[4]) — days
            owner = cells[5].get_text(strip=True) if len(cells) > 5 else ""
            trade_type = cells[6].get_text(strip=True) if len(cells) > 6 else ""
            size_range = cells[7].get_text(strip=True) if len(cells) > 7 else ""

            # Parse fields
            politician = _parse_politician_cell(politician_text)
            ticker = _extract_ticker(issuer_text)
            issuer_name = _extract_issuer_name(issuer_text)
            traded_date = _parse_trade_date(traded_date_text)

            # Skip non-stock instruments (treasury bills, bonds, etc.)
            if not ticker or ticker == "N/A":
                continue

            trade = {
                "politician_name": politician["name"],
                "party": politician["party"],
                "chamber": politician["chamber"],
                "state": politician["state"],
                "ticker": ticker,
                "issuer_name": issuer_name,
                "transaction_date": traded_date or "",
                "owner": owner,
                "transaction": trade_type,  # buy, sell, exchange
                "size_range": size_range,
            }
            all_trades.append(trade)

        logger.info(f"Page {page}: {len(rows)} rows, {len(all_trades)} total trades so far")

        if page < max_pages:
            time.sleep(SCRAPE_DELAY)

    logger.info(f"Fetched {len(all_trades)} trades from Capitol Trades ({page} pages)")
    return all_trades


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# DEPRECATED — use mcap_cache.get_market_cap_cached() instead
# def _get_market_cap(ticker):
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
#         logger.debug(f"yfinance lookup failed for {ticker}: {e}")
#     return None


def _check_committee_alignment(politician_name: str, ticker: str) -> Optional[str]:
    """Check if member's committee aligns with traded stock's sector."""
    name_lower = politician_name.lower().strip()
    committees = MEMBER_COMMITTEES_BY_NAME.get(name_lower, [])
    if not committees:
        return None
    ticker_upper = ticker.upper()
    for committee in committees:
        sectors = COMMITTEE_SECTOR_MAP.get(committee, [])
        for sector in sectors:
            sector_tickers = SECTOR_TICKER_MAP.get(sector, [])
            if ticker_upper in sector_tickers:
                return committee
    return None


def _is_purchase(transaction: str) -> bool:
    t = transaction.lower()
    return "buy" in t or "purchase" in t


def _is_sale(transaction: str) -> bool:
    return "sell" in transaction.lower() or "sale" in transaction.lower()


def _is_options(transaction: str) -> bool:
    t = transaction.lower()
    return "exchange" in t or "option" in t or "exercise" in t


# ---------------------------------------------------------------------------
# Signal dedup
# ---------------------------------------------------------------------------

def _load_dedup_log(filepath):
    if filepath and os.path.exists(filepath):
        try:
            with open(filepath) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_dedup_log(filepath, log):
    if filepath:
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, "w") as f:
                json.dump(log, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save dedup log: {e}")


def _signal_hash(politician_name: str, ticker: str, transaction_date: str) -> str:
    raw = f"{politician_name}|{ticker}|{transaction_date}"
    return hashlib.md5(raw.encode()).hexdigest()


def _is_novel(politician_name: str, ticker: str, transaction_date: str,
              dedup_log: dict, window_days: int = DEDUP_WINDOW_DAYS) -> bool:
    h = _signal_hash(politician_name, ticker, transaction_date)
    if h in dedup_log:
        first_seen = dedup_log[h]
        try:
            first_date = datetime.strptime(first_seen, "%Y-%m-%d")
            if (datetime.now() - first_date).days < window_days:
                return False
        except ValueError:
            pass
    return True


# ---------------------------------------------------------------------------
# Signal strength estimation
# ---------------------------------------------------------------------------

def _estimate_strength(trade: dict, committee_match: Optional[str],
                       amount_mid: float, cluster_count: int,
                       market_cap_mm: float = 0) -> int:
    """Estimate signal strength 1-5 based on trade characteristics.

    Includes Q-014 filter: spouse/child small-dollar mega-cap Commerce trades
    are downgraded to strength 2 (noise pattern — e.g. Ro Khanna spouse trades).
    """
    strength = 2  # Base strength for any congressional trade

    # Committee alignment is a strong signal
    if committee_match:
        strength = max(strength, 4)

    # Q-014: Downgrade spouse/child small-dollar mega-cap Commerce trades.
    # Pattern: Owner ∈ {Child, Spouse}, trade ≤ $50K midpoint,
    #          company mcap > $100B, committee = Commerce.
    # These are routine family portfolio trades in mega-cap tech, not insider signals.
    owner = trade.get("owner", "").strip().lower()
    if (committee_match == "Commerce"
            and owner in ("spouse", "child")
            and amount_mid <= 50000
            and market_cap_mm > 100_000):
        strength = 2  # Force back to base — not a meaningful signal

    # Large trades are more noteworthy
    if amount_mid >= 1000000:  # $1M+
        strength = max(strength, 5)
    elif amount_mid >= 250000:  # $250K+
        strength = max(strength, 4)
    elif amount_mid >= UNUSUAL_SIZE_THRESHOLD:
        strength = max(strength, 3)

    # Options activity
    if _is_options(trade.get("transaction", "")):
        strength = max(strength, 4)

    # Timing clusters (multiple members trading same stock)
    if cluster_count >= 3:
        strength = max(strength, 4)
    elif cluster_count >= 2:
        strength = max(strength, 3)

    return min(strength, 5)


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan_trades(days_back: int = 45, min_amount: float = MIN_TRADE_AMOUNT,
                market_cap_filter: bool = True, max_pages: int = MAX_PAGES) -> List[dict]:
    """Scan trades from Capitol Trades and produce pipeline signals.

    Note: days_back defaults to 45 because STOCK Act allows up to 45 days
    between trade date and disclosure. Capitol Trades pages are sorted by
    publication date, so recently-published trades may have old trade dates.
    """
    trades = fetch_trades_from_capitol(max_pages=max_pages)
    if not trades:
        return []

    cutoff_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    dedup_log = _load_dedup_log(DEDUP_FILE)

    # Filter trades by date first
    trades = [t for t in trades if t.get("transaction_date", "") >= cutoff_date]
    logger.info(f"After date filter ({days_back}d): {len(trades)} trades")

    # Pre-processing: detect timing clusters
    ticker_traders: Dict[str, set] = {}
    for trade in trades:
        ticker = trade.get("ticker", "")
        if ticker:
            name = trade.get("politician_name", "")
            ticker_traders.setdefault(ticker, set()).add(name)

    ticker_cluster_count = {t: len(members) for t, members in ticker_traders.items()}

    all_signals = []
    mcap_cache = {}

    for trade in trades:
        ticker = trade.get("ticker", "")
        if not ticker:
            continue

        amount_mid = _midpoint_amount(trade.get("size_range", ""))
        if amount_mid < min_amount:
            continue

        politician_name = trade.get("politician_name", "")
        t_date = trade.get("transaction_date", "")

        if not _is_novel(politician_name, ticker, t_date, dedup_log):
            continue

        market_cap_mm = 0
        if market_cap_filter:
            if ticker not in mcap_cache:
                mcap_cache[ticker] = _get_market_cap(ticker)
            market_cap_mm = mcap_cache[ticker] or 0
            if 0 < market_cap_mm < MARKET_CAP_FLOOR_MM:
                continue

        committee_match = _check_committee_alignment(politician_name, ticker)
        cluster_count = ticker_cluster_count.get(ticker, 1)
        strength = _estimate_strength(trade, committee_match, amount_mid, cluster_count,
                                      market_cap_mm=market_cap_mm)

        signal_flags = []
        if committee_match:
            signal_flags.append(f"committee_aligned:{committee_match}")
        if amount_mid >= UNUSUAL_SIZE_THRESHOLD:
            signal_flags.append("unusual_size")
        if cluster_count >= 2:
            signal_flags.append(f"timing_cluster:{cluster_count}_members")
        if _is_options(trade.get("transaction", "")):
            signal_flags.append("options_activity")

        signal_type = "congressional_trade"
        if signal_flags:
            signal_type = "congressional_" + "+".join(signal_flags)

        signal = {
            "ticker": ticker,
            "isin": None,
            "company_name": trade.get("issuer_name", "") or ticker,
            "market_cap_mm": round(market_cap_mm, 1) if market_cap_mm else None,
            "signal_type": signal_type,
            "signal_category": "congressional",
            "strength_estimate": strength,
            "source_url": "https://www.capitoltrades.com/trades",
            "source_date": t_date,
            "scan_date": today,
            "raw_data": {
                "representative": politician_name,
                "party": trade.get("party", ""),
                "house": trade.get("chamber", ""),
                "state": trade.get("state", ""),
                "transaction": trade.get("transaction", ""),
                "range": trade.get("size_range", ""),
                "amount_midpoint": amount_mid,
                "transaction_date": t_date,
                "owner": trade.get("owner", ""),
                "committee_alignment": committee_match,
                "cluster_count": cluster_count,
                "signal_flags": signal_flags,
            },
        }
        all_signals.append(signal)

        h = _signal_hash(politician_name, ticker, t_date)
        if h not in dedup_log:
            dedup_log[h] = today

    _save_dedup_log(DEDUP_FILE, dedup_log)
    logger.info(f"Congressional scan complete: {len(all_signals)} signals from {len(trades)} trades")
    return all_signals


def run_full_scan(days_back: int = 45, min_amount: float = MIN_TRADE_AMOUNT,
                  market_cap_filter: bool = True, save_signals: bool = True,
                  max_pages: int = MAX_PAGES) -> List[dict]:
    """Run full congressional trading scan and optionally save signals.

    Interface compatible with pipeline_runner.py.
    """
    # Initialize paths if not set
    global SIGNALS_DIR, DEDUP_FILE
    if not SIGNALS_DIR:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_dir = os.path.dirname(script_dir)
        SIGNALS_DIR = os.path.join(project_dir, "signals")
        DEDUP_FILE = os.path.join(project_dir, "signals", "congressional_dedup.json")

    all_signals = scan_trades(
        days_back=days_back, min_amount=min_amount,
        market_cap_filter=market_cap_filter, max_pages=max_pages,
    )
    if save_signals and SIGNALS_DIR and all_signals:
        os.makedirs(SIGNALS_DIR, exist_ok=True)
        output_file = os.path.join(
            SIGNALS_DIR,
            f"congressional_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
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

    parser = argparse.ArgumentParser(description="Congressional Trading Scanner (Capitol Trades)")
    parser.add_argument("--days", type=int, default=45, help="Days to look back (default: 45)")
    parser.add_argument("--min-amount", type=float, default=MIN_TRADE_AMOUNT,
                        help=f"Min trade midpoint amount (default: ${MIN_TRADE_AMOUNT:,.0f})")
    parser.add_argument("--pages", type=int, default=MAX_PAGES,
                        help=f"Max pages to scrape (default: {MAX_PAGES}, ~12 trades/page)")
    parser.add_argument("--no-market-cap", action="store_true", help="Disable market cap filtering")
    parser.add_argument("--dry-run", action="store_true", help="Print signals without saving")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    global SIGNALS_DIR, DEDUP_FILE
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    SIGNALS_DIR = os.path.join(project_dir, "signals")
    DEDUP_FILE = os.path.join(project_dir, "signals", "congressional_dedup.json")

    signals = run_full_scan(
        days_back=args.days, min_amount=args.min_amount,
        market_cap_filter=not args.no_market_cap,
        save_signals=not args.dry_run,
        max_pages=args.pages,
    )

    print(f"\nCongressional Trading Scan -- {len(signals)} signals found")
    purchases = sum(1 for s in signals if _is_purchase(s["raw_data"]["transaction"]))
    sales = sum(1 for s in signals if _is_sale(s["raw_data"]["transaction"]))
    print(f"Purchases: {purchases} | Sales: {sales}")

    for s in sorted(signals, key=lambda x: -x["strength_estimate"])[:15]:
        rd = s["raw_data"]
        flags = ", ".join(rd["signal_flags"]) if rd["signal_flags"] else "base"
        print(f"  [{s['strength_estimate']}] {rd['representative']:25s} | {s['ticker']:6s} | "
              f"{rd['transaction']:6s} | {rd['range']:12s} | {flags}")


if __name__ == "__main__":
    main()
