"""
Congressional Trading Scanner  (v1.0 — 2026-04-09)
===================================================
Scans US congressional stock trades via Quiver Quantitative API for unusual
activity, particularly committee-aligned trades that may reflect non-public
legislative intelligence.

Data Source:
- Quiver Quantitative: https://api.quiverquant.com/beta/live/congresstrading
- Free, no auth required
- Returns 1,000 most recent trades
- Fields: Representative, BioGuideID, ReportDate, TransactionDate, Ticker,
  Transaction, Range, House, Amount, Party, TickerType, Description,
  ExcessReturn, PriceChange, SPYChange

Usage:
    python congressional_trading.py                  # Run full scan
    python congressional_trading.py --days 14        # Scan last 14 days
    python congressional_trading.py --min-amount 50  # Min $50K trades
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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

QUIVER_URL = "https://api.quiverquant.com/beta/live/congresstrading"
REQUEST_TIMEOUT = 20

# Triage thresholds
MARKET_CAP_FLOOR_MM = 300    # $300M minimum
MIN_TRADE_AMOUNT = 15000     # $15K minimum (filter trivial trades)
UNUSUAL_SIZE_THRESHOLD = 50000  # $50K — flags "unusual size" signal

# Dedup
DEDUP_WINDOW_DAYS = 90

# Output
SIGNALS_DIR = None  # Set at runtime
DEDUP_FILE = None   # Set at runtime

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

# Known committee members (high-profile, partial — expand over time)
MEMBER_COMMITTEES = {
    "W000817": ["Armed Services", "Commerce"],
    "T000476": ["Banking", "Intelligence"],
    "C001098": ["Commerce", "Judiciary"],
    "S000033": ["HELP", "Energy"],
    "W000779": ["Finance", "Intelligence"],
    "C001056": ["Armed Services"],
    "G000359": ["Appropriations", "Judiciary"],
    "C001035": ["Appropriations", "Intelligence"],
    "T000278": ["Health"],
    "C000141": ["Foreign Relations", "HELP"],
    "P000197": ["Appropriations"],
    "G000583": ["Financial Services"],
    "D000632": ["Financial Services", "Homeland Security"],
    "M001157": ["Ways and Means"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_amount_range(range_str):
    """Parse Quiver's Range field into (low, high) dollars."""
    if not range_str:
        return (0, 0)
    clean = range_str.replace("$", "").replace(",", "").strip()
    parts = re.split(r"\s*-\s*", clean)
    try:
        low = float(parts[0])
        high = float(parts[1]) if len(parts) > 1 else low
        return (low, high)
    except (ValueError, IndexError):
        return (0, 0)


def _midpoint_amount(range_str):
    low, high = _parse_amount_range(range_str)
    return (low + high) / 2


def _get_market_cap(ticker):
    if not ticker:
        return None
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        info = stock.info
        mcap = info.get("marketCap")
        if mcap:
            return mcap / 1_000_000
    except Exception as e:
        logger.debug(f"yfinance lookup failed for {ticker}: {e}")
    return None


def _check_committee_alignment(bio_id, ticker):
    """Check if member's committee aligns with traded stock's sector."""
    committees = MEMBER_COMMITTEES.get(bio_id, [])
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


def _is_purchase(transaction):
    t = transaction.lower()
    return "purchase" in t or "buy" in t


def _is_sale(transaction):
    return "sale" in transaction.lower()


def _is_options(transaction):
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


def _signal_hash(bio_id, ticker, transaction_date):
    raw = f"{bio_id}|{ticker}|{transaction_date}"
    return hashlib.md5(raw.encode()).hexdigest()


def _is_novel(bio_id, ticker, transaction_date, dedup_log, window_days=DEDUP_WINDOW_DAYS):
    h = _signal_hash(bio_id, ticker, transaction_date)
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

