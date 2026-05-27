# Conan Data Flow & AI Jobs — System Diagram

**As of 2026-05-13.** Dual-pipeline state: v2 (legacy, phasing down) + v3 (FDA-focused, in-flight).

---

## 1. Top-Level: Layers & Flow

```mermaid
flowchart TB
    subgraph L1["① INGESTION (scanners + fetchers)"]
        S_v2["v2 scanners<br/>(19 total, mostly paused)<br/>edgar_filing_monitor, fda_pdufa,<br/>takeover, esma, courtlistener…"]
        S_v3["v3 fetchers (Modal)<br/>openfda_corpus_ingest<br/>clinicaltrials_ingest<br/>federal_register_ingest<br/>edgar_8k_ingest"]
        S_fda["fda_signal_bridge<br/>(operator-fed)"]
    end

    subgraph L2["② DOCUMENT LINKING & EXTRACTION (v3 AI)"]
        AL1["🤖 asset_linker pass-1<br/>Sonnet 4.5 · */15"]
        AL2["🤖 asset_linker pass-2<br/>Haiku 4.5 · :10,:40"]
        FX["🤖 fact_extractor<br/>Sonnet 4.5 · :20 hourly"]
    end

    subgraph L3["③ ASSESSMENT — v3 ORCHESTRATOR (10 stages, N=7 ensemble)"]
        ORCH["Stage 0-10 pipeline<br/>(detail below)"]
    end

    subgraph L4["④ REACTOR & CONVERGENCE (v2 + v3 routing)"]
        REACT["reactor edge fn<br/>convergence stamping<br/>auto-caps + band promotion"]
    end

    subgraph L5["⑤ PROMOTION & PUBLISHING"]
        ALERTS["alerts table"]
        TJ["thesis_jobs"]
        FAN["fanout edge fn → Resend email"]
        TW["🤖 thesis_writer<br/>Claude API Immediate<br/>15/day cap (Cowork)"]
        DASH["Dashboard<br/>(Next.js · conan-dashboard)"]
    end

    subgraph L6["⑥ FEEDBACK & CALIBRATION (v3)"]
        PM["post_mortem_queue"]
        CAL["🤖 nightly calibration refit<br/>isotonic + D-103 gate"]
        EVAL["eval_harness / eval_runs"]
    end

    %% Data flow
    S_v2 --> SIGS[(signals)]
    S_v3 --> DOCS[(documents)]
    S_fda --> SIGS
    DOCS --> AL1 --> ADOCS[(asset_documents)]
    ADOCS --> AL2
    ADOCS --> FX --> EF[(extracted_facts)]
    ADOCS -- "INSERT triggers" --> REACT
    REACT -- "v3: enqueue" --> OR[(orchestrator_runs)]
    OR --> ORCH
    ORCH --> CA[(convergence_assessments)]
    ORCH --> FAR[(fda_agent_reviews<br/>incl. ic_memo)]
    CA --> DASH
    FAR -- "operator promotes" --> RPC["fda_signal_promote_to_thesis()"]
    RPC --> SIGS
    SIGS --> REACT
    REACT -- "band=immediate" --> ALERTS
    REACT -- "band=immediate" --> TJ
    ALERTS --> FAN
    TJ --> TW --> DASH
    CA --> PM
    PM --> CAL --> ORCH
    EVAL --> CAL

    style L1 fill:#e3f2fd
    style L2 fill:#fff3e0
    style L3 fill:#f3e5f5
    style L4 fill:#fce4ec
    style L5 fill:#e8f5e9
    style L6 fill:#fffde7
```

---

## 2. Ingestion Layer (Scanners + Fetchers)

```mermaid
flowchart LR
    subgraph V2["v2 scanners (Modal @modal.Cron dispatch)"]
        EF[edgar_filing_monitor<br/>3h · activist_governance]
        FP[fda_pdufa_pipeline<br/>3h · binary_catalyst]
        FB[fda_signal_bridge<br/>3h · v3 pass-through]
        TKC[takeover_candidate_scanner<br/>weekly]
        PAUSED[16 scanners PAUSED<br/>esma, courtlistener, lse_rns,<br/>tdnet, asx, sedar, hkex, kind,<br/>bse_nse, cvm, bmv, sec_enforce,<br/>pre_phase3, insider_form4,<br/>delaware_chancery, congressional]
    end

    subgraph V3["v3 fetchers (Modal · pg_cron triggered)"]
        OF[openfda_corpus_ingest<br/>06 UTC daily]
        CT[clinicaltrials_ingest<br/>on-demand]
        FR[federal_register_ingest<br/>13 UTC]
        EDG[edgar_8k_ingest<br/>13 UTC]
    end

    EF --> SIG_v2[(signals · v2)]
    FP --> SIG_v2
    FB --> SIG_v2
    TKC --> SIG_v2

    OF --> DOCS[(documents)]
    CT --> DOCS
    FR --> DOCS
    EDG --> DOCS

    DOCS -. "reactor INSERT trigger" .-> ORCH[orchestrator_runs queue]

    note1["⚠️ fda_regulatory_events<br/>(operator one-shot, 35 rows, frozen)<br/>NOT auto-fed"]
    note2["⚠️ catalyst_universe<br/>(fetcher-fed, 1791 rows growing)<br/>parallel ledger to fda_regulatory_events"]
```

