"""
Thin CLI wrapper around pipeline_runner.run(). This is the entry point invoked
by the operational skill.

Usage:
    python tools/run_scanner.py --scanner lse_rns --window 7d
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from tools import pipeline_runner


def _parse_window(s: str) -> int:
    """Accepts '7d', '72h', '3', or plain integer number of days."""
    s = s.strip().lower()
    m = re.match(r"^(\d+)\s*d$", s)
    if m:
        return int(m.group(1))
    m = re.match(r"^(\d+)\s*h$", s)
    if m:
        hours = int(m.group(1))
        return max(1, hours // 24)
    try:
        return int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid window: {s}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scanner", required=True, choices=list(pipeline_runner.SCANNER_REGISTRY))
    parser.add_argument("--window", type=_parse_window, default=7)
    args = parser.parse_args()
    summary = pipeline_runner.run(args.scanner, args.window)
    print(json.dumps(summary, indent=2))
    # exit nonzero if scanner produced errors
    return 1 if summary.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
