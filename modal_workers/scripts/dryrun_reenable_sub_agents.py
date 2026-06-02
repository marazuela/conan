#!/usr/bin/env python3
"""Dry-run validator for re-enabling the literature + commercial_opportunity sub-agents.

CONTEXT
  #187 disabled both roles (ORCH_DISABLE_*) after a 0/N schema_pass cost audit.
  #188 fixed the root cause (per-role token cap + commercial label-bloat projection
  + skill tool-name drift) but deliberately left them DISABLED "until a dry-run
  validates them". #189 added a graceful degraded-payload fallback + one-shot retry.

  CONSEQUENCE FOR THIS VALIDATOR: with #189, schema_pass=True is no longer proof a
  role WORKS — the degraded fallback makes it trivially true (empty papers[] +
  partial_output=true still validates). So the real acceptance signal is REAL
  CONTENT: partial_output == false AND non-trivial structured output
    - literature: >= 1 paper AND a synthesis.summary
    - commercial_opportunity: >= 1 standard_of_care OR soc_side_effect
  This script reports both and emits GO / REVIEW / NO-GO per role.

WHAT IT DOES
  - Picks N active fda_assets with a drug_name + indication (or use --asset-id).
  - Builds the SAME asset_context Stage 1 builds (orchestrator_runtime/runtime.py:718).
  - Runs each role's runner DIRECTLY via runner.run(..., budget_token_cap=PER_ROLE_BUDGET_TOKENS),
    which (a) ignores ORCH_DISABLE_* so disabled roles can be validated, and (b) writes
    NO sub_agent_calls / failed_reactor_events rows (no production-table pollution).
  Faithful to production: same runner classes, same per-role budget cap (#188), same
  asset_context shape — just isolated from the live dispatch/logging path.

COST / SAFETY
  Makes LIVE Anthropic + provider (PubMed / openFDA) calls: ~$0.4-0.9 per role per asset.
  Default 2 roles x 2 assets = ~4 calls (~$2-3). Requires --confirm to fire calls;
  without it, prints the plan + estimate and exits 0.

ENV
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   (asset picking)
  ANTHROPIC_API_KEY                         (the runners' OrchestratorClient)
  POLYGON_API_KEY                           (only if validating options_microstructure)

RUN
  python -m modal_workers.scripts.dryrun_reenable_sub_agents --confirm
  python -m modal_workers.scripts.dryrun_reenable_sub_agents --asset-id <uuid> --asset-id <uuid> --confirm
  python -m modal_workers.scripts.dryrun_reenable_sub_agents --roles literature --n 3 --confirm

ON GO -> RE-ENABLE
  1. Ensure PR #189 (degraded fallback) + #188 are on main.
  2. Redeploy the orchestrator on latest main (from the gated worktree, HEAD==origin/main).
  3. Remove env vars ORCH_DISABLE_LITERATURE / ORCH_DISABLE_COMMERCIAL_OPPORTUNITY.
  4. Monitor the next ~10 runs:
       select role, count(*), count(*) filter (where schema_pass) pass,
              count(*) filter (where (output->>'partial_output')::bool) partial,
              round(avg(cost_usd)::numeric,3) avg_cost
       from sub_agent_calls where created_at > now()-interval '1 day' group by role;
  ROLLBACK: re-add the ORCH_DISABLE_* env vars + redeploy (role -> $0, < 1 min).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

# repo root on path so `python modal_workers/scripts/<this>.py` works too
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

REAL_CONTENT_THRESHOLD = 0.6

ROLE_QUESTIONS = {
    "literature": (
        "What is the pivotal clinical-trial evidence and the key efficacy and safety "
        "findings for {drug} in {indication}? Surface the most relevant peer-reviewed papers."
    ),
    "commercial_opportunity": (
        "Assess the commercial opportunity for {drug} in {indication}: TAM, the current "
        "standard of care and its limitations/side-effects, unmet-need severity, and the "
        "FDA regulatory incentives the program likely has."
    ),
    "competitive": (
        "Map the competitive pipeline and the differentiation/moat for {drug} in {indication}."
    ),
    "regulatory_history": (
        "Summarise prior AdComms, analogous approvals, and FDA-staff concerns relevant to "
        "{drug} in {indication}."
    ),
}


def _pick_assets(n: int, asset_ids: List[str]) -> List[Dict[str, Any]]:
    from modal_workers.shared.supabase_client import SupabaseClient
    sb = SupabaseClient()
    select = "id,ticker,drug_name,indication,reference_class_signature"
    if asset_ids:
        params = {"select": select, "id": f"in.({','.join(asset_ids)})"}
    else:
        params = {
            "select": select,
            "is_active": "eq.true",
            "drug_name": "not.is.null",
            "indication": "not.is.null",
            "order": "next_catalyst_date.asc.nullslast",
            "limit": str(n),
        }
    return sb._rest("GET", "fda_assets", params=params) or []


def _asset_context(row: Dict[str, Any]) -> Dict[str, Any]:
    # Mirrors orchestrator_runtime/runtime.py:718 exactly.
    return {
        "asset_id": row.get("id"),
        "ticker": row.get("ticker"),
        "drug_name": row.get("drug_name"),
        "indication": row.get("indication"),
        "reference_class": row.get("reference_class_signature"),
    }


def _content_ok(role: str, output: Dict[str, Any]) -> bool:
    if not isinstance(output, dict):
        return False
    if role == "literature":
        papers = output.get("papers")
        summary = (output.get("synthesis") or {}).get("summary")
        return isinstance(papers, list) and len(papers) >= 1 and bool(summary)
    if role == "commercial_opportunity":
        soc = output.get("standard_of_care") or []
        aes = output.get("soc_side_effects") or []
        return len(soc) >= 1 or len(aes) >= 1
    # generic: non-empty and not flagged partial
    return bool(output) and not output.get("partial_output")


def _validate_one(role: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    from modal_workers.sub_agents import ROLE_REGISTRY, SubAgentSchemaError
    from orchestrator_runtime.sub_agent_dispatcher import PER_ROLE_BUDGET_TOKENS

    rec: Dict[str, Any] = {
        "role": role,
        "asset": ctx.get("drug_name") or ctx.get("ticker") or ctx.get("asset_id"),
        "schema_pass": False, "partial": None, "content_ok": False,
        "n_papers": None, "n_soc": None,
        "cost_usd": 0.0, "tokens": 0, "latency_ms": 0, "error": None,
    }
    runner_cls = ROLE_REGISTRY.get(role)
    if runner_cls is None:
        rec["error"] = f"unknown role {role}"
        return rec
    question = ROLE_QUESTIONS.get(role, "Assess {drug} in {indication}.").format(
        drug=ctx.get("drug_name") or "the asset",
        indication=ctx.get("indication") or "its indication",
    )
    try:
        result = runner_cls().run(
            question=question, asset_context=ctx, budget_token_cap=PER_ROLE_BUDGET_TOKENS,
        )
        out = result.output if isinstance(result.output, dict) else {}
        rec["schema_pass"] = bool(result.schema_pass)
        rec["partial"] = bool(out.get("partial_output"))
        rec["content_ok"] = _content_ok(role, out)
        rec["cost_usd"] = round(result.cost_usd, 4)
        rec["tokens"] = result.tokens_input + result.tokens_output
        rec["latency_ms"] = result.latency_ms
        if role == "literature":
            rec["n_papers"] = len(out.get("papers") or [])
        if role == "commercial_opportunity":
            rec["n_soc"] = len(out.get("standard_of_care") or [])
    except SubAgentSchemaError as exc:
        rec["error"] = f"schema_fail: {exc.errors[:1]}"
        rec["cost_usd"] = round(exc.cost_usd or 0.0, 4)
        rec["tokens"] = (exc.tokens_input or 0) + (exc.tokens_output or 0)
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="Dry-run validate sub-agent roles before re-enable.")
    ap.add_argument("--roles", default="literature,commercial_opportunity",
                    help="comma-separated roles to validate")
    ap.add_argument("--n", type=int, default=2,
                    help="number of assets to auto-pick (ignored if --asset-id given)")
    ap.add_argument("--asset-id", action="append", default=[], help="explicit asset id (repeatable)")
    ap.add_argument("--confirm", action="store_true",
                    help="actually make live API calls (otherwise prints the plan and exits)")
    ap.add_argument("--json", action="store_true", help="also emit JSON results")
    args = ap.parse_args()

    roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    assets = _pick_assets(args.n, args.asset_id)
    if not assets:
        print("No matching assets found (need is_active + drug_name + indication).", file=sys.stderr)
        return 2

    n_calls = len(assets) * len(roles)
    print(f"Plan: {len(roles)} role(s) x {len(assets)} asset(s) = {n_calls} live call(s), "
          f"~${n_calls * 0.6:.1f} est.")
    for a in assets:
        print(f"  - {a.get('drug_name')} ({a.get('ticker')}) / {a.get('indication')}")
    if not args.confirm:
        print("\nDRY (no calls made). Re-run with --confirm to execute the validation.")
        return 0

    results: List[Dict[str, Any]] = []
    print()
    for a in assets:
        ctx = _asset_context(a)
        for role in roles:
            rec = _validate_one(role, ctx)
            results.append(rec)
            real = rec["schema_pass"] and not rec["partial"] and rec["content_ok"]
            mark = "OK" if real else ("DEGRADED" if rec["schema_pass"] else "FAIL")
            print(f"  [{mark:>8}] {role:<22} {str(rec['asset'])[:26]:<26} "
                  f"papers={rec['n_papers']} soc={rec['n_soc']} "
                  f"${rec['cost_usd']:.3f} {rec['tokens']}tok {rec['latency_ms']}ms"
                  + (f"  ERR={rec['error']}" if rec["error"] else ""))

    print("\n=== VERDICT ===")
    overall_go = True
    for role in roles:
        rr = [r for r in results if r["role"] == role]
        n = len(rr)
        any_fail = any((not r["schema_pass"]) or r["error"] for r in rr)
        real = sum(1 for r in rr if r["schema_pass"] and not r["partial"] and r["content_ok"])
        real_frac = (real / n) if n else 0.0
        if any_fail:
            verdict, ok = "NO-GO (a call failed schema / crashed)", False
        elif real_frac >= REAL_CONTENT_THRESHOLD:
            verdict, ok = f"GO ({real}/{n} real content)", True
        else:
            verdict, ok = (f"REVIEW ({real}/{n} real; rest degraded — #188 budget/label "
                           f"fix may be insufficient)", False)
        overall_go = overall_go and ok
        print(f"  {role:<22} {verdict}")
    print(f"\nOVERALL: {'GO — safe to re-enable' if overall_go else 'NOT GO — see above'}")

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    return 0 if overall_go else 1


if __name__ == "__main__":
    raise SystemExit(main())