---

## 3. v4 Orchestrator Pipeline — AI-First Single Pass

```mermaid
flowchart TB
    START([orchestrator_runs pending<br/>trigger: new_doc / cross_source /<br/>operator_refresh / tier2_escalation])

    S0[Stage 0: Document load + memory read<br/>Loads asset corpus + memory_files + RAG context]
    S4[Stage 4: Reference-class anchor<br/>base rate + similar resolved cases]
    RAG[Optional RAG retrieval<br/>Voyage embed + rerank over targeted corpora]
    S1[Stage 1: AI thesis synthesis<br/>FDA + commercial judgment, hypotheses, kill criteria inline]
    S9[Stage 9: Structured extraction<br/>JSON + commercial_dimensions]
    S7[Stage 7: Deterministic citation validation<br/>no LLM spend]
    CAL[Calibration + market gate<br/>isotonic curve, EV check]
    S10[Stage 10: Persist + memory append<br/>convergence_assessments + post_mortem_queue]

    DONE([convergence_assessments INSERT<br/>+ assessment_stage_metrics])

    START --> S0 --> S4 --> RAG --> S1 --> S9 --> S7 --> CAL --> S10 --> DONE

    BUDGET{{"Cost gate: per-run hard kill<br/>orchestrator_runtime/pricing.py"}}
    BUDGET -.-> S1
```

---

## 4. v2 Reactor Path (Legacy)

```mermaid
flowchart TB
    INS[signals.INSERT or UPDATE]
    R1[1. Parse webhook payload]
    R2[2. Resolve convergence_key<br/>FIGI → issuer match]
    R3[3. Query 14d window<br/>30d if litigation]
    R4[4. classifyGroup<br/>contradiction / same /<br/>orthogonal / single]
    R5[5. Compute bonus<br/>RPC: rubric_apply_caps]
    R6[6. UPDATE winner<br/>displace prior winners]
    R7{band = immediate?}
    R8a[alerts.INSERT<br/>1d dedup]
    R8b[thesis_jobs.INSERT]
    DLQ[(failed_reactor_events<br/>shared DLQ)]

    INS --> R1 --> R2 --> R3 --> R4 --> R5 --> R6 --> R7
    R7 -- yes --> R8a
    R7 -- yes --> R8b
    R7 -- no --> END([end])
    R1 -. on error .-> DLQ
    R5 -. on error .-> DLQ

    R8a --> FAN[fanout edge fn<br/>Resend email]
    R8b --> COW[Cowork thesis_writer skill<br/>🤖 Claude API Immediate · 15/day]
```

---

## 5. Feedback Loop & Calibration

```mermaid
flowchart LR
    CA[(convergence_assessments)] --> WAIT[await forward-return window]
    WAIT --> PM[(post_mortem_queue<br/>realized hit/miss)]
    PM --> NIGHT[🤖 nightly_calibration_refit.py<br/>fits isotonic on conviction_pct, hit pairs]
    NIGHT --> GATE{D-103 gate:<br/>Brier↓, bootstrap p,<br/>max asset contrib?}
    GATE -- pass --> ACT[calibration_curves<br/>is_active=true]
    GATE -- fail --> KEEP[keep current curve]
    ACT --> STAGE8[Stage 8 reads<br/>active curve]
    EH[(eval_harness<br/>~81 curated + 1502 staged)] --> NIGHT
    ROLL[Hourly rollback monitor D-104<br/>Spearman over 30d] -.-> ACT
```

---

## 6. AI Jobs — Complete Inventory

| # | Job | Trigger | Model | Compute | Cost/run |
|---|-----|---------|-------|---------|----------|
| 1 | **asset_linker pass-1** | pg_cron `*/15` | Sonnet 4.5 | Modal | $3–15 |
| 2 | **asset_linker pass-2** | pg_cron `:10,:40` | Haiku 4.5 | Modal | $2 |
| 3 | **fact_extractor** | pg_cron `:20 hourly` | Sonnet 4.5 | Modal | $30 |
| 4 | **Stage 1 RAG** | orchestrator_run_one | Voyage-3-large + rerank-2.5 | Modal | embed cost |
| 5 | **Stage 1 thesis synthesis** | orchestrator_run_one | Sonnet 4.5/4.6 | Modal | main analysis spend |
| 6 | **Stage 9 structured extraction** | post Stage 1 | Sonnet/Haiku extractor | Modal | low |
| 7 | **Stage 7 citation validation** | post Stage 9 | deterministic Python | Modal | none |
| 8 | **Stage 10 IC memo synthesis** | operator-triggered | Sonnet 4.5 | Modal | medium |
| 13 | **signal_resolver** (v2) | Cowork daily | Sonnet 4.5 (rescore_with_dims) | Cowork→Modal endpoint | shared budget |
| 14 | **candidate_aging** (v2) | Cowork weekly | Sonnet 4.5 (assess_thesis_v2) | Cowork→Modal endpoint | Tier 2 |
| 15 | **thesis_writer** | thesis_jobs.INSERT | Claude API (Immediate band) | Cowork (JGoror) | 15/day external cap |
| 12 | **nightly calibration refit** | pg_cron `02:00 UTC` | scipy.isotonic (no LLM) | Modal | compute-only |

