# Sub-Agent Runner / Schema / Prompt Three-Way Drift

**Date filed:** 2026-05-23
**Status:** Audit complete; reconciliation NOT done. Phase 2C is halted on this finding.
**Severity:** P0 for Phase 2C (`ORCH_ENABLE_SUB_AGENTS=1`) — 100% of dispatches will land `schema_pass=false`. P2 today (flag stays OFF in production, no visible bite).
**Owner:** unassigned
**Related plan:** [/Users/Pico/.claude/plans/phase-2c-flip-async-treasure.md](../../.claude/plans/phase-2c-flip-async-treasure.md)
**Related audit:** [ic_memo_specialist_pipeline_drift.md](ic_memo_specialist_pipeline_drift.md) (sibling drift in the IC-memo write path, F-IC2 in particular)
**Related memory:** [failed_reactor_events_shared_dlq](../memory/failed_reactor_events_shared_dlq.md)

---

## TL;DR

The 4 Stage-1 sub-agent roles each have THREE places that nominally describe their output shape — and on each, those three places disagree:

- The **JSON Schema** under `conan-cowork-skills/schemas/<role>_v1.json` (Draft-7, hard-validated at runtime).
- The **skill markdown prompt** under `conan-fda-orchestrator-plugin/skills/sub_agent_<role>.md` (Claude reads this to know what to emit).
- The **actual Claude output** captured at runtime in `sub_agent_calls.output`.

