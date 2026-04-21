"""
Pipeline Runner  (v1.1 — 2026-04-09)
=====================================
Orchestrates the full daily investment discovery pipeline:

  1. Run all 5 scanner tools (error-isolated)
  2. Normalize signals via OpenFIGI
  3. Run convergence engine
  4. Generate daily report

Each scanner runs independently — a failure in one does not block others.
Signals are aggregated, normalized, checked for convergence, and summarized
in a daily markdown report saved to reports/.

Usage:
    python pipeline_runner.py                   # Full daily scan
    python pipeline_runner.py --skip edgar      # Skip one scanner
    python pipeline_runner.py --scanners-only   # Run scanners only, no report
    python pipeline_runner.py --dry-run         # Print, don't save report
    python pipeline_runner.py -v                # Verbose logging
"""

import json
import os
import sys
import time
import logging
import argparse
import traceback
import subprocess
import tempfile
from datetime import datetime, date
from typing import List, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Path setup — works from any CWD
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
SIGNALS_DIR = os.path.join(PROJECT_DIR, "signals")
REPORTS_DIR = os.path.join(PROJECT_DIR, "reports")
CANDIDATES_DIR = os.path.join(PROJECT_DIR, "candidates")

# Ensure tools/ is on the path so we can import sibling modules
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

logger = logging.getLogger("pipeline_runner")


# ---------------------------------------------------------------------------
# Scanner Registry
# ---------------------------------------------------------------------------
# Each entry: (name, module_name, function_name, default_kwargs)
# function_name is the main scan entry point in each module.

SCANNER_REGISTRY = [
    {
        "name": "edgar",
        "module": "edgar_filing_monitor",
        "function": "run_full_scan",
        "kwargs": {"days_back": 2, "market_cap_filter": True, "save_signals": True},
        "description": "SEC EDGAR keyword & filing scan",
    },
    {
        "name": "esma_short",
        "module": "esma_short_scanner",
        "function": "run_scan",
        "kwargs": {"market_cap_filter": True, "save_signals": True, "resolve_tickers": True},
        "description": "FCA/ESMA short position aggregation",
    },
    {
        "name": "congressional",
        "module": "congressional_trading",
        "function": "run_full_scan",
        "kwargs": {"days_back": 45, "market_cap_filter": True, "save_signals": True},
        "description": "Congressional trading via Capitol Trades",
    },
    {
        "name": "contract",
        "module": "contract_monitor",
        "function": "run_scan",
        "kwargs": {"market_cap_filter": True, "save_signals": True},
        "description": "USAspending.gov contract awards",
    },
    {
        "name": "fda_pdufa",
        "module": "fda_pdufa_pipeline",
        "function": "run_scan",
        "kwargs": {"market_cap_filter": True, "save_signals": True},
        "description": "FDA PDUFA calendar pipeline",
    },
]


# ---------------------------------------------------------------------------
# Scanner Execution (Error-Isolated)
# ---------------------------------------------------------------------------

class ScannerResult:
    """Result from a single scanner run."""
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self.signals: List[dict] = []
        self.success: bool = False
        self.error: Optional[str] = None
        self.duration_s: float = 0.0
        self.skipped: bool = False

    @property
    def signal_count(self) -> int:
        return len(self.signals)

    def summary(self) -> str:
        if self.skipped:
            return f"  {self.name:20s}  SKIPPED"
        status = "OK" if self.success else "FAIL"
        return (
            f"  {self.name:20s}  {status:4s}  "
            f"{self.signal_count:3d} signals  "
            f"{self.duration_s:5.1f}s"
            + (f"  ERROR: {self.error}" if self.error else "")
        )


# Per-scanner timeout in seconds. If a scanner exceeds this, its subprocess
# is killed. Prevents a hung API from blocking the entire pipeline.
SCANNER_TIMEOUT_S = 120


