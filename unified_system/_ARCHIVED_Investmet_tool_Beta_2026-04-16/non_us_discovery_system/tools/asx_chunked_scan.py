"""
Chunked ASX scan with on-disk checkpointing.

Splits the 426-ticker universe into chunks, fetches announcements per chunk,
and persists raw signals incrementally. Designed to fit each chunk under the
45-second bash timeout, so the full scan can be completed across multiple
foreground bash calls.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from tools.asx_universe import load_universe
from tools import asx_scanner

CHECKPOINT_PATH = ROOT / "working" / "asx_chunked_state.json"


def _load_state():
    if CHECKPOINT_PATH.exists():
        try:
            return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"cursor": 0, "signals": [], "scan_date": None}


def _save_state(state):
    CHECKPOINT_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", type=int, default=80, help="tickers per call")
    ap.add_argument("--throttle", type=float, default=0.04)
    ap.add_argument("--window", type=int, default=7)
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    universe = load_universe(auto_refresh=False).get("tickers", [])
    if not universe:
        print("ERROR: empty universe")
        sys.exit(1)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = {"cursor": 0, "signals": [], "scan_date": today} if args.reset else _load_state()
    if state.get("scan_date") != today:
        state = {"cursor": 0, "signals": [], "scan_date": today}

    start = state["cursor"]
    end = min(start + args.chunk, len(universe))
    if start >= len(universe):
        print(f"DONE: {len(universe)} tickers processed; signals collected: {len(state['signals'])}")
        return

    chunk = universe[start:end]
    print(f"Chunk {start}:{end} of {len(universe)}")

    # Monkey-patch the scanner to use only this chunk
    import tools.asx_scanner as scn
    orig = scn._load_universe
    scn._load_universe = lambda: chunk
    try:
        t0 = time.time()
        new_signals = scn.fetch_raw_signals(window_days=args.window, throttle_seconds=args.throttle)
        elapsed = time.time() - t0
    finally:
        scn._load_universe = orig

    state["signals"].extend(new_signals)
    state["cursor"] = end
    _save_state(state)

    print(f"Chunk done in {elapsed:.1f}s — new signals: {len(new_signals)} — total: {len(state['signals'])}")
    print(f"Cursor now {end}/{len(universe)}")


if __name__ == "__main__":
    main()