def _estimate_strength(trade, committee_match, amount_mid, cluster_count):
    strength = 2
    if committee_match:
        strength = max(strength, 4)
    if amount_mid >= 100000:
        strength = max(strength, 4)
    elif amount_mid >= UNUSUAL_SIZE_THRESHOLD:
        strength = max(strength, 3)
    if _is_options(trade.get("Transaction", "")):
        strength = max(strength, 4)
    if cluster_count >= 3:
        strength = max(strength, 4)
    elif cluster_count >= 2:
        strength = max(strength, 3)
    er = trade.get("ExcessReturn")
    if er is not None and er > 5:
        strength = min(strength + 1, 5)
    return strength


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def fetch_trades():
    try:
        resp = requests.get(QUIVER_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"Fetched {len(data)} trades from Quiver Quantitative")
        return data
    except Exception as e:
        logger.error(f"Failed to fetch trades: {e}")
        return []


def scan_trades(days_back=14, min_amount=MIN_TRADE_AMOUNT, market_cap_filter=True):
    trades = fetch_trades()
    if not trades:
        return []

    cutoff_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    dedup_log = _load_dedup_log(DEDUP_FILE)

    # Pre-processing: detect timing clusters
    ticker_traders = {}
    for trade in trades:
        t_date = trade.get("TransactionDate", "")
        if t_date < cutoff_date:
            continue
        ticker = trade.get("Ticker", "")
        if ticker:
            ticker_traders.setdefault(ticker, []).append(trade)

    ticker_cluster_count = {}
    for ticker, trade_list in ticker_traders.items():
        unique_members = set(t.get("BioGuideID", "") for t in trade_list)
        ticker_cluster_count[ticker] = len(unique_members)

    all_signals = []
    mcap_cache = {}

    for trade in trades:
        t_date = trade.get("TransactionDate", "")
        if t_date < cutoff_date:
            continue

        ticker_type = trade.get("TickerType", "")
        if ticker_type and ticker_type.lower() not in ("stock", ""):
            continue

        ticker = trade.get("Ticker", "")
        if not ticker:
            continue

        amount_mid = _midpoint_amount(trade.get("Range", ""))
        if amount_mid < min_amount:
            continue

        bio_id = trade.get("BioGuideID", "")
        representative = trade.get("Representative", "")

        if not _is_novel(bio_id, ticker, t_date, dedup_log):
            continue

        market_cap_mm = 0
        if market_cap_filter:
            if ticker not in mcap_cache:
                mcap_cache[ticker] = _get_market_cap(ticker)
            market_cap_mm = mcap_cache[ticker] or 0
            if 0 < market_cap_mm < MARKET_CAP_FLOOR_MM:
                continue

        committee_match = _check_committee_alignment(bio_id, ticker)
        cluster_count = ticker_cluster_count.get(ticker, 1)
        strength = _estimate_strength(trade, committee_match, amount_mid, cluster_count)

        signal_flags = []
        if committee_match:
            signal_flags.append(f"committee_aligned:{committee_match}")
        if amount_mid >= UNUSUAL_SIZE_THRESHOLD:
            signal_flags.append("unusual_size")
        if cluster_count >= 2:
            signal_flags.append(f"timing_cluster:{cluster_count}_members")
        if _is_options(trade.get("Transaction", "")):
            signal_flags.append("options_activity")
        er = trade.get("ExcessReturn")
        if er is not None and er > 5:
            signal_flags.append(f"high_excess_return:{er:.1f}%")

        signal_type = "congressional_trade"
        if signal_flags:
            signal_type = "congressional_" + "+".join(signal_flags)

        signal = {
            "ticker": ticker,
            "isin": None,
            "company_name": trade.get("Description", "") or ticker,
            "market_cap_mm": round(market_cap_mm, 1) if market_cap_mm else None,
            "signal_type": signal_type,
            "signal_category": "congressional",
            "strength_estimate": strength,
            "source_url": "https://api.quiverquant.com/beta/live/congresstrading",
            "source_date": trade.get("ReportDate", ""),
            "scan_date": today,
            "raw_data": {
                "representative": representative,
                "bio_guide_id": bio_id,
                "party": trade.get("Party", ""),
                "house": trade.get("House", ""),
                "transaction": trade.get("Transaction", ""),
                "range": trade.get("Range", ""),
                "amount_midpoint": amount_mid,
                "transaction_date": t_date,
                "report_date": trade.get("ReportDate", ""),
                "ticker_type": ticker_type,
                "excess_return": trade.get("ExcessReturn"),
                "price_change": trade.get("PriceChange"),
                "spy_change": trade.get("SPYChange"),
                "committee_alignment": committee_match,
                "cluster_count": cluster_count,
                "signal_flags": signal_flags,
            },
        }
        all_signals.append(signal)

        h = _signal_hash(bio_id, ticker, t_date)
        if h not in dedup_log:
            dedup_log[h] = today

    _save_dedup_log(DEDUP_FILE, dedup_log)
    logger.info(f"Congressional scan complete: {len(all_signals)} signals from {len(trades)} trades")
    return all_signals


