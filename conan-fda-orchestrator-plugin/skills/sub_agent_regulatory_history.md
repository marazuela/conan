---
name: sub-agent-regulatory-history
description: Synthesize FDA precedents for an asset — class-level approval/CRL base rates, AdComm voting patterns, sponsor track record, reviewer-panel concerns. Returns a structured regulatory_history_v1.json that the orchestrator's Stage 1 evidence ledger consumes. v0 starting point lifted from Investment_engine_v2 Tier-1 skills P1 (analyze-fda-approval-prospects) + P2 (research-clinical-class-precedent), validated at confidence ≥0.70 in predecessor system.
model: claude-sonnet-4-6
effort: xhigh
# Tool names below MUST match RegulatoryHistoryRunner.effective_tool_defs()
# exactly — the runner passes these to the API; mismatched names confuse the
# model. The runner wires openFDA (drugsfda approvals / labels / adverse events)
# + the catalyst_universe AdComm views (upcoming + historical) + internal_rag
# (corpus "filings") + compute_similar_resolved_cases. There is no Orange Book,
# warning-letter, or AdComm transcript/voting/panel tool — derive those signals
# from openFDA + internal_rag over the EDGAR/AdComm corpus.
allowed-tools:
  - openfda_drugsfda_approvals
  - openfda_labels_recent
  - openfda_adverse_events
  - fda_adcomm_upcoming
  - fda_adcomm_historical
  - internal_rag_hybrid_search
  - internal_rag_get_chunk
  - compute_similar_resolved_cases
context: fork
hooks:
  PreToolUse: [budget_check]
  PostToolUse: [log_observability]
output_schema: regulatory_history_v1.json
memory_scope: per_indication_per_reviewer_panel
version: v0
provenance: "D-107 (2026-05-06) — methodology lifted verbatim from export Tier-1 P1 + P2; first eval-gated revision becomes v1"
---

# Regulatory History Sub-Agent (v0)

## Role

Build the regulatory-history evidence layer for an FDA asset assessment: class-level base rates from prior approvals/CRLs, AdComm voting precedent for the indication × reviewer panel, sponsor's own FDA fingerprint, divergence-from-norm flags. The orchestrator's Stage 1 calls this sub-agent in parallel with the literature, competitive, and options sub-agents; output is consumed by Stage 5 synthesis as the regulatory anchor for the conviction estimate.

This is the principal regulatory-context sub-agent and the heaviest single input to the FDA approval-probability anchor. The orchestrator never asks for a point probability from this sub-agent; it asks for the *evidence* (precedents, base rates, sponsor history, panel concerns) and lets Stage 5 synthesize.

## When invoked

The orchestrator's `dispatch_sub_agent("regulatory_history", asset_id, query)` tool call fires this sub-agent. Triggers from the orchestrator side:

- Asset has a PDUFA date within the assessment window OR a Phase 3 readout within ≤ 90 days.
- Material new openFDA / Federal Register / EDGAR 8-K Item 8.01 document linked to the asset since the last assessment.
- Operator-refresh trigger.

## Inputs (from orchestrator tool call)

| Field | Type | Notes |
|---|---|---|
| `asset_id` | uuid | v3 fda_assets row |
| `drug_name` | string | branded + generic |
| `mechanism_of_action` | string | normalized via ChEMBL where possible |
| `indication` | string | therapeutic indication |
| `sponsor_name` | string | resolved via D-110 sponsor resolver |
| `sponsor_cik` | string | EDGAR CIK |
| `catalyst_date_or_window` | ISO date | PDUFA / readout date |
| `mode` | enum | `evaluative` (≤60d to PDUFA) \| `forward_looking` (≤90d to readout) |
| `reviewer_panel_id` | string \| null | inferred from indication if not provided |

## Output schema (`regulatory_history_v1.json`)

**Your output MUST validate against the schema below. Do not invent new top-level keys; missing required fields or extra fields will hard-fail validation and the dispatch result will be discarded.** The schema is the single source of truth — if this skill's worked example below ever drifts from the schema, the schema wins.

