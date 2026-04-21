"""
Convergence Engine  (v1.4 — 2026-04-14 maint-2: restored truncated tail. File was cut mid-statement at "report = gener"; completed to "report = generate_report(convergences)\n    print(report)" + trailing __main__ guard. py_compile PASSED despite the truncation because the partial line was a syntactically valid assignment of an undefined name — meaning py_compile alone is INSUFFICIENT to detect this class of truncation. Maintenance protocol should also check for intended-tail markers like `if __name__ == "__main__":` at EOF.)
=========================================
Reads all signal files from the pipeline, groups by entity (ticker/ISIN),
and detects convergence — multiple independent strategies flagging the same
company within a time window. Convergence signals are the highest-conviction
outputs of the pipeline.

Architecture:
1. Load all signal JSON files from signals/ directory
2. Normalize entity identifiers (ticker, ISIN) to a canonical key
3. Group signals by entity
4. Detect convergence: 2+ distinct strategy categories within 14-day window
5. Score convergence signals
6. Output convergence report + signals

Convergence Types:
- 2-way: Two strategies fire (e.g., EDGAR activist filing + congressional buy)
- 3-way+: Three or more strategies fire (rare, very high conviction)
- Cross-border: ESMA short + US catalyst (e.g., EDGAR filing for dual-listed)

Usage:
    python convergence_engine.py                  # Scan all signals
    python convergence_engine.py --window 21      # 21-day convergence window
    python convergence_engine.py --min-strategies 3  # Require 3+ strategies
    python convergence_engine.py --dry-run        # Print without saving
"""

import json
import os
import glob
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple, Set
from collections import defaultdict

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONVERGENCE_WINDOW_DAYS = 14
MIN_STRATEGIES = 2
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SCRIPT_DIR)
SIGNALS_DIR = os.path.join(_PROJECT_DIR, "signals")
OUTPUT_DIR = os.path.join(_PROJECT_DIR, "signals")

logger = logging.getLogger("convergence_engine")

# ---------------------------------------------------------------------------
# Convergence Suppression List
# ---------------------------------------------------------------------------
# Tickers that repeatedly appear as false-positive convergences due to high
# signal volume across strategies (e.g., mega-caps with routine congressional
# trades + frequent EDGAR filings). These are suppressed from convergence
# alerts but still logged in the raw signal data. Add tickers here with a
# reason string for audit trail.
#
# Format: { "TICKER": "reason for suppression — session that added it" }

CONVERGENCE_SUPPRESS = {
    "AMT": "Mega-cap REIT with routine congressional trades + frequent SEC filings. "
           "False convergence every scan cycle. Added S38.",
    "FBLG": "Sub-floor micro-cap ($6.2M mcap, below $215M minimum). Recurring false-positive "
            "convergence across S50-S55 (6 consecutive sessions). Added S55 (2026-04-14).",
}


# ---------------------------------------------------------------------------
# Directional Classification
# ---------------------------------------------------------------------------

# Map signal_type patterns to directional bias: "bullish", "bearish", "neutral"
# This prevents false convergences between opposing signals (e.g., insider buy
# + short position increase on the same entity).

BEARISH_PATTERNS = {
    "short_new_position", "short_position_increase", "short_large_position",
    "short_crowded_short",
    "distress_keyword", "going_concern",
    "insider_sell", "congressional_sell",
    "governance_keyword",  # Governance signals (poison pills, bylaw changes) typically indicate distress or activist defense
}

BULLISH_PATTERNS = {
    "short_position_decrease", "short_covering",
    "mna_keyword", "activist_keyword", "tender_offer",
    "insider_buy", "congressional_buy", "congressional_purchase",
    "contract_award", "contract_new_award", "contract_modification", "new_contract",
    "fda_approval", "pdufa_upcoming", "breakthrough_therapy",
    "congressional_trade",  # Default trade direction (most congressional trades are purchases)
}

# Everything else is neutral (contributes to convergence in either direction)


def classify_direction(signal: dict) -> str:
    """Classify a signal as bullish, bearish, or neutral based on signal_type."""
    stype = (signal.get("signal_type") or "").lower().strip()
    if stype in BEARISH_PATTERNS:
        return "bearish"
    if stype in BULLISH_PATTERNS:
        return "bullish"
    # Check partial matches for compound signal types
    for pat in BEARISH_PATTERNS:
        if pat in stype:
            return "bearish"
    for pat in BULLISH_PATTERNS:
        if pat in stype:
            return "bullish"
    return "neutral"


