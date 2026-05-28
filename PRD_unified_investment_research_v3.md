# PRD — Unified Investment Research System v3 (FDA Depth)

**Status:** Phase 6 closed 2026-05-28 (code-level — see DECISIONS.md D-133). Gate 6 (50-asset operator review + final PRD stamp) blocked on Anthropic credit top-up — see `tasks/v4_phase6_close_blockers.md`.
**Date:** 2026-05-07 (initial draft) · last amended 2026-05-28 for v4 Phase 6 close-out
**Author:** Pedro (with Claude Code as architectural collaborator)
**Predecessor:** [PRD_unified_investment_research_v2.md](PRD_unified_investment_research_v2.md) (frozen at the v3 cutover; do not amend)

> **Reader note (2026-05-28):** the production runtime is **v4**, not v3. The "v4 AI-first runtime" subsection in §5 supersedes the original v3 multi-stage description elsewhere in this PRD. Phase 6a flipped the flag (2026-05-26), Phase 6b deleted Tier-2 (2026-05-26 Modal + 2026-05-28 Cowork), Phase 6c deleted the v3 codepath (2026-05-27, PR #152). Per D-132 and `docs/v4_phase6_runbook.md` the v3 rollback path no longer exists.

---

## 1. Context

v2 shipped a hardened, event-driven substrate: 19 scanners on Modal, Supabase as the event bus and state store, Postgres-INSERT-fires-edge-function reactor convergence, Resend email fan-out, a Next.js dashboard (private repo `marazuela/conan-dashboard`), and 6 scoring profiles seeded from the v1 `WEIGHTS` dict. That migration succeeded — every load-bearing v1 invariant was preserved (OpenFIGI normalize, candidate-gate v2 schema, atomic-write semantics, scanner timeouts, append-only DECISIONS register).

What v2 did **not** do: it kept the v1 mental model of "score every signal across 6 dimensions, threshold into Immediate/Watchlist/Archive/Discard, fan out alerts." That model is fast and broad — it covers 17 scanners and 7 thematic profiles — but it is shallow. A typical Immediate-band fire on an FDA signal looked like a 4-line dossier with no resolved reference class, no labeled outcome history, no confidence calibration, no asymmetric reasoning. The v2 dashboard could not answer the operator's actual question on a real catalyst: "Why is your conviction what it is, what would make you wrong, and what does the historical empirical base rate of cases like this look like?"

The strategic pivot (D-108, 2026-05-06) is **breadth → depth on FDA + EDGAR only**. The acknowledged trade: the predecessor `Investment_engine_v2`'s `binary_catalyst` + `activist_governance` profiles (and their convergence-driven RPAY-class wins) are sacrificed. The bet is that an FDA orchestrator with 5 specialist sub-agents, RAG-augmented synthesis, ensemble-with-dispersion, isotonic calibration, and a closed feedback loop produces conviction quality that justifies dropping breadth. This is revisited at the Phase 6 retrospective: if FDA conviction quality fails the operator-review gate (≥85% "watchlist-worthy", ≥70% "agree with direction" on 50 random `band='immediate'` v3 assessments), `activist_governance` reopens using the export's labeled events as bootstrap.

## 2. Objective

Ship a production v3 in which:

- Every active FDA asset (`fda_assets.is_active=true AND watch_priority<=2`) gets a refreshed `convergence_assessments` row at least daily, rendered on `/fda/[asset]` with calibrated `conviction_pct ∈ [0,100]`, hypothesis stack (bull / base / bear, each with ≥2 falsifiable `kill_conditions`), full citation graph, sub-agent panels (5 specialists), and reference-class anchor.
- Every conviction prediction has its outcome resolved within 7 days of its window-end, fed to the post-mortem queue, fitted into a fresh isotonic calibration curve nightly, and gated for activation by D-103's paired-bootstrap criteria.
- Operator can promote any IC memo into the standard signals/thesis pipeline via `fda_signal_promote_to_thesis(event_id, ic_memo_review_id, note)` from the dashboard.
- Cost ceiling is enforced at $15/run (Tier 1) and $1.50/run (Tier 2) with hard kill on overage, soft alerts at 80% of daily/per-asset budget via `operator_flags(source='orchestrator_cost')`.
- Eval harness Brier ≤ 0.18 on the held-out resolved-signal set; calibration accuracy 70-80% in the 70-80% bucket.

## 3. Non-goals (explicit)

- Reopening any non-FDA scanner. Per D-125, all 15 non-FDA scanners (asx, bse_nse, bmv, congressional_trading, courtlistener, cvm, delaware_chancery, esma_short, hkex, kind, lse_rns, sec_enforcement, sedar_plus, takeover_candidate, tdnet) are `status='deprecated'` in the registry. Deletion happens at Phase 7 cleanup.
- Multi-model debate (Opus vs Sonnet adversarial). Research suggests it does not beat self-critique for factual synthesis at the scale we operate. Deferred to Phase 8+ spike.
- A pure-research Tier 3 (multi-day Opus deep-dive on individual assets). The Tier 1 / Tier 2 split below is sufficient for the operator workflow.
- Replacing the v2-era `candidate_gate.assess_thesis_v2` validator. The v3 IC memo runner emits a structurally richer payload (5 sections vs 5 fields), but the legacy validator stays as the operator-facing thesis-quality gate for v2 compatibility.
- A backtest UI separate from the eval harness dashboard. `/eval` and `/eval/[caseId]` are the only backtest surfaces.

## 4. Users

Same as v2: Pedro primary, 2–3 collaborators secondary. Single shared workspace, RLS on `annotations` / `watchlists` / `notifications_prefs` only. Auth via Supabase magic link.

New v3 user surface: `/eval` (case list + run trends), `/eval/[caseId]` (per-case drill-in with calibration overlay), `/fda/[id]/memory` (per-asset memory file viewer reading from `memory_files` Storage bucket), `/calibration` (active curve + history).

## 5. Architecture — v3 decisions

These are settled (DECISIONS.md D-100..D-127). The Claude Code session should not relitigate; surface concerns before any unilateral change.

**v4 AI-first runtime.**
- **Live path** — API SDK direct, single-pass FDA + commercial Stage 1 synthesis, Stage 9 structured extraction, deterministic citation validation, isotonic calibration, market-side gate, and Stage 10 persistence/memory writeback. The former Stage 2 hypothesis module, Stage 3 premortem module, Stage 6 ensemble module, and semantic Stage 7 constitutional module were removed in the Phase 6c cleanup. Triggered by `new_doc`, `cross_source`, `operator_refresh`, `manual`, and scheduled drain events. Cost envelope is governed by the per-run hard kill in `orchestrator_runtime/client.py`.
- **Eval-only sidecar** — `.claude/skills/assess-fda-binary-catalyst/SKILL.md` can produce single-shot Opus assessment artifacts for replay comparison. It is not a production writer in Phases 0-2; promotion requires eval evidence.
- **Retired** — Tier-2 Cowork `bulk_orchestrator` and the v3 `ORCH_V4=0` rollback branch are sunset.

**Sub-agents (5 roles, all under `modal_workers/sub_agents/`).** Each runner subclasses `SubAgentRunner` (D-124), exposes a Sonnet 4.5 tool-use loop wired to in-process MCP equivalents, validates output against a JSON Schema (Draft-7) at `conan-cowork-skills/schemas/<role>_v1.json`, raises `SubAgentSchemaError` on failure (DLQs to `failed_reactor_events` with `source='sub_agent.<role>'`). Per-role kill switches via `ORCH_DISABLE_<ROLE>=1`. Stage 1 dispatch global flag `ORCH_ENABLE_SUB_AGENTS=0` (default off; flips at Phase 2C).

| Role | Tools | Output schema |
|---|---|---|
| `literature` | pubmed search/fetch/citation_graph; biorxiv (stub); internal_rag.hybrid_search | literature_review_v1 |
| `competitive` | clinicaltrials search/by_nct; pubmed search/fetch | competitive_landscape_v1 |
| `regulatory_history` | openfda drugsfda/labels/aes; fda_adcomm upcoming/historical; compute.similar_cases | regulatory_history_v1 |
| `options_microstructure` | polygon get_chain/get_iv/straddle/event_window_liquidity (degraded-mode when key absent) | options_microstructure_v1 |
| `ic_memo` | (none — synthesis-only over the 4 specialists + Stage 9 thesis) | ic_memo_v1 |

**MCP servers (8, all in `conan-fda-orchestrator-plugin/mcp_servers/`).** FastMCP Python: pubmed, biorxiv, clinicaltrials, openfda, fda_adcomm, polygon, internal_rag, compute. Smoke-tested per tool in `modal_workers/tests/test_mcp_servers.py`. The internal_rag server wraps the same `modal_workers/rag/hybrid_search` that the in-process `rag_handle.py` calls — identical logic, two surface areas (in-process for Tier 1 runtime, MCP for Cowork bulk).

**RAG.** Voyage-3-large embeddings (Matryoshka 2000-dim cap per pgvector HNSW limit, locked in by D-127's contextual-augmenter app). Voyage rerank-2.5. HNSW indexes (m=32, ef_construction=200, ef_search=200). 4 corpora: `literature`, `filings`, `labels_aes`, `news` (separate `chunk_embeddings_<corpus>` tables). Hybrid search = BM25 top-150 + dense top-150 → RRF fuse → rerank-2.5 → top-20. Stage 1 retrieval gated by `ORCH_ENABLE_STAGE_1_RAG`. Backfill via `modal_workers/scripts/backfill_rag_corpus.py` (idempotent, resumable).

**Eval-gated everything.** Every prompt change, every architecture change, every calibration refit gated by `eval_runs.passed_gate=true` per D-103's paired-bootstrap criteria: Brier delta > 0; paired-bootstrap p < 0.05; n ≥ 200; AUC delta ≥ 0.05; max single-asset contribution ≤ 5%. Implementation in `modal_workers/scripts/nightly_calibration_refit.py` + `modal_workers/shared/post_mortem_runner.py`. Snapshot policy: prompts append-only in `prompt_versions`; `calibration_curves` keyed by `version` PRIMARY KEY; rollback monitor (D-104) computes daily Spearman over last 30 days of resolved post_mortems and reverts when correlation < 0.20 or drops ≥ 0.15 from prior.

**Cost ceiling enforcement (D-125).** `orchestrator_runtime/client.py` + `orchestrator_runtime/pricing.py` accumulate per-call USD against the per-run budget; hard kill via `BudgetExceededError`. `modal_workers/shared/cost_budget.py` runs 24h-window threshold checks and surfaces soft alerts via `operator_flags(source='orchestrator_cost')`. Per-sub-agent caps enforced in the dispatcher (`ORCH_SUB_AGENT_BUDGET_TOKENS=200000` default).

**Memory hierarchy (D-123 Contract C5).** Five scopes — `asset`, `indication`, `reviewer_panel`, `reference_class`, `sub_agent` — backing markdown blobs in the `memory_files` Storage bucket, indexed by the `memory_files` table. Stage 0 loads, Stage 10 appends `## Recent assessments` entry per assessment via `MemoryStore.write` (`orchestrator_runtime/memory.py`).

**Replay cassette (D-127).** `orchestrator_runtime/eval_harness/{cassette,replay_runner}.py` provides deterministic record/replay of orchestrator runs. SHA-256 hash of `(model, system, messages, tools, tool_choice)` keys each entry; mismatches raise `CassetteMismatchError`. Cost recomputed via `pricing.estimate_cost` so budget accumulator behaves identically across modes. Tool-use loops are NOT supported in replay (cassette captures `text` + `thinking`, not `tool_use` / `tool_result`); sub-agent dispatch is feature-flagged off in replay mode.

## 6. Preserved artifacts from v2

These survive unchanged or with documented narrowing.

- **OpenFIGI normalize_ticker** (`shared/openfigi_resolver.py::normalize_ticker`) — v1 invariant from D-052, preserved verbatim through v2 and v3.
- **`fda_signal_bridge` scanner + `fda_event` rubric.** The v3 IC-memo promotion path uses these (signal_id namespaced `v3:`) so existing thesis_writer / candidate_gate / fanout machinery reuses unchanged.
- **`candidate_gate.assess_thesis_v2`.** Operator-facing thesis quality gate — kept as the v2/v3 boundary on the dashboard.
- **`memory_path` field on `fda_assets`** + the per-asset memory hierarchy. v2 ingestion wrote this; v3 orchestrator reads + appends.
- **`auth.users` RLS model** + magic-link auth + per-user `annotations` / `watchlists` / `notifications_prefs`. Untouched.
- **Reactor + fanout edge functions.** Reactor extends to dispatch `convergence_assessments AFTER INSERT` to fanout when `band='immediate' AND superseded_by IS NULL` (D-122). Fanout email templates extended to render the v3 assessment shape, falling back to the v2 alert template when the assessment row is null.
- **`alerts` + `candidates` + `signals` tables.** Schema unchanged. v3 writes promote into the same `signals` table via the `fda_signal_promote_to_thesis` RPC.

## 7. Data model — v3 additions (column-level in `supabase/migrations/2026050[6-9]_*.sql`)

Authoritative spec lives in the migration files. This is orientation only.

- `documents`, `asset_documents`, `extracted_facts` — Phase 1 document buffer + extractor output. `asset_documents` has pass-2 verifier columns added by D-125 (`pass2_verdict`, `pass2_confidence`, `pass2_at`).
- `convergence_assessments` — v3 orchestrator output (the main row). 30+ columns covering Stages 0–10 trace + ensemble + calibration + market snapshot + cost/latency. Indexes on `(asset_id, created_at DESC) WHERE superseded_at IS NULL`.
- `assessment_stage_metrics` — per-stage observability (one row per stage per assessment).
- `orchestrator_runs` — top-level run audit; `tier` ∈ {1, 2, 3}, `status` ∈ {pending, running, completed, failed, killed_budget, skipped_budget}. The `dashboard_signal_rows` view exposes the latest completed run's tier per entity_id via LATERAL JOIN.
- `sub_agent_calls` — per-dispatch observability with token / cost / latency / schema_pass.
- `post_mortem_queue` — closed feedback loop driver; outcomes resolve via `label_forward_returns`.
- `eval_harness` — held-out resolved historical FDA signals (81 curated rows + 1502 staged for Phase 4B ETL).
- `eval_runs` — per-prompt-change gate decisions (D-103 fields populated).
- `calibration_curves` — isotonic curves keyed by `version`, only one `is_active=true` at a time.
- `reference_class_base_rates` — empirical FDA approval rates by class; refit nightly via post-mortem outcomes (Wilson CI).
- `memory_files` — index into Storage; one row per (scope, scope_id) tuple.
- `prompt_versions` — append-only prompt snapshots for D-104 rollback.
- `failed_reactor_events` — DLQ shared by reactor edge function and Cowork preflight skills (filter by `payload->>'source'` to distinguish).

## 8. Event flow

```
Modal scanner → documents INSERT → asset_linker (pass1+pass2)
                                   → extracted_facts populate
                                   → orchestrator_run trigger
                                       → v4: load evidence + reference anchor
                                           → AI thesis synthesis
                                           → structured extraction
                                           → deterministic citation validation
                                           → calibration + market gate
                                       → convergence_assessments INSERT
                                          → reactor edge fn dispatches to fanout (if band=immediate)
                                          → fanout sends email + Realtime broadcast
                                       → memory_files append
                                       → post_mortem_queue INSERT (window-end scheduled)

Window-end resolution → post_mortem_runner → realized_outcome INSERT into post_mortem_queue
                                           → reference_class_base_rates UPSERT (Wilson CI)
                                           → memory_files append (resolved post-mortem entry)

Nightly (≥10 new resolved) → nightly_calibration_refit
                              → fit isotonic on (raw_conviction_pct, hit) pairs
                              → evaluate_gate (paired_bootstrap p, AUC delta, asset concentration)
                              → INSERT calibration_curves (is_active=false)
                              → if gate.passed AND ENABLE_PROMOTION: flip is_active
                              → INSERT eval_runs (audit)

Operator → /fda/[id] → review assessment + citation graph + optional IC memo
                     → promote via fda_signal_promote_to_thesis(event_id, ic_memo_review_id, note)
                       → signals INSERT (signal_id 'v3:<event_id>')
                       → thesis_writer pipeline runs unchanged
```

## 9. Verification (acceptance gates)

Per `~/.claude/plans/plan-it-for-optimal-twinkling-bubble.md`:

- **Gate 0 (operator):** `anthropic-orchestrator` Modal secret created; Voyage key in `scanner-secrets`; Polygon decision (provide or accept degraded).
- **Gate 1 (foundation):** RAG corpus backfill complete; sub-agent schemas + jsonschema validation green; MCP smoke tests + polygon degraded-mode green.
- **Gate 2 (Tier 1 live):** AXS-05 produces 4 specialist `fda_agent_reviews` + 1 IC memo + 1 `convergence_assessments` row + non-null `conviction_pct` + cache-hit metrics logged.
- **Gate 3 (dashboard):** real conviction + tier render on `/fda/[id]`; IC memo promotion writes a `thesis_jobs` row; `/eval/[caseId]` shows calibration overlay.
- **Gate 4 (calibration closure):** ≥1200 eval_harness rows seeded; ≥80% have ≥3 linked documents; first refit cycle produces `calibration_curves` row #2 with `passed_gate IS NOT NULL`.
- **Gate 5 (Tier 2):** 30-day side-by-side Brier delta `tier1 - tier2 < 0.15`.
- **Gate 6 (cutover):** 50-asset operator review ≥85%/70% pass; v3 default flipped; legacy delete walked; this PRD stamped final.
- **Gate 7 (ongoing):** rollback monitor's daily Spearman > 0.20; nightly refit logs show gate decisions.

## 10. Open decisions (revisit at Phase 6 retrospective)

- Reference-class granularity: start coarse, refine when `n_cases ≥ 50`.
- Multi-model debate (Opus vs Sonnet): deferred to Phase 8+ spike.
- Hard delete vs rename-deprecate legacy columns: rename to `_deprecated_*_v1`, drop after 2-week soak.
- Routine quotas for Tier 2: 80% alert threshold; operator confirms allowance per Max account.
- Reopening `activist_governance`: only if FDA conviction quality fails the Phase 6 operator-review gate.
