---
name: bulk-orchestrator
description: Tier-2 single-shot synthesis path for FDA assets. Reads extracted_facts + memory file + reference class anchor for the asset, emits a convergence_assessment_v1.json (conviction_pct, thesis_direction, hypothesis stack, kill_conditions, citations, uncertainties). Cheaper than Tier-1 — no parallel sub-agent fan-out, no ensemble — but covers more breadth daily. Escalates to Tier-1 when it lands a high-conviction or direction-changing read.
model: claude-sonnet-4-6
effort: xhigh
allowed-tools:
  - mcp__internal-rag-mcp__hybrid_search
  - mcp__internal-rag-mcp__get_chunk
  - mcp__internal-rag-mcp__get_document_summary
  - mcp__compute-mcp__base_rate
  - mcp__compute-mcp__similar_cases
  - mcp__compute-mcp__isotonic_calibrate
context: fork
hooks:
  PreToolUse: [budget_check]
  PostToolUse: [log_observability]
output_schema: convergence_assessment_v1.json
memory_scope: per_asset
version: v0
provenance: "Phase 5.7 (2026-05-07) — Tier-2 path called from Cowork on a daily/weekly cadence based on watch_priority. Methodology compresses Stages 1/2/3/4/8 of the Tier-1 pipeline into a single Sonnet pass with strict output validation; Stages 6/7 (ensemble + constitutional) are deliberately skipped for cost. First eval-gated revision becomes v1."
---

# Bulk Orchestrator (Tier 2) — v0

## Role

Produce a `convergence_assessment_v1.json` row for one FDA asset in a single Sonnet pass. The output schema and persistence target match the Tier-1 orchestrator (`convergence_assessments` table) — Tier-2 is identifiable by `convergence_assessments.tier=2` (set by the caller), and the per-asset memory file appends a `## Recent assessments` entry exactly the same way Tier-1 does (per D-123 Contract C5).

The Tier-2 path is the **breadth lever** in v3:
- **Tier 1** (API-SDK, parallel sub-agents, ensemble, constitutional check): high-conviction depth on event-driven assets. ~$10–15 / run, ~3–4 min hot. Triggered by `new_doc`, `cross_source`, `operator_refresh`, `tier2_escalation`.
- **Tier 2** (this skill, single Sonnet call): daily / weekly coverage on the watch list. ~$0.30–0.80 / run, ~30–60s. Triggered by `scheduled` cadence + the `watch_priority` bucket.

## When invoked

- Cowork routine `bulk_orchestrator_run` fires on cadence per `fda_assets.watch_priority`:
  - `=1` → daily
  - `=2` → weekly
  - `=3..5` → event-only (skip; Tier 1 takes over via cross_source / new_doc triggers)
- Operator-refresh fallback when Tier 1 is rate-limited.

## Inputs

The Cowork routine pre-populates the workspace with one JSON blob per asset:

```json
{
  "asset_id": "<uuid>",
  "ticker": "AXSM",
  "drug_name": "AXS-05",
  "indication": "...",
  "reference_class_signature": "phase3_oncology_breakthrough_no_prior_crl",
  "memory_path": "memory_files/asset/<asset_id>.md",
  "evidence_packet": {
    "ok": true,
    "errors": [],
    "counts": {
      "material_primary_documents": 1,
      "extracted_facts": 8,
      "asset_documents": 5
    }
  },
  "extracted_facts": [...],         // up to 200 most-recent extracted_facts rows
  "asset_documents": [...],         // up to 50 most-recent asset_documents rows
  "prior_assessment": {...} | null  // latest non-superseded convergence_assessments row, if any
}
```

The harness only enqueues blobs whose Tier-2 `evidence_packet.ok=true`: ticker + drug identity and at least one material primary/safety document link. The skill reads this blob, loads the memory file (if present) via the file system path, and proceeds.

## Process

1. **Pull base-rate anchor.** Call `compute-mcp.base_rate` with the asset's `reference_class_signature`. If `n_cases < 30`, mark anchor as `n_below_threshold` and lower the synthesis confidence by one bucket.

2. **Pull 3–5 similar resolved cases.** Call `compute-mcp.similar_cases` to ground reasoning in priors that actually resolved. Cite by `assessment_id` in the output `citations[]`.

3. **Triangulate via RAG.** Two `internal-rag-mcp.hybrid_search` calls on the asset:
   - Drug + indication phrase → top-8 across `literature` + `filings` corpora.
   - PDUFA / AdComm / endpoint phrase → top-8 across `labels_aes` + `news` corpora.

4. **Synthesize.** In one Sonnet pass produce:
   - `thesis_direction` ∈ `long | short | neutral | straddle`
   - `raw_conviction_pct` ∈ [0, 100], pre-calibration
   - `hypotheses` — a JSON **array** (never an object/map) of exactly 3
     elements, one each for the bull, base, and bear case. Each element is an
     object: `{"label": "bull"|"base"|"bear", "claim": "...",
     "kill_conditions": ["...", "..."]}` with **≥2** `kill_conditions`
     entries (D-115 contract). Emit `[ {...}, {...}, {...} ]`, NOT
    `{"bull": {...}, "base": {...}, "bear": {...}}`. The server-side
    harness canonicalizes the keyed object shape for compatibility, but the
    preferred emitted schema is the array form above.
   - `cited_prose_blocks[]` — 4–8 short paragraphs, every claim cited via `[F:fact_id]` or `[D:doc_id]` notation
   - `uncertainties[]` — explicit gaps that would warrant a Tier-1 escalation
   - `evidence_quality` ∈ [0, 1] reflecting how much of the synthesis rests on cited primary sources vs inferred priors