def run_full_scan(days_back=14, min_amount=MIN_TRADE_AMOUNT,
                  market_cap_filter=True, save_signals=True):
    all_signals = scan_trades(
        days_back=days_back, min_amount=min_amount,
        market_cap_filter=market_cap_filter,
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

    parser = argparse.ArgumentParser(description="Congressional Trading Scanner")
    parser.add_argument("--days", type=int, default=14, help="Days to look back (default: 14)")
    parser.add_argument("--min-amount", type=float, default=MIN_TRADE_AMOUNT,
                        help=f"Min trade midpoint amount (default: ${MIN_TRADE_AMOUNT:,.0f})")
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
    )

    print(f"\n{'=' * 60}")
    print(f"Congressional Trading Scan — {len(signals)} signals found")
    print(f"{'=' * 60}")

    committee_aligned = [s for s in signals if s["raw_data"].get("committee_alignment")]
    unusual_size = [s for s in signals if s["raw_data"]["amount_midpoint"] >= UNUSUAL_SIZE_THRESHOLD]

    if committee_aligned:
        print(f"\n--- Committee-Aligned Trades ({len(committee_aligned)}) ---")
        for s in committee_aligned[:10]:
            rd = s["raw_data"]
            print(f"  [{s['strength_estimate']}] {rd['representative']:30s} | {s['ticker']:6s} | "
                  f"{rd['transaction']:20s} | {rd['range']:25s} | "
                  f"Committee: {rd['committee_alignment']}")

    if unusual_size:
        print(f"\n--- Unusual Size Trades ({len(unusual_size)}) ---")
        for s in unusual_size[:10]:
            rd = s["raw_data"]
            print(f"  [{s['strength_estimate']}] {rd['representative']:30s} | {s['ticker']:6s} | "
                  f"{rd['transaction']:20s} | {rd['range']:25s}")

    clusters = {}
    for s in signals:
        if s["raw_data"]["cluster_count"] >= 2:
            clusters.setdefault(s["ticker"], []).append(s)
    if clusters:
        print(f"\n--- Timing Clusters ({len(clusters)} tickers) ---")
        for ticker, sigs in sorted(clusters.items(), key=lambda x: -len(x[1]))[:5]:
            members = set(s["raw_data"]["representative"] for s in sigs)
            print(f"  {ticker:6s} — {len(members)} members: {', '.join(list(members)[:3])}")

    purchases = sum(1 for s in signals if _is_purchase(s["raw_data"]["transaction"]))
    sales = sum(1 for s in signals if _is_sale(s["raw_data"]["transaction"]))
    print(f"\nPurchases: {purchases} | Sales: {sales}")

    high_strength = [s for s in signals if s["strength_estimate"] >= 4]
    if high_strength:
        print(f"\n--- High Strength (>=4): {len(high_strength)} ---")
        for s in sorted(high_strength, key=lambda x: -x["strength_estimate"])[:10]:
            rd = s["raw_data"]
            flags = ", ".join(rd["signal_flags"]) if rd["signal_flags"] else "base"
            print(f"  [{s['strength_estimate']}] {rd['representative']:30s} | {s['ticker']:6s} | "
                  f"{rd['transaction']:15s} | {flags}")


if __name__ == "__main__":
    main()
