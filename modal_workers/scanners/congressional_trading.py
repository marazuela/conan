"""
Congressional Trading scanner — Modal port of tools/congressional_trading.py.

Data source:
  - Capitol Trades (https://www.capitoltrades.com/trades) — free, no auth.
  - Paginated HTML table (12 trades/page). We scrape up to MAX_PAGES with a
    1.0s polite delay between pages (v1 parity).

Preserved from v1:
  - Capitol Trades HTML table parsing (BeautifulSoup + regex helpers for size
    ranges, traded dates, tickers, issuer names, politician cells).
  - COMMITTEE_SECTOR_MAP, SECTOR_TICKER_MAP, MEMBER_COMMITTEES_BY_NAME
    committee-alignment classifier (byte-equivalent).
  - SIZE_RANGE_MAP, _midpoint_amount, _parse_trade_date.
  - Strength heuristics: base 2, +committee=4, +$1M=5, +$250K=4,
    +unusual ($25K)=3, +options=4, +cluster>=3=4, +cluster>=2=3.
    Q-014 spouse/child mega-cap Commerce downgrade preserved structurally, but
    gated on amount_mid only (market-cap filter removed — see deviation below).
  - MIN_TRADE_AMOUNT=$5K, UNUSUAL_SIZE_THRESHOLD=$25K.
  - filter_excluded_filers: Ro Khanna default (via registry config).

Deviations from v1:
  - mcap_cache dropped per v2 spec. No market-cap floor filter in the scanner;
    downstream auto-caps + dashboard filtering handle liquidity gating.
  - Q-014 Commerce-downgrade fires without market-cap gate (v1 also required
    mcap>$100B). The structural pattern — spouse/child + small $ + Commerce —
    still matches the routine-family-trade noise shape.
  - Dedup replaced with a rolling 30-day trades cache used for cluster
    detection across runs. Per-signal dedup falls out of
    (source_content_hash, scoring_profile) at insert time.
  - Two signal subtypes:
      `congress_trade`    — single politician/ticker/date trade.
      `congress_cluster`  — 3+ politicians buying the same ticker in a
                            rolling 7-day window (detected via cache).
  - source_content_hash = sha256:<hex> over
    (politician_name, ticker, trade_date, action, size_bucket).
  - MAX_PAGES reduced from 30 to 20 (configurable via cfg.config.max_pages)
    to stay under the 45s soft budget (20 pages × 1s polite delay = 20s min).
  - No CLI, no OUT_FILE — only scan(cfg).

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from modal_workers.shared.scanner_base import Signal, ScannerResult
from modal_workers.shared.supabase_client import EntityHints, ScannerConfig, SupabaseClient

NAME = "congressional_trading"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CAPITOL_TRADES_URL = "https://www.capitoltrades.com/trades"
REQUEST_TIMEOUT = 15
SCRAPE_DELAY = 1.0  # seconds between pages — be polite (v1 parity)
MAX_PAGES_DEFAULT = 20  # v1 was 30; reduced to fit 45s soft budget
USER_AGENT = "Mozilla/5.0 (compatible; InvestmentResearchBot/1.0; contact: research@example.com)"

# Triage thresholds (v1 parity)
MIN_TRADE_AMOUNT = 5000
UNUSUAL_SIZE_THRESHOLD = 25000

# Cluster detection
CLUSTER_WINDOW_DAYS = 7
CLUSTER_MIN_MEMBERS = 3
CACHE_RETENTION_DAYS = 30  # rolling window kept in cache

logger = logging.getLogger(NAME)


# ---------------------------------------------------------------------------
# Committee-Sector Mapping (verbatim from v1)
# ---------------------------------------------------------------------------

COMMITTEE_SECTOR_MAP: Dict[str, List[str]] = {
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

SECTOR_TICKER_MAP: Dict[str, List[str]] = {
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

MEMBER_COMMITTEES_BY_NAME: Dict[str, List[str]] = {
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
# Size / date / ticker / politician cell parsing (verbatim from v1)
# ---------------------------------------------------------------------------

SIZE_RANGE_MAP: Dict[str, Tuple[float, float]] = {
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
    if not size_str:
        return (0, 0)
    size_str = size_str.strip()
    if size_str in SIZE_RANGE_MAP:
        return SIZE_RANGE_MAP[size_str]
    cleaned = size_str.replace("\u2013", "–").replace(" ", "").strip()
    if cleaned in SIZE_RANGE_MAP:
        return SIZE_RANGE_MAP[cleaned]
    m = re.match(r"(\d+(?:\.\d+)?)\s*([KMB]?)\s*[–\-]\s*(\d+(?:\.\d+)?)\s*([KMB]?)", cleaned)
    if m:
        def _to_num(val: str, suffix: str) -> float:
            n = float(val)
            if suffix == "K":
                return n * 1000
            if suffix == "M":
                return n * 1000000
            if suffix == "B":
                return n * 1000000000
            return n
        return (_to_num(m.group(1), m.group(2)), _to_num(m.group(3), m.group(4)))
    return (0, 0)


def _midpoint_amount(size_str: str) -> float:
    low, high = _parse_size_range(size_str)
    return (low + high) / 2


def _size_bucket(size_str: str) -> str:
    """Stable bucket label for content-hashing. Normalises whitespace + unicode."""
    if not size_str:
        return ""
    return size_str.strip().replace("\u2013", "–").replace(" ", "")


def _parse_trade_date(date_text: str) -> Optional[str]:
    if not date_text:
        return None
    cleaned = re.sub(r"(\w{3})(\d{4})", r"\1 \2", date_text.strip())
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _extract_ticker(issuer_text: str) -> Optional[str]:
    if not issuer_text:
        return None
    m = re.search(r"([A-Z]{1,5})(?:\.[A-Z])?:US", issuer_text)
    if m:
        return m.group(1)
    m = re.search(r"([A-Z]{1,5}):([A-Z]{2})", issuer_text)
    if m:
        return m.group(1)
    return None


def _extract_issuer_name(issuer_text: str) -> str:
    if not issuer_text:
        return ""
    cleaned = re.sub(r"[A-Z]{1,6}:[A-Z]{2}\s*$", "", issuer_text).strip()
    cleaned = re.sub(r"N/A\s*$", "", cleaned).strip()
    return cleaned


def _parse_politician_cell(cell_text: str) -> Dict[str, str]:
    result = {"name": "", "party": "", "chamber": "", "state": ""}
    if not cell_text:
        return result
    text = cell_text.strip()

    m = re.search(r"([A-Z]{2})\s*$", text)
    if m:
        result["state"] = m.group(1)
        text = text[:m.start()].strip()

    for chamber in ("Senate", "House"):
        if chamber in text:
            result["chamber"] = chamber
            text = text.replace(chamber, "", 1).strip()
            break

    for party in ("Democrat", "Republican", "Independent"):
        if party in text:
            result["party"] = party
            text = text.replace(party, "", 1).strip()
            break

    result["name"] = text.strip()
    return result


def _is_purchase(transaction: str) -> bool:
    t = (transaction or "").lower()
    return "buy" in t or "purchase" in t


def _is_sale(transaction: str) -> bool:
    t = (transaction or "").lower()
    return "sell" in t or "sale" in t


def _is_options(transaction: str) -> bool:
    t = (transaction or "").lower()
    return "exchange" in t or "option" in t or "exercise" in t


def _canonical_action(transaction: str) -> str:
    if _is_purchase(transaction):
        return "buy"
    if _is_sale(transaction):
        return "sell"
    if _is_options(transaction):
        return "option"
    return (transaction or "").strip().lower() or "unknown"


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

def _fetch_trades(max_pages: int, budget_deadline: float,
                  warnings: List[str]) -> List[Dict[str, Any]]:
    """Scrape trades from Capitol Trades. Pages are publication-date-sorted,
    so no early-exit by trade date. Stops when the soft budget is blown."""
    all_trades: List[Dict[str, Any]] = []
    headers = {"User-Agent": USER_AGENT}

    last_page = 0
    for page in range(1, max_pages + 1):
        if time.time() > budget_deadline:
            warnings.append(f"wall-clock budget exceeded after page {last_page}")
            break

        url = f"{CAPITOL_TRADES_URL}?page={page}"
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001
            warnings.append(f"fetch page {page}: {type(e).__name__}: {e}")
            break

        try:
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:  # noqa: BLE001
            warnings.append(f"parse page {page}: {type(e).__name__}: {e}")
            break

        table = soup.find("table")
        if not table:
            warnings.append(f"no table on page {page}")
            break

        rows = table.find_all("tr")[1:]  # skip header
        if not rows:
            break

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 8:
                continue

            politician_text = cells[0].get_text(strip=True) if cells[0] else ""
            issuer_text = cells[1].get_text(strip=True) if cells[1] else ""
            traded_date_text = cells[3].get_text(strip=True) if cells[3] else ""
            owner = cells[5].get_text(strip=True) if len(cells) > 5 else ""
            trade_type = cells[6].get_text(strip=True) if len(cells) > 6 else ""
            size_range = cells[7].get_text(strip=True) if len(cells) > 7 else ""

            politician = _parse_politician_cell(politician_text)
            ticker = _extract_ticker(issuer_text)
            issuer_name = _extract_issuer_name(issuer_text)
            traded_date = _parse_trade_date(traded_date_text)

            if not ticker or ticker == "N/A":
                continue

            all_trades.append({
                "politician_name": politician["name"],
                "party": politician["party"],
                "chamber": politician["chamber"],
                "state": politician["state"],
                "ticker": ticker,
                "issuer_name": issuer_name,
                "transaction_date": traded_date or "",
                "owner": owner,
                "transaction": trade_type,
                "size_range": size_range,
            })

        last_page = page
        if page < max_pages:
            time.sleep(SCRAPE_DELAY)

    return all_trades


# ---------------------------------------------------------------------------
# Committee alignment + strength
# ---------------------------------------------------------------------------

def _check_committee_alignment(politician_name: str, ticker: str) -> Optional[str]:
    name_lower = (politician_name or "").lower().strip()
    committees = MEMBER_COMMITTEES_BY_NAME.get(name_lower, [])
    if not committees:
        return None
    ticker_upper = ticker.upper()
    for committee in committees:
        sectors = COMMITTEE_SECTOR_MAP.get(committee, [])
        for sector in sectors:
            if ticker_upper in SECTOR_TICKER_MAP.get(sector, []):
                return committee
    return None


def _estimate_strength(trade: Dict[str, Any], committee_match: Optional[str],
                       amount_mid: float, cluster_count: int) -> int:
    """Ported from v1 _estimate_strength. mcap gate on Q-014 removed (see deviations)."""
    strength = 2

    if committee_match:
        strength = max(strength, 4)

    # Q-014: spouse/child small-dollar Commerce trades — structural noise pattern.
    owner = (trade.get("owner") or "").strip().lower()
    if (committee_match == "Commerce"
            and owner in ("spouse", "child")
            and amount_mid <= 50000):
        strength = 2

    if amount_mid >= 1_000_000:
        strength = max(strength, 5)
    elif amount_mid >= 250_000:
        strength = max(strength, 4)
    elif amount_mid >= UNUSUAL_SIZE_THRESHOLD:
        strength = max(strength, 3)

    if _is_options(trade.get("transaction", "")):
        strength = max(strength, 4)

    if cluster_count >= 3:
        strength = max(strength, 4)
    elif cluster_count >= 2:
        strength = max(strength, 3)

    return min(strength, 5)


# ---------------------------------------------------------------------------
# Cache: rolling 30-day window of recent trades for cross-run cluster detection
# ---------------------------------------------------------------------------

_CACHE_KEY = "recent_trades.json"
_CACHE_PREFIX = "congressional"


def _load_recent_trades(client: SupabaseClient) -> List[Dict[str, Any]]:
    raw = client.read_cache(_CACHE_PREFIX, _CACHE_KEY)
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return []
    if isinstance(data, list):
        return data
    return []


def _save_recent_trades(client: SupabaseClient, trades: List[Dict[str, Any]]) -> None:
    client.write_cache(
        _CACHE_PREFIX, _CACHE_KEY,
        json.dumps(trades).encode("utf-8"),
        content_type="application/json",
    )


def _prune_cache(trades: List[Dict[str, Any]], today: datetime) -> List[Dict[str, Any]]:
    cutoff = (today - timedelta(days=CACHE_RETENTION_DAYS)).strftime("%Y-%m-%d")
    pruned: List[Dict[str, Any]] = []
    for t in trades:
        td = t.get("transaction_date") or ""
        if td and td >= cutoff:
            pruned.append(t)
    return pruned


# ---------------------------------------------------------------------------
# Signal builders
# ---------------------------------------------------------------------------

def _content_hash(politician_name: str, ticker: str, trade_date: str,
                  action: str, size_bucket: str) -> str:
    raw = f"{politician_name}|{ticker}|{trade_date}|{action}|{size_bucket}"
    return f"sha256:{hashlib.sha256(raw.encode()).hexdigest()}"


def _parse_iso_date(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _resolve_figi(ticker: Optional[str]) -> Optional[str]:
    if not ticker:
        return None
    try:
        from modal_workers.shared.openfigi_resolver import resolve_ticker
        res = resolve_ticker(ticker, exch_code="US")
        if res.resolved:
            return res.issuer_figi
    except Exception:
        pass
    return None


def _build_trade_signal(trade: Dict[str, Any], cluster_count: int,
                        scan_date: datetime) -> Optional[Signal]:
    ticker = trade.get("ticker") or ""
    politician = trade.get("politician_name") or ""
    t_date = trade.get("transaction_date") or ""
    if not ticker or not politician or not t_date:
        return None

    amount_mid = _midpoint_amount(trade.get("size_range", ""))
    if amount_mid < MIN_TRADE_AMOUNT:
        return None

    action = _canonical_action(trade.get("transaction", ""))
    size_bucket = _size_bucket(trade.get("size_range", ""))
    content_hash = _content_hash(politician, ticker, t_date, action, size_bucket)
    signal_id = f"congress_{content_hash[7:7 + 32]}"

    source_date = _parse_iso_date(t_date) or scan_date

    committee_match = _check_committee_alignment(politician, ticker)
    strength = _estimate_strength(trade, committee_match, amount_mid, cluster_count)

    signal_flags: List[str] = []
    if committee_match:
        signal_flags.append(f"committee_aligned:{committee_match}")
    if amount_mid >= UNUSUAL_SIZE_THRESHOLD:
        signal_flags.append("unusual_size")
    if cluster_count >= 2:
        signal_flags.append(f"timing_cluster:{cluster_count}_members")
    if _is_options(trade.get("transaction", "")):
        signal_flags.append("options_activity")

    issuer_figi = _resolve_figi(ticker)

    if action == "buy":
        direction = "long"
    elif action == "sell":
        direction = "short"
    else:
        direction = None

    raw_payload: Dict[str, Any] = {
        "representative": politician,
        "party": trade.get("party", ""),
        "chamber": trade.get("chamber", ""),
        "state": trade.get("state", ""),
        "ticker": ticker,
        "company_name": trade.get("issuer_name") or ticker,
        "transaction": trade.get("transaction", ""),
        "action": action,
        "size_range": trade.get("size_range", ""),
        "amount_midpoint": amount_mid,
        "transaction_date": t_date,
        "owner": trade.get("owner", ""),
        "committee_alignment": committee_match,
        "cluster_count": cluster_count,
        "signal_flags": signal_flags,
        "headline": f"{politician} {action} {ticker} ({trade.get('size_range', '')})",
    }

    entity_hints = EntityHints(
        issuer_figi=issuer_figi,
        ticker=ticker,
        mic=None,
        name=trade.get("issuer_name") or ticker,
        country="US",
    )

    return Signal(
        signal_id=signal_id,
        source_content_hash=content_hash,
        source_date=source_date,
        scan_date=scan_date,
        signal_type="congress_trade",
        raw_payload=raw_payload,
        source_url=CAPITOL_TRADES_URL,
        issuer_figi=issuer_figi,
        entity_hints=entity_hints,
        thesis_direction=direction,
        strength_estimate=strength,
    )


def _build_cluster_signal(ticker: str, members: List[Dict[str, Any]],
                          scan_date: datetime) -> Optional[Signal]:
    """Emit a congress_cluster signal: 3+ politicians buying same ticker in 7d."""
    if not members:
        return None
    # Normalise member list for deterministic hashing + latest date for source_date
    members_sorted = sorted(members, key=lambda m: (m.get("transaction_date", ""),
                                                     m.get("politician_name", "")))
    latest_date = max((m.get("transaction_date") or "" for m in members_sorted), default="")
    source_date = _parse_iso_date(latest_date) or scan_date

    raw = "|".join(f"{m.get('politician_name','')}:{m.get('transaction_date','')}"
                   for m in members_sorted)
    content_hash = f"sha256:{hashlib.sha256(f'cluster|{ticker}|{raw}'.encode()).hexdigest()}"
    signal_id = f"congress_cluster_{content_hash[7:7 + 32]}"

    issuer_figi = _resolve_figi(ticker)
    issuer_name = members_sorted[0].get("issuer_name") or ticker

    raw_payload: Dict[str, Any] = {
        "ticker": ticker,
        "company_name": issuer_name,
        "cluster_count": len(members_sorted),
        "window_days": CLUSTER_WINDOW_DAYS,
        "members": [
            {
                "politician": m.get("politician_name", ""),
                "party": m.get("party", ""),
                "chamber": m.get("chamber", ""),
                "state": m.get("state", ""),
                "transaction_date": m.get("transaction_date", ""),
                "size_range": m.get("size_range", ""),
                "owner": m.get("owner", ""),
            }
            for m in members_sorted
        ],
        "headline": f"{len(members_sorted)} Congress members bought {ticker} in {CLUSTER_WINDOW_DAYS}d",
        "signal_flags": [f"timing_cluster:{len(members_sorted)}_members"],
    }

    entity_hints = EntityHints(
        issuer_figi=issuer_figi,
        ticker=ticker,
        mic=None,
        name=issuer_name,
        country="US",
    )

    # Cluster strength: 4 baseline; 5 if >=5 members.
    strength = 5 if len(members_sorted) >= 5 else 4

    return Signal(
        signal_id=signal_id,
        source_content_hash=content_hash,
        source_date=source_date,
        scan_date=scan_date,
        signal_type="congress_cluster",
        raw_payload=raw_payload,
        source_url=CAPITOL_TRADES_URL,
        issuer_figi=issuer_figi,
        entity_hints=entity_hints,
        thesis_direction="long",  # clusters are buy-side by definition here
        strength_estimate=strength,
    )


# ---------------------------------------------------------------------------
# Cluster detection over (fresh + cached) trades
# ---------------------------------------------------------------------------

def _ticker_buy_groups(all_trades: List[Dict[str, Any]],
                       scan_date: datetime) -> Dict[str, List[Dict[str, Any]]]:
    """Bucket purchase trades by ticker, restricted to the rolling
    CLUSTER_WINDOW_DAYS window ending at scan_date. Caller filters by
    distinct-member count for the 2-member strength boost vs 3-member cluster
    signal threshold.
    """
    window_cutoff = (scan_date - timedelta(days=CLUSTER_WINDOW_DAYS)).strftime("%Y-%m-%d")
    by_ticker: Dict[str, List[Dict[str, Any]]] = {}
    for t in all_trades:
        if not _is_purchase(t.get("transaction", "")):
            continue
        td = t.get("transaction_date") or ""
        if td < window_cutoff:
            continue
        ticker = t.get("ticker") or ""
        if not ticker:
            continue
        by_ticker.setdefault(ticker, []).append(t)
    return by_ticker


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    client = SupabaseClient()

    # Route OpenFIGI cache through Supabase Storage.
    try:
        from modal_workers.shared.openfigi_resolver import set_cache_backend
        set_cache_backend(*client.openfigi_cache_backend())
    except Exception:
        pass  # non-fatal — resolver falls back to in-process cache

    scan_date = datetime.now(timezone.utc)
    max_pages = int(cfg.config.get("max_pages", MAX_PAGES_DEFAULT))
    excluded_filers: List[str] = [
        n.lower().strip() for n in cfg.config.get("filter_excluded_filers", ["Ro Khanna"])
    ]

    # Reserve ~5s for cluster detection + signal building + cache write.
    budget = max(10, cfg.timeout_soft_s - 5)
    budget_deadline = time.time() + budget

    warnings: List[str] = []
    fresh_trades = _fetch_trades(max_pages, budget_deadline, warnings)

    # Apply filter_excluded_filers.
    if excluded_filers:
        fresh_trades = [
            t for t in fresh_trades
            if (t.get("politician_name") or "").lower().strip() not in excluded_filers
        ]

    # Load rolling cache + dedupe by (politician, ticker, date, action, size_bucket).
    cached_trades = _load_recent_trades(client)
    seen_keys: set[str] = set()
    merged: List[Dict[str, Any]] = []

    def _dedup_key(t: Dict[str, Any]) -> str:
        return (f"{(t.get('politician_name') or '').lower()}|"
                f"{t.get('ticker','')}|{t.get('transaction_date','')}|"
                f"{_canonical_action(t.get('transaction',''))}|"
                f"{_size_bucket(t.get('size_range',''))}")

    for t in fresh_trades + cached_trades:
        k = _dedup_key(t)
        if k in seen_keys:
            continue
        seen_keys.add(k)
        merged.append(t)

    # Prune to 30-day retention for the saved cache.
    retained = _prune_cache(merged, scan_date)

    # Cluster detection over the merged set (rolling 7-day buy window).
    buy_groups = _ticker_buy_groups(merged, scan_date)
    ticker_cluster_count: Dict[str, int] = {}
    clusters: Dict[str, List[Dict[str, Any]]] = {}
    for ticker, trades in buy_groups.items():
        distinct = len({t.get("politician_name", "")
                        for t in trades if t.get("politician_name")})
        ticker_cluster_count[ticker] = distinct
        if distinct >= CLUSTER_MIN_MEMBERS:
            clusters[ticker] = trades

    # Build signals only for fresh_trades (cached trades were already emitted on prior runs
    # and will hit the (source_content_hash, scoring_profile) unique constraint at insert).
    signals: List[Signal] = []
    for trade in fresh_trades:
        cluster_n = ticker_cluster_count.get(trade.get("ticker", ""), 1)
        sig = _build_trade_signal(trade, cluster_n, scan_date)
        if sig is not None:
            signals.append(sig)

    # Cluster signals (one per ticker meeting threshold).
    for ticker, members in clusters.items():
        sig = _build_cluster_signal(ticker, members, scan_date)
        if sig is not None:
            signals.append(sig)

    # Persist rolling window cache (best-effort).
    try:
        _save_recent_trades(client, retained)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"cache write: {type(e).__name__}: {e}")

    status = "partial" if warnings else "ok"
    if warnings and not signals and not fresh_trades:
        status = "error"

    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=signals,
        warnings=warnings,
        fetched_records=len(fresh_trades),
    )
