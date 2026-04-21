"""
Post-Scan Aggregation  (v1.1 — 2026-04-09)
============================================
Run AFTER individual scanners to:
  1. Read scanner result files from signals/
  2. Normalize signals via OpenFIGI
  3. Run convergence engine
  4. Generate daily report

Usage:
    python run_post_scan.py                  # Full post-scan pipeline
    python run_post_scan.py --skip-figi      # Skip OpenFIGI normalization
    python run_post_scan.py --dry-run        # Print report, don't save
"""

import json
import os
import sys
import glob
import logging
import argparse
from datetime import datetime, date

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
SIGNALS_DIR = os.path.join(PROJECT_DIR, "signals")
REPORTS_DIR = os.path.join(PROJECT_DIR, "reports")
CANDIDATES_DIR = os.path.join(PROJECT_DIR, "candidates")

if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("post_scan")


def load_scanner_results():
    """Load all _scanner_result_*.json files from signals/."""
    results = []
    pattern = os.path.join(SIGNALS_DIR, "_scanner_result_*.json")
    for path in sorted(glob.glob(pattern)):
        with open(path, "r") as f:
            data = json.load(f)
            results.append(data)
        logger.info(f"Loaded result: {data['name']} — "
                     f"{'OK' if data['success'] else 'FAIL'} — "
                     f"{data['signal_count']} signals")
    return results


def collect_all_signals(scanner_results):
    """Extract signals from all successful scanner results."""
    all_signals = []
    for r in scanner_results:
        if r.get("success"):
            all_signals.extend(r.get("signals", []))
    return all_signals


def normalize_signals(signals):
    """Run OpenFIGI normalization (non-fatal on failure)."""
    if not signals:
        return signals
    try:
        from openfigi_resolver import normalize_signals as figi_normalize
        normalized = figi_normalize(signals)
        resolved = sum(1 for s in normalized if s.get("composite_figi"))
        logger.info(f"OpenFIGI: resolved {resolved}/{len(signals)} signals")
        return normalized
    except Exception as e:
        logger.warning(f"OpenFIGI normalization failed (non-fatal): {e}")
        return signals


def run_convergence():
    """Run convergence engine on all signals."""
    try:
        from convergence_engine import run_convergence as converge
        convergences = converge(signals_dir=SIGNALS_DIR, save_output=True)
        logger.info(f"Convergence: {len(convergences)} convergent entities")
        return convergences, None
    except Exception as e:
        logger.error(f"Convergence engine failed: {e}")
        return [], str(e)