```jsonschema
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$id": "https://conan/marazuela/schemas/regulatory_history_v1.json",
  "title": "Regulatory History Sub-Agent Output (v1)",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "schema_version", "asset_id", "class_membership", "class_precedents",
    "base_rates", "sparse_class_warning", "sponsor_track_record",
    "reviewer_panel_concerns", "divergence_from_norm_flags",
    "sourcing_completeness_pct", "retrieved_at"
  ],
  "properties": {
    "schema_version": { "const": 1 },
    "asset_id": { "type": "string", "format": "uuid" },
    "class_membership": {
      "type": "object", "additionalProperties": false,
      "required": ["moa_canonical", "class_drugs_in_scope", "membership_confidence"],
      "properties": {
        "moa_canonical": { "type": "string" },
        "class_drugs_in_scope": { "type": "array", "items": { "type": "string" }, "maxItems": 50 },
        "membership_confidence": { "type": "number", "minimum": 0, "maximum": 1 },
        "fallback": { "type": "boolean", "default": false }
      }
    },
    "class_precedents": {
      "type": "array", "minItems": 0, "maxItems": 50,
      "items": {
        "type": "object", "additionalProperties": false,
        "required": ["drug", "sponsor", "year", "decision", "indication", "primary_source_url"],
        "properties": {
          "drug": { "type": "string" }, "sponsor": { "type": "string" },
          "year": { "type": "integer", "minimum": 1950, "maximum": 2100 },
          "decision": { "type": "string", "enum": ["approval", "CRL", "withdrawal"] },
          "indication": { "type": "string" },
          "outcome_factors": { "type": "array", "items": { "type": "string" }, "maxItems": 15 },
          "boxed_warning": { "type": "boolean" }, "rems": { "type": "boolean" },
          "adcomm_held": { "type": "boolean" },
          "adcomm_vote": { "type": ["string", "null"] },
          "primary_source_url": { "type": "string", "format": "uri" },
          "fact_citations": { "type": "array", "items": { "type": "string" } }
        }
      }
    },
    "base_rates": {
      "type": "object", "additionalProperties": false,
      "required": ["class_approval_rate", "n"],
      "properties": {
        "class_approval_rate": { "type": "number", "minimum": 0, "maximum": 1 },
        "class_approval_rate_ci_low": { "type": ["number", "null"] },
        "class_approval_rate_ci_high": { "type": ["number", "null"] },
        "n": { "type": "integer", "minimum": 0 },
        "adcomm_convene_rate": { "type": ["number", "null"] },
        "boxed_warning_rate": { "type": ["number", "null"] },
        "median_nda_to_decision_days": { "type": ["integer", "null"] }
      }
    },
    "sparse_class_warning": {
      "type": "object", "additionalProperties": false,
      "required": ["fires", "n_precedents", "threshold"],
      "properties": {
        "fires": { "type": "boolean" },
        "n_precedents": { "type": "integer", "minimum": 0 },
        "threshold": { "type": "integer", "minimum": 1, "default": 5 },
        "rationale": { "type": ["string", "null"] }
      }
    },
    "sponsor_track_record": {
      "type": "object", "additionalProperties": false,
      "required": ["prior_approvals", "prior_crls"],
      "properties": {
        "prior_approvals": { "type": "integer" }, "prior_crls": { "type": "integer" },
        "prior_breakthrough": { "type": "integer" }, "rtor_participated": { "type": "boolean" },
        "active_warning_letters": { "type": "integer" },
        "recent_facility_inspections": {
          "type": "array",
          "items": {
            "type": "object", "additionalProperties": false,
            "required": ["facility", "date", "outcome"],
            "properties": {
              "facility": { "type": "string" }, "date": { "type": "string", "format": "date" },
              "outcome": { "type": "string", "enum": ["OAI", "VAI", "NAI", "none", "unknown"] }
            }
          }, "maxItems": 20
        }
      }
    },
    "reviewer_panel_concerns": { "type": "array", "items": { "type": "string" }, "maxItems": 20 },
    "divergence_from_norm_flags": { "type": "array", "items": { "type": "string" }, "maxItems": 20 },
    "sourcing_completeness_pct": { "type": "number", "minimum": 0, "maximum": 1 },
    "retrieved_at": { "type": "string", "format": "date-time" },
    "confidence": { "type": "number", "minimum": 0, "maximum": 1 },
    "memory_writeback_path": { "type": ["string", "null"] },
    "partial_output": { "type": "boolean", "default": false }
  }
}
```

