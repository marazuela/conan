# Sub-Agent Schema Drift — VRDN Dry-Run Blocker

**Date filed:** 2026-05-23
**Status:** Strategy A landed 2026-05-23 — code + prompt changes applied in worktree `brave-mestorf-ff922c`; awaiting fresh VRDN/AXS-05 dry-run validation before Phase 2C flip
**Severity:** P1 (blocks `ORCH_ENABLE_SUB_AGENTS=1` flip; not a live regression because flag is still OFF in production)
**Owner:** unassigned (awaiting Pedro's reconciliation pick)
**Related plan:** `/Users/Pico/.claude/plans/phase-2c-flip-async-treasure.md` (parent)
**Related memory:** [failed_reactor_events_shared_dlq](../memory/failed_reactor_events_shared_dlq.md), [ic_memo_specialist_pipeline_drift.md](ic_memo_specialist_pipeline_drift.md) (sibling drift doc covering F-IC2 name mismatch only)
**Live evidence:** VRDN dry-run 2026-05-23 09:52–09:53 UTC against asset `4f3aeeef-6deb-4fd3-a0ce-45d414038dda` (`assess_convergence ..., enable_sub_agents=True, dry_run=True`). All three dispatched roles landed `sub_agent_calls.schema_pass=false` with a sibling `failed_reactor_events.source='sub_agent.<role>'` row. Query verbatim shapes via:

```sql
SELECT id, role, output
FROM sub_agent_calls
WHERE created_at > '2026-05-23 09:50:00'
  AND created_at < '2026-05-23 10:00:00';
```

---

## TL;DR

The four sub-agent role triads — **schema JSON** (`conan-cowork-skills/schemas/<role>_v1.json`) × **skill markdown prompt** (`conan-fda-orchestrator-plugin/skills/sub_agent_<role>.md`) × **runtime-produced output** (what Sonnet actually emits in `modal_workers/sub_agents/<role>.py`'s tool-use loop) — are all out of sync. The schemas are the original D-107 (2026-05-06) contract; the skill prompts drifted during the rebuild; the runtime is making its own best guess that matches neither.

This is **deeper than F-IC2** in `audit/ic_memo_specialist_pipeline_drift.md`. F-IC2 was about *role-name* mismatches between the orchestrator's table-keying conventions and the dashboard's `fda_agent_reviews.agent_kind` enum. F-IC2 is closed by the read-side bridge. **F-SD1 through F-SD4 below** are about *internal field shapes* — the actual JSON the model produces doesn't validate against the schemas the dispatcher validates against, and the prompts that should ground the model document a third shape entirely.

Phase 2C remains halted until Pedro picks a canonical shape per role and the three sources are reconciled.

---

## Findings

### F-SD1 — `regulatory_history` has three disjoint shapes

| Source | Top-level keys |
|---|---|
| **Schema** (`schemas/regulatory_history_v1.json`) | `schema_version`, `asset_id`, `prior_adcomms[]`, `analogous_approvals[]`, `regulatory_risks[]`, `retrieved_at`, *(opt)* `crl_precedent_found`, `confidence`, `partial_output` |
| **Skill prompt** ([sub_agent_regulatory_history.md:62–96](../conan-fda-orchestrator-plugin/skills/sub_agent_regulatory_history.md)) | `schema_version`, `asset_id`, `class_membership`, `class_precedents[]`, `base_rates`, `sponsor_track_record`, `reviewer_panel_concerns`, `divergence_from_norm_flags`, `sourcing_completeness_pct`, `confidence`, `memory_writeback_path` |
| **VRDN runtime output** (from `failed_reactor_events.error_message`) | `regulatory_history_summary`, `adcomm_scheduled`, `base_rate_context`, `evidence_gaps`, `fda_safety_concerns_precedent`, `last_updated`, `manufacturing_inspection_signals`, `precedent_comparator`, `rems_requirement` |

**Intersection across all three:** ∅ (zero fields shared).
**Schema ∩ Skill:** `{schema_version, asset_id, confidence}` — three of the schema's six required fields are not in the prompt.
**Skill ∩ Runtime:** ∅ — the prompt does NOT ground the runtime output.
**Schema ∩ Runtime:** ∅ — the validator rejects every key.

The schema is the only artifact with an `$id` URL, schema_version constant, and a `validateAdditionalProperties: false` contract. The prompt drifted to a richer per-class-precedent shape with sponsor-history and panel concerns; the runtime drifted to a completely independent ontology (summary-prose with `regulatory_history_summary`, `evidence_gaps` etc.). The Sonnet runner is composing its own answer to the question and ignoring the schema instruction in the `build_user_content` tail (`return ONLY a JSON object matching the regulatory_history_v1.json schema`).

**Canonical shape recommendation: pick Schema's contract** (`prior_adcomms`, `analogous_approvals`, `regulatory_risks`). It's the only one with formal validation, it's the simplest of the three, and it cleanly maps to evidence-ledger usage in Stage 5 synthesis. Rewrite the skill prompt and update the schema only to *additively* admit the more useful prompt fields (e.g., add an optional `base_rates` block to the schema if Pedro wants base-rate computation surfaced).

### F-SD2 — `options_microstructure` runtime output is mostly `additionalProperties: false` violations

| Source | Top-level keys |
|---|---|
| **Schema** (`schemas/options_microstructure_v1.json`) | `schema_version`, `asset_id`, `ticker`, `computed_at`, *(opt)* `underlying_price`, `event_date`, `straddle_implied_move_pct`, `iv_30d`, `iv_60d`, `iv_term_slope`, `event_window_liquidity_score`, `oi_concentration{}`, `position_inferred`, `data_quality`, `confidence`, `partial_output` |
| **Skill prompt** ([sub_agent_options_microstructure.md:55–78](../conan-fda-orchestrator-plugin/skills/sub_agent_options_microstructure.md)) | matches schema almost exactly (uses `iv_30d`, `iv_60d`, `iv_term_slope`, `event_window_liquidity_score`, `oi_concentration`) |
| **VRDN runtime output** *(verified against `sub_agent_calls.output` on 2026-05-23)* | `analysis_date`, `atm_iv_pct`, `data_quality`, `event_date`, `event_type`, `front_month_expiry`, `iv_term_structure`, `liquidity_score`, `notes`, `open_interest_analysis`, `skew_metrics`, `straddle_implied_move_pct`, `ticker` |

**Intersection across all three:** `{ticker, straddle_implied_move_pct, data_quality, event_date}` — 4 fields out of 13.
**Schema ∩ Runtime:** the four above. Missing schema-required: `schema_version`, `asset_id`, `computed_at`. Renamed by Sonnet: `iv_term_structure` (was `iv_term_slope`), `liquidity_score` (was `event_window_liquidity_score`), `open_interest_analysis` (was `oi_concentration`). All renames trigger `additionalProperties: false` rejections.
**Skill ∩ Schema:** ~100%. The prompt and schema agree.

**Diagnosis:** this is the cleanest case — schema and prompt are aligned, only Sonnet is inventing names. Likely cause: `build_user_content` in [runtime.py:205–212](../modal_workers/sub_agents/runtime.py) only embeds the schema *filename* (not the schema body) in the instruction. The model is recalling typical options-chain ontology from training rather than reading the schema. Fix is mechanical.

**Canonical shape recommendation: keep schema as-is. Inject the resolved schema JSON into the user content (or system prompt) so Sonnet sees the actual required-field list and `additionalProperties: false` constraint.** This is also the cheapest fix that will benefit all four roles.

### F-SD3 — `competitive_landscape` runtime hit the 200k budget cap; nothing useful to compare

| Source | Top-level keys |
|---|---|
| **Schema** (`schemas/competitive_landscape_v1.json`) | `schema_version`, `asset_id`, `competitors[]`, `moat_summary{}`, `retrieved_at`, *(opt)* `confidence`, `partial_output` |
| **Skill prompt** ([sub_agent_competitive_landscape.md:53–87](../conan-fda-orchestrator-plugin/skills/sub_agent_competitive_landscape.md)) | `schema_version`, `asset_id`, `indication`, `class_membership_source`, `competitors[]`, `market_dynamics{}`, `white_space_assessment{}`, `sourcing_completeness_pct`, `confidence`, `memory_writeback_path` |
| **VRDN runtime output** | `{partial_output: <truncated synthesis prose>}` only — sub-agent exceeded `budget_token_cap=200000` (`in=205076 out=1942`) before final JSON synthesis |

The competitive runner ran past the 200k aggregate token cap during the Sonnet tool-use loop (likely tool_result accumulation from `clinicaltrials_search` + `internal_rag_hybrid_search` on a well-documented asset like VRDN). The `partial_output: true` branch in `runtime.py:370–371` ran, and the validation pass rejected everything else.

**Intersection comparison is not meaningful here**, but the *prompt-vs-schema* drift mirrors F-SD1: the prompt documents `indication`, `market_dynamics`, `white_space_assessment` — none of which the schema accepts. So even if the runner *had* successfully synthesized matching the prompt, the schema would have rejected it.

**Canonical shape recommendation: pick the schema's contract** (`competitors[]` + `moat_summary{}` is sufficient for IC memo synthesis). Add an additive `white_space{}` block to the schema if Pedro wants Stage 5 to read it. Either way, the prompt needs a full rewrite to match.

### F-SD4 — `literature` never dispatched in the dry-run

Stage 1 emitted three `dispatch_sub_agent` tool_use blocks within `SUB_AGENT_LOOP_MAX_TURNS=4` (one per turn, sequentially in this run — Sonnet did not parallelize them despite the prompt instruction). After receiving the 3rd tool_result on turn 3, Stage 1 produced the prose `"Let me get one more search to find Tepezza clinical trial data for comparison."` instead of a final synthesis, and the for-loop in [`_stage_1_synthesize_with_dispatch`](../orchestrator_runtime/runtime.py#L533) exhausted before turn 4. `parse_json_or_none` at the end of Stage 9 then failed because Stage 1's final text was that prose, not JSON.

So we have no runtime output to compare against for `literature` in this dry-run. But the schema-vs-prompt drift is the same as F-SD1 / F-SD3:

| Source | Top-level keys |
|---|---|
| **Schema** (`schemas/literature_review_v1.json`) | `schema_version`, `asset_id`, `papers[]`, `synthesis{thesis_alignment, summary, kill_conditions, contradictory_findings}`, `query_used`, `retrieved_at`, *(opt)* `confidence`, `partial_output` |
| **Skill prompt** ([sub_agent_literature_reviewer.md:55–84](../conan-fda-orchestrator-plugin/skills/sub_agent_literature_reviewer.md)) | `schema_version`, `asset_id`, `papers[]`, `contradictory_findings[]` *(at top level, not nested under synthesis)*, `missed_seminal_via_citation_graph`, `sourcing_completeness_pct`, `confidence`, `memory_writeback_path` |

**Schema ∩ Skill:** `{schema_version, asset_id, papers, confidence}`. The schema folds `contradictory_findings` into a `synthesis{}` block and adds `kill_conditions[]`, `thesis_alignment`, `summary`. The prompt flattens those and has no `synthesis{}` envelope. Per-paper field mismatches also exist: schema has `relevance_score` (required), `study_type`, `evidence_strength`; prompt adds `venue`, `citations_inbound`, `citations_outbound_seminal`, `verbatim_quote_for_finding` (schema spells the last as `verbatim_quote`).

**Canonical shape recommendation: keep schema** (`synthesis{}` block is the right abstraction; Stage 5 consumes it as the academic-evidence anchor). Rewrite the prompt to use the synthesis envelope. Most of the prompt's extra fields (`missed_seminal_via_citation_graph`, `citations_inbound`) are nice-to-have and could be additively merged into the schema — Pedro picks.

---

## Aggregate picture

| Role | Schema-canonical fields | Skill-prompt fields | Runtime output fields | Three-way ∩ |
|---|---:|---:|---:|---:|
| regulatory_history | 6 req + 3 opt | 10 | 9 | **0** |
| options_microstructure | 4 req + 12 opt | ~13 | 13 | **4** |
| competitive_landscape | 5 req + 2 opt | 9 | n/a (partial) | n/a |
| literature | 6 req + 2 opt | 7 | n/a (no dispatch) | n/a |

Across the only two roles that produced usable evidence, the runtime field-name overlap with the schema is **4/13 (options)** and **0/9 (regulatory)**. The skill prompt mediates neither successfully. **No reconciliation will work without changing at least two of the three artifacts per role.**

---

## Why this isn't already breaking production

The flag `ORCH_ENABLE_SUB_AGENTS=1` is OFF in the production `anthropic-orchestrator` Modal secret. Phase 2C's whole point was to flip it. The dry-run is the *first* end-to-end exercise of all four sub-agents against a real asset since the runners landed. The schemas have been in place since D-107 (2026-05-06) but never been validated against runtime output until now because `sub_agent_calls` had 0 rows pre-dry-run.

So this audit catches the drift exactly at the right moment — before the flip — which is the cheap fix window.

---

## Reconciliation strategy options

The four roles are independent, so Pedro can pick a different strategy per role.

### Strategy A — "Schema is canonical; rewrite prompt + ground Sonnet" *(recommended)*

- **Keep:** schema files as-is. They have `additionalProperties: false`, formal `required[]`, JSON-Schema Draft 7 validation. They're the only artifact that's actually *enforced*.
- **Rewrite:** each skill prompt's "Output schema" section to mirror the schema verbatim, including the `additionalProperties: false` constraint.
- **Add:** schema body injection in `build_user_content` (or system prompt) so Sonnet sees the structural contract, not just the filename string. ~10-line change in [`modal_workers/sub_agents/runtime.py`](../modal_workers/sub_agents/runtime.py) (`SubAgentRunner.build_user_content`).

**Files changed:**

| File | Change | Est. lines |
|---|---|---:|
| `conan-fda-orchestrator-plugin/skills/sub_agent_regulatory_history.md` | rewrite §"Output schema" to match `regulatory_history_v1.json`; trim methodology sections that reference removed fields (`class_membership`, `sponsor_track_record`, etc.) | ~80 |
| `conan-fda-orchestrator-plugin/skills/sub_agent_options_microstructure.md` | minor edits — prompt is already close to schema; reword to remove the "I'll invent field names" tendency by quoting the schema's `additionalProperties: false` clause | ~15 |
| `conan-fda-orchestrator-plugin/skills/sub_agent_competitive_landscape.md` | rewrite §"Output schema" to match `competitive_landscape_v1.json`; drop `market_dynamics`, `white_space_assessment` (or push them into Pedro's "additive schema fields" list) | ~50 |
| `conan-fda-orchestrator-plugin/skills/sub_agent_literature_reviewer.md` | rewrite §"Output schema" to use the `synthesis{}` envelope and the correct per-paper field names (`verbatim_quote`, not `verbatim_quote_for_finding`) | ~40 |
| `modal_workers/sub_agents/runtime.py` | inject resolved schema JSON into `build_user_content` so Sonnet sees the structural contract. Optional companion: add a short "structural reminder" to the system prompt fallback at L221–224. | ~15 |
| **No schema files touched.** | | 0 |

**Total: ~200 lines across 5 files.**

### Strategy B — "Prompt is canonical; rewrite schemas to accept the richer shape"

- **Keep:** skill prompts (richest of the three artifacts; closest to Pedro's mental model in D-107).
- **Rewrite:** schema files to admit the prompt's fields. Loses the discipline of `additionalProperties: false` (would have to keep it but add all prompt fields explicitly).
- **Don't touch:** runners.
- **Risk:** Sonnet runtime output is *still* drifted from prompt (F-SD1 runtime ≠ prompt — completely disjoint). So this strategy still needs the runtime-grounding fix from Strategy A; doing it alone won't unblock.

**Total: ~150 lines across 4 schema files + same runtime fix as A.** Net cost similar to A but loses validation rigor.

### Strategy C — "Runtime is canonical; rewrite schema + prompt to match what Sonnet wants to emit"

- **Keep:** runtime behavior.
- **Rewrite:** schema + prompt to match the runtime output for `regulatory_history` and `options_microstructure`. Re-run dry-runs for `competitive` and `literature` to capture *their* drift first.
- **Why this is wrong:** Sonnet's output isn't grounded in anything — the runtime "shape" today is whatever it pattern-matched from training. There's no semantic reason to canonicalize `manufacturing_inspection_signals` over `analogous_approvals`. Strategy C just locks in noise.

**Recommendation: A.** Net diff (~200 lines, no schema migrations, runners untouched) is smaller than B (~150 + same runner work) and dramatically smaller than the rewrite C would imply across all four roles after re-running dry-runs.

---

## Sibling concerns surfaced by the dry-run

### S-1 — `sub_agent_calls` metrics zeroed on schema failure

The three failed dispatches landed `tokens=0, cost_usd=0, latency_ms=0` in `sub_agent_calls` despite real Anthropic spend. Tracing it:

- [`SubAgentRunner.run`](../modal_workers/sub_agents/runtime.py#L378–379) raises `SubAgentSchemaError(self.role, errors, payload)` on validation failure. The exception carries `errors` and `payload` but **not** `tokens_input`, `tokens_output`, `cost_usd`, `latency_ms`.
- [`sub_agent_dispatcher.dispatch_sub_agent`](../orchestrator_runtime/sub_agent_dispatcher.py#L247–251) catches `SubAgentSchemaError` and assigns `output = exc.payload`, but `tokens`, `cost`, `latency` remain at their initial 0 from L233–235.
- `_log_call` then writes the zeros into `sub_agent_calls`.

This is a **dispatcher bug, not a runner bug**, and it masks real cost during exactly the runs we most need cost visibility on (failures). Fix:

1. In `SubAgentSchemaError.__init__`, accept and store `tokens_input`, `tokens_output`, `cost_usd`, `latency_ms`.
2. In `runtime.py:378–379`, pass those four fields when raising.
3. In the dispatcher's `except SubAgentSchemaError` block, copy them onto the outer `tokens`, `cost`, `latency` locals.

~20 lines across 2 files. Should land in the same PR as the Strategy A reconciliation — it's load-bearing for Step 3 (live drain observation) of the phase-2c plan, which checks `cost_actual_usd` vs `$15` hard kill.

### S-2 — `SUB_AGENT_LOOP_MAX_TURNS=4` is too tight even after schema reconciliation

[`orchestrator_runtime/runtime.py:110`](../orchestrator_runtime/runtime.py#L110) caps Stage 1's outer dispatch loop at 4 turns. In the VRDN dry-run, Sonnet dispatched 3 sub-agents *sequentially* (turns 0, 1, 2) — it did not parallelize despite the DISPATCH_TOOL_DEF docstring explicitly inviting parallel tool_use blocks. On turn 3, Stage 1 wanted to fire `literature` ("Let me get one more search…") but the loop exited.

Reconciling schemas will let the 3 dispatches *land successfully*, but does nothing to fix the 4-turn limit — Stage 1 will still need at minimum:
- 1 turn to fan out the 4 dispatches (best case: parallel; observed: sequential)
- 1 turn to synthesize after the last result

Worst-case observed (sequential dispatch + 1 synthesis turn) = 5 turns. **Recommended bump: 6 turns minimum, 8 turns conservative.**

Cost implication: each Stage 1 turn ≈ $0.30–$0.60 (Sonnet input growing as tool_results accumulate; output bounded by `max_tokens=4096`). Bumping +4 turns max = +$1.20–$2.40 per assessment in worst-case scenario where Sonnet never parallelizes. Average case (some parallelization) closer to +$0.50. Already inside the $15/run hard kill.

The bump is a 1-line constant change in [runtime.py:110](../orchestrator_runtime/runtime.py#L110). Plus consider whether to expose it as `ORCH_SUB_AGENT_LOOP_MAX_TURNS` env var for runtime tuning — would mirror the `ORCH_SUB_AGENT_BUDGET_TOKENS` env convention. ~5 lines.

### S-3 — 200k aggregate token budget is on the edge

`DEFAULT_BUDGET_TOKENS = 200000` ([sub_agent_dispatcher.py:42](../orchestrator_runtime/sub_agent_dispatcher.py#L42)) is the *aggregate* cap across all sub-agents in a single assessment. The competitive sub-agent alone burned `in=205076 out=1942` on VRDN — a *single role* exhausted the *whole assessment's* cap.

Two diagnoses, not mutually exclusive:
1. **Per-tool-result truncation (`MAX_TOOL_RESULT_CHARS=30000`) isn't enough for cumulative growth.** Sonnet calls 4–5 tools per role, each delivering ~30k chars (post-truncation) = 8k–10k tokens. Across 5 tool calls that's 50k tokens *just for tool_results*, plus the system prompt + asset_context + prior assistant turns. Crossing 200k by turn 3–4 is plausible.
2. **`internal_rag_hybrid_search` with `corpus="all"` (competitive's setting) returns the most rows per query.** Tightening competitive's corpus default or its `k` cap would cut tool_result size.

Strategy options:
- **A.** Raise `DEFAULT_BUDGET_TOKENS` to 300000 — uses Sonnet's 200k context for one role + headroom for 1-2 other small calls. Cost-naive but simple.
- **B.** Add per-role token caps that sum to 200k (e.g., literature=80k, competitive=70k, regulatory=40k, options=10k). More work; tighter discipline.
- **C.** Cut competitive's `corpus="all"` to `"filings"` and add per-query `k=5` ceiling. Smallest change; surgical.

Sibling fix; not part of the schema reconciliation but should land in the same Phase 2C cycle. Recommend **C** as the first move (cheapest), reassess after a 7-day soak.

### S-4 — `failed_reactor_events.payload->>'source'` confirmed shared-DLQ pattern

Memory [`failed_reactor_events_shared_dlq`](../memory/failed_reactor_events_shared_dlq.md) documented that the table accepts events from both reactor edge function AND Cowork preflight skills, distinguished by `payload->>'source'`. The dry-run added a third producer: `sub_agent_dispatcher._log_to_dlq` ([sub_agent_dispatcher.py:118–133](../orchestrator_runtime/sub_agent_dispatcher.py#L118)) writes with `source='sub_agent.<role>'`. The dispatch source matches the parent plan's expectation and the memory's pattern. No new DLQ schema work needed; just confirm the memory entry is current.

---

## What is NOT broken (verified during this audit)

- ROLE_REGISTRY structure ([modal_workers/sub_agents/__init__.py](../modal_workers/sub_agents/__init__.py)): 4 sub-agents + `ic_memo` = 5 entries. Test asserts this exactly ([test_sub_agent_runners.py:33–37](../orchestrator_runtime/tests/test_sub_agent_runners.py#L33)).
- DISPATCH_TOOL_DEF enum lists exactly the 4 expected roles ([test_sub_agent_dispatcher.py:169–173](../orchestrator_runtime/tests/test_sub_agent_dispatcher.py#L169)).
- Per-role kill switch (`ORCH_DISABLE_<ROLE>=1`) works ([test_sub_agent_dispatcher.py:82–95](../orchestrator_runtime/tests/test_sub_agent_dispatcher.py#L82)).
- DLQ write path is exercised on schema failure ([test_sub_agent_dispatcher.py:122–136](../orchestrator_runtime/tests/test_sub_agent_dispatcher.py#L122)).
- Polygon degraded-mode contract still works ([test_sub_agent_runners.py:101–119](../orchestrator_runtime/tests/test_sub_agent_runners.py#L101)).
- `SCHEMA_DIR` resolution in [modal_workers/sub_agents/runtime.py:54–62](../modal_workers/sub_agents/runtime.py#L54) finds the canonical schemas in this checkout (the worktree-relative fallback works).

What the tests *don't* exercise: end-to-end Sonnet → schema-validated payload with a real model call. All tests use `_FakeRunner` returning canned dicts. **This is the gap that let F-SD1–F-SD4 ship.**

---

## Estimated effort

Adopt Strategy A across all 4 roles + sibling S-1 + sibling S-2 in one batch:

| Item | Effort |
|---|---|
| Rewrite 4 skill prompts to match schemas | 3h |
| `build_user_content` schema-injection in `runtime.py` | 1h |
| `SubAgentSchemaError` metrics fix (S-1) | 1h |
| `SUB_AGENT_LOOP_MAX_TURNS` bump + env var (S-2) | 0.5h |
| New end-to-end test: stub Anthropic → emit prompt-grounded JSON → assert schema_pass=True for all 4 roles | 3h |
| Re-run dry-run on AXS-05 + one more (per Step 1 of phase-2c plan) | 1h |
| Pedro reviews 4 prompts + sibling S-3 corpus decision | async |
| **Total engineer time** | **~1.5 engineer-days** |

Sibling S-3 (200k budget tightening) is a separate decision — left out of this batch's hard estimate, recommend tackling in the post-flip 7-day soak window.

---

## When this becomes urgent

Already urgent — Phase 2C is halted. Watch for:

- Pedro picking a reconciliation strategy (Strategy A is the recommended path; B or C remain open).
- Anyone manually setting `ORCH_ENABLE_SUB_AGENTS=1` in the Modal secret without reconciling first — would land 100% schema_pass=false in production and saturate the DLQ at the next pg_cron drain tick.
- Phase 2C.5 (ensemble path) planning starting — that plan should depend on this reconciliation.

Until Strategy A lands, Phase 2C cannot pass Step 1 of its own preflight (dry-run validation).

---

## Strategy A — landing record (2026-05-23, worktree `brave-mestorf-ff922c`)

Applied on Pedro approval ("go with a"). All changes are textual edits to prompts + 3 code locations; no schema migrations, no runner-class logic changes.

| Change | File | Lines |
|---|---|---:|
| S-1: thread metrics through `SubAgentSchemaError` | `modal_workers/sub_agents/runtime.py` (constructor + raise site) | +14 |
| S-1: dispatcher copies metrics from `exc` instead of leaving zero | `orchestrator_runtime/sub_agent_dispatcher.py` (except block) | +6 |
| S-2: bump `SUB_AGENT_LOOP_MAX_TURNS` 4 → 6 + env override | `orchestrator_runtime/runtime.py:110` | +6/-1 |
| Schema-body injection in `build_user_content` | `modal_workers/sub_agents/runtime.py` (`SubAgentRunner.build_user_content`) | +14/-3 |
| Rewrite Output-schema section + Internal-loop bullets | `conan-fda-orchestrator-plugin/skills/sub_agent_regulatory_history.md` | ~80 |
| Rewrite Output-schema section | `conan-fda-orchestrator-plugin/skills/sub_agent_options_microstructure.md` | ~25 |
| Rewrite Output-schema section | `conan-fda-orchestrator-plugin/skills/sub_agent_competitive_landscape.md` | ~35 |
| Rewrite Output-schema section | `conan-fda-orchestrator-plugin/skills/sub_agent_literature_reviewer.md` | ~45 |

**Tests:** 35/35 pass (`test_sub_agent_dispatcher.py`, `test_sub_agent_runners.py`, `test_stage1_dispatch_sub_agent.py`, `test_sub_agent_rag_tools.py`). The dispatcher tests use `_FakeRunner` stubs so they don't exercise schema-body injection end-to-end; a stronger integration test (stub Anthropic → emit prompt-grounded JSON → assert `schema_pass=True` for all 4 roles) is owed before flip — captured below as a remaining-work item.

**Smoke verification:** `LiteratureRunner.build_user_content` now emits ~7k-char prompts containing the resolved schema body (verified in worktree against the local schema copy).

**Remaining work before Phase 2C flip:**

1. Land this branch on `main` and redeploy the Modal `conan-v3-orchestrator` image so the new prompts + code reach production.
2. Re-run the VRDN + AXS-05 dry-runs from `phase-2c-flip-async-treasure.md` Step 1. Expected: 4/4 schema_pass=true per assessment; `sub_agent_calls.tokens > 0` even on partial failures; Stage 1 doesn't exhaust `SUB_AGENT_LOOP_MAX_TURNS`.
3. If sub-agent burns confirm < $2/assessment and DLQ stays empty for 24h, flip `ORCH_ENABLE_SUB_AGENTS=1` in the Modal secret.
4. (Operator action) re-sync `/Users/Pico/Documents/Claude/Projects/Conan/.claude/worktrees/conan-cowork-skills/` to match canonical or delete it (see Appendix A above).
5. Add integration test that loads each runner's resolved prompt and asserts the schema body's `required[]` strings appear in it.
6. Defer S-3 (200k aggregate token cap tightening) to the post-flip 7-day soak.

---

## Appendix — Live DB verification (2026-05-23)

Queried Supabase project `conan` (`xvwvwbnxdsjpnealarkh`) via MCP to confirm runtime shapes after the audit was drafted from `failed_reactor_events.error_message` narratives. Findings match the audit body with one correction (already applied above): the options runtime emits 13 fields including `event_date`, raising the three-way intersection to 4/13.

**`sub_agent_calls` rows from the dry-run** (`SELECT id, role, schema_pass, tokens, cost_usd, latency_ms, jsonb_object_keys(output)`):

| sub_agent_calls.id | role | schema_pass | tokens | cost_usd | latency_ms | output keys (verbatim) |
|---|---|---|---:|---:|---:|---|
| `191f009b-…` | regulatory_history | false | 0 | 0.0000 | 0 | `adcomm_scheduled, base_rate_context, evidence_gaps, fda_safety_concerns_precedent, last_updated, manufacturing_inspection_signals, precedent_comparator, regulatory_history_summary, rems_requirement` |
| `85c8d7f3-…` | options_microstructure | false | 0 | 0.0000 | 0 | `analysis_date, atm_iv_pct, data_quality, event_date, event_type, front_month_expiry, iv_term_structure, liquidity_score, notes, open_interest_analysis, skew_metrics, straddle_implied_move_pct, ticker` |
| `fc3b7162-…` | competitive | false | 0 | 0.0000 | 0 | `partial_output` *(only — confirms the 200k aggregate budget exhaustion)* |

**Sibling concern S-1 confirmed verbatim:** all three rows show `tokens=0, cost_usd=0.0000, latency_ms=0`. The dispatcher's `except SubAgentSchemaError` block in [`sub_agent_dispatcher.py:247–251`](../orchestrator_runtime/sub_agent_dispatcher.py#L247) is dropping the partial-execution metrics on the floor. Fix outlined in S-1.

**DLQ rows from the same window** (`failed_reactor_events WHERE payload->>'source' LIKE 'sub_agent.%'`):

| failed_reactor_events.id | source | error_message (first 280 chars) |
|---|---|---|
| `5fd58365-…` | `sub_agent.regulatory_history` | `[]: Additional properties are not allowed ('adcomm_scheduled', 'base_rate_context', 'evidence_gaps', 'fda_safety_concerns_precedent', 'last_updated', 'manufacturing_inspection_signals', 'precedent_comparator', 'regulatory_history_summary', 'rems_requirement' were unexpected); []:` |
| `4b19c5ce-…` | `sub_agent.options_microstructure` | `[]: Additional properties are not allowed ('analysis_date', 'atm_iv_pct', 'event_type', 'front_month_expiry', 'iv_term_structure', 'liquidity_score', 'notes', 'open_interest_analysis', 'skew_metrics' were unexpected); []: 'schema_version' is a required property; []: 'asset_id' is …` |
| `2ce35d60-…` | `sub_agent.competitive` | `[]: 'schema_version' is a required property; []: 'asset_id' is a required property; []: 'competitors' is a required property; []: 'moat_summary' is a required property; []: 'retrieved_at' is a required property` |

**Confirms S-4:** the `failed_reactor_events.payload->>'source'` shared-DLQ pattern (`sub_agent.<role>`) matches memory [`failed_reactor_events_shared_dlq`](../memory/failed_reactor_events_shared_dlq.md). No new DLQ work needed.

**Note on regulatory's error_message** (truncated at 280 chars in the table above): the validator surfaced only the `additionalProperties` rejections in the first error window; the missing-required-property errors (`prior_adcomms`, `analogous_approvals`, `regulatory_risks`, `retrieved_at`) almost certainly follow downstream in the same message but were trimmed by the dispatcher's `"; ".join(errors)[:1000]` cap at [sub_agent_dispatcher.py:129](../orchestrator_runtime/sub_agent_dispatcher.py#L129). The full error array is preserved in-process; consider widening that cap if DLQ triage needs more detail (sibling fix; ~1 line).

**Full error texts retrieved post-audit (2026-05-23):**

- `sub_agent.regulatory_history`: missing-required errors confirm canonical schema = `{schema_version, asset_id, prior_adcomms, analogous_approvals, regulatory_risks, retrieved_at}`. Matches `/Users/Pico/Documents/Claude/Projects/conan-cowork-skills/schemas/regulatory_history_v1.json` exactly.
- `sub_agent.options_microstructure`: missing-required errors confirm `{schema_version, asset_id, computed_at}` + an enum violation `['data_quality']: 'degraded' is not one of ['fresh', 'stale', 'unavailable']`. Matches canonical schema. **Bonus finding: the runner's `_degraded()` helper at [options_microstructure.py:96–101](../modal_workers/sub_agents/options_microstructure.py#L96) returns `status='degraded'` (status, not data_quality), and Sonnet conflated the two fields, emitting `data_quality='degraded'`. The Strategy A schema-injection should ground Sonnet's emission to the proper enum; if it still drifts, consider tightening the runner's degraded-mode contract or adding an explicit enum reminder in `sub_agent_options_microstructure.md`.**
- `sub_agent.competitive`: missing-required errors confirm `{schema_version, asset_id, competitors, moat_summary, retrieved_at}`. Matches canonical schema.

### Appendix A — Schema-sync gotcha discovered during Strategy A landing

The runner's `SCHEMA_DIR` resolution at [modal_workers/sub_agents/runtime.py:54–62](../modal_workers/sub_agents/runtime.py#L54) walks `parents[3]/conan-cowork-skills/schemas` and falls back to `parents[2]/conan-cowork-skills/schemas`. From the worktree-root `modal_workers/sub_agents/runtime.py`, `parents[3]` = `.claude/worktrees/` — and there's a STALE copy of `conan-cowork-skills` checked out at that path with a RICHER `regulatory_history_v1.json` (including `class_membership`, `class_precedents`, `base_rates`, `sponsor_track_record` as required fields). This drifted from the canonical schema at `/Users/Pico/Documents/Claude/Projects/conan-cowork-skills/schemas/` at some prior point.

Production is NOT affected — the live `failed_reactor_events.error_message` rows above prove the production Modal image carries the canonical MINIMAL schema (matches `/Users/Pico/Documents/Claude/Projects/conan-cowork-skills/schemas/` byte-for-byte by required-list).

But local Cowork runs on a machine that has the rich stale copy at `.claude/worktrees/conan-cowork-skills/` would inject the wrong schema into the prompt and accept the wrong outputs. **Recommend:** delete or re-sync that stale checkout to match canonical. ~zero code change; one operator action. Not a Strategy A blocker.

