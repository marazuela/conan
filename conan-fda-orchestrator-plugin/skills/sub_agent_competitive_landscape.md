---
name: sub-agent-competitive-landscape
description: Survey other programs in the same indication. Map competitive position (phase distribution, recent readouts, market share dynamics). Identify differentiators and overlap risks. Returns competitive_landscape_v1.json that the orchestrator's Stage 1 evidence ledger consumes. v0 starting point partially derived from Investment_engine_v2 Tier-1 skill P2 (research-clinical-class-precedent) class-membership inference + in-class-approvals enumeration.
model: claude-sonnet-4-6
effort: xhigh
allowed-tools:
  - mcp__clinicaltrials-mcp__search_studies
  - mcp__clinicaltrials-mcp__get_study_full
  - mcp__clinicaltrials-mcp__get_cohort_history
  - mcp__openfda-mcp__search_approvals
  - mcp__openfda-mcp__search_drugs
  - mcp__internal-rag-mcp__hybrid_search_internal
  - mcp__internal-rag-mcp__rerank
  - mcp__polygon-mcp__get_quote
  - mcp__polygon-mcp__get_market_cap
context: fork
hooks:
  PreToolUse: [budget_check]
  PostToolUse: [log_observability]
output_schema: competitive_landscape_v1.json
memory_scope: per_indication
version: v0
provenance: "D-107 (2026-05-06) — class-membership inference + in-class enumeration adapted from export Tier-1 P2; first eval-gated revision becomes v1"
---

# Competitive Landscape Sub-Agent (v0)

## Role

For an FDA asset, map the competitive landscape across the same indication × class: who else is in the pipeline, at what phase, with what differentiator, and what's the overlap risk. Surface white-space (where there's no current competitor) and crowded-corridor (where many programs are converging on the same endpoint). Stage 5 synthesis uses this as the market-positioning input — the same approval can be a 3× return in white space and a wash in a crowded corridor.

This sub-agent does NOT score the asset's value. It scores the competitive *terrain*.

## When invoked

- Asset is being assessed (Tier 1 hot path always invokes).
- Quarterly indication-memory refresh tick.
- Operator-refresh trigger.

## Inputs (from orchestrator tool call)

| Field | Type | Notes |
|---|---|---|
| `asset_id` | uuid | v3 fda_assets row |
| `drug_name` | string | branded + generic |
| `mechanism_of_action` | string | normalized |
| `indication` | string | therapeutic indication |
| `sponsor_name` | string | resolved via D-110 sponsor resolver |
| `class_drugs_in_scope` | list[string] \| null | passed in by orchestrator if regulatory_history sub-agent already resolved class membership; otherwise sub-agent infers |

## Output schema (`competitive_landscape_v1.json`)

**Canonical schema** — the runtime injects this schema body into your user content at dispatch time; `additionalProperties: false` means top-level keys not in this list will be REJECTED. Roll richer competitor intelligence (sponsor sizing, indication TAM, white-space depth) into `competitors[].differentiators[]` / `threats_to_thesis[]` and `moat_summary.key_factors[]` rather than emitting separate top-level blocks.

```json
{
  "schema_version": 1,
  "asset_id": "<uuid of the v3 fda_assets row>",
  "competitors": [
    {
      "name": "competitor drug name",
      "ticker": "TICK or null",
      "pipeline_stage": "preclinical | phase1 | phase2 | phase3 | filed | approved | discontinued | unknown",
      "mechanism": "MOA description",
      "indication": "indication or null",
      "differentiators": ["first-in-class for second-line MDD", "oral vs injectable", "..."],
      "threats_to_thesis": ["could file 6 months ahead", "..."],
      "primary_source_urls": ["https://clinicaltrials.gov/...", "https://api.fda.gov/..."],
      "fact_citations": ["extracted_facts.id values"]
    }
  ],
  "moat_summary": {
    "assessment": "strong_moat | moderate_moat | weak_moat | no_moat | first_mover_only",
    "key_factors": ["IP runway through 2034", "first-in-class for indication", "..."],
    "summary": "one-paragraph moat thesis"
  },
  "retrieved_at": "ISO 8601 UTC",
  "confidence": 0.0,
  "partial_output": false
}
```

