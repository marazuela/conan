"""
backfill_signal_log — one-time idempotent normalizer for signal_log.json.

Normalizations applied (each signal is touched at most once per pass; re-runs
are no-ops):

  1. `_scanner` -> `scanner`
  2. Populate missing `scoring_profile` via `tools.profile_map.profile_for`
  3. Derive missing `ticker` from `ticker_plus_mic`

Usage:
    python3 tools/backfill_signal_log.py
    python3 tools/backfill_signal_log.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).parent.parent
SIGNAL_LOG = REPO / "signals" / "signal_log.json"

sys.path.insert(0, str(Path(__file__).parent))
from profile_map import profile_for  # noqa: E402


def _load(path: Path):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("signals"), list):
        return data["signals"]
    raise SystemExit(f"signal_log.json has unexpected shape: {type(data).__name__}")


def _save_atomic(path: Path, signals):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(signals, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _ticker_from_tpm(ticker_plus_mic: str):
    if not ticker_plus_mic or "." not in ticker_plus_mic:
        return None
    head = ticker_plus_mic.split(".", 1)[0].strip()
    return head or None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    args = parser.parse_args()

    signals = _load(SIGNAL_LOG)
    scanner_copied = 0
    profile_added = 0
    ticker_added = 0
    still_no_profile = 0
    unresolved_breakdown: dict[str, int] = {}

    for signal in signals:
        if not isinstance(signal, dict):
            continue

        if not signal.get("scanner") and signal.get("_scanner"):
            signal["scanner"] = signal["_scanner"]
            scanner_copied += 1

        if not signal.get("scoring_profile"):
            scanner = signal.get("scanner") or signal.get("upstream_scanner") or signal.get("scanner_source") or signal.get("_scanner")
            signal_type = signal.get("signal_type")
            profile = profile_for(signal_type, scanner)
            if profile:
                signal["scoring_profile"] = profile
                profile_added += 1
            else:
                still_no_profile += 1
                key = f"{scanner or '<no-scanner>'}|{signal_type or '<no-type>'}"
                unresolved_breakdown[key] = unresolved_breakdown.get(key, 0) + 1

        if not signal.get("ticker"):
            ticker_plus_mic = signal.get("ticker_plus_mic") or signal.get("ticker_local")
            ticker = _ticker_from_tpm(ticker_plus_mic) if ticker_plus_mic and "." in (ticker_plus_mic or "") else ticker_plus_mic
            if ticker:
                signal["ticker"] = ticker
                ticker_added += 1

    report = {
        "ran_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "signal_log_size": len(signals),
        "scanner_field_normalized": scanner_copied,
        "scoring_profile_added": profile_added,
        "ticker_derived_from_tpm": ticker_added,
        "still_unresolved_profile": still_no_profile,
        "unresolved_breakdown": dict(sorted(unresolved_breakdown.items(), key=lambda kv: -kv[1])[:20]),
        "dry_run": bool(args.dry_run),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))

    if args.dry_run:
        return

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = SIGNAL_LOG.with_name(f"signal_log.backfill_pre_{ts}.bak.json")
    backup.write_text(SIGNAL_LOG.read_text(encoding="utf-8"), encoding="utf-8")
    _save_atomic(SIGNAL_LOG, signals)
    print(f"\nWrote backup -> {backup.name}")
    print(f"Wrote signal_log.json ({len(signals)} records)")


if __name__ == "__main__":
    main()
