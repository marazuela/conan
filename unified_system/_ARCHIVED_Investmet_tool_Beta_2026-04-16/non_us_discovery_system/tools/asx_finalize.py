"""
Stage-based finalize with checkpointing for the chunked ASX scan.

Stages:
  1. triage  — fast, no I/O
  2. resolve — OpenFIGI calls, checkpointed (resume-friendly)
  3. dedup   — convergence engine, fast
  4. score   — fast
  5. write   — persist processed file + signal_log entries

State file: working/asx_finalize_state.json
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

from tools import pipeline_runner as pr
from tools import convergence_engine

CHECKPOINT_PATH = ROOT / "working" / "asx_chunked_state.json"
STATE_PATH = ROOT / "working" / "asx_finalize_state.json"
SIGNALS_DIR = ROOT / "signals"


def _load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"stage": "triage", "triaged": [], "resolved": [], "resolve_cursor": 0}


def _save_state(s):
    STATE_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--resolve-batch", type=int, default=20)
    ap.add_argument("--time-budget", type=float, default=38.0)
    args = ap.parse_args()

    chunked = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    raw = chunked["signals"]
    scan_date = chunked.get("scan_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = {"stage": "triage", "triaged": [], "resolved": [], "resolve_cursor": 0} if args.reset else _load_state()

    t_start = time.time()

    # Stage 1: triage
    if state["stage"] == "triage":
        novelty = {s.get("source_content_hash") for s in pr._load_historical_log() if s.get("source_content_hash")}
        triaged = []
        drops = {}
        for sig in raw:
            ok, r = pr.triage(sig, "ASX", novelty)
            if ok:
                triaged.append(sig)
            else:
                drops[r] = drops.get(r, 0) + 1
        state["triaged"] = triaged
        state["stage"] = "resolve"
        state["resolve_cursor"] = 0
        state["resolved"] = []
        print(f"[triage] kept {len(triaged)}/{len(raw)} (drops {drops})")
        _save_state(state)

    # Stage 2: resolve (checkpointed)
    if state["stage"] == "resolve":
        triaged = state["triaged"]
        cur = state["resolve_cursor"]
        resolved = state["resolved"]
        end = min(cur + args.resolve_batch, len(triaged))
        print(f"[resolve] {cur}:{end} of {len(triaged)}")
        for i in range(cur, end):
            if time.time() - t_start > args.time_budget:
                state["resolve_cursor"] = i
                state["resolved"] = resolved
                _save_state(state)
                print(f"[resolve] paused at cursor {i} (time budget hit)")
                return
            sig = pr.resolve_entity(triaged[i])
            resolved.append(sig)
        state["resolve_cursor"] = end
        state["resolved"] = resolved
        if end >= len(triaged):
            state["stage"] = "post"
            print(f"[resolve] done — {len(resolved)} processed, {sum(1 for s in resolved if s.get('figi'))} resolved")
        else:
            print(f"[resolve] partial — {end}/{len(triaged)} so far")
        _save_state(state)
        if state["stage"] != "post":
            return

    # Stage 3+: dedup, score, write — all fast
    if state["stage"] == "post":
        resolved_ok = [s for s in state["resolved"] if s.get("figi")]
        print(f"[dedup] input: {len(resolved_ok)}")
        historical = pr._load_historical_log()
        processed = convergence_engine.process(resolved_ok, historical)
        survivors = [s for s in processed if not s.get("dedup_dropped")]
        print(f"[dedup] survivors: {len(survivors)}")
        scored = [pr.score_signal(s) for s in survivors]
        summary = {"immediate": 0, "watchlist": 0, "archive": 0, "discard": 0, "manual_review": 0}
        for sig in scored:
            r = pr.route(sig)
            sig["_routing"] = r
            summary[r] = summary.get(r, 0) + 1
        print(f"[route] {summary}")

        scored_sorted = sorted(
            [s for s in scored if s.get("score_total") is not None],
            key=lambda s: -s["score_total"],
        )
        print("\nTop 15 by score:")
        for s in scored_sorted[:15]:
            hdl = (s.get("raw_data") or {}).get("headline", "")[:75]
            print(f"  {s.get('score_total'):>5}  {s.get('_routing'):>10}  {s.get('ticker_local'):>5} | {s.get('signal_type'):>24} | {hdl}")

        out = SIGNALS_DIR / f"asx_{scan_date}_processed.json"
        out.write_text(json.dumps(scored, indent=2))
        print(f"\nWrote {out}")

        log_entries = [{
            "signal_id": s.get("signal_id"),
            "issuer_figi": s.get("issuer_figi"),
            "ticker_plus_mic": s.get("ticker_plus_mic"),
            "scan_date": scan_date,
            "source_date": s.get("source_date"),
            "source_content_hash": s.get("source_content_hash"),
            "scanner": "asx",
            "score_total": s.get("score_total"),
            "routing": s.get("_routing"),
        } for s in scored]
        pr._append_log(log_entries)
        print(f"Appended {len(log_entries)} to signal_log.json")

        state["stage"] = "done"
        _save_state(state)


if __name__ == "__main__":
    main()