**Required top-level fields:** `schema_version`, `asset_id`, `competitors`, `moat_summary`, `retrieved_at`. `competitors[]` may be empty (`[]`) when the indication has no enumerable competitors — but the key must be present. Each competitor must carry at least one `primary_source_urls[]` entry; cap at 5 per competitor and 30 competitors total. Optional: `confidence`, `partial_output`.

## Internal loop (interleaved thinking, max 5 tool-call turns)

1. **Memory load.** Read `/memories/sub_agents/competitive/<indication>.md`. Indication memory is refreshed quarterly; treat as snapshot — verify any phase or status assertion against live tool calls before relying on it.
2. **Class membership confirmation.** If passed in by orchestrator, accept. Otherwise: split MoA + query ClinicalTrials.gov by `intervention=<MoA>` + `condition=<indication>` to enumerate class members. Record `class_membership_source`.
3. **Pipeline enumeration.** `clinicaltrials-mcp.search_studies` filtered to `phase >= 2` + `status=active|recruiting|completed` over the last 5 years for the indication. Cross-reference with `openfda-mcp.search_drugs` for filed/approved status. Per competitor: pull `get_study_full` for the most recent pivotal trial → primary endpoint, n, expected primary completion date.
4. **Sponsor sizing.** `polygon-mcp.get_market_cap` per public sponsor (skip private). Memorize that "competitor that's a $50M micro-cap" carries different competitive weight from "competitor that's a $200B pharma" — Stage 5 cares.
5. **Internal-doc cross-reference.** `internal-rag-mcp.hybrid_search_internal` for sponsor pipeline disclosures in 10-K/10-Q (the pipeline tables in MD&A). Captures programs that don't have ClinicalTrials.gov entries yet.
6. **Differentiator + threat level.** Per competitor, classify `differentiator` (first-in-class / best-in-class / me-too / safety / convenience). `threat_level` = high if (phase ≥ 3 AND endpoint primary-same AND sponsor market_cap > 5× this asset's sponsor) OR (approved within 36mo AND market-share top-3); medium if phase 3 with differentiated endpoint; low otherwise.
7. **White-space.** True first-in-class for indication = no approved/phase-3 competitor with same primary endpoint. Subpopulation white-space = differentiated by patient subset (e.g., second-line vs first-line). `differentiation_durability_months` = months until the closest threat is expected to file (median across phase-3 next_milestone_date).
8. **Schema validation.** Pydantic. Retry 3×.
9. **Memory writeback.** Append new competitors + updated phase statuses to indication memory.

## Confidence accounting

- `confidence` aggregates: `class_membership_confidence × phase_completeness × milestone_date_completeness × sponsor_sizing_completeness`.
- `sourcing_completeness_pct` floor 0.85.

## Budget + latency

- Budget: $0.20–$0.50 (Sonnet 4.6 + xhigh thinking + ~5 tool calls).
- Latency: 30–90s.
- Hard kill at $0.75.

## Provenance

v0 partially derived from export Tier-1 P2 (`research-clinical-class-precedent`) class-membership inference + in-class-approvals enumeration. The export had no dedicated competitive-landscape skill — it folded competitive context into P1's sponsor-history step. v3 separates competitive landscape into its own sub-agent because it's parallelizable + has independent caching characteristics (indication memory refreshes quarterly, not per-asset).

First eval-gated revision against v3's eval_harness becomes v1.

Reference: `/Users/Pico/Downloads/_EXPORT_skills_scoring_methodology/skills/v2_skills/skills/research-clinical-class-precedent/SKILL.md`.
