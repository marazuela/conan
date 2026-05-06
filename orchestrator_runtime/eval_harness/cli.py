"""Eval-harness CLI.

  python -m orchestrator_runtime.eval_harness.cli replay --version v0.1 --prompt-hash abc123
  python -m orchestrator_runtime.eval_harness.cli stats

Phase 0: replay uses the stub_orchestrator (harness wiring self-test).
Phase 2: --orchestrator-fn flag (or env var) selects the real orchestrator
runtime entry point.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import List

from orchestrator_runtime.eval_harness import gold_standard, metrics, replay


def cmd_replay(args: argparse.Namespace) -> int:
    cases: List[gold_standard.HarnessCase]
    if args.dev:
        cases = gold_standard.load_dev_set()
    elif args.all:
        cases = gold_standard.load_all()
    else:
        cases = gold_standard.load_holdout_set()

    if not cases:
        print("eval_harness: no rows loaded — Phase 0 curation pending. "
              "Add cases via the eval_harness table.", file=sys.stderr)
        return 1

    print(f"eval_harness: replaying {len(cases)} case(s) "
          f"(version={args.version}, prompt_hash={args.prompt_hash[:12]}…)")

    # Phase 0: stub orchestrator only. Phase 2 wires the real one.
    orchestrator_fn = replay.stub_orchestrator
    if args.orchestrator_fn:
        # Allow override via dotted path, e.g. orchestrator_runtime.runtime:run
        module_path, fn_name = args.orchestrator_fn.split(":")
        module = __import__(module_path, fromlist=[fn_name])
        orchestrator_fn = getattr(module, fn_name)

    per_case = replay.replay_all(
        cases, args.version, args.prompt_hash, orchestrator_fn,
    )
    result = metrics.aggregate(
        orchestrator_version=args.version,
        prompt_hash=args.prompt_hash,
        per_assessment_results=per_case,
        reference_brier=args.reference_brier,
    )

    print(f"\nBrier: {result.brier_score:.4f}  "
          f"AUC: {result.ranking_auc:.3f}  "
          f"n: {result.n_assessments}  "
          f"passed_gate: {result.passed_gate}")
    print("\nCalibration buckets:")
    for b in result.calibration.buckets:
        print(f"  [{b.lower:>5.1f},{b.upper:>5.1f})  "
              f"n={b.n:>3}  pred={b.predicted_mean:>5.1f}  "
              f"actual={b.actual_rate*100:>5.1f}  dev={b.deviation:+.1f}")

    if args.write_eval_run:
        from modal_workers.shared.supabase_client import SupabaseClient
        sb = SupabaseClient()
        sb._rest("POST", "eval_runs", json_body=result.as_eval_runs_row(),
                 prefer="return=minimal")
        print("\neval_runs row written.")

    return 0 if result.passed_gate else 2


def cmd_stats(_args: argparse.Namespace) -> int:
    holdout = gold_standard.load_holdout_set()
    dev = gold_standard.load_dev_set()
    print(f"holdout cases: {len(holdout)}")
    print(f"dev cases:     {len(dev)}")
    if holdout:
        outcome_dist: dict = {}
        for c in holdout:
            outcome_dist[c.realized_outcome] = outcome_dist.get(c.realized_outcome, 0) + 1
        print("holdout outcome distribution:")
        for outcome, n in sorted(outcome_dist.items(), key=lambda x: -x[1]):
            print(f"  {outcome:>30}  {n}")
    return 0


def main(argv: List[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(prog="eval_harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_replay = sub.add_parser("replay", help="Run a replay against the harness")
    p_replay.add_argument("--version", required=True,
                          help="orchestrator_version (e.g. orch-v0.1.0)")
    p_replay.add_argument("--prompt-hash", required=True,
                          help="sha256 hash of the system prompt")
    p_replay.add_argument("--orchestrator-fn", default=None,
                          help="dotted path to orchestrator entrypoint (default: stub)")
    p_replay.add_argument("--dev", action="store_true",
                          help="use dev set (is_holdout=false) instead of holdout")
    p_replay.add_argument("--all", action="store_true",
                          help="use all cases (holdout + dev)")
    p_replay.add_argument("--reference-brier", type=float, default=None,
                          help="reference Brier for gating (PR fails if > this + tolerance)")
    p_replay.add_argument("--write-eval-run", action="store_true",
                          help="persist result to eval_runs table")
    p_replay.set_defaults(func=cmd_replay)

    p_stats = sub.add_parser("stats", help="Print harness composition stats")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
