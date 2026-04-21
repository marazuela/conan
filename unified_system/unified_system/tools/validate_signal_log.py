"""
validate_signal_log.py — signal-log QA (ROADMAP Step 4, D-029, 2026-04-21)
==========================================================================

Foundation-level self-audit for `engine/signals/signal_log.json`.
Intended to run as a post-scan hook (via `run_post_scan.py`) or on an
hourly scheduled task. Produces `engine/signals/_validation_report.json`,
which `health_check.py` consumes via a new `validation` family.

Checks
------
1. **Duplicate fingerprints**
   - Duplicate `signal_id` values (should be impossible if the ingest
     dedup layer is correct — hard RED if it happens).
   - Duplicate "logical" fingerprints: same `(issuer_key, source_date,
     upstream_scanner, signal_type)` tuple but different `signal_id`.
     This indicates an ingest layer that missed a dedup opportunity.

2. **Null rates**
   - Fraction of records missing `scoring_profile`, `ticker`/
     `ticker_plus_mic`/`ticker_local`, `source_date`, `scan_date`.
   - Each null rate is bucketed into a severity (>5% = yellow, >20% = red).

3. **Ticker / MIC consistency**
   - When both `ticker` and `ticker_plus_mic` exist, the prefix of
     `ticker_plus_mic` before the first `.` must equal `ticker`.
   - `ticker_plus_mic` must look like `SYMBOL.MIC4` (trailing 4-letter
     MIC, ISO-10383 shape). Non-conforming records are flagged.

4. **Stale convergence groups**
   - Reads the latest `working/convergence_report_*.json` and compares
     each group's member `signal_ids` to current signal_log ids. If a
     group has zero or ≤25% surviving ids, the group is stale.
   - Reports also the age of the latest convergence report file.

5. **Orphan signals**
   - Signals in the log whose `upstream_scanner` doesn't correspond to
     any `signals/*_scanner_output.json` (ignoring deprecated scanners
     in `config/scanner_registry.json` with `status != operational`).

Output
------
  engine/signals/_validation_report.json

Exit codes:
  0 if no RED findings, 1 if any.

CLI
---
  python3 tools/validate_signal_log.py          # full run, write + exit-code
  python3 tools/validate_signal_log.py --dry-run  # print only
  python3 tools/validate_signal_log.py --quiet    # summary line only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).parent.parent
SIGNALS_DIR = REPO / "signals"
SIGNAL_LOG = SIGNALS_DIR / "signal_log.json"
VALIDATION_REPORT = SIGNALS_DIR / "_validation_report.json"
CONFIG_DIR = REPO / "config"
REGISTRY_PATH = CONFIG_DIR / "scanner_registry.json"
WORKING = REPO / "working"

try:
    sys.path.insert(0, str(Path(__file__).parent))
    from profile_map import TICKERLESS_BY_DESIGN_SCANNERS  # type: ignore
except Exception:
    TICKERLESS_BY_DESIGN_SCANNERS = frozenset(
        {
            "pre_phase3_readout_scanner",
            "cvm_scanner",
            "courtlistener_scanner",
            "sec_enforcement_scanner",
        }
    )

NULL_RATE_YELLOW = 0.05
NULL_RATE_RED = 0.20
CONVERGENCE_STALE_DAYS = 3
STALE_GROUP_SURVIVOR_FLOOR = 0.25
_MIC_RE = re.compile(r"^([A-Z0-9\.\-]{1,32})\.([A-Z]{4})$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
    except Exception:
        pass
    os.replace(tmp, path)


def _parse_iso(s: Any) -> Optional[datetime]:
    if not s:
        return None
    try:
        s2 = str(s).replace("Z", "+00:00")
        if "T" not in s2 and len(s2) == 10:
            s2 = s2 + "T00:00:00+00:00"
        return datetime.fromisoformat(s2).astimezone(timezone.utc)
    except Exception:
        return None


def _load_signal_log() -> List[Dict[str, Any]]:
    if not SIGNAL_LOG.exists():
        return []
    try:
        data = json.loads(SIGNAL_LOG.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else (data.get("signals") or [])


def _load_registry_operational() -> set:
    out = set()
    if not REGISTRY_PATH.exists():
        return out
    try:
        reg = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return out
    for scanner in reg.get("scanners") or []:
        name = scanner.get("name")
        if name:
            out.add(str(name))
            out.add(str(name).replace("_scanner", ""))
    return out


def _latest_convergence_report() -> Optional[Path]:
    reports = sorted(WORKING.glob("convergence_report_*.json"))
    return reports[-1] if reports else None


def check_duplicate_fingerprints(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    if not signals:
        return findings

    ids = Counter(signal.get("signal_id") for signal in signals if isinstance(signal, dict))
    dupes = [signal_id for signal_id, count in ids.items() if count > 1 and signal_id]
    if dupes:
        findings.append(
            {
                "severity": "red",
                "check": "duplicate_fingerprints",
                "msg": f"{len(dupes)} duplicate signal_id values in log",
                "sample": dupes[:5],
            }
        )

    logical = defaultdict(list)
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        issuer = (
            signal.get("ticker_plus_mic")
            or signal.get("ticker")
            or signal.get("figi")
            or signal.get("issuer_figi")
            or signal.get("cik")
            or signal.get("codigo_cvm")
            or signal.get("company_name_en")
            or signal.get("company_name_local")
        )
        if not issuer:
            continue
        src_date = signal.get("source_date") or signal.get("scan_date")
        scanner = signal.get("upstream_scanner") or signal.get("scanner") or signal.get("scanner_source")
        signal_type = signal.get("signal_type") or signal.get("signal_category") or ""
        content_hash = signal.get("source_content_hash") or ""
        if src_date and "T" in str(src_date):
            src_date = str(src_date).split("T", 1)[0]
        key = (str(issuer), str(src_date or ""), str(scanner or ""), str(signal_type or ""), str(content_hash or ""))
        logical[key].append(signal.get("signal_id"))
    logical_dupes = {key: values for key, values in logical.items() if len(values) > 1}
    if logical_dupes:
        sample = []
        for key, values in list(logical_dupes.items())[:5]:
            sample.append({"fingerprint": list(key), "signal_ids": values})
        severity = "yellow" if len(logical_dupes) < 20 else "red"
        findings.append(
            {
                "severity": severity,
                "check": "duplicate_fingerprints",
                "msg": f"{len(logical_dupes)} logical duplicates — same (issuer,date,scanner,type) but different signal_id",
                "sample": sample,
            }
        )
    return findings


def _null_rate_sev(rate: float) -> str:
    if rate > NULL_RATE_RED:
        return "red"
    if rate > NULL_RATE_YELLOW:
        return "yellow"
    return "green"


def check_null_rates(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    total = len(signals)
    if not total:
        return findings

    def _any(signal: Dict[str, Any], keys) -> bool:
        return any(signal.get(key) not in (None, "") for key in keys)

    tickered = ("ticker", "ticker_plus_mic", "ticker_local")
    any_issuer = tickered + (
        "figi",
        "issuer_figi",
        "cik",
        "sec_cik",
        "subject_cik",
        "codigo_cvm",
        "cnpj",
        "company_name_en",
        "company_name_local",
    )

    def _scanner_of(signal: Dict[str, Any]) -> str:
        return signal.get("scanner") or signal.get("upstream_scanner") or signal.get("scanner_source") or signal.get("_scanner") or ""

    tickered_universe = [signal for signal in signals if _scanner_of(signal) not in TICKERLESS_BY_DESIGN_SCANNERS]

    miss_scoring = sum(1 for signal in signals if not signal.get("scoring_profile"))
    miss_tickered = sum(1 for signal in tickered_universe if not _any(signal, tickered))
    tickered_total = len(tickered_universe) or total
    miss_any_issuer = sum(1 for signal in signals if not _any(signal, any_issuer))
    miss_source_date = sum(1 for signal in signals if not signal.get("source_date"))
    miss_scan_date = sum(1 for signal in signals if not signal.get("scan_date"))
    miss_scanner = sum(
        1 for signal in signals if not _any(signal, ("upstream_scanner", "scanner", "scanner_source"))
    )

    def _record(field: str, missing: int, severity_override: Optional[str] = None) -> None:
        rate = missing / total
        severity = severity_override or _null_rate_sev(rate)
        findings.append(
            {
                "severity": severity,
                "check": "null_rate",
                "field": field,
                "missing": missing,
                "total": total,
                "rate": round(rate, 4),
                "msg": f"{missing}/{total} ({100 * rate:.1f}%) missing {field}",
            }
        )

    scoring_severity = _null_rate_sev(miss_scoring / total)
    if scoring_severity == "red":
        scoring_severity = "yellow"
    _record("scoring_profile", miss_scoring, severity_override=scoring_severity)

    ticker_rate = (miss_tickered / tickered_total) if tickered_total else 0.0
    ticker_severity = "green"
    if ticker_rate > 0.5:
        ticker_severity = "red"
    elif ticker_rate > 0.25:
        ticker_severity = "yellow"
    findings.append(
        {
            "severity": ticker_severity,
            "check": "null_rate",
            "field": "ticker/ticker_plus_mic/ticker_local",
            "missing": miss_tickered,
            "total": tickered_total,
            "rate": round(ticker_rate, 4),
            "msg": f"{miss_tickered}/{tickered_total} ({100 * ticker_rate:.1f}%) missing ticker/ticker_plus_mic/ticker_local (ticker-less-by-design scanners excluded: {sorted(TICKERLESS_BY_DESIGN_SCANNERS)})",
        }
    )

    _record("any issuer identifier (ticker/figi/cik/cvm/name)", miss_any_issuer)
    _record("source_date", miss_source_date)
    _record("scan_date", miss_scan_date)
    _record("upstream_scanner/scanner/scanner_source", miss_scanner)
    return findings


def check_ticker_mic_consistency(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    disagree: List[Dict[str, Any]] = []
    malformed: List[Dict[str, Any]] = []
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        ticker_plus_mic = signal.get("ticker_plus_mic")
        ticker = signal.get("ticker")
        if ticker_plus_mic and not _MIC_RE.match(str(ticker_plus_mic)):
            malformed.append({"signal_id": signal.get("signal_id"), "ticker_plus_mic": ticker_plus_mic})
        if ticker_plus_mic and ticker:
            prefix = str(ticker_plus_mic).split(".", 1)[0]
            if prefix.strip().upper() != str(ticker).strip().upper():
                disagree.append(
                    {
                        "signal_id": signal.get("signal_id"),
                        "ticker": ticker,
                        "ticker_plus_mic": ticker_plus_mic,
                    }
                )
    if malformed:
        severity = "yellow" if len(malformed) < 20 else "red"
        findings.append(
            {
                "severity": severity,
                "check": "ticker_mic_shape",
                "msg": f"{len(malformed)} ticker_plus_mic values do not match SYMBOL.MIC4 shape",
                "sample": malformed[:5],
            }
        )
    else:
        findings.append({"severity": "green", "check": "ticker_mic_shape", "msg": "all ticker_plus_mic values match SYMBOL.MIC4 shape"})
    if disagree:
        findings.append(
            {
                "severity": "yellow",
                "check": "ticker_mic_disagree",
                "msg": f"{len(disagree)} records where ticker != ticker_plus_mic prefix",
                "sample": disagree[:5],
            }
        )
    else:
        findings.append({"severity": "green", "check": "ticker_mic_disagree", "msg": "ticker and ticker_plus_mic prefix agree on all records"})
    return findings


def check_stale_convergence(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    latest = _latest_convergence_report()
    if not latest:
        findings.append({"severity": "yellow", "check": "convergence_freshness", "msg": "no convergence_report_*.json present in working/"})
        return findings
    try:
        mtime = datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)
        age_days = (_now() - mtime).total_seconds() / 86400.0
    except Exception:
        age_days = None
    if age_days is not None and age_days > CONVERGENCE_STALE_DAYS:
        findings.append({"severity": "yellow", "check": "convergence_freshness", "msg": f"latest convergence report ({latest.name}) is {age_days:.1f} days old"})
    else:
        findings.append({"severity": "green", "check": "convergence_freshness", "msg": f"latest convergence report: {latest.name} ({age_days or 0:.1f}d old)"})

    try:
        convergence = json.loads(latest.read_text(encoding="utf-8"))
    except Exception as exc:
        findings.append({"severity": "red", "check": "convergence_parse", "msg": f"cannot parse {latest.name}: {exc}"})
        return findings
    groups = convergence.get("groups") if isinstance(convergence, dict) else []
    if not isinstance(groups, list) or not groups:
        findings.append({"severity": "info", "check": "convergence_groups", "msg": "convergence report has no groups"})
        return findings

    current_ids = {signal.get("signal_id") for signal in signals if isinstance(signal, dict)}
    stale_groups = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_ids = group.get("signal_ids") or []
        if not group_ids:
            continue
        survivors = sum(1 for signal_id in group_ids if signal_id in current_ids)
        survivor_rate = survivors / len(group_ids)
        if survivor_rate <= STALE_GROUP_SURVIVOR_FLOOR:
            stale_groups.append(
                {
                    "issuer_key": group.get("issuer_key"),
                    "signal_count": len(group_ids),
                    "survivors": survivors,
                    "survivor_rate": round(survivor_rate, 3),
                }
            )
    if stale_groups:
        severity = "yellow" if len(stale_groups) < max(3, int(0.25 * len(groups))) else "red"
        findings.append(
            {
                "severity": severity,
                "check": "convergence_groups_stale",
                "msg": f"{len(stale_groups)}/{len(groups)} convergence groups have ≤{int(STALE_GROUP_SURVIVOR_FLOOR * 100)}% of members still in signal_log",
                "sample": stale_groups[:5],
            }
        )
    else:
        findings.append({"severity": "green", "check": "convergence_groups_stale", "msg": f"all {len(groups)} convergence groups have fresh members"})
    return findings


def check_orphans(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    registry = _load_registry_operational()
    scanner_ids: Dict[str, set] = {}
    for path in sorted(SIGNALS_DIR.glob("*_scanner_output.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        records = data if isinstance(data, list) else (data.get("signals") or data.get("records") or data.get("results") or [])
        if not isinstance(records, list):
            continue
        scanner_name = path.name.replace("_scanner_output.json", "")
        ids = {record.get("signal_id") for record in records if isinstance(record, dict) and record.get("signal_id")}
        scanner_ids[scanner_name] = ids

    unknown_scanner_counts: Counter = Counter()
    orphan_records: List[Dict[str, Any]] = []
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        scanner = signal.get("upstream_scanner") or signal.get("scanner") or signal.get("scanner_source")
        if not scanner:
            continue
        scanner_name = str(scanner).replace("_scanner", "")
        if scanner_name not in scanner_ids and scanner not in registry and scanner_name not in registry:
            unknown_scanner_counts[scanner_name] += 1
            if len(orphan_records) < 10:
                orphan_records.append({"signal_id": signal.get("signal_id"), "scanner": scanner})
    if unknown_scanner_counts:
        findings.append(
            {
                "severity": "yellow",
                "check": "orphan_scanner",
                "msg": f"{sum(unknown_scanner_counts.values())} signals reference scanners that are not in the registry and have no current output file",
                "by_scanner": dict(unknown_scanner_counts.most_common()),
                "sample_records": orphan_records[:5],
            }
        )
    else:
        findings.append({"severity": "green", "check": "orphan_scanner", "msg": "every signal's upstream_scanner is known"})
    return findings


SEVERITY_ORDER = {"red": 3, "yellow": 2, "green": 1, "info": 0}


def _sev(severity: str) -> int:
    return SEVERITY_ORDER.get(severity, 0)


def run(dry_run: bool = False) -> Dict[str, Any]:
    signals = _load_signal_log()
    findings: List[Dict[str, Any]] = []
    findings += check_duplicate_fingerprints(signals)
    findings += check_null_rates(signals)
    findings += check_ticker_mic_consistency(signals)
    findings += check_stale_convergence(signals)
    findings += check_orphans(signals)

    max_severity = "green"
    counts = {"red": 0, "yellow": 0, "green": 0, "info": 0}
    for finding in findings:
        severity = finding.get("severity", "info")
        counts[severity] = counts.get(severity, 0) + 1
        if _sev(severity) > _sev(max_severity):
            max_severity = severity

    result = {
        "ran_at_utc": _now().isoformat().replace("+00:00", "Z"),
        "signal_log_size": len(signals),
        "max_severity": max_severity,
        "counts": counts,
        "findings": findings,
    }

    if not dry_run:
        _atomic_write(VALIDATION_REPORT, json.dumps(result, indent=2, ensure_ascii=False))
        result["_out"] = str(VALIDATION_REPORT)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Signal-log validation")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    result = run(dry_run=args.dry_run)
    if args.quiet:
        print(
            json.dumps(
                {
                    "max_severity": result["max_severity"],
                    "counts": result["counts"],
                    "signal_log_size": result["signal_log_size"],
                },
                ensure_ascii=False,
            )
        )
    else:
        print(
            json.dumps(
                {
                    "ran_at_utc": result["ran_at_utc"],
                    "signal_log_size": result["signal_log_size"],
                    "max_severity": result["max_severity"],
                    "counts": result["counts"],
                    "out": result.get("_out"),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    if result["max_severity"] == "red":
        sys.exit(1)


if __name__ == "__main__":
    main()