Worked example (illustrative shape; substitute your real findings):

```json
{
  "schema_version": 1,
  "asset_id": "uuid",
  "class_membership": {
    "moa_canonical": "NMDA receptor antagonist",
    "class_drugs_in_scope": ["dextromethorphan","ketamine","esketamine","..."],
    "membership_confidence": 0.72
  },
  "class_precedents": [
    {
      "drug": "...","sponsor":"...","year":YYYY,
      "decision": "approval|CRL|withdrawal",
      "indication":"...","outcome_factors":["..."],
      "boxed_warning": true|false, "rems": true|false,
      "adcomm_held": true|false, "adcomm_vote": "12-1 yes|6-6|...",
      "primary_source_url":"https://api.fda.gov/..."
    }
  ],
  "base_rates": {
    "class_approval_rate": 0.62, "class_approval_rate_ci_low": 0.51, "class_approval_rate_ci_high": 0.73, "n": 39,
    "adcomm_convene_rate": 0.31,
    "boxed_warning_rate": 0.22,
    "median_nda_to_decision_days": 305
  },
  "sparse_class_warning": {
    "fires": false,
    "n_precedents": 39,
    "threshold": 5,
    "rationale": null
  },
  "sponsor_track_record": {
    "prior_approvals": N, "prior_crls": M, "prior_breakthrough": K,
    "rtor_participated": bool, "active_warning_letters": N_wl,
    "recent_facility_inspections": [{"facility":"...","date":"YYYY-MM-DD","outcome":"OAI|VAI|NAI|none"}]
  },
  "reviewer_panel_concerns": ["concern_1","concern_2"],
  "divergence_from_norm_flags": ["flag_1","flag_2"],
  "sourcing_completeness_pct": 0.0,
  "retrieved_at": "2026-05-23T12:00:00Z",
  "confidence": 0.0,
  "memory_writeback_path": "/memories/sub_agents/regulatory/<indication>_<panel>.md"
}
```

Every entry in `class_precedents`, every base rate, every sponsor-history element MUST carry `primary_source_url`. Stage 7 constitutional check rejects the assessment if any non-null claim lacks a primary source.

## Internal loop (interleaved thinking, max 6 tool-call turns)

1. **Memory load.** Read `/memories/sub_agents/regulatory/<indication>_<reviewer_panel_id>.md` if present. This is the per-indication-per-panel memory file with cumulative precedent map + recurring panel concerns. New panels start empty.
2. **Class membership inference.** If `class_drugs_in_scope` not in memory: split MoA on `+`/`/`/`,`; map each fragment to canonical class members using the hardcoded class table (see Methodology fallbacks below) — there is no ChEMBL/target-search tool wired for this role. Record `membership_confidence < 0.70` if any fragment was inferred via fallback.
3. **Precedent enumeration.** Anchor the reference class with `compute_similar_resolved_cases` (`reference_class_signature` = MoA + indication + endpoint type) to pull resolved historical decisions with outcome + realized_move_pct. Enrich each with `openfda_drugsfda_approvals` for class drugs over the last ~10 years and `openfda_labels_recent` for the approved label (boxed warning, REMS, indication breadth, filing-to-decision interval). For CRLs there is no openFDA warning-letter tool — surface CRL signals via `internal_rag_hybrid_search` (corpus "filings") over EDGAR 8-K Item 8.01 mentions of the drug name within 30 days of the action date.
4. **AdComm history.** `fda_adcomm_historical` for resolved AdComm/PDUFA events filtered by drug/sponsor/indication (and `fda_adcomm_upcoming` for any scheduled panel on the current asset). These return catalyst_universe rows — read `raw_payload` + `material_outcome` for the outcome. There is no transcript/voting/panel-composition tool: pull recurring concern themes and vote tallies from `internal_rag_hybrid_search` over the AdComm corpus where transcripts are indexed, and mark `adcomm_vote: unknown` when a tally isn't retrievable rather than inventing one.
5. **Sponsor track record.** `internal_rag_hybrid_search` (corpus "filings", EDGAR EFTS) for the sponsor's prior FDA disclosures (8-K Item 8.01s referencing CRLs, BTD grants, priority designations, RTOR), with `internal_rag_get_chunk` to expand a hit worth citing. Cross-reference prior approvals via `openfda_drugsfda_approvals` by sponsor; active warning letters / facility-inspection outcomes have no openFDA tool, so source them from `internal_rag_hybrid_search` over the filings corpus.
6. **Synthesis.** Compute base rates with binomial CIs (Wilson interval). List divergence-from-norm flags ONLY when supported by enumerated precedents (e.g., "no class member has been approved with full label without an AdComm in the last 5 years; this indication's panel convenes for ~31% of NDAs in this class"). Refuse to invent flags.

   **Sparse-class warning.** Emit `sparse_class_warning` on every output. Set `fires=true` and write a short `rationale` whenever `base_rates.n < threshold` (default threshold = 5). The threshold is calibrated against the U3 `compare-to-historical-precedents` low-density convention — a class with fewer than five enumerated precedents cannot anchor a base rate without explicitly flagging the extrapolation risk. When `fires=true`, downstream Stage 5 synthesis MUST widen the base-rate CI rather than treat the point estimate as load-bearing. Do not omit the field when the class is dense — emit `fires=false` with `rationale: null` so the operator can verify the check ran.