5. **Calibrate.** Call `compute-mcp.isotonic_calibrate` with `raw_conviction_pct`. The MCP applies the active `calibration_curves` row and returns `conviction_pct_calibrated`. Set the final `conviction_pct = conviction_pct_calibrated` (Tier 2 has no ensemble shrinkage; Tier 1 subtracts dispersion).

6. **Emit.** Return ONLY a `convergence_assessment_v1.json` payload. The Cowork harness writes it to `convergence_assessments` with `tier=2`, `orchestrator_version='bulk_v0'`, and triggers the standard fanout / alert pipeline.

## Escalation rule

After emitting, the harness checks the new row against the prior assessment for the same asset and enqueues a Tier-1 run within 1 hour if **any** of:

- `conviction_pct >= 60` (high-conviction read deserves the depth pass)
- `conviction_pct >= 45` AND `evidence_quality <= 0.45` (material but under-evidenced read needs a depth pass, not a quiet discard)
- `thesis_direction != prior.thesis_direction` (direction change)
- `document_ids` includes a doc not in `prior.document_ids` AND that doc's `doc_type IN ('label','adcomm_briefing','crl','complete_response_letter','press_release_pdufa')` (new primary doc)

The harness writes `extensions.escalated_to_tier1=true` on the row and inserts an `orchestrator_runs` row with `trigger_type='tier2_escalation'`.

## Budget envelope

- Hard ceiling per run: $1.50 (vs $15 for Tier 1).
- Token cap: 60K input + 8K output (vs 250K for Tier 1).
- Latency target: P50 < 60s, P95 < 180s.
- The PreToolUse `budget_check` hook enforces these.

## Output schema

`output_schema: convergence_assessment_v1.json` — same schema as Tier 1. Fields populated:

- ✅ `schema_version`, `asset_id`, `tier=2`, `orchestrator_version`
- ✅ `thesis_direction`, `raw_conviction_pct`, `conviction_pct`, `conviction_pct_calibrated`, `band`
- ✅ `hypotheses` — JSON array of 3 objects (bull/base/bear), each with `label`, `claim`, and ≥2 `kill_conditions` (array). Never a keyed object.
- ✅ `cited_prose_blocks`, `key_facts`, `uncertainties`, `citations`
- ✅ `reference_class`, `reference_class_base_rate`, `similar_resolved_case_ids`
- ✅ `evidence_quality`
- ✅ `gate_status` — **must be `"tier2_skipped"`** (PR-5, 2026-05-14). The
  Tier-2 path is architecturally exempt from the Stage 7 constitutional check;
  this column makes the exemption explicit so downstream filters can use
  `gate_status='pass'` without conflating Tier-2 with Tier-1 mid-flight rows.
- ❌ `ensemble_*` — null (Tier 2 is single-shot)
- ❌ `pre_mortem`, `adversarial_challenges` — null (use Tier 1 for adversarial pass)
- ❌ `constitutional_*` — null (the gate truth lives in `gate_status` above;
     `constitutional_pass` stays NULL to preserve the
     `TIER2_FORBIDDEN_NON_NULL` contract in `orchestrator_runtime/tier2.py`)
- ❌ `market_implied_move`, `options_iv` — null (no options sub-agent in Tier 2)

The schema's existing `additionalProperties: false` means missing fields must be explicitly null in the emitted JSON.

## Verification (D-103 paired-bootstrap eval)

Tier 2 is **eval-gated identically to Tier 1**: the nightly calibration refit (`modal_workers/scripts/nightly_calibration_refit.py`) computes Brier separately for `tier=1` and `tier=2` runs. Pass condition for staying enabled: 30-day Brier delta `tier1 - tier2 < 0.15` (i.e. Tier 2 is within 15% relative of Tier 1 on overlapping assets). Failure → operator alert via `operator_flags(source='tier2_quality')`; Tier 2 keeps running but the dashboard surfaces the gap until prompt iteration closes it.

## Known limitations (and what to do about them)

- **No options data.** Catalysts where market-implied move sharply diverges from base-rate priors will surface as a `uncertainties[]` entry recommending a Tier-1 escalation; the harness honors that recommendation when conviction ≥ 50.
- **No ensemble dispersion.** Confidence interval on conviction is implicit (the calibration curve's CI). Tier-2 outputs should be read as point estimates only.
- **Single-pass adversarial.** No pre_mortem step. The skill prompt body explicitly asks for one self-critique paragraph in `pre_mortem`-style reasoning before emitting `hypotheses`, but it doesn't write a separate row.
- **Memory writeback is the same as Tier 1.** The harness appends the standard `## Recent assessments` entry to the asset memory file; this means subsequent Tier-1 runs see the Tier-2 results as historical context, which is intended.