def generate_report(scanner_results, all_signals, convergences,
                    convergence_error, report_date, dry_run=False):
    """Generate daily markdown report."""
    lines = []
    lines.append(f"# Daily Signal Report — {report_date}")
    lines.append("")
    lines.append(f"**Generated**: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"**Pipeline version**: v1.1")
    lines.append("")

    # Executive Summary
    total_signals = sum(r.get("signal_count", 0) for r in scanner_results)
    active = sum(1 for r in scanner_results if r.get("success"))
    failed = [r for r in scanner_results if not r.get("success")]

    lines.append("## Executive Summary")
    lines.append("")
    lines.append(f"- **Total signals detected**: {total_signals}")
    lines.append(f"- **Active scanners**: {active}/{len(scanner_results)}")
    if failed:
        lines.append(f"- **Failed scanners**: {', '.join(r['name'] for r in failed)}")
    lines.append(f"- **Convergent entities**: {len(convergences)}")
    lines.append("")

    # Scanner Results Table
    lines.append("## Scanner Results")
    lines.append("")
    lines.append("| Scanner | Status | Signals | Time (s) | Notes |")
    lines.append("|---------|--------|---------|----------|-------|")
    for r in scanner_results:
        status = "OK" if r.get("success") else "FAIL"
        notes = r.get("error", "") or ""
        lines.append(
            f"| {r['name']} | {status} | {r.get('signal_count', 0)} | "
            f"{r.get('duration_s', 0):.1f} | {notes} |"
        )
    lines.append("")

    # Signals by Strategy
    lines.append("## Signals by Strategy")
    lines.append("")

    by_category = {}
    for sig in all_signals:
        cat = sig.get("signal_category", sig.get("source", "unknown"))
        by_category.setdefault(cat, []).append(sig)

    if by_category:
        for cat, sigs in sorted(by_category.items()):
            # Sort signals by strength (highest first)
            sigs_sorted = sorted(sigs, key=lambda s: s.get("strength_estimate", 0),
                                 reverse=True)
            high_strength = [s for s in sigs_sorted
                            if (s.get("strength_estimate") or 0) >= 4]
            lines.append(f"### {cat.replace('_', ' ').title()} ({len(sigs)} signals"
                         f"{f', {len(high_strength)} high-strength' if high_strength else ''})")
            lines.append("")

            # Show EDGAR rotation info if applicable
            if cat == "edgar" and sigs:
                raw = sigs[0].get("raw_data", {})
                kw_sample = raw.get("keyword", "")
                if kw_sample:
                    # Infer category from keywords
                    kw_cats = set()
                    for s in sigs:
                        kw = s.get("raw_data", {}).get("keyword", "")
                        if kw in ("merger agreement", "definitive agreement",
                                  "tender offer"):
                            kw_cats.add("mna")
                        elif kw in ("13D", "activist", "undervalued"):
                            kw_cats.add("activist")
                        elif kw in ("going concern", "substantial doubt"):
                            kw_cats.add("distress")
                        elif kw in ("poison pill", "rights plan"):
                            kw_cats.add("governance")
                        else:
                            kw_cats.add(kw.split()[0] if kw else "unknown")
                    if kw_cats:
                        lines.append(f"*Rotation category: {', '.join(kw_cats)}*")
                        lines.append("")

            for sig in sigs_sorted[:10]:
                ticker = sig.get("ticker", "???")
                company = sig.get("company_name", "Unknown")
                sig_type = sig.get("signal_type", "unknown")
                strength = sig.get("strength_estimate", "?")
                source_date = sig.get("source_date", "?")
                # Add strength indicator
                s_icon = ""
                try:
                    s_val = int(strength)
                    if s_val >= 4:
                        s_icon = " **HIGH**"
                    elif s_val <= 2:
                        s_icon = " (low)"
                except (ValueError, TypeError):
                    pass
                lines.append(
                    f"- **{ticker}** ({company}) — {sig_type} — "
                    f"strength {strength}{s_icon} — {source_date}"
                )
            if len(sigs) > 10:
                lines.append(f"- ... and {len(sigs) - 10} more")
            lines.append("")
    else:
        lines.append("*No signals detected this scan.*")
        lines.append("")

    # Convergence Alerts
    lines.append("## Convergence Alerts")
    lines.append("")

    if convergence_error:
        lines.append(f"**Convergence engine error**: {convergence_error}")
        lines.append("")
    elif convergences:
        lines.append(
            f"**{len(convergences)} convergent entities detected** "
            f"— highest-priority signals."
        )
        lines.append("")
        for conv in convergences:
            entity = conv.get("entity_key", conv.get("ticker", "???"))
            strategies = conv.get("strategies", [])
            signal_count = conv.get("signal_count", len(conv.get("signals", [])))
            score_bonus = conv.get("convergence_bonus", 0)
            lines.append(f"### {entity}")
            lines.append(f"- **Strategies**: {', '.join(strategies)}")
            lines.append(f"- **Signal count**: {signal_count}")
            lines.append(f"- **Convergence bonus**: +{score_bonus}")
            for sig in conv.get("signals", [])[:5]:
                sig_type = sig.get("signal_type", "")
                cat = sig.get("signal_category", "")
                lines.append(f"  - [{cat}] {sig.get('ticker', '???')}: {sig_type}")
            lines.append("")
    else:
        lines.append("*No convergent entities detected.*")
        lines.append("")

    # Strategy Health
    lines.append("## Strategy Health Check")
    lines.append("")
    all_ok = all(r.get("success") for r in scanner_results)
    if all_ok:
        lines.append("All active scanners completed successfully.")
    else:
        for r in failed:
            lines.append(f"- **{r['name']}**: FAILED — {r.get('error', 'Unknown')}")
    lines.append("")

    # Active Candidates
    lines.append("## Active Candidates")
    lines.append("")
    candidates_found = False
    if os.path.isdir(CANDIDATES_DIR):
        for cfile in sorted(glob.glob(os.path.join(CANDIDATES_DIR, "*.md"))):
            fname = os.path.basename(cfile)
            try:
                with open(cfile, "r") as f:
                    header_lines = []
                    for hline in f:
                        header_lines.append(hline.strip())
                        if len(header_lines) > 10:
                            break
                # Extract title, score, status from frontmatter
                title = header_lines[0].replace("# ", "") if header_lines else fname
                score_line = next((l for l in header_lines if "Score" in l and "/" in l), "")
                status_line = next((l for l in header_lines if "Status" in l), "")
                if "Active" in status_line or "Watchlist" in status_line:
                    lines.append(f"- **{title}** — {score_line.strip('> ')} — `{fname}`")
                    candidates_found = True
            except Exception:
                pass
    if not candidates_found:
        lines.append("*No active candidates.*")
    lines.append("")

    # PDUFA Watchlist
    pdufa_path = os.path.join(SIGNALS_DIR, "pdufa_watchlist.json")
    if os.path.exists(pdufa_path):
        try:
            with open(pdufa_path, "r") as f:
                watchlist = json.load(f)
            if watchlist:
                lines.append("## PDUFA Watchlist")
                lines.append("")
                lines.append("| Ticker | Drug | PDUFA Date | Status |")
                lines.append("|--------|------|-----------|--------|")
                active_watchlist = [e for e in watchlist if e.get("status", "active") not in ("approved", "killed", "withdrawn")]
                for entry in sorted(active_watchlist, key=lambda x: x.get("pdufa_date", "")):
                    ticker = entry.get("ticker", "?")
                    drug = entry.get("drug_name", "?")
                    pdate = entry.get("pdufa_date", "?")
                    status = entry.get("status", "active")
                    # Calculate days until PDUFA
                    try:
                        pdufa_dt = datetime.strptime(pdate, "%Y-%m-%d").date()
                        days_to = (pdufa_dt - date.today()).days
                        if days_to <= 7:
                            status = f"**T-{days_to} days — IMMINENT**"
                        elif days_to <= 30:
                            status = f"T-{days_to} days"
                        else:
                            status = f"{days_to} days"
                    except Exception:
                        pass
                    lines.append(f"| {ticker} | {drug} | {pdate} | {status} |")
                lines.append("")
        except Exception:
            pass

    # Next Steps
    lines.append("## Next Steps")
    lines.append("")
    if convergences:
        lines.append("- [ ] Deep dive on convergent entities (highest priority)")
    if total_signals > 0:
        lines.append("- [ ] Score all new signals using 7-dimension rubric")
        lines.append("- [ ] Full candidate writeup for any signal scoring 30+")
    if failed:
        lines.append(f"- [ ] Investigate scanner failures: {', '.join(r['name'] for r in failed)}")
    lines.append("- [ ] Check existing candidates against kill conditions")
    lines.append("- [ ] Monitor active candidates for developments")
    lines.append("")
    lines.append("---")
    lines.append(f"*Report generated by run_post_scan.py v1.1 at "
                 f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}*")

    report_text = "\n".join(lines)

    if dry_run:
        print(report_text)
        return None

    os.makedirs(REPORTS_DIR, exist_ok=True)
    report_path = os.path.join(REPORTS_DIR, f"{report_date}_daily_report.md")
    with open(report_path, "w") as f:
        f.write(report_text)
    logger.info(f"Daily report saved: {report_path}")
    return report_path


def cleanup_old_convergence_files(max_age_days: int = 7):
    """Remove convergence output files older than max_age_days.

    Convergence engine creates a new convergence_*.json file each run.
    Without cleanup these accumulate indefinitely. Keeps only recent files.
    """
    import time as _time
    cutoff = _time.time() - (max_age_days * 86400)
    removed = 0
    for filepath in glob.glob(os.path.join(SIGNALS_DIR, "convergence_*.json")):
        try:
            if os.path.getmtime(filepath) < cutoff:
                os.remove(filepath)
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info(f"Cleaned up {removed} convergence files older than {max_age_days} days")


def main():
    parser = argparse.ArgumentParser(
        description="Post-scan aggregation: OpenFIGI + convergence + report."
    )
    parser.add_argument("--skip-figi", action="store_true",
                        help="Skip OpenFIGI normalization")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print report to stdout, don't save")
    args = parser.parse_args()

    report_date = date.today().isoformat()

    # Step 1: Load scanner results
    scanner_results = load_scanner_results()
    if not scanner_results:
        print("No scanner results found in signals/. Run scanners first.")
        return 1

    # Step 2: Collect signals
    all_signals = collect_all_signals(scanner_results)
    print(f"\nLoaded {len(scanner_results)} scanner results, "
          f"{len(all_signals)} total signals")

    # Step 3: Normalize
    if not args.skip_figi and all_signals:
        all_signals = normalize_signals(all_signals)

    # Step 4: Convergence
    convergences, conv_error = run_convergence()

    if convergences:
        print(f"\n{'!' * 60}")
        print(f"  CONVERGENCE ALERT: {len(convergences)} entities!")
        for conv in convergences:
            entity = conv.get("entity_key", conv.get("ticker", "???"))
            strats = conv.get("strategies", [])
            print(f"    → {entity}: {', '.join(strats)}")
        print(f"{'!' * 60}\n")

    # Step 4b: Clean up old convergence files
    cleanup_old_convergence_files(max_age_days=7)

    # Step 5: Generate report
    report_path = generate_report(
        scanner_results, all_signals, convergences,
        conv_error, report_date, dry_run=args.dry_run,
    )

    if report_path:
        print(f"\nReport saved: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
