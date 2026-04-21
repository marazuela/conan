"""
Single Scanner Runner  (v1.1 — 2026-04-10)
==========================================
Runs ONE scanner at a time, with a hard process-level timeout.
Designed for Cowork sessions where bash has a 45s timeout limit.

Usage:
    python run_scanner.py edgar           # Run EDGAR scanner
    python run_scanner.py congressional   # Run Congressional scanner
    python run_scanner.py esma_short      # Run ESMA short scanner
    python run_scanner.py contract        # Run contract monitor
    python run_scanner.py fda_pdufa       # Run FDA PDUFA scanner
    python run_scanner.py --list          # List available scanners

Each scanner writes its signals to signals/ as JSON.
After running all scanners, use run_post_scan.py for convergence + report.
"""

import json
import os
import sys
import time
import argparse
import logging

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
SIGNALS_DIR = os.path.join(PROJECT_DIR, "signals")

if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_scanner")

# ---------------------------------------------------------------------------
# Scanner definitions (mirrors pipeline_runner.py registry)
# ---------------------------------------------------------------------------

SCANNERS = {
    "edgar": {
        "module": "edgar_filing_monitor",
        "function": "run_full_scan",
        "kwargs": {"days_back": 2, "market_cap_filter": True, "save_signals": True},
        "description": "SEC EDGAR keyword & filing scan",
    },
    "esma_short": {
        "module": "esma_short_scanner",
        "function": "run_scan",
        "kwargs": {"market_cap_filter": True, "save_signals": True, "resolve_tickers": True},
        "description": "FCA/ESMA short position aggregation",
    },
    "congressional": {
        "module": "congressional_trading",
        "function": "run_full_scan",
        "kwargs": {"days_back": 45, "market_cap_filter": True, "save_signals": True},
        "description": "Congressional trading via Capitol Trades",
    },
    "contract": {
        "module": "contract_monitor",
        "function": "run_scan",
        "kwargs": {"market_cap_filter": True, "save_signals": True},
        "description": "USAspending.gov contract awards",
    },
    "fda_pdufa": {
        "module": "fda_pdufa_pipeline",
        "function": "run_scan",
        "kwargs": {"market_cap_filter": True, "save_signals": True},
        "description": "FDA PDUFA calendar pipeline",
    },
}


def run_one(name: str, no_market_cap: bool = False, rotate: bool = False) -> int:
    """Run a single scanner. Returns 0 on success, 1 on failure."""
    if name not in SCANNERS:
        print(f"Unknown scanner: {name}")
        print(f"Available: {', '.join(SCANNERS.keys())}")
        return 1

    entry = SCANNERS[name]
    kwargs = dict(entry["kwargs"])

    if no_market_cap and "market_cap_filter" in kwargs:
        kwargs["market_cap_filter"] = False

    logger.info(f"Running {name}: {entry['description']}")
    t0 = time.time()

    try:
        mod = __import__(entry["module"])

        # EDGAR rotation mode: scan one category per run (cycles across runs)
        if name == "edgar" and rotate:
            category = mod.get_next_rotation_category()
            kw_signals = mod.scan_keywords(
                categories=[category],
                days_back=kwargs.get("days_back", 2),
                market_cap_filter=kwargs.get("market_cap_filter", True),
            )
            type_signals = mod.scan_filing_types(
                days_back=kwargs.get("days_back", 2),
                market_cap_filter=kwargs.get("market_cap_filter", True),
            )
            signals = (kw_signals or []) + (type_signals or [])
        else:
            fn = getattr(mod, entry["function"])
            signals = fn(**kwargs)

        if signals is None:
            signals = []
        if not isinstance(signals, list):
            signals = [signals]

        elapsed = time.time() - t0
        logger.info(f"  {name}: {len(signals)} signals in {elapsed:.1f}s")

        # Save summary to a scanner result file for post-scan aggregation
        result_file = os.path.join(SIGNALS_DIR, f"_scanner_result_{name}.json")
        os.makedirs(SIGNALS_DIR, exist_ok=True)
        with open(result_file, "w") as f:
            json.dump({
                "name": name,
                "success": True,
                "signal_count": len(signals),
                "duration_s": round(elapsed, 1),
                "error": None,
                "signals": signals,
            }, f, indent=2, default=str)

        print(f"OK: {name} — {len(signals)} signals in {elapsed:.1f}s")
        return 0

    except Exception as e:
        elapsed = time.time() - t0
        error_msg = f"{type(e).__name__}: {e}"
        logger.error(f"  {name} FAILED: {error_msg}")

        # Save failure result
        result_file = os.path.join(SIGNALS_DIR, f"_scanner_result_{name}.json")
        os.makedirs(SIGNALS_DIR, exist_ok=True)
        with open(result_file, "w") as f:
            json.dump({
                "name": name,
                "success": False,
                "signal_count": 0,
                "duration_s": round(elapsed, 1),
                "error": error_msg,
                "signals": [],
            }, f, indent=2, default=str)

        print(f"FAIL: {name} — {error_msg}")
        return 1



def main():
    parser = argparse.ArgumentParser(description="Run a single scanner.")
    parser.add_argument("scanner", nargs="?", help="Scanner name to run")
    parser.add_argument("--list", action="store_true", help="List available scanners")
    parser.add_argument("--no-market-cap", action="store_true",
                        help="Disable $215M market cap filter")
    parser.add_argument("--rotate", action="store_true",
                        help="EDGAR only: use category rotation (one category per run)")
    args = parser.parse_args()

    if args.list or not args.scanner:
        print("Available scanners:")
        for name, entry in SCANNERS.items():
            print(f"  {name:20s}  {entry['description']}")
        return 0

    return run_one(args.scanner, no_market_cap=args.no_market_cap, rotate=args.rotate)


if __name__ == "__main__":
    sys.exit(main())

# --- END OF FILE ---
