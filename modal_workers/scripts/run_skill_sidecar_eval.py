"""Score single-shot skill sidecar outputs against eval_harness.

The sidecar is deliberately eval-only in Phases 0-2: this script consumes
outputs produced by `.claude/skills/assess-fda-binary-catalyst/SKILL.md` and
compares them through the same metrics path as the live v4 orchestrator.

Run:
  python3 -m modal_workers.scripts.run_skill_sidecar_eval \
    --output-dir skills/assess-fda-binary-catalyst/outputs \
    --reference-brier 0.21
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from modal_workers.shared.supabase_client import SupabaseClient
from orchestrator_runtime.eval_harness.gold_standard import load_holdout_set
from orchestrator_runtime.eval_harness.metrics import aggregate
from orchestrator_runtime.eval_harness.replay import replay_all
from orchestrator_runtime.eval_harness.skill_sidecar import make_skill_sidecar_fn


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="run_skill_sidecar_eval")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--version", default="assess_fda_binary_catalyst_sidecar_v0")
    parser.add_argument("--prompt-hash", default="skill-file")
    parser.add_argument("--reference-brier", type=float)
    parser.add_argument("--write-eval-run", action="store_true")
    args = parser.parse_args(argv)

    cases = load_holdout_set(SupabaseClient())
    results = replay_all(
        cases,
        orchestrator_version=args.version,
        prompt_hash=args.prompt_hash,
        orchestrator_fn=make_skill_sidecar_fn(Path(args.output_dir)),
    )
    agg = aggregate(
        args.version,
        args.prompt_hash,
        results,
        reference_brier=args.reference_brier,
    )
    row = agg.as_eval_runs_row()
    print(json.dumps(row, indent=2, sort_keys=True, default=str))

    if args.write_eval_run:
        sb = SupabaseClient()
        existing = sb._rest(
            "GET",
            "eval_runs",
            params={
                "select": "id",
                "orchestrator_version": f"eq.{args.version}",
                "prompt_hash": f"eq.{args.prompt_hash}",
                "limit": "1",
            },
        ) or []
        if existing:
            sb._rest(
                "PATCH",
                "eval_runs",
                params={"id": f"eq.{existing[0]['id']}"},
                json_body=row,
                prefer="return=minimal",
            )
        else:
            sb._rest("POST", "eval_runs", json_body=row, prefer="return=minimal")

    return 0 if agg.passed_gate else 1


if __name__ == "__main__":
    sys.exit(main())
