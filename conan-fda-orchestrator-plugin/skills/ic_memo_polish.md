---
name: ic-memo-polish
description: Polish a Stage-9 convergence_assessment into an operator-facing IC memo. Tightens prose, normalizes citations to the [F:short]/[D:short] notation if not already done, ensures bull/base/bear hypotheses each have at least 2 kill conditions, and emits the final operator-readable narrative. Operator-triggered only — Stage 1 does NOT fire this in parallel.
model: claude-sonnet-4-6
effort: medium
allowed-tools:
  - mcp__compute-mcp__base_rate
  - mcp__compute-mcp__similar_cases
context: fork
hooks:
  PreToolUse: [budget_check]
  PostToolUse: [log_observability]
output_schema: convergence_assessment_v1.json
memory_scope: per_asset
version: v0
provenance: "Stream 4 (2026-05-07) — operator-facing polish layer for Stage 10 output. Originally an inline pass inside runtime.py; promoted to a skill so operators can re-run it standalone after editing the underlying prose."
---

# IC Memo Polish Skill (v0)

## Role

Take a Stage-9 convergence_assessment row and polish it into the operator-facing IC memo prose. This is NOT a fan-out research sub-agent — it's a single-shot Sonnet pass that:

  1. Tightens cited_prose_blocks for readability without dropping any [F:fact_id] or [D:doc_id] cite.
  2. Verifies each hypothesis has bull/base/bear plus ≥2 kill_conditions (D-115). If missing, requests a re-run rather than silently dropping cites.
  3. Optionally fetches `compute-mcp.base_rate` + `similar_cases` to add the reference-class anchor sentence to the executive summary.
  4. Emits the final convergence_assessment_v1.json with refined cited_prose_blocks + reasoning_summary.

## When invoked

- Operator clicks "polish memo" on a watchlist or immediate-band assessment in the dashboard.
- Cron job that polishes top-band assessments nightly (optional; off by default).

## Inputs

| Field | Type | Notes |
|---|---|---|
| `assessment_id` | uuid | convergence_assessments.id |
| `assessment_payload` | object | full row contents (Stage 10 output) |
| `audience` | enum | `operator` \| `lp` \| `archive` — controls verbosity |

## Output

The same `convergence_assessment_v1.json` schema as Stage 10, with `cited_prose_blocks` rewritten and `reasoning_summary` populated. **All fact_id / doc_id citations preserved** — the polish layer must never drop a cite.

## Internal loop (single turn, no tool-use except optional compute-mcp lookup)

1. Load assessment_payload.
2. Optional: `compute-mcp.base_rate(reference_class)` if `reference_class` is set and the executive summary lacks the anchor sentence "Reference class N=… approval rate p…".
3. Pass the payload + skill instructions to Sonnet with `effort=medium`. Sonnet returns a polished JSON.
4. Diff fact_citations + doc_citations sets pre/post. Any missing cite → reject the polish (return original).
5. Schema validate. Return.

## Budget + latency

- Budget: $0.02–$0.05 (Sonnet 4.6 medium + 1-2 tool calls).
- Latency: 3-8s.

## Constitutional rules

- NEVER add a citation that wasn't already in the input; only re-arrange or contextualize existing cites.
- NEVER lower a stated conviction_pct_calibrated; that's Stage 8's domain.
- NEVER touch hypotheses[].kill_conditions count; if <2 on bull/base/bear, return error rather than fabricate.

## Provenance

v0 — operator-facing polish layer, first ship. Originally inline inside `runtime.py` Stage 10; promoted to a skill so operators can re-run after editing the underlying prose. First eval-gated revision becomes v1 once IC memo readability metrics are tracked.