---

## 7. Scheduled Jobs — Where They Run

```mermaid
flowchart LR
    subgraph PGCRON["pg_cron (Supabase, unlimited slots)"]
        PC1["v3-orchestrator-drain · */5"]
        PC2["v3-asset-linker-pass1 · */15"]
        PC3["v3-asset-linker-pass2 · :10,:40"]
        PC4["v3-fact-extractor · :20 hourly"]
        PC5["v3-feedback-loop-daily · 02:00"]
        PC6["v3-pipeline-watchdog · :05 hourly"]
        PC7["dispatch_3h (v2)"]
        PC8["dispatch_release_times (v2)"]
    end

    subgraph COWORK["Cowork skills (JGoror Windows)"]
        CW1["thesis_writer<br/>on thesis_jobs.INSERT"]
        CW2["signal_resolver<br/>daily"]
        CW3["candidate_aging<br/>weekly"]
        CW4["bulk_orchestrator<br/>per watch_priority"]
    end

    subgraph MODAL["Modal endpoints (HTTP)"]
        M1["compute_v3 multiplex<br/>orchestrator_app.py"]
        M2["rescore_with_dims_endpoint"]
        M3["assess_thesis_endpoint"]
        M4["feedback_loop_app.py"]
    end

    subgraph EDGE["Supabase Edge Functions"]
        E1["reactor (v2+v3 routing)"]
        E2["fanout (Resend email)"]
        E3["scanner-health"]
    end

    PC1 --> M1
    PC2 --> M1
    PC3 --> M1
    PC4 --> M1
    PC5 --> M4
    CW1 -. Claude API .-> ANTHROPIC[Anthropic API]
    CW2 --> M2
    CW3 --> M3
```

---

## 8. Data Stores — Where Each Stage Reads/Writes

| Layer | Key tables | Notes |
|-------|------------|-------|
| **Ingestion** | `documents`, `signals`, `scanner_runs`, `catalyst_universe`, `fda_regulatory_events` | v2 writes signals; v3 writes documents. `catalyst_universe` ≠ `fda_regulatory_events` (parallel ledgers) |
| **Linking** | `asset_documents`, `extracted_facts`, `fda_assets` | Sonnet pass-1, Haiku pass-2 verdict |
| **Queue** | `orchestrator_runs` | Status: pending/running/completed/failed/killed_budget |
| **Assessment** | `convergence_assessments`, `assessment_stage_metrics`, `sub_agent_calls`, `fda_agent_reviews`, `memory_files` | Stage 10 IC memo lives in `fda_agent_reviews` agent_kind='ic_memo' |
| **Reactor** | `signals` (UPDATE), `alerts`, `thesis_jobs`, `failed_reactor_events` | Shared DLQ across reactor + Cowork preflight |
| **Calibration** | `post_mortem_queue`, `eval_harness`, `eval_runs`, `calibration_curves`, `reference_class_base_rates` | D-103 gate; D-104 hourly rollback |
| **v2 legacy** | `candidates`, `fda_event_features` | Phasing out per v2 teardown |

---

## 9. v2 vs v3 — Side-by-Side

| Dimension | v2 | v3 |
|-----------|----|----|
| Scope | 19 scanners, 6 profiles | FDA-only + EDGAR pairing |
| Pipeline | Flat reactor + convergence bonus | 10-stage orchestrator + N=7 ensemble + isotonic calibration |
| Conviction | Categorical bands (Immediate/Watchlist/Archive) | Probabilistic `conviction_pct` ∈ [0,100], calibrated nightly |
| Sub-agents | None | Literature, Competitive, Regulatory, Options Microstructure, IC Memo |
| Cost control | Unbudgeted | $15 Tier-1 / $1.50 Tier-2 hard kill |
| Dispatch | Modal @modal.Cron (5-slot limit) | pg_cron → compute_v3 multiplex (unlimited) |
| Sources | All scanners | documents → asset_documents → orchestrator |
| Output | signals → alerts/thesis_jobs | convergence_assessments → ic_memo → operator-promoted signals |

Both coexist; v2 teardown is phased (Phase 1 landed PR #30; Phases 2–4 deferred).

---

## Legend

- 🤖 = LLM/AI invocation
- 💰 = cost-gated step
- `(table)` = Supabase Postgres table
- `🟦 dashed arrow` = trigger/event (not data movement)
- pg_cron times are UTC