For `regulatory_history`, all three are mutually disjoint. For `competitive`, all three diverge on field names + structure. For `options_microstructure`, schema and skill prompt match — but Claude went off-script under degraded mode and produced its own shape. For `literature`, schema and skill prompt diverge mildly; runtime is unknown (never fired in dry-run because Stage 1's `SUB_AGENT_LOOP_MAX_TURNS=4` cap ran out first).

The most likely root cause: D-107 (2026-05-06) wrote the skill prompts by lifting Investment_engine_v2 Tier-1 P1/P2 methodology verbatim, then D-124 (2026-05-07) wrote the JSON schemas as a tighter v3 contract — without back-propagating the contract into the skill prompts. The runners were tested with mock outputs that matched the schemas (pytest green per Phase 2C preflight #1), but no end-to-end test ever asked a live Sonnet to read the skill prompt and produce something the schema would accept.

Phase 2C dry-run on VRDN (2026-05-23 09:52–09:53 UTC, asset_id `4f3aeeef-6deb-4fd3-a0ce-45d414038dda`, `assessment_id IS NULL` in `sub_agent_calls`) is the live evidence.

## Live evidence — VRDN dry-run produced shapes

Query: `SELECT id, role, output FROM sub_agent_calls WHERE created_at BETWEEN '2026-05-23 09:50' AND '2026-05-23 10:00' ORDER BY created_at;`

| Role | Top-level keys produced | `schema_pass` | Companion DLQ row |
|---|---|---|---|
| regulatory_history | `adcomm_scheduled, base_rate_context, evidence_gaps, fda_safety_concerns_precedent, last_updated, manufacturing_inspection_signals, precedent_comparator, regulatory_history_summary, rems_requirement` | false | `failed_reactor_events.payload.source = 'sub_agent.regulatory_history'`, error: `'schema_version' is a required property; <9 produced keys> were unexpected` |
| options_microstructure | `analysis_date, atm_iv_pct, data_quality, event_date, event_type, front_month_expiry, iv_term_structure, liquidity_score, notes, open_interest_analysis, skew_metrics, straddle_implied_move_pct, ticker` | false | source `sub_agent.options_microstructure`, error: `'schema_version', 'asset_id' required; <9 of 13 produced keys> were unexpected` |
| competitive | `partial_output` only | false | source `sub_agent.competitive`, error: `'schema_version', 'asset_id', 'competitors', 'moat_summary', 'retrieved_at' required` |
| literature | n/a — never dispatched (Stage 1 ran out of turns) | n/a | n/a |

Per-role detail follows.

---

## R-1 — `regulatory_history` (THREE disjoint shapes)

### What the JSON schema requires
File: `conan-cowork-skills/schemas/regulatory_history_v1.json`

Required top-level: `schema_version, asset_id, prior_adcomms[], analogous_approvals[], regulatory_risks[], retrieved_at`.

`prior_adcomms[i]` requires `date, drug, indication, vote`. `analogous_approvals[i]` requires `drug, indication, approval_date, basis_for_approval`. `regulatory_risks[i]` requires `risk, severity ∈ {high,medium,low}`.

### What the skill markdown prompt tells Claude to emit
File: `conan-fda-orchestrator-plugin/skills/sub_agent_regulatory_history.md` lines 59–96.

Top-level: `schema_version, asset_id, class_membership{}, class_precedents[], base_rates{}, sponsor_track_record{}, reviewer_panel_concerns[], divergence_from_norm_flags[], sourcing_completeness_pct, confidence, memory_writeback_path`.

`class_precedents[i]` carries `drug, sponsor, year, decision ∈ {approval,CRL,withdrawal}, indication, outcome_factors[], boxed_warning, rems, adcomm_held, adcomm_vote, primary_source_url`. `base_rates` carries `class_approval_rate + binomial CI, adcomm_convene_rate, boxed_warning_rate, median_nda_to_decision_days`.

Provenance line on the skill: *"v0 starting point lifted from Investment_engine_v2 Tier-1 skills P1 (analyze-fda-approval-prospects) + P2 (research-clinical-class-precedent), validated at confidence ≥0.70 in predecessor system."* So the skill prompt is the predecessor-validated methodology; the schema is a separate, narrower contract written later.

### What Claude actually emitted on VRDN
Top-level: `regulatory_history_summary, adcomm_scheduled, base_rate_context, precedent_comparator, rems_requirement, fda_safety_concerns_precedent, manufacturing_inspection_signals, evidence_gaps, last_updated`.

Reads like Claude wrote its own best-guess shape after deciding the skill prompt's example wouldn't validate against whatever it inferred the schema was. (The dispatcher's `system` block doesn't include the schema — Claude only sees the skill markdown.)

### Three-way diff
| Concept | Schema field | Skill prompt field | Runtime field |
|---|---|---|---|
| AdComm precedents | `prior_adcomms[].{date,drug,indication,vote}` | `class_precedents[].{adcomm_held,adcomm_vote}` (mixed in with class precedents) | `precedent_comparator, adcomm_scheduled` (separate, less structured) |
| Class base rate | (not in schema) | `base_rates.{class_approval_rate,ci_low,ci_high,n}` | `base_rate_context` (free text) |
| Sponsor history | (not in schema) | `sponsor_track_record.{prior_approvals,prior_crls,...}` | (not in runtime output) |
| Boxed warning rate | (not in schema) | `base_rates.boxed_warning_rate` | (not in runtime output) |
| Safety concerns | (not in schema) | `reviewer_panel_concerns[]` | `fda_safety_concerns_precedent` |
| REMS | (not in schema) | `class_precedents[].rems` | `rems_requirement` (top-level) |
| Manufacturing | (not in schema) | `sponsor_track_record.recent_facility_inspections` | `manufacturing_inspection_signals` (top-level) |
| Summary prose | (not in schema) | (not in skill prompt) | `regulatory_history_summary` |
| Evidence gaps | (not in schema) | (implicit in `confidence` aggregation) | `evidence_gaps` (top-level) |

The schema is the **leanest** of the three; the skill prompt is the **richest** (and matches Investment_engine_v2 predecessor methodology); the runtime is a **third synthesis** of both, weighted toward narrative summary rather than structured rows.

### Proposed canonical: skill prompt's shape (with `partial_output, retrieved_at, confidence` from schema)
Rationale: the skill prompt's shape is grounded in the v2 export's proven Tier-1 methodology (D-107 provenance line), and is what Stage 5 actually wants for synthesis (per `_load_ic_memo_context` mapping comments in `orchestrator_runtime/ic_memo_runner.py`). The schema needs to grow to match. The runner prompt + schema diff is ~80 lines of JSON.

---

## R-2 — `competitive` (skill prompt vs schema diverge on field names + structure)

### Schema
File: `conan-cowork-skills/schemas/competitive_landscape_v1.json`. Required: `schema_version, asset_id, competitors[], moat_summary{assessment ∈ enum, key_factors[]}, retrieved_at`.

`competitors[i]` requires `name, pipeline_stage ∈ {preclinical, phase1, phase2, phase3, filed, approved, discontinued, unknown}, mechanism`. Optional: `ticker, indication, differentiators[], threats_to_thesis[], primary_source_urls[1..5], fact_citations[]`.

### Skill prompt
File: `conan-fda-orchestrator-plugin/skills/sub_agent_competitive_landscape.md` lines 51–87.

`competitors[i].{sponsor, drug, ticker, sponsor_market_cap_usd, phase, next_milestone, next_milestone_date, differentiator, endpoint_overlap, threat_level, primary_source_url}` (singular). Plus top-level `market_dynamics{n_competitors_phase3_or_later, n_recent_in_class_approvals_36mo, n_recent_in_class_crls_36mo, indication_TAM_usd, incumbent_market_share_top3_pct[]}`, `white_space_assessment{is_first_in_class_for_indication, is_first_in_subpopulation, subpopulation, differentiation_durability_months}`, `sourcing_completeness_pct, confidence, memory_writeback_path`.

### Field-name divergence
| Concept | Schema | Skill prompt |
|---|---|---|
| Drug name | `competitors[].name` | `competitors[].drug` (+ separate `sponsor`) |
| Phase | `pipeline_stage` (enum) | `phase` (enum, different ordering) |
| Differentiator | `differentiators[]` (array) | `differentiator` (single string) |
| Threats | `threats_to_thesis[]` (array of strings) | `threat_level ∈ {high,medium,low}` (single enum) + `endpoint_overlap` |
| Source URL | `primary_source_urls[1..5]` (required ≥1) | `primary_source_url` (single, optional) |
| Moat | `moat_summary{assessment, key_factors[]}` | (not modeled — implicit in `white_space_assessment + differentiation_durability_months`) |
| Sponsor sizing | (not modeled) | `sponsor_market_cap_usd` |
| Market dynamics | (not modeled) | `market_dynamics{}` block (4 fields) |

### Runtime
Cannot characterize: the VRDN run blew the 200,000-token per-role budget at `in=205076, out=1942` and the dispatcher hard-stopped competitive with `{partial_output: ...}`. (See "Sibling finding S-3" below — the 200k cap is at the edge of viable.)

### Proposed canonical: skill prompt's shape
Rationale: the skill prompt distinguishes sponsor (legal entity) from drug (program), captures `sponsor_market_cap_usd` which Stage 5 explicitly uses in synthesis ("competitor that's a $50M micro-cap carries different competitive weight from $200B pharma"), and models `market_dynamics{}` + `white_space_assessment{}` separately. The schema's `moat_summary{}` block is a derived enum-rollup that can be computed from skill-prompt fields rather than emitted natively. Reconcile by growing the schema.

---

## R-3 — `options_microstructure` (schema ≈ skill prompt; runtime drifted independently)

### Schema vs skill prompt
File: `conan-cowork-skills/schemas/options_microstructure_v1.json` vs `conan-fda-orchestrator-plugin/skills/sub_agent_options_microstructure.md` lines 52–78.

**Mostly aligned.** Both have `schema_version, asset_id, ticker, underlying_price, event_date, straddle_implied_move_pct, iv_30d, iv_60d, iv_term_slope ∈ {front_loaded,flat,backward_loaded}, event_window_liquidity_score ∈ 0..5, oi_concentration{top_strikes[], put_call_ratio}, position_inferred, computed_at, data_quality ∈ {fresh,stale,unavailable}, confidence, partial_output`. Schema requires only `schema_version, asset_id, ticker, computed_at` — everything else nullable. Skill prompt provides a complete worked example.

This is the role where reconciliation is cheapest: schema and prompt agree.

### Runtime drifted
Claude produced `analysis_date, atm_iv_pct, event_date, event_type, front_month_expiry, iv_term_structure, liquidity_score, notes, open_interest_analysis, skew_metrics, ticker, data_quality, straddle_implied_move_pct`.

Likely cause: POLYGON_API_KEY was unset (logged: `options_microstructure runner degraded: POLYGON_API_KEY env var is unset`), so the runner couldn't call any of its Polygon tools and Claude synthesized a *plausible-sounding* but spec-disjoint output rather than emitting the spec'd degraded-mode shape (`data_quality: 'unavailable', confidence: 0, partial_output: true`, every other field null).

Field-name aliasing:
| Concept | Schema/prompt | Runtime |
|---|---|---|
| IV at 30/60 day | `iv_30d, iv_60d` | `atm_iv_pct` (single, no horizon) |
| IV term | `iv_term_slope` (enum) | `iv_term_structure` (likely object) |
| Liquidity | `event_window_liquidity_score` (0–5 int) | `liquidity_score` |
| Top strikes | `oi_concentration.top_strikes[]` | `open_interest_analysis` (object, structure unknown) |
| Computed time | `computed_at` | `analysis_date` |

### Proposed canonical: schema/skill prompt shape (no change to those)
Rationale: schema and prompt agree; only the runner's behavior under degraded mode needs fixing. Two specific runner changes:
1. When `POLYGON_API_KEY` is absent, short-circuit before Sonnet is called and return the spec'd degraded shape directly (`data_quality='unavailable'`, every other field null/0). No Anthropic spend.
2. Strengthen the skill prompt's degraded-mode instruction so Claude — even when called — doesn't invent its own shape.

---

## R-4 — `literature` (drift smaller; runtime unknown)

### Schema vs skill prompt
File: `conan-cowork-skills/schemas/literature_review_v1.json` vs `conan-fda-orchestrator-plugin/skills/sub_agent_literature_reviewer.md` lines 52–85.

Top-level structure aligned: `schema_version, asset_id, papers[], retrieved_at`. Schema adds `synthesis{thesis_alignment ∈ enum, summary, kill_conditions[], contradictory_findings[]}` and `query_used`; skill prompt has `contradictory_findings[]` at the top level (not nested under `synthesis`), plus `missed_seminal_via_citation_graph[], sourcing_completeness_pct, memory_writeback_path`.

`papers[i]` field-name divergences:
| Concept | Schema | Skill prompt |
|---|---|---|
| Journal | `journal` | `venue` |
| Verbatim quote | `verbatim_quote` | `verbatim_quote_for_finding` |
| Citations inbound | (not modeled) | `citations_inbound` (integer) |
| Citations outbound | (not modeled) | `citations_outbound_seminal[]` |
| Peer review | `is_peer_reviewed` | `is_peer_reviewed` (matches) |
| Authors | `authors[]` (≤50) | (not modeled in prompt example) |
| Abstract | `abstract` | `abstract` (matches) |

### Runtime
Unknown. `literature` never dispatched on the VRDN dry-run because Stage 1 used all 4 `SUB_AGENT_LOOP_MAX_TURNS` on (regulatory_history, options_microstructure, competitive) before getting to it. This was the role most likely to produce viable output too — its MCPs (PubMed, internal RAG over literature corpus) are key-free, no degraded-mode trap. Worth retrying after S-1 (loop-turn cap) is addressed.

### Proposed canonical: merge — schema's `synthesis{}` block + skill prompt's `citations_inbound + citations_outbound_seminal + missed_seminal_via_citation_graph`
Rationale: schema's `synthesis{thesis_alignment, kill_conditions}` is what Stage 1 actually consumes for prose; skill prompt's citation-graph fields are what justifies the additional cost of pubmed/semanticscholar 1-hop expansion. Reconcile by growing both: schema gains the citation fields, skill prompt example gains the `synthesis{}` wrapper.

---

## Sibling findings

### S-1 — `SUB_AGENT_LOOP_MAX_TURNS=4` is too tight

File: [orchestrator_runtime/runtime.py:110](../orchestrator_runtime/runtime.py:110).

VRDN evidence: 3 of 4 roles dispatched, then Stage 1 emitted *"Let me get one more search to find Tepezza clinical trial data for comparison"* on turn 4 — meaning Claude wanted to dispatch literature OR do a second-pass refinement and was out of turns. Stage 9 then received that single-sentence prose as its input and `parse_json_or_none` failed.

Minimum viable cap = 5 (1 turn for parallel dispatch + 1 turn per role for retry/refinement + 1 turn for final synthesis). Suggested cap = **8** to leave room for the IC-memo-pattern of dispatch → retrieve → refine → second-dispatch on a missing dimension. Each turn is ~$0.01–0.05 in Sonnet 4.5 input tokens at this prompt scale; even 8 turns is well under the $15/run hard kill.

### S-2 — Dispatcher `_log_call` loses `tokens / cost_usd / latency_ms` on schema failure

File: [orchestrator_runtime/sub_agent_dispatcher.py:230–268](../orchestrator_runtime/sub_agent_dispatcher.py:230).

In `dispatch_sub_agent`, the metric capture lives inside the `try` block after `result = runner.run(...)`. The `except SubAgentSchemaError` path replaces `output` with `exc.payload` but leaves `tokens=0, cost=0.0, latency=0` because the exception didn't carry them. Result: every schema-failed row in `sub_agent_calls` shows zero burn even though real Anthropic tokens were spent. This corrupts the 24h cost soak metric used in Phase 2C verification.

Fix: `SubAgentSchemaError` needs `tokens_input, tokens_output, cost_usd, latency_ms` fields. Runner's `_validate_and_raise` constructs the exception, so it has the metrics in scope.

### S-3 — 200,000-token per-role budget is at the edge

`ORCH_SUB_AGENT_BUDGET_TOKENS=200000` ([sub_agent_dispatcher.py:42](../orchestrator_runtime/sub_agent_dispatcher.py:42)). The VRDN `competitive` sub-agent hit `in=205076, out=1942` and was hard-stopped mid-flight. VRDN is a relatively well-documented asset (Veligrotug for TED, 34 docs in `asset_documents` last 30d). On a more-documented asset the limit will bite harder; on a less-documented one the search-pass-2 retries will hit it instead.

Options: (a) raise cap to 350k (catches the tail with Sonnet's 200k context window plus output budget); (b) lower per-search-result token budget in the skill prompt (more aggressive snippet truncation); (c) move skill from `effort: xhigh` to `effort: high` to trim per-turn thinking budget. Probably (a) + (b), not (c).

### S-4 — Skill prompt doesn't include the JSON schema as a constraint

Reading the four skill prompts: each documents an "Output schema" section with a *worked example*, but does NOT include the actual JSON Schema text. So Claude is shown a shape via example, not a contract. When the example happens to drift from the schema (which is what happened here), there's no validator-aware fallback path — Claude just emits the example shape and hits validation.

Fix: include the literal schema in the skill prompt as a `\`\`\`jsonschema` fenced block, AND a one-line instruction *"Your output MUST validate against the schema above; do not invent new top-level keys."* This wouldn't have fully prevented the regulatory_history runtime drift, but would have raised Claude's prior toward the schema shape.

---

## Reconciliation strategy — picking one canonical per role

Recommendation table:

| Role | Canonical source | Schema work | Skill prompt work | Runner / runtime work |
|---|---|---|---|---|
| regulatory_history | **skill prompt** (richer, v2-grounded) | grow schema to add `class_membership, class_precedents, base_rates, sponsor_track_record, reviewer_panel_concerns, divergence_from_norm_flags, sourcing_completeness_pct` | embed literal schema as fenced jsonschema block | re-test against fixture |
| competitive | **skill prompt** (sponsor-sizing + market_dynamics matter) | grow schema to add `sponsor, sponsor_market_cap_usd, phase (rename), next_milestone, next_milestone_date, endpoint_overlap, threat_level (enum), market_dynamics{}, white_space_assessment{}` | embed literal schema | re-test fixture; also fix budget cap (S-3) |
| options_microstructure | **schema / skill prompt (already match)** | none | none | (a) short-circuit when POLYGON_API_KEY absent → emit degraded shape directly; (b) tighten skill prompt's degraded-mode instruction |
| literature | **merged** | add `citations_inbound, citations_outbound_seminal, missed_seminal_via_citation_graph, sourcing_completeness_pct` | add `synthesis{}` wrapper to skill prompt example; rename `venue → journal` for consistency | re-test after retrying dispatch in Phase 2C re-run |

Plus cross-cutting:
- S-1 (raise `SUB_AGENT_LOOP_MAX_TURNS` to 8) — single-constant change.
- S-2 (`SubAgentSchemaError` carries metrics) — ~20 lines in [sub_agents/runtime.py](../modal_workers/sub_agents/runtime.py) + [sub_agent_dispatcher.py](../orchestrator_runtime/sub_agent_dispatcher.py).
- S-4 (literal schema in skill prompt) — append-only change to 4 skill markdown files.
- S-3 (budget cap raise) — single-env-var change in `anthropic-orchestrator` Modal secret.

Estimated effort: **2–3 engineer-days** for the full reconciliation + a paired-fixture pytest that asserts each runner's mock output validates against its schema. Plus a re-run dry-run before flipping Phase 2C.

---

## What's NOT broken (signals that survived the dry-run)

- Stage 1 tool-use loop wiring (`--enable-sub-agents` → `_stage_1_synthesize_with_dispatch` → `DISPATCH_TOOL_DEF`) — works end-to-end. Sub-agents fired in parallel within a single assistant turn.
- `sub_agent_calls` insert path — every dispatch landed a row; `schema_pass`, `output`, `created_at` all populated.
- `failed_reactor_events` DLQ path with `payload->>'source' = 'sub_agent.<role>'` — every schema failure landed a companion row.
- The dispatcher's per-role kill-switch path was not exercised in this dry-run but is covered by `test_sub_agent_dispatcher.py::test_dispatch_disabled_via_orch_disable`.
- `BudgetExceededError`-style $15/run hard kill was not triggered (each sub-agent cost was well below the per-run cap before its own 200k-token cap fired).

## When this becomes urgent (escalation triggers)

Phase 2C is currently halted. The reconciliation becomes urgent when:
- The plan owner decides to proceed with Phase 2C anyway (e.g. flip the flag in dev/staging-equivalent for prompt iteration).
- Anyone deploys a separate code path that ALSO calls the sub-agent dispatcher (e.g. IC memo synthesis via `rpc_ic_memo_run`).
- The next eval-harness backfill cycle wants sub-agent outputs as features (currently dark, so no calibration delta).

Until then, this sits as a known structural blocker with the reconciliation strategy scoped above.
