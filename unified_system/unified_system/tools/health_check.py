"""
Unified System Health Check (v1.0 — 2026-04-20)
================================================

Consolidated local integrity / drift / schema check. Intended to slot into the
maintenance path so the operator learns about local artifact regressions, schema
drift, stale generated data, and signal-log issues without manual inspection.

This tool reads the local unified_system registry and output files. It is NOT
the live scanner fleet source of truth; Supabase-backed runtime health now
lives behind the `scanner-health` endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).parent.parent
SIGNALS_DIR = REPO / "signals"
SIGNAL_LOG = SIGNALS_DIR / "signal_log.json"
CONFIG_DIR = REPO / "config"
REGISTRY_PATH = CONFIG_DIR / "scanner_registry.json"
CURATED_PATH = REPO / "candidates" / "_curated_rationales.json"
WORKING = REPO / "working"
WORKING.mkdir(exist_ok=True)
HISTORY_PATH = WORKING / "health_history.jsonl"
VALIDATION_REPORT = SIGNALS_DIR / "_validation_report.json"
VALIDATION_STALE_HOURS = 6

SEVERITY_ORDER = {"red": 3, "yellow": 2, "green": 1, "info": 0}
REQUIRED_SCANNER_FIELDS = ("signal_id", "source_date")
REQUIRED_SCANNER_EITHER = (
    (
        "ticker_plus_mic",
        "ticker",
        "ticker_local",
        "figi",
        "issuer_figi",
        "cik",
        "sec_cik",
        "subject_cik",
        "cnpj",
        "codigo_cvm",
        "company_name_en",
        "company_name_local",
    ),
)

DRAFT_STALE_DAYS = 7
DIR_STALE_DAYS = 30
LOCAL_REGISTRY_NOTE = (
    "local artifact view only — live fleet health is owned by the Supabase scanner-health endpoint"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
    except Exception:
        pass
    os.replace(tmp, path)


def _cadence_to_minutes(cadence: Any) -> Optional[int]:
    if cadence is None:
        return None
    text = str(cadence).strip().lower()
    if text == "daily":
        return 1440
    if text == "weekly":
        return 10080
    if text == "hourly":
        return 60
    match = re.match(r"^(\d+)\s*([hmd])?$", text)
    if match:
        count = int(match.group(1))
        unit = (match.group(2) or "m").lower()
        return {"h": 60, "m": 1, "d": 1440}[unit] * count
    match = re.match(r"^(\d+)\s*min", text)
    if match:
        return int(match.group(1))
    match = re.match(r"^(\d+)\s*hour", text)
    if match:
        return int(match.group(1)) * 60
    return None


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _file_age_hours(path: Path) -> Optional[float]:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return (_now() - mtime).total_seconds() / 3600.0
    except Exception:
        return None


def check_registry_coherence() -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = [
        {"severity": "info", "family": "registry", "msg": LOCAL_REGISTRY_NOTE}
    ]
    if not REGISTRY_PATH.exists():
        return [{"severity": "red", "family": "registry", "msg": "scanner_registry.json missing"}]
    try:
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return [{"severity": "red", "family": "registry", "msg": f"scanner_registry.json unparseable: {exc}"}]

    scanners = registry.get("scanners") or []
    for scanner in scanners:
        name = scanner.get("name", "?")
        status = scanner.get("status")
        cadence = scanner.get("cadence")
        if status != "operational":
            findings.append({"severity": "info", "family": "registry", "scanner": name, "msg": f"status={status} — skipped from coherence check"})
            continue
        cadence_min = _cadence_to_minutes(cadence)
        if cadence_min is None:
            findings.append({"severity": "yellow", "family": "registry", "scanner": name, "msg": f"cadence '{cadence}' unparseable"})
            continue
        out_path = SIGNALS_DIR / f"{name.replace('_scanner', '')}_scanner_output.json"
        alt_path = SIGNALS_DIR / f"{name}_output.json"
        last_run = _parse_iso(scanner.get("last_run_utc"))
        age_hours = None
        if last_run:
            age_hours = (_now() - last_run).total_seconds() / 3600.0
        elif out_path.exists():
            age_hours = _file_age_hours(out_path)
        elif alt_path.exists():
            age_hours = _file_age_hours(alt_path)
        max_allowed = 2 * cadence_min / 60.0
        if age_hours is None:
            findings.append({"severity": "red", "family": "registry", "scanner": name, "msg": "no last_run_utc and no output file"})
            continue
        if age_hours > max_allowed:
            findings.append({"severity": "red", "family": "registry", "scanner": name, "msg": f"stale: last run {age_hours:.1f}h ago, cadence={cadence_min}min, allowed 2x={max_allowed:.1f}h"})
        elif age_hours > max_allowed * 0.75:
            findings.append({"severity": "yellow", "family": "registry", "scanner": name, "msg": f"approaching staleness: last run {age_hours:.1f}h ago"})
        else:
            findings.append({"severity": "green", "family": "registry", "scanner": name, "msg": f"fresh: {age_hours:.1f}h ago"})
        last_status = scanner.get("last_run_status")
        if last_status not in (None, "ok", "success"):
            findings.append({"severity": "red", "family": "registry", "scanner": name, "msg": f"last_run_status={last_status}"})
    return findings


def check_scanner_output_schema() -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for path in sorted(SIGNALS_DIR.glob("*_scanner_output.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            findings.append({"severity": "red", "family": "schema", "file": path.name, "msg": f"unparseable JSON: {exc}"})
            continue
        records = None
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict):
            for key in ("signals", "records", "results"):
                if isinstance(data.get(key), list):
                    records = data[key]
                    break
        if records is None:
            findings.append({"severity": "red", "family": "schema", "file": path.name, "msg": "neither list nor dict with signals/records/results"})
            continue
        if not records:
            status = data.get("status") if isinstance(data, dict) else None
            error = (data.get("error") if isinstance(data, dict) else None) or (data.get("errors") if isinstance(data, dict) else None)
            if status and str(status).lower() != "ok":
                findings.append({"severity": "info", "family": "schema", "file": path.name, "msg": f"0 records (scanner reports status={status!r})"})
            elif error:
                findings.append({"severity": "info", "family": "schema", "file": path.name, "msg": "0 records (scanner reports an error payload)"})
            else:
                findings.append({"severity": "info", "family": "schema", "file": path.name, "msg": "0 records (scanner status=ok — no qualifying signals this window; regressions are handled by drift check)"})
            continue
        missing_required = 0
        missing_identifier = 0
        for record in records:
            if not isinstance(record, dict):
                missing_required += 1
                continue
            for field in REQUIRED_SCANNER_FIELDS:
                if field not in record or record.get(field) in (None, ""):
                    missing_required += 1
                    break
            for either in REQUIRED_SCANNER_EITHER:
                if not any(record.get(key) not in (None, "") for key in either):
                    missing_identifier += 1
                    break
        severity = "green"
        message_bits = [f"n={len(records)}"]
        if missing_required:
            severity = "red"
            message_bits.append(f"{missing_required} records missing required (signal_id/source_date)")
        if missing_identifier:
            if severity == "green":
                severity = "yellow"
            message_bits.append(f"{missing_identifier} records missing any issuer id")
        findings.append({"severity": severity, "family": "schema", "file": path.name, "msg": "; ".join(message_bits)})
    return findings


def _scanner_stats_from_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    records = data if isinstance(data, list) else (data.get("signals") or data.get("records") or data.get("results") or [])
    if not isinstance(records, list) or not records:
        return None
    tickers = set()
    scores = []
    for record in records:
        if not isinstance(record, dict):
            continue
        ticker = record.get("ticker_plus_mic") or record.get("ticker") or record.get("ticker_local")
        if ticker:
            tickers.add(str(ticker))
        score = record.get("score_total")
        if isinstance(score, (int, float)):
            scores.append(float(score))
    return {
        "n_records": len(records),
        "n_unique_tickers": len(tickers),
        "mean_score": round(sum(scores) / len(scores), 2) if scores else None,
        "n_scored": len(scores),
    }


def check_drift() -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    findings: List[Dict[str, Any]] = []
    snapshot: Dict[str, Dict[str, Any]] = {}
    for path in sorted(SIGNALS_DIR.glob("*_scanner_output.json")):
        stats = _scanner_stats_from_file(path)
        if stats:
            snapshot[path.name] = stats

    history: List[Dict[str, Any]] = []
    if HISTORY_PATH.exists():
        try:
            lines = HISTORY_PATH.read_text(encoding="utf-8").strip().split("\n")
            for line in lines[-14:]:
                if line:
                    try:
                        history.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            pass

    for filename, stats in snapshot.items():
        prior_ns = [item.get("snapshot", {}).get(filename, {}).get("n_records") for item in history]
        prior_ns = [value for value in prior_ns if isinstance(value, int)]
        prior_scores = [item.get("snapshot", {}).get(filename, {}).get("mean_score") for item in history]
        prior_scores = [value for value in prior_scores if isinstance(value, (int, float))]

        if len(prior_ns) >= 3:
            median = sorted(prior_ns)[len(prior_ns) // 2]
            if median > 0 and stats["n_records"] < 0.5 * median:
                findings.append({"severity": "red", "family": "drift", "file": filename, "msg": f"n_records={stats['n_records']} is <50% of 14d median {median}"})
            elif median > 0 and stats["n_records"] < 0.75 * median:
                findings.append({"severity": "yellow", "family": "drift", "file": filename, "msg": f"n_records={stats['n_records']} is <75% of 14d median {median}"})
        if len(prior_scores) >= 3 and stats.get("mean_score") is not None:
            median = sorted(prior_scores)[len(prior_scores) // 2]
            if median and abs(stats["mean_score"] - median) / max(abs(median), 1e-6) > 0.3:
                findings.append({"severity": "yellow", "family": "drift", "file": filename, "msg": f"mean_score={stats['mean_score']} shifted >30% from median {median}"})
    if not history:
        findings.append({"severity": "info", "family": "drift", "msg": "no prior history yet — first snapshot written"})
    return findings, snapshot


def check_signal_log() -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    if not SIGNAL_LOG.exists():
        return [{"severity": "red", "family": "signal_log", "msg": "signal_log.json missing"}]
    try:
        data = json.loads(SIGNAL_LOG.read_text(encoding="utf-8"))
    except Exception as exc:
        return [{"severity": "red", "family": "signal_log", "msg": f"unparseable: {exc}"}]
    signals = data if isinstance(data, list) else data.get("signals", [])
    if not isinstance(signals, list):
        return [{"severity": "red", "family": "signal_log", "msg": "signals not a list"}]
    ids = Counter(signal.get("signal_id") for signal in signals if isinstance(signal, dict))
    dupes = [signal_id for signal_id, count in ids.items() if count > 1 and signal_id]
    if dupes:
        findings.append({"severity": "red", "family": "signal_log", "msg": f"{len(dupes)} duplicate signal_ids (e.g. {dupes[0][:16]})"})
    missing_id = sum(1 for signal in signals if isinstance(signal, dict) and not signal.get("signal_id"))
    if missing_id:
        findings.append({"severity": "red", "family": "signal_log", "msg": f"{missing_id} entries missing signal_id"})
    missing_date = sum(1 for signal in signals if isinstance(signal, dict) and not signal.get("scan_date"))
    if missing_date:
        findings.append({"severity": "yellow", "family": "signal_log", "msg": f"{missing_date} entries missing scan_date"})
    no_score = sum(1 for signal in signals if isinstance(signal, dict) and not isinstance(signal.get("score_total"), (int, float)))
    findings.append({"severity": "info", "family": "signal_log", "msg": f"{len(signals)} total, {no_score} without score_total ({100 * no_score / max(len(signals), 1):.0f}%)"})
    return findings


def check_curated_integrity() -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    if not CURATED_PATH.exists():
        return [{"severity": "yellow", "family": "curated", "msg": "no _curated_rationales.json"}]
    try:
        curated = json.loads(CURATED_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return [{"severity": "red", "family": "curated", "msg": f"unparseable: {exc}"}]
    if not isinstance(curated, dict):
        return [{"severity": "red", "family": "curated", "msg": "not a dict"}]
    now = _now()
    for ticker, entry in curated.items():
        if ticker.startswith("_"):
            continue
        if not isinstance(entry, dict):
            findings.append({"severity": "red", "family": "curated", "ticker": ticker, "msg": "entry is not a dict"})
            continue
        if entry.get("_draft") is True:
            generated_at = _parse_iso(entry.get("_draft_generated_at"))
            if generated_at:
                days_old = (now - generated_at).days
                if days_old > DRAFT_STALE_DAYS:
                    findings.append({"severity": "yellow", "family": "curated", "ticker": ticker, "msg": f"draft stale ({days_old} days old)"})
        for field in ("one_liner", "hypothesis", "price_targets"):
            if field not in entry:
                findings.append({"severity": "yellow", "family": "curated", "ticker": ticker, "msg": f"missing field '{field}'"})
                break
    archived = curated.get("_archived") or {}
    if isinstance(archived, dict):
        for ticker, entry in archived.items():
            if isinstance(entry, dict) and not entry.get("outcome"):
                findings.append({"severity": "yellow", "family": "curated", "ticker": ticker, "msg": "archived but no outcome recorded"})
    findings.append({"severity": "info", "family": "curated", "msg": f"{sum(1 for key in curated if not key.startswith('_'))} active, {len(archived) if isinstance(archived, dict) else 0} archived"})
    return findings


def check_working_dir() -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    cutoff = _now() - timedelta(days=DIR_STALE_DAYS)
    patterns = [
        "convergence_report_*.json",
        "maintenance_log_*.md",
        "maintenance_log_*.jsonl",
        "health_report_*.json",
        "health_report_*.md",
        "calibration_*.json",
        "calibration_*.md",
        "candidate_monitor_report_*.json",
        "thesis_gate_audit_*.json",
    ]
    for pattern in patterns:
        stale = []
        for path in WORKING.glob(pattern):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    stale.append(path.name)
            except Exception:
                continue
        if stale:
            findings.append({"severity": "yellow", "family": "working_dir", "pattern": pattern, "msg": f"{len(stale)} files > {DIR_STALE_DAYS}d old", "sample": stale[:3]})
    big = []
    for path in WORKING.glob("*"):
        if path.is_file():
            try:
                size = path.stat().st_size
                if size > 50 * 1024 * 1024:
                    big.append((path.name, size))
            except Exception:
                continue
    for name, size in big:
        findings.append({"severity": "yellow", "family": "working_dir", "msg": f"{name} is {size / 1024 / 1024:.1f}MB"})
    return findings


def check_validation() -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    if not VALIDATION_REPORT.exists():
        return [{"severity": "yellow", "family": "validation", "msg": "_validation_report.json missing — run tools/validate_signal_log.py"}]
    age_h = _file_age_hours(VALIDATION_REPORT) or 0.0
    if age_h > VALIDATION_STALE_HOURS:
        findings.append({"severity": "yellow", "family": "validation", "msg": f"validation report is {age_h:.1f}h old (> {VALIDATION_STALE_HOURS}h threshold)"})
    try:
        report = json.loads(VALIDATION_REPORT.read_text(encoding="utf-8"))
    except Exception as exc:
        return findings + [{"severity": "red", "family": "validation", "msg": f"validation report unparseable: {exc}"}]
    for finding in report.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        findings.append(
            {
                "severity": finding.get("severity", "info"),
                "family": "validation",
                "check": finding.get("check"),
                "file": finding.get("field") or finding.get("check"),
                "msg": finding.get("msg", "?"),
            }
        )
    return findings


def _sev(severity: str) -> int:
    return SEVERITY_ORDER.get(severity, 0)


def run(dry_run: bool = False, quiet: bool = False) -> Dict[str, Any]:
    all_findings: List[Dict[str, Any]] = []
    all_findings += check_registry_coherence()
    all_findings += check_scanner_output_schema()
    drift_findings, snapshot = check_drift()
    all_findings += drift_findings
    all_findings += check_signal_log()
    all_findings += check_curated_integrity()
    all_findings += check_working_dir()
    all_findings += check_validation()

    by_family: Dict[str, List[Dict[str, Any]]] = {}
    max_severity = "green"
    counts = {"red": 0, "yellow": 0, "green": 0, "info": 0}
    for finding in all_findings:
        severity = finding.get("severity", "info")
        counts[severity] = counts.get(severity, 0) + 1
        by_family.setdefault(finding.get("family", "?"), []).append(finding)
        if _sev(severity) > _sev(max_severity):
            max_severity = severity

    result = {
        "ran_at_utc": _now().isoformat().replace("+00:00", "Z"),
        "max_severity": max_severity,
        "counts": counts,
        "by_family": by_family,
    }

    if not dry_run:
        timestamp = _now().strftime("%Y%m%d_%H%M")
        json_path = WORKING / f"health_report_{timestamp}.json"
        md_path = WORKING / f"health_report_{timestamp}.md"
        _atomic_write(json_path, json.dumps(result, indent=2, ensure_ascii=False))
        _atomic_write(md_path, _render_md(result))
        try:
            with open(HISTORY_PATH, "a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "ts": _now().isoformat().replace("+00:00", "Z"),
                            "max_severity": max_severity,
                            "counts": counts,
                            "snapshot": snapshot,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:
            pass
        result["_out_json"] = str(json_path)
        result["_out_md"] = str(md_path)
    return result


def _render_md(result: Dict[str, Any]) -> str:
    lines = [f"# System Health Report — {result['ran_at_utc'][:19]}Z", ""]
    severity = result["max_severity"].upper()
    lines.append(f"**Max severity**: {severity}")
    lines.append("")
    counts = result["counts"]
    lines.append(f"**Counts**: red={counts.get('red', 0)}, yellow={counts.get('yellow', 0)}, green={counts.get('green', 0)}, info={counts.get('info', 0)}")
    lines.append("")
    order = ["registry", "schema", "drift", "signal_log", "validation", "curated", "working_dir"]
    for family in order:
        items = result["by_family"].get(family, [])
        if not items:
            continue
        lines.append(f"## {family}")
        lines.append("")
        for item in sorted(items, key=lambda value: -_sev(value.get("severity", "info"))):
            tag = item.get("scanner") or item.get("file") or item.get("ticker") or item.get("pattern") or ""
            if tag:
                lines.append(f"- **{tag}** — {item.get('msg', '?')}")
            else:
                lines.append(f"- {item.get('msg', '?')}")
        lines.append("")
    lines.append("---")
    lines.append("_Generated by `tools/health_check.py`. History at `working/health_history.jsonl`._")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified system health check")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="Summary line only")
    args = parser.parse_args()
    result = run(dry_run=args.dry_run, quiet=args.quiet)
    if args.quiet:
        print(json.dumps({"max_severity": result["max_severity"], "counts": result["counts"]}, ensure_ascii=False))
    else:
        compact = {
            "ran_at_utc": result["ran_at_utc"],
            "max_severity": result["max_severity"],
            "counts": result["counts"],
            "by_family_counts": {key: len(value) for key, value in result["by_family"].items()},
            "out_json": result.get("_out_json"),
            "out_md": result.get("_out_md"),
        }
        print(json.dumps(compact, indent=2, ensure_ascii=False))
    if result["max_severity"] == "red":
        sys.exit(1)


if __name__ == "__main__":
    main()
