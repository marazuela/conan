# Skills drift audit — 2026-05-11

**Scope:** three Cowork↔Plugin skill pairs flagged by domain overlap. Find-only — Pedro accepts/rejects.
**Method:** read both bodies + cross-reference call sites (`modal_workers/sub_agents/*.py`, `orchestrator_runtime/tier2.py`, `compose_features`, `fda_agent_reviews` consumers).

## Pair 1 — microstructure

| | `fda_microstructure_review` (cowork) | `sub_agent_options_microstructure` (plugin) |
|---|---|---|
| Trigger | Hourly cron `45 * * * *`; drains `fda_agent_reviews WHERE agent_kind='microstructure' AND status='queued'` | v3 orchestrator Stage 1 tool-call; "Asset has PDUFA / AdComm within 60d" |
| Quota | 10 / UTC day soft cap | Budget gate: $0.05–0.10/run, hard kill $0.20 |
| Inputs | Existing `fda_event_evidence` rows (`source='polygon'` if any); FINRA / IBKR / whalewisdom / dataroma / seekingalpha via WebSearch+WebFetch | `polygon-mcp.{get_chain,get_iv,straddle_implied_move,event_window_liquidity}` only |
| Output schema | Custom: `options_liquidity_score (0-5)`, `implied_move_pct`, `borrow_cost_bps`, `crowding_score`, `event_window_open_interest` | `options_microstructure_v1.json`: `straddle_implied_move_pct`, `iv_30d`, `iv_60d`, `iv_term_slope`, `event_window_liquidity_score`, `oi_concentration{}`, `position_inferred` |
| Consumer | v2 `compose_features` in `modal_workers/scanners/fda_event_features.py` | v3 orchestrator Stage 1 evidence ledger → Stage 5 synthesis |
| Validator | `modal_workers.shared.fda_agent_validator.validate('microstructure', …)` | Pydantic against `options_microstructure_v1.json` |

**Verdict:** `keep_both_distinct` short-term; **flag cowork as v2-teardown-phase-2 candidate.**

Different schemas, different runtimes, different consumers. Cowork's only adds borrow-cost + crowding on top of Polygon. Plugin sub-agent owns the implied-move-vs-IV-term-structure analysis. Under the strategic pivot (D-100, 2026-05-06) v3 is the keeper; cowork's microstructure drainer is v2-cockpit-only.

## Pair 2 — regulatory

| | `fda_regulatory_review` (cowork) | `sub_agent_regulatory_history` (plugin) |
|---|---|---|
| Trigger | Hourly cron `30 * * * *`; drains `agent_kind='regulatory'` queued rows | v3 orchestrator Stage 1; PDUFA ≤60d OR Phase 3 readout ≤90d |
| Output schema | `adcom_risk_score (1-5)`, `crl_precedent (bool)`, `resubmission_pathway (smooth/difficult/unlikely/n/a)`, `staff_review_redflags[]`, `evidence_confidence_boost (±0.40)` | `regulatory_history_v1.json`: `class_membership{}`, `class_precedents[]` (each with primary_source_url), `base_rates{}` w/ Wilson CIs, `sponsor_track_record{}`, `reviewer_panel_concerns[]`, `divergence_from_norm_flags[]` |
| Inputs | fda.gov AdCom calendar + Federal Register + FDA briefing books + EDGAR 8-K via WebSearch+WebFetch | `openfda-mcp.{search_approvals,search_warning_letters,get_orange_book,search_faers}`, `fda-adcomm-mcp.{get_calendar,search_transcripts,get_panel_composition,get_voting_history}`, `internal-rag-mcp.hybrid_search_{adcomm,internal}`, `compute-mcp.compute_base_rate` |
| Methodology depth | Single-pass narrative + a scalar ±0.40 boost | Full base-rate computation with binomial CIs over 10-year lookback; AdComm voting history; sponsor EDGAR fingerprint; memory writeback to `/memories/sub_agents/regulatory/<indication>_<panel>.md` |
| Consumer | v2 `compose_features` | v3 Stage 1 ledger |

**Verdict:** `keep_both_distinct` short-term; **flag cowork as v2-teardown-phase-2 candidate.**

Plugin sub-agent is dramatically more thorough and is the authoritative regulatory-context source under v3. Cowork's regulatory review is a lightweight scout that exists only to feed the v2 cockpit's deterministic feature math. Once v2 is torn down, cowork's job is gone.

## Pair 3 — medical / literature

| | `fda_medical_review` (cowork) | `sub_agent_literature_reviewer` (plugin) |
|---|---|---|
| Trigger | Hourly cron `15 * * * *`; drains `agent_kind='medical'` queued rows | v3 orchestrator Stage 1; Phase 3 readout or PDUFA in assessment window |
| Output schema | `endpoint_quality (1-5)`, `safety_concerns[]`, `effect_size_pp`, `precedent_class_outcome (approved/CRL/withdrawn)`, `fair_probability_modifier (±0.10)` | `literature_review_v1.json`: `papers[]` (pmid, abstract, venue, study_type, relevance_score, evidence_strength, supports_thesis_direction, primary_source_url, citations_inbound, verbatim_quote_for_finding), `contradictory_findings[]`, `missed_seminal_via_citation_graph[]` |
| Inputs | ClinicalTrials.gov + PubMed/NEJM/Lancet/JAMA + FDA briefing books + class-precedent searches via WebSearch+WebFetch | `pubmed-mcp.{search,fetch_full_text,citation_graph_expand}`, `biorxiv-mcp.{search,fetch_preprint_pdf}`, `internal-rag-mcp.{hybrid_search_literature,rerank,fetch_chunk_with_context}` |
| Methodology depth | Broad clinical-merit assessment + scalar modifier | Endpoint-integrity check (alpha spending, hierarchical testing, ITT/mITT/PP), citation-graph expansion, contradiction surfacing (no resolution) |
| Consumer | v2 `compose_features` (±0.10 on `fair_probability`) | v3 Stage 1 ledger |

**Verdict:** `keep_both_distinct` short-term; **flag cowork as v2-teardown-phase-2 candidate.** Note `fda_medical_review` actually overlaps **both** plugin sub-agents (literature reviewer + regulatory history's class-precedent enumeration). When migrating, cowork's medical review splits: published-evidence portion → `sub_agent_literature_reviewer`; class-precedent portion → `sub_agent_regulatory_history`. The scalar `fair_probability_modifier` is v2-specific (it feeds `compose_features` deterministic math) and dies with v2.

## Summary

| Cowork skill | Plugin counterpart | Now | Post-v2-teardown |
|---|---|---|---|
| `fda_microstructure_review` | `sub_agent_options_microstructure` | keep both | retire cowork |
| `fda_regulatory_review` | `sub_agent_regulatory_history` | keep both | retire cowork |
| `fda_medical_review` | `sub_agent_literature_reviewer` (+ partly `sub_agent_regulatory_history`) | keep both | retire cowork (split into 2 plugin sub-agents on migration) |

**No immediate action required** — the three cowork skills are still load-bearing for the v2 cockpit's `compose_features`. Per `v2_teardown_phasing` memory, phase 2/3/4 are deferred; this audit is the input for whichever phase retires the v2 FDA agent-review pipeline. Decisions land in `DECISIONS.md` when Pedro signs off.

**Out of scope but worth noting:** none of the cowork↔plugin pairs share frontmatter `name:` (cowork uses `snake_case`, plugin uses `kebab-case`), so neither runtime accidentally loads the other's file. No collision risk today.