def run_scanner(entry: dict, skip_list: List[str]) -> ScannerResult:
    """Run a single scanner in a **subprocess** with a hard timeout.

    Each scanner executes as an isolated Python process.  If it hangs
    (network timeout, unresponsive API), the subprocess is killed after
    SCANNER_TIMEOUT_S seconds without affecting the rest of the pipeline.
    """
    result = ScannerResult(entry["name"], entry["description"])

    if entry["name"] in skip_list:
        result.skipped = True
        logger.info(f"Skipping scanner: {entry['name']}")
        return result

    logger.info(f"Running scanner: {entry['name']} ({entry['description']})")
    t0 = time.time()

    # Build a small Python script that imports and runs the scanner,
    # writing its signals to a temporary JSON file.
    kwargs_json = json.dumps(entry["kwargs"])

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, dir=SIGNALS_DIR
    ) as tmp:
        tmp_path = tmp.name

    script = f"""
import json, sys, os
sys.path.insert(0, {repr(SCRIPT_DIR)})
try:
    mod = __import__({repr(entry["module"])})
    fn = getattr(mod, {repr(entry["function"])})
    kwargs = json.loads({repr(kwargs_json)})
    signals = fn(**kwargs)
    if signals is None:
        signals = []
    if not isinstance(signals, list):
        signals = [signals]
    with open({repr(tmp_path)}, "w") as f:
        json.dump(signals, f, default=str)
    print(f"OK:{{len(signals)}}")
except Exception as e:
    import traceback
    traceback.print_exc()
    print(f"FAIL:{{type(e).__name__}}: {{e}}")
    sys.exit(1)
"""

    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=SCANNER_TIMEOUT_S,
            env={**os.environ, "PYTHONPATH": SCRIPT_DIR},
        )

        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()

        if proc.returncode == 0 and stdout.startswith("OK:"):
            # Read signals from temp file
            with open(tmp_path, "r") as f:
                result.signals = json.load(f)
            result.success = True
            logger.info(f"  {entry['name']}: {len(result.signals)} signals")
        else:
            # Scanner reported failure or non-zero exit
            last_line = stdout.split("\n")[-1] if stdout else ""
            result.error = last_line if last_line.startswith("FAIL:") else (
                stderr[-500:] if stderr else f"exit code {proc.returncode}"
            )
            logger.error(f"  {entry['name']} FAILED: {result.error}")

    except subprocess.TimeoutExpired:
        result.error = f"Timeout after {SCANNER_TIMEOUT_S}s — subprocess killed"
        logger.error(f"  {entry['name']} TIMED OUT after {SCANNER_TIMEOUT_S}s")

    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        logger.error(f"  {entry['name']} FAILED: {result.error}")
        logger.debug(traceback.format_exc())

    finally:
        # Clean up temp file
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    result.duration_s = time.time() - t0
    return result


def run_all_scanners(skip_list: List[str] = None) -> List[ScannerResult]:
    """Run all registered scanners sequentially with error isolation."""
    skip_list = skip_list or []
    results = []
    for entry in SCANNER_REGISTRY:
        result = run_scanner(entry, skip_list)
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Signal Normalization (OpenFIGI)
# ---------------------------------------------------------------------------

def normalize_signals(signals: List[dict]) -> List[dict]:
    """Pass signals through OpenFIGI entity resolver.

    Adds canonical FIGI, name, ticker, and exchange code to each signal.
    Non-critical: if resolution fails, signals pass through unchanged.
    """
    if not signals:
        return signals

    try:
        from openfigi_resolver import normalize_signals as figi_normalize
        normalized = figi_normalize(signals)
        resolved_count = sum(1 for s in normalized if s.get("composite_figi"))
        logger.info(f"OpenFIGI: resolved {resolved_count}/{len(signals)} signals")
        return normalized
    except Exception as e:
        logger.warning(f"OpenFIGI normalization failed (non-fatal): {e}")
        return signals


# ---------------------------------------------------------------------------
# Convergence Detection
# ---------------------------------------------------------------------------

def run_convergence_check(save_output: bool = True) -> Tuple[List[dict], Optional[str]]:
    """Run the convergence engine on all signals in the signals directory.

    Returns (convergence_records, report_path_or_none).
    """
    try:
        from convergence_engine import run_convergence
        convergences = run_convergence(
            signals_dir=SIGNALS_DIR,
            save_output=save_output,
        )
        logger.info(f"Convergence engine: {len(convergences)} convergent entities found")
        return convergences, None
    except Exception as e:
        logger.error(f"Convergence engine failed: {e}")
        logger.debug(traceback.format_exc())
        return [], str(e)


# ---------------------------------------------------------------------------
# Daily Report Generation
# ---------------------------------------------------------------------------

