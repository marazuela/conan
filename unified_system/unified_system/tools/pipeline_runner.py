"""
Unified Pipeline Runner — dispatches due scanners based on scanner_registry.json.

Per D-014 (subprocess isolation): each scanner runs in its own subprocess
with a 120s hard kill. Scanner crashes do not propagate.

Per D-052 (atomic writes): registry updates after each scanner completion
are atomic (tmp + rename).

Usage:
    python3 tools/pipeline_runner.py               # run all due scanners
    python3 tools/pipeline_runner.py --scanner edgar_filing_monitor
    python3 tools/pipeline_runner.py --force-all   # ignore cadence, run everything
    python3 tools/pipeline_runner.py --dry-run     # print plan, don't execute
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional

REPO = Path(__file__).parent.parent
REGISTRY_PATH = REPO / "config" / "scanner_registry.json"
SIGNAL_LOG = REPO / "signals" / "signal_log.json"


def _read_registry() -> Dict:
    with open(REGISTRY_PATH) as f:
        return json.load(f)


def _atomic_write(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def _write_registry(reg: Dict) -> None:
    reg["_last_updated"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _atomic_write(REGISTRY_PATH, reg)


def _is_due(scanner: Dict, now: datetime) -> bool:
    if scanner["status"] not in ("operational",):
        return False
    last_run_s = scanner.get("last_run_utc")
    if last_run_s is None:
        return True
    try:
        last = datetime.fromisoformat(last_run_s.replace("Z", "+00:00"))
    except Exception:
        return True
    elapsed = now - last
    cadence = scanner.get("cadence", "daily")
    if cadence == "3h":
        return elapsed >= timedelta(hours=3)
    if cadence == "daily":
        return last.date() < now.date()
    if cadence == "on_demand":
        return False
    return False


def _run_scanner_subprocess(scanner: Dict, soft_timeout_s: int, hard_timeout_s: int) -> Dict:
    name = scanner["name"]
    tool_path = REPO / scanner["tool_path"]
    if not tool_path.exists():
        return {"status": "skipped", "reason": f"missing tool {tool_path}", "signals": 0}

    cmd = [sys.executable, str(tool_path)]
    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO),
            timeout=hard_timeout_s,
            capture_output=True,
            text=True,
            env={**os.environ, "UNIFIED_SOFT_TIMEOUT": str(soft_timeout_s)},
        )
        elapsed = time.time() - start
        if proc.returncode != 0:
            return {
                "status": "error",
                "reason": f"exit {proc.returncode}",
                "signals": 0,
                "elapsed_s": elapsed,
                "stderr_tail": proc.stderr[-2000:] if proc.stderr else "",
            }
        # By convention scanners print JSON summary on final line: {"signals": N, ...}
        summary_line = (proc.stdout or "").strip().splitlines()[-1:] if proc.stdout else []
        signals = 0
        if summary_line:
            try:
                parsed = json.loads(summary_line[0])
                signals = int(parsed.get("signals", 0))
            except Exception:
                pass
        return {
            "status": "ok",
            "signals": signals,
            "elapsed_s": elapsed,
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "signals": 0, "elapsed_s": hard_timeout_s}
    except Exception as e:
        return {"status": "error", "reason": str(e), "signals": 0, "elapsed_s": time.time() - start}


def run(scanner_filter: Optional[str] = None, force_all: bool = False, dry_run: bool = False) -> Dict:
    reg = _read_registry()
    now = datetime.now(timezone.utc)

    results: Dict[str, Dict] = {}

    for scanner in reg["scanners"]:
        name = scanner["name"]
        if scanner_filter and name != scanner_filter:
            continue
        if not force_all and not _is_due(scanner, now):
            results[name] = {"status": "not_due"}
            continue
        if scanner["status"] != "operational":
            results[name] = {"status": "non_operational", "registry_status": scanner["status"]}
            continue
        if dry_run:
            results[name] = {"status": "dry_run_would_execute"}
            continue

        print(f"→ running {name}", flush=True)
        r = _run_scanner_subprocess(
            scanner,
            soft_timeout_s=scanner.get("timeout_soft_s", 60),
            hard_timeout_s=scanner.get("timeout_hard_s", 120),
        )
        results[name] = r
        scanner["last_run_utc"] = now.isoformat().replace("+00:00", "Z")
        scanner["last_run_status"] = r["status"]
        scanner["last_run_signals"] = r.get("signals", 0)

    if not dry_run:
        _write_registry(reg)

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scanner", help="Run only this scanner")
    ap.add_argument("--force-all", action="store_true", help="Ignore cadence")
    ap.add_argument("--dry-run", action="store_true", help="Print plan, don't execute")
    args = ap.parse_args()

    results = run(scanner_filter=args.scanner, force_all=args.force_all, dry_run=args.dry_run)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()

# --- END OF FILE ---