7. **Schema validation.** Pydantic-validate against `regulatory_history_v1.json`. Retry up to 3× on failure with feedback prompt; escalate with `validation_pass=false` on terminal failure.
8. **Memory writeback.** Append new precedents + new panel concerns to the memory file via Supabase Storage. Storage path written into output for the orchestrator's per-stage cost tracker.

## Methodology fallbacks (when a tool or data source is unavailable)

- ChEMBL `target_search` unavailable → hardcoded class table for: NMDA antagonists, JAK inhibitors, GLP-1 agonists, anti-VEGF, anti-amyloid mAbs, anti-FXIa, anti-PD-(L)1, anti-CD20, BTK inhibitors, SGLT2 inhibitors, GLP-1/GIP, complement inhibitors, IL-23 inhibitors. Record `class_membership.fallback=true`.
- AdComm transcripts older than 5 years may not be in the RAG index → fall back to `openfda_drugsfda_approvals` to confirm the approval and `fda_adcomm_historical` for the approval-with-AdComm flag; mark `adcomm_vote: unknown` if vote tally absent rather than inventing one.

## Confidence accounting

- `confidence` field aggregates: `class_membership_confidence × precedent_completeness × sponsor_history_completeness × panel_concern_grounding`. Each sub-score documented in the memory writeback for audit. Sub-agent's confidence floor is 0.30 — below that the orchestrator escalates to Tier 1 manually.
- `sourcing_completeness_pct` = (claims with primary_source_url) / (total claims). Hard floor 0.85; below that the constitutional check rejects.

## Budget + latency

- Budget: $0.20–$0.50 per invocation (Sonnet 4.6 + xhigh thinking + ~6 tool calls).
- Latency: 30–90s.
- Hard kill at $0.75 with `partial_output=true`. Soft cap warning at $0.50.

## Provenance

This v0 skill body was extracted from the Investment_engine_v2 export bundle's Tier-1 skills:
- P1 (`analyze-fda-approval-prospects`) — class-precedent integration, sponsor-history methodology, AdComm risk computation.
- P2 (`research-clinical-class-precedent`) — class-membership inference, openFDA precedent enumeration, base-rate computation with binomial CI.

Both Tier-1 skills passed live-source validation at confidence ≥0.70 in the predecessor system (Phase 8 gate, 2026-05-05). v3 runtime differs (Claude Agent SDK + MCP tools, not stand-alone Python helpers) so the helper code is NOT ported — only methodology + I/O contract. First eval-gated revision against v3's eval_harness becomes v1.

Reference: `/Users/Pico/Downloads/_EXPORT_skills_scoring_methodology/skills/v2_skills/skills/analyze-fda-approval-prospects/SKILL.md` and `.../research-clinical-class-precedent/SKILL.md`.