def generate_daily_report(
    scanner_results: List[ScannerResult],
    all_signals: List[dict],
    convergences: List[dict],
    convergence_error: Optional[str],
    report_date: str,
    dry_run: bool = False,
) -> Optional[str]:
    """Generate a daily markdown report and save to reports/.

    Returns the file path if saved, None if dry_run.
    """
    lines = []
    lines.append(f"# Daily Signal Report — {report_date}")
    lines.append("")
    lines.append(f"**Generated**: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"**Pipeline version**: v1.1")
    lines.append("")

    # ---- Executive Summary ----
    total_signals = sum(r.signal_count for r in scanner_results)
    active_scanners = sum(1 for r in scanner_results if r.success)
    failed_scanners = [r for r in scanner_results if not r.success and not r.skipped]
    skipped_scanners = [r for r in scanner_results if r.skipped]

    lines.append("## Executive Summary")
    lines.append("")
    lines.append(f"- **Total signals detected**: {total_signals}")
    lines.append(f"- **Active scanners**: {active_scanners}/{len(scanner_results)}")
    if failed_scanners:
        lines.append(f"- **Failed scanners**: {', '.join(r.name for r in failed_scanners)}")
    if skipped_scanners:
        lines.append(f"- **Skipped scanners**: {', '.join(r.name for r in skipped_scanners)}")
    lines.append(f"- **Convergent entities**: {len(convergences)}")
    lines.append("")

    # ---- Scanner Results ----
    lines.append("## Scanner Results")
    lines.append("")
    lines.append("| Scanner | Status | Signals | Time (s) | Notes |")
    lines.append("|---------|--------|---------|----------|-------|")
    for r in scanner_results:
        if r.skipped:
            lines.append(f"| {r.name} | SKIPPED | — | — | User-skipped |")
        elif r.success:
            lines.append(f"| {r.name} | OK | {r.signal_count} | {r.duration_s:.1f} | |")
        else:
            lines.append(f"| {r.name} | FAIL | 0 | {r.duration_s:.1f} | {r.error or 'Unknown'} |")
    lines.append("")

    # ---- Signals by Strategy ----
    lines.append("## Signals by Strategy")
    lines.append("")

    # Group all_signals by signal_category
    by_category: Dict[str, List[dict]] = {}
    for sig in all_signals:
        cat = sig.get("signal_category", sig.get("source", "unknown"))
        by_category.setdefault(cat, []).append(sig)

    if by_category:
        for cat, sigs in sorted(by_category.items()):
            lines.append(f"### {cat.replace('_', ' ').title()} ({len(sigs)} signals)")
            lines.append("")
            # Show up to 10 signals per category, summarized
            for i, sig in enumerate(sigs[:10]):
                ticker = sig.get("ticker", "???")
                company = sig.get("company_name", "Unknown")
                sig_type = sig.get("signal_type", "unknown")
                strength = sig.get("strength_estimate", "?")
                source_date = sig.get("source_date", "?")
                lines.append(
                    f"- **{ticker}** ({company}) — {sig_type} — "
                    f"strength {strength} — {source_date}"
                )
            if len(sigs) > 10:
                lines.append(f"- ... and {len(sigs) - 10} more")
            lines.append("")
    else:
        lines.append("*No signals detected this scan.*")
        lines.append("")

    # ---- Convergence Alerts ----
    lines.append("## Convergence Alerts")
    lines.append("")

    if convergence_error:
        lines.append(f"**Convergence engine error**: {convergence_error}")
        lines.append("")
    elif convergences:
        lines.append(
            f"**{len(convergences)} convergent entities detected** "
            f"— these are the highest-priority signals."
        )
        lines.append("")
        for conv in convergences:
            entity = conv.get("entity_key", conv.get("ticker", "???"))
            strategies = conv.get("strategies", [])
            signal_count = conv.get("signal_count", len(conv.get("signals", [])))
            score_bonus = conv.get("convergence_bonus", 0)
            lines.append(
                f"### {entity}"
            )
            lines.append(f"- **Strategies**: {', '.join(strategies)}")
            lines.append(f"- **Signal count**: {signal_count}")
            lines.append(f"- **Convergence bonus**: +{score_bonus}")
            # List individual signals
            for sig in conv.get("signals", [])[:5]:
                ticker = sig.get("ticker", "???")
                sig_type = sig.get("signal_type", "")
                cat = sig.get("signal_category", "")
                lines.append(f"  - [{cat}] {ticker}: {sig_type}")
            lines.append("")
    else:
        lines.append("*No convergent entities detected.*")
        lines.append("")

    # ---- Strategy Health Check ----
    lines.append("## Strategy Health Check")
    lines.append("")
    all_healthy = True
    for r in scanner_results:
        if not r.success and not r.skipped:
            lines.append(f"- **{r.name}**: FAILED — {r.error}")
            all_healthy = False
    if all_healthy:
        lines.append("All active scanners completed successfully.")
    lines.append("")

    # ---- Watchlist / Next Steps ----
    lines.append("## Next Steps")
    lines.append("")
    if convergences:
        lines.append("- [ ] Deep dive on convergent entities (highest priority)")
    if total_signals > 0:
        lines.append("- [ ] Score all new signals using 7-dimension rubric")
        lines.append("- [ ] Full candidate writeup for any signal scoring 30+")
    if failed_scanners:
        lines.append(f"- [ ] Investigate scanner failures: {', '.join(r.name for r in failed_scanners)}")
    lines.append("- [ ] Check existing candidates against kill conditions")
    lines.append("")

    # ---- Footer ----
    lines.append("---")
    lines.append(f"*Report generated by pipeline_runner.py v1.0 at "
                 f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}*")

    report_text = "\n".join(lines)

    if dry_run:
        print(report_text)
        return None

    # Save report
    os.makedirs(REPORTS_DIR, exist_ok=True)
    report_path = os.path.join(REPORTS_DIR, f"{report_date}_daily_report.md")
    with open(report_path, "w") as f:
        f.write(report_text)

    logger.info(f"Daily report saved: {report_path}")
    return report_path


# ---------------------------------------------------------------------------
# Full Pipeline Orchestration
# ---------------------------------------------------------------------------

def run_pipeline(
    skip_list: List[str] = None,
    scanners_only: bool = False,
    dry_run: bool = False,
) -> Dict:
    """Run the complete daily investment discovery pipeline.

    Returns a summary dict with all results for programmatic use.
    """
    report_date = date.today().isoformat()
    logger.info(f"=== Pipeline Run: {report_date} ===")
    pipeline_start = time.time()

    # Step 1: Run all scanners
    logger.info("--- Phase 1: Running scanners ---")
    scanner_results = run_all_scanners(skip_list or [])

    # Collect all signals from successful scanners
    all_signals = []
    for r in scanner_results:
        all_signals.extend(r.signals)

    logger.info(f"Phase 1 complete: {len(all_signals)} total signals from "
                f"{sum(1 for r in scanner_results if r.success)} scanners")

    # Print scanner summary
    print("\n" + "=" * 60)
    print(f"  SCANNER RESULTS — {report_date}")
    print("=" * 60)
    for r in scanner_results:
        print(r.summary())
    print(f"\n  Total signals: {len(all_signals)}")
    print("=" * 60 + "\n")

    if scanners_only:
        return {
            "date": report_date,
            "scanner_results": scanner_results,
            "all_signals": all_signals,
            "convergences": [],
            "report_path": None,
        }

    # Step 2: Normalize via OpenFIGI
    logger.info("--- Phase 2: OpenFIGI normalization ---")
    if all_signals:
        all_signals = normalize_signals(all_signals)
    else:
        logger.info("No signals to normalize.")

    # Step 3: Convergence detection
    logger.info("--- Phase 3: Convergence detection ---")
    convergences, conv_error = run_convergence_check(save_output=not dry_run)

    if convergences:
        print(f"\n{'!'*60}")
        print(f"  CONVERGENCE ALERT: {len(convergences)} entities detected!")
        for conv in convergences:
            entity = conv.get("entity_key", conv.get("ticker", "???"))
            strats = conv.get("strategies", [])
            print(f"    → {entity}: {', '.join(strats)}")
        print(f"{'!'*60}\n")

    # Step 4: Generate daily report
    logger.info("--- Phase 4: Daily report ---")
    report_path = generate_daily_report(
        scanner_results=scanner_results,
        all_signals=all_signals,
        convergences=convergences,
        convergence_error=conv_error,
        report_date=report_date,
        dry_run=dry_run,
    )

    pipeline_duration = time.time() - pipeline_start
    logger.info(f"=== Pipeline complete: {pipeline_duration:.1f}s total ===")

    return {
        "date": report_date,
        "scanner_results": scanner_results,
        "all_signals": all_signals,
        "convergences": convergences,
        "report_path": report_path,
        "duration_s": pipeline_duration,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    global SCANNER_TIMEOUT_S
    parser = argparse.ArgumentParser(
        description="Run the daily investment discovery pipeline."
    )
    parser.add_argument(
        "--skip",
        nargs="*",
        default=[],
        choices=[s["name"] for s in SCANNER_REGISTRY],
        help="Scanners to skip (space-separated)",
    )
    parser.add_argument(
        "--scanners-only",
        action="store_true",
        help="Run scanners only — skip normalization, convergence, and report",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print report to stdout instead of saving to file",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=SCANNER_TIMEOUT_S,
        help=f"Per-scanner timeout in seconds (default: {SCANNER_TIMEOUT_S})",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) logging",
    )
    args = parser.parse_args()

    # Apply timeout override
    SCANNER_TIMEOUT_S = args.timeout

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Run the pipeline
    result = run_pipeline(
        skip_list=args.skip or [],
        scanners_only=args.scanners_only,
        dry_run=args.dry_run,
    )

    # Exit summary
    total_signals = len(result["all_signals"])
    convergence_count = len(result["convergences"])

    print(f"\nPipeline finished: {total_signals} signals, "
          f"{convergence_count} convergences")

    if result.get("report_path"):
        print(f"Report saved: {result['report_path']}")

    # Exit code: 0 if any scanner succeeded, 1 if all failed
    any_success = any(
        r.success for r in result["scanner_results"] if not r.skipped
    )
    sys.exit(0 if any_success else 1)


if __name__ == "__main__":
    main()