# ---------------------------------------------------------------------------
# Signal Loading
# ---------------------------------------------------------------------------

def _load_ticker_cache(signals_dir: str) -> Dict[str, str]:
    """Load the ESMA ticker cache for ISIN→ticker enrichment."""
    cache_path = os.path.join(signals_dir, "esma_ticker_cache.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                cache = json.load(f)
            return {k: v for k, v in cache.items() if v}  # skip nulls
        except Exception:
            pass
    return {}


def _enrich_tickers(signals: List[dict], ticker_cache: Dict[str, str]) -> int:
    """Enrich signals that have ISIN but no ticker using the ticker cache.

    Returns count of newly enriched signals.
    """
    enriched = 0
    for sig in signals:
        ticker = (sig.get("ticker") or "").strip()
        if ticker:
            continue  # already has ticker
        isin = (sig.get("isin") or "").strip()
        if isin and isin in ticker_cache:
            sig["ticker"] = ticker_cache[isin]
            enriched += 1
    return enriched


def load_all_signals(signals_dir: str) -> List[dict]:
    """Load all signal JSON files from the signals directory.

    Skips dedup files, watchlist files, snapshot directories, rotation state,
    and ticker cache files. Enriches ISIN-only signals with ticker cache.
    Returns flat list of all signal dicts.
    """
    if not signals_dir or not os.path.exists(signals_dir):
        logger.warning(f"Signals directory not found: {signals_dir}")
        return []

    all_signals = []
    skip_keywords = ("dedup", "watchlist", "snapshot", "rotation", "cache",
                     "_scanner_result", "convergence", "pdufa_watchlist")

    for filepath in glob.glob(os.path.join(signals_dir, "*.json")):
        basename = os.path.basename(filepath)

        # Skip non-signal files
        if any(kw in basename for kw in skip_keywords):
            continue

        try:
            with open(filepath) as f:
                data = json.load(f)
            if isinstance(data, list):
                for sig in data:
                    sig["_source_file"] = basename
                all_signals.extend(data)
            elif isinstance(data, dict):
                # Skip dict-format files that aren't signals (e.g. convergence results)
                if "signals" in data:
                    # Scanner result format — extract signals list
                    for sig in data.get("signals", []):
                        sig["_source_file"] = basename
                    all_signals.extend(data.get("signals", []))
                elif "signal_category" in data:
                    data["_source_file"] = basename
                    all_signals.append(data)
        except Exception as e:
            logger.warning(f"Failed to load {filepath}: {e}")

    logger.info(f"Loaded {len(all_signals)} signals from {signals_dir}")

    # Enrich ISIN-only signals with ticker cache
    ticker_cache = _load_ticker_cache(signals_dir)
    if ticker_cache:
        enriched = _enrich_tickers(all_signals, ticker_cache)
        if enriched:
            logger.info(f"Enriched {enriched} signals with tickers from cache")

    return all_signals


# ---------------------------------------------------------------------------
# Entity Resolution / Grouping
# ---------------------------------------------------------------------------

def _entity_key(signal: dict) -> Optional[str]:
    """Extract canonical entity key from a signal.

    Priority: ticker > ISIN > company_name (normalized).
    Returns None if no usable key.
    """
    ticker = signal.get("ticker", "").strip().upper()
    if ticker:
        return f"T:{ticker}"

    isin = signal.get("isin", "")
    if isin and isinstance(isin, str) and len(isin) == 12:
        return f"I:{isin}"

    company = signal.get("company_name", "").strip().upper()
    if company:
        # Normalize: remove common suffixes (longer first to avoid partial matches)
        for suffix in [" CORPORATION", " HOLDINGS", " INC.", " INC",
                       " LLC", " LTD", " PLC", " CORP", " CO.",
                       " CO", " GROUP", ",", "."]:
            company = company.replace(suffix, "")
        company = company.strip()
        if company:
            return f"C:{company}"

    return None


def group_by_entity(signals: List[dict]) -> Dict[str, List[dict]]:
    """Group signals by canonical entity key.

    Returns dict of entity_key -> list of signals.
    """
    groups: Dict[str, List[dict]] = defaultdict(list)

    for sig in signals:
        key = _entity_key(sig)
        if key:
            groups[key].append(sig)

    logger.info(f"Grouped into {len(groups)} entities")
    return dict(groups)


# ---------------------------------------------------------------------------
# Convergence Detection
# ---------------------------------------------------------------------------

def _parse_signal_date(signal: dict) -> Optional[datetime]:
    """Extract the most relevant date from a signal."""
    for field in ["source_date", "scan_date"]:
        date_str = signal.get(field, "")
        if date_str:
            try:
                return datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                continue
    return None


def detect_convergence(groups: Dict[str, List[dict]],
                       window_days: int = CONVERGENCE_WINDOW_DAYS,
                       min_strategies: int = MIN_STRATEGIES) -> List[dict]:
    """Detect convergence across signal groups.

    For each entity, check if signals from 2+ distinct strategy categories
    fall within the convergence window.

    Returns list of convergence result dicts.
    """
    convergences = []

    suppressed_count = 0
    for entity_key, signals in groups.items():
        if len(signals) < 2:
            continue

        # Check suppression list — extract ticker from entity_key
        # entity_key format is typically "T:TICKER" or "I:ISIN"
        _suppress_ticker = entity_key.split(":")[-1].strip().upper() if ":" in entity_key else entity_key.strip().upper()
        if _suppress_ticker in CONVERGENCE_SUPPRESS:
            suppressed_count += 1
            logger.info(f"Suppressed convergence for {entity_key}: {CONVERGENCE_SUPPRESS[_suppress_ticker]}")
            continue

        # Get distinct categories
        categories = set()
        for sig in signals:
            cat = sig.get("signal_category", "")
            if cat:
                categories.add(cat)

        if len(categories) < min_strategies:
            continue

        # Check temporal window
        dates = []
        for sig in signals:
            d = _parse_signal_date(sig)
            if d:
                dates.append(d)

        if len(dates) < 2:
            # No temporal data — still flag if multiple categories present
            pass
        else:
            dates.sort()
            # Check if any pair of signals from different categories is within window
            in_window = False
            for i, sig_i in enumerate(signals):
                for j, sig_j in enumerate(signals):
                    if j <= i:
                        continue
                    cat_i = sig_i.get("signal_category", "")
                    cat_j = sig_j.get("signal_category", "")
                    if cat_i == cat_j:
                        continue
                    d_i = _parse_signal_date(sig_i)
                    d_j = _parse_signal_date(sig_j)
                    if d_i and d_j:
                        if abs((d_i - d_j).days) <= window_days:
                            in_window = True
                            break
                if in_window:
                    break

            if not in_window and len(dates) >= 2:
                continue  # Outside temporal window

        # --- Directional analysis ---
        directions = {"bullish": [], "bearish": [], "neutral": []}
        for sig in signals:
            d = classify_direction(sig)
            sig["_direction"] = d
            directions[d].append(sig)

        has_bullish = len(directions["bullish"]) > 0
        has_bearish = len(directions["bearish"]) > 0

        if has_bullish and has_bearish:
            convergence_type = "conflicting"
        elif has_bullish:
            convergence_type = "bullish"
        elif has_bearish:
            convergence_type = "bearish"
        else:
            convergence_type = "neutral"

        # Build convergence record
        ticker = ""
        isin = ""
        company_name = ""
        max_market_cap = 0

        for sig in signals:
            if not ticker:
                ticker = sig.get("ticker", "")
            if not isin and sig.get("isin"):
                isin = sig.get("isin", "")
            if not company_name:
                company_name = sig.get("company_name", "")
            mcap = sig.get("market_cap_mm") or 0
            if mcap > max_market_cap:
                max_market_cap = mcap

        # Score convergence
        n_categories = len(categories)
        max_strength = max(sig.get("strength_estimate", 0) for sig in signals)
        avg_strength = sum(sig.get("strength_estimate", 0) for sig in signals) / len(signals)

        # Convergence strength: base = max signal strength + bonus per additional category
        convergence_strength = min(max_strength + (n_categories - 1), 5)

        # Convergence score (for ranking)
        score = (n_categories * 10) + (avg_strength * 5) + max_strength

        # Conflicting convergences get a penalty — they need human review
        # but are still flagged because the disagreement itself is informative
        if convergence_type == "conflicting":
            score *= 0.7  # 30% penalty

        convergence = {
            "entity_key": entity_key,
            "ticker": ticker,
            "isin": isin,
            "company_name": company_name,
            "market_cap_mm": round(max_market_cap, 1) if max_market_cap else None,
            "n_strategies": n_categories,
            "categories": sorted(categories),
            "n_signals": len(signals),
            "convergence_type": convergence_type,
            "direction_summary": {
                "bullish": len(directions["bullish"]),
                "bearish": len(directions["bearish"]),
                "neutral": len(directions["neutral"]),
            },
            "convergence_strength": convergence_strength,
            "convergence_score": round(score, 1),
            "signals": signals,
            "detected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        convergences.append(convergence)

    # Sort by score descending
    convergences.sort(key=lambda x: -x["convergence_score"])
    logger.info(f"Detected {len(convergences)} convergences (suppressed {suppressed_count})")
    return convergences


# ---------------------------------------------------------------------------
# Report Generation
# ---------------------------------------------------------------------------

def generate_report(convergences: List[dict]) -> str:
    """Generate a human-readable convergence report."""
    lines = []
    lines.append(f"{'=' * 70}")
    lines.append(f"CONVERGENCE REPORT — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"{'=' * 70}")
    lines.append(f"Total convergences: {len(convergences)}")
    lines.append("")

    for i, c in enumerate(convergences, 1):
        ticker = c.get("ticker") or c.get("isin") or c["entity_key"]
        company = c.get("company_name", "")[:35]
        n_strat = c["n_strategies"]
        n_sig = c["n_signals"]
        score = c["convergence_score"]
        strength = c["convergence_strength"]
        mcap = c.get("market_cap_mm")
        mcap_str = f"${mcap:,.0f}M" if mcap else "N/A"

        ctype = c.get("convergence_type", "unknown").upper()
        dir_sum = c.get("direction_summary", {})
        dir_str = f"B:{dir_sum.get('bullish',0)} R:{dir_sum.get('bearish',0)} N:{dir_sum.get('neutral',0)}"

        lines.append(f"#{i}. [{strength}] {ticker} — {company}  [{ctype}]")
        lines.append(f"    Score: {score} | Strategies: {n_strat} | Signals: {n_sig} | MCap: {mcap_str}")
        lines.append(f"    Categories: {', '.join(c['categories'])} | Direction: {dir_str}")

        # Brief per-signal summary
        for sig in c["signals"]:
            stype = sig.get("signal_type", "?")
            sdate = sig.get("source_date", "?")
            sstr = sig.get("strength_estimate", 0)
            lines.append(f"      - [{sstr}] {stype} ({sdate})")

        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main Scan
# ---------------------------------------------------------------------------

def run_convergence(signals_dir: str = None,
                    window_days: int = CONVERGENCE_WINDOW_DAYS,
                    min_strategies: int = MIN_STRATEGIES,
                    save_output: bool = True) -> List[dict]:
    """Run convergence detection on all signals.

    Returns list of convergence dicts.
    """
    sdir = signals_dir or SIGNALS_DIR
    if not sdir:
        logger.error("No signals directory specified")
        return []

    # Load all signals
    signals = load_all_signals(sdir)
    if not signals:
        logger.warning("No signals to process")
        return []

    # Group by entity
    groups = group_by_entity(signals)

    # Detect convergence
    convergences = detect_convergence(groups, window_days, min_strategies)

    # Save output
    if save_output and OUTPUT_DIR:
        os.makedirs(OUTPUT_DIR, exist_ok=True)

        # Save convergence data
        data_file = os.path.join(
            OUTPUT_DIR,
            f"convergence_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        )
        # Strip internal fields from signals before saving
        clean = []
        for c in convergences:
            cc = dict(c)
            cc["signals"] = [{k: v for k, v in s.items() if not k.startswith("_")}
                             for s in c["signals"]]
            clean.append(cc)
        with open(data_file, "w") as f:
            json.dump(clean, f, indent=2)
        logger.info(f"Saved convergence data: {data_file}")

        # Save report
        report = generate_report(convergences)
        report_file = os.path.join(
            OUTPUT_DIR,
            f"convergence_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
        )
        with open(report_file, "w") as f:
            f.write(report)
        logger.info(f"Saved report: {report_file}")

    return convergences


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Convergence Engine")
    parser.add_argument("--window", type=int, default=CONVERGENCE_WINDOW_DAYS,
                        help=f"Convergence window in days (default: {CONVERGENCE_WINDOW_DAYS})")
    parser.add_argument("--min-strategies", type=int, default=MIN_STRATEGIES,
                        help=f"Minimum distinct strategies (default: {MIN_STRATEGIES})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print without saving")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    global SIGNALS_DIR, OUTPUT_DIR
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    SIGNALS_DIR = os.path.join(project_dir, "signals")
    OUTPUT_DIR = os.path.join(project_dir, "candidates")

    convergences = run_convergence(
        window_days=args.window,
        min_strategies=args.min_strategies,
        save_output=not args.dry_run,
    )

    report = generate_report(convergences)
    print(report)


if __name__ == "__main__":
    main()

# --- END OF FILE ---
