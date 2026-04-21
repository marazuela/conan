# Unified Investment Research System v2 — Spec (Phase 0)

**Status:** Phase 0 deliverable for Pedro's approval
**Date:** 2026-04-20
**Scope:** substrate migration from JSON-file bus + local Python scripts to Supabase + Modal + Vercel + Resend, preserving scanner logic, rubrics, and convergence semantics verbatim.

---

## 1. Context & scope

v2 is the production upgrade of the existing Unified Investment Research System. The current v1 runs 17 Python scanners, a file-coupled post-scan scorer, a JSON-log convergence engine, and a reportlab PDF generator — all on Pedro's laptop on 3h/4h cron. v2 keeps the four-layer separation (discovery → scoring → convergence → reporting) and replaces the substrate:

- **State:** `signals/signal_log.json` and `candidates/*.md` → Supabase Postgres + Storage.
- **Bus:** file globs + cron order → database webhooks on `signals` / `alerts` / `filings`.
- **Workers:** subprocess-on-laptop → Modal scheduled functions.
- **Alerts:** "wait for next 3h PDF" → Resend email only after AI-reviewed pre-edge promotion (spec §7.4 `thesis_writer` skill passes the gate and the candidate row is created). Raw `alerts` INSERTs do NOT fire email; they are audit + Realtime-broadcast only. The end-to-end SLA now measures signal → pre-edge promotion email (dominated by the 15-min `thesis_writer` cron interval); p95 ≤ 20 min. Directive locked 2026-04-20 (memory: `email_alert_gating.md`).
- **UI:** nothing (read PDFs) → Next.js on Vercel (signal stream, convergence view, candidate review queue, scanner health).
- **Candidate authoring:** human-drafted `candidates/*.md` + ad-hoc Claude sessions → `thesis_writer` Cowork skill running under Pedro's Claude plan (§7.4). Users review and approve, never draft. Applies to Immediate-band signals only.

This spec defines the column-level schema, edge function contracts, Modal signatures, event flow, migration plan, test strategy, and Phase 1 task list with acceptance criteria. Pedro's approval unblocks Phase 1 foundation work.

**Reading map.** Pedro reads end-to-end. Collaborators onboarding a narrow concern should read §2 (what's preserved), §8 (event flow diagram), and §11 (Phase 1 tasks). Anyone touching the database reads §3 + Appendix A. Anyone touching a scanner reads §7.

**Out of scope for v2** (PRD §3 + §14): rubric retuning, specialist analyst agents (medical-paper reviewer, financial-ratio agent), reportlab replacement, new primary sources, market-cap floor changes, back-testing, news aggregation, DECISIONS.md migration to DB, mobile push, Slack/Teams, multi-tenancy beyond the single shared workspace. `extensions` JSONB columns and pgvector are enabled from day one so future agents can graft on without schema changes. **Exception added 2026-04-20:** thesis authoring via Claude app routine is in scope (§7.4, §12) — framed as a pipeline stage, not a specialist analyst agent. PRD §3 / §14 need a one-line amendment tracking this.

---

## 2. Preserved artifacts (PRD §6 coverage)

Every v1 artifact that v2 must preserve byte-for-byte or behavior-for-behavior, mapped to its v2 destination.

| v1 artifact | v2 destination | Mode | Deviation |
|---|---|---|---|
| `tools/openfigi_resolver.py::normalize_ticker` (JP 5-char fix) | `modal_workers/shared/openfigi_resolver.py` — same module imported by every scanner | Verbatim port. Storage-backed cache replaces local `working/openfigi_cache/`. | None |
| `tools/openfigi_resolver.py` (full module) | `modal_workers/shared/openfigi_resolver.py` | Verbatim port; same public functions `resolve_ticker`, `resolve_ticker_mic`, `resolve_isin`, `resolve_batch`, `normalize_ticker`. | Cache path configurable via env; rate-limiter unchanged. |
| 17 scanner implementations in `tools/*_scanner.py` + `edgar_filing_monitor.py` + `fda_pdufa_pipeline.py` + `congressional_trading.py` + `takeover_candidate_scanner.py` + `pre_phase3_readout_scanner.py` | `modal_workers/scanners/<name>.py` — one Modal function per scanner | Internal scan logic unchanged. Only IO rewired: config from `scanners` table, filings to Storage + `filings` table, signals to `signals` table. | All scanners lose their `signals/<name>_scanner_output.json` write path; gain a `scanner_base.persist(signals)` helper call. |
| `tools/candidate_gate.py::promote_candidate` | `modal_workers/shared/candidate_gate.py` — invoked by the `thesis_writer` Cowork skill (§7.4) via Bash (`python3 -c 'from modal_workers.shared.candidate_gate import assess_thesis_v2; …'`). | Python validation + markdown render ported; **schema extended v1→v2**: gate required 5 fields (situation, why_underpriced, next_catalyst, next_catalyst_date, kill_conditions); v2 adds **steelman** (min 120 chars + boilerplate regex), **web_research** (≥3 entries with `{url, retrieved_at, finding, lean}`, ≥1 non-strengthening lean), and **reasoning_tag coverage** (every load-bearing claim tagged `[verified]`/`[inferred]`/`[speculated]` per INSTRUCTIONS.md "Prime Directive"; ≥5 tags, ≥1 verified anchor, ≤2 untagged load-bearing claims tolerated). No edge function proxy and no dashboard-facing authoring endpoint. `assess_thesis` v1 retained for historical dossier import (§9.3). | Output routing: skill writes `candidates` row + dossier markdown into `candidates.dossier_markdown` + structured `kill_conditions` into the JSONB column (§3.4); canonical markdown to Storage. Caller is the thesis_writer skill running under Pedro's Claude plan, never a user. The v2 gate expansion is load-bearing because it closes the "correct-prose, no-asymmetry" failure mode documented in `candidates/rejected_pending_thesis/` (ITRK archetype). |
| `tools/run_post_scan.py::WEIGHTS` (6 profiles) | `rubrics` table, seeded as `rubric_version=1` for each profile | Verbatim dict. | **Note:** PRD §2 mentions "5 profile-specific rubrics"; live code has 6 (`merger_arb`, `activist_governance`, `binary_catalyst`, `short_positioning`, `litigation`, `takeover_candidate`). Spec seeds 6. See §12. |
| `tools/run_post_scan.py::apply_auto_caps` | `modal_workers/shared/rubric_engine.py::apply_auto_caps` | Verbatim Python. Each cap returns a stable `rule_id` string that is written to `signals.auto_caps_triggered` (text[]). | None |
| `tools/run_post_scan.py::classify_band` + thresholds 35/25/15 | `modal_workers/shared/rubric_engine.py::classify_band` | Live Conan v2 authoritative contract. | **Important:** the separate `Scoring engine/` folder contains a later D-034 experiment that shifted a legacy file-bus copy to 30/20/10; v2 deliberately does **not** adopt that shift unless a future rubric version explicitly does so. |
| `config/scanner_registry.json` (17 scanners) | `scanners` table | Seeded once; edits live in Supabase Studio thereafter. | Registry schema becomes the table DDL; no back-port. |
| `config/pe_filer_allowlist.json` (45 rows: 39 PE + 6 activist-crossover) | `pe_filer_allowlist` table | Seeded once. | None |
| `config/phase3_approval_base_rates.json` (39 indications) | `phase3_base_rates` table | Seeded once. | None |
| `candidates/_curated_rationales.json` (schema v2 per D-010/D-011) | `candidate_rationales` table | Seeded once; dashboard edits thereafter. | `_archived` sub-block flattens to `archived` bool + `archived_meta` JSONB. |
| `framework/candidate_template.md` (11-section dossier) | `modal_workers/services/candidate_gate_service.py` renderer | Markdown render stays identical; only emission target changes (DB+Storage instead of filesystem). | None |
| Atomic-write pattern (D-052) | Obsolete for Postgres rows (ACID). Retained for openfigi_cache under bridge mode. | Bridge mode uses `tmp + fsync + rename`; Modal workers use Storage PUT. | None |
| EDGAR 35s wall-clock budget (D-018) | Modal function `timeout=35` (soft) + 120 (hard) per EDGAR, takeover_candidate, sec_enforcement scanners | Passed through. | **Verify under EU-West Modal region** (§12). If P1 smoke test runs hot due to transatlantic latency, bump `timeout_soft_s` in the `scanners` row — not a code change. |
| Scanner 120s hard-kill (D-014) | Modal function `timeout=` (hard) per scanner | Passed through. | None |
| Post-edge disqualifier (D-013) — `takeover_candidate.post_edge_disqualified` cap | `rubric_engine.apply_auto_caps` — band returns to `discard`; signal row still written for audit but never alertable. | Verbatim. | None |
| Scanner persistent state (EDGAR dedup + rotation, ESMA snapshots, LSE pre-warmed cache, ASX rotation, openfigi_cache) | Supabase Storage `scanner-caches/` bucket with per-scanner prefixes | Same read/write semantics, Storage-backed. | Pre-warmed LSE cache gets refreshed by a dedicated Modal function on same cadence as v1 maintenance task. |
| 17 auth-required graceful envelopes | Modal `scanner_base.run_scanner` detects missing secrets and returns `ScannerRun(status='auth_required', ...)` without raising | Same envelope as v1: `{scanner, status, signals:[], warnings:[...]}`. | None |

---

## 3. Data model

All tables in the `public` schema unless stated. Every table has `created_at timestamptz NOT NULL DEFAULT now()` (omitted below for brevity). Tables that support mutation additionally have `updated_at timestamptz NOT NULL DEFAULT now()` maintained by a shared `set_updated_at()` trigger. RLS is ON for every table; policies stated per-table. Complete DDL is in Appendix A.

### 3.1 Registry tables

**`sources`** — one row per primary-source feed (EDGAR, ESMA-FCA, TDnet, …). Lightweight lookup; scanners reference it for audit, not runtime config.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | `gen_random_uuid()` |
| `name` | `text` NOT NULL UNIQUE | e.g., `edgar`, `fca_short_disclosure` |
| `kind` | `text` NOT NULL | CHECK ∈ {edgar, esma, fda, lse, tdnet, asx, sedar, hkex, kind, bse_nse, cvm, bmv, courtlistener, sec_enforcement, clinicaltrials} |
| `base_url` | `text` | |
| `notes` | `text` | |

RLS: SELECT to `authenticated`; INSERT/UPDATE/DELETE to `service_role` only.

**`scanners`** — the runtime registry, replacing `scanner_registry.json`.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `name` | `text` NOT NULL UNIQUE | matches `scanner_registry.json::name` |
| `tool_path` | `text` | historical pointer to v1 source |
| `status` | `text` NOT NULL | CHECK ∈ {operational, planned, deprecated, experimental} |
| `geography` | `text` | US/EU/UK/JP/AU/CA/HK/KR/IN/BR/MX |
| `cadence` | `text` NOT NULL | CHECK ∈ {3h, daily, weekly, on_demand} |
| `default_scoring_profile` | `text` NOT NULL | FK-logical to `rubrics.profile` |
| `signal_type_profile_map` | `jsonb` NOT NULL DEFAULT `'{}'` | maps signal_type → profile |
| `endpoints` | `jsonb` NOT NULL DEFAULT `'{}'` | `{primary, secondary, fallback, note}` |
| `timeout_soft_s` | `int` NOT NULL DEFAULT 60 | |
| `timeout_hard_s` | `int` NOT NULL DEFAULT 120 | |
| `config` | `jsonb` NOT NULL DEFAULT `'{}'` | scanner-specific (excluded filers, window sizes, strategy_spec, notes) |
| `last_run_utc` | `timestamptz` | last successful Modal invocation |
| `last_run_status` | `text` | ok/error/auth_required/timeout/partial |
| `last_run_signals` | `int` | |

Indexes: `scanners_status_idx (status)`. RLS: SELECT to `authenticated`; UPDATE of `last_run_*` to `service_role` (Modal); other writes to `service_role`.

**`rubrics`** — versioned scoring profiles. Seed once with `rubric_version=1` from `WEIGHTS`; never mutate an existing row.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `profile` | `text` NOT NULL | ∈ {merger_arb, activist_governance, binary_catalyst, short_positioning, litigation, takeover_candidate} |
| `rubric_version` | `int` NOT NULL | starts at 1; increments on rubric change |
| `dimension_weights` | `jsonb` NOT NULL | e.g., `{"spread_size":3.0,"deal_certainty":2.5,...}` |
| `effective_at` | `timestamptz` NOT NULL DEFAULT `now()` | |
| `superseded_at` | `timestamptz` | NULL while active |
| `notes` | `text` | |

Unique: `(profile, rubric_version)`. Partial index `rubrics_active_idx (profile) WHERE superseded_at IS NULL` for fast lookups.
RLS: SELECT to `authenticated`; INSERT/UPDATE to `service_role`.

**`pe_filer_allowlist`** — per D-013 / takeover_candidate scanner.

| Column | Type | Notes |
|---|---|---|
| `filer_name` | `text` PK | e.g., `Silver Lake Partners` |
| `cik` | `text` | nullable |
| `filer_type` | `text` NOT NULL | CHECK ∈ {pe, activist_crossover} |
| `notes` | `text` | |

RLS: SELECT to `authenticated`; writes to `service_role`.

**`phase3_base_rates`** — per pre_phase3_readout scanner.

| Column | Type | Notes |
|---|---|---|
| `indication` | `text` PK | e.g., `oncology_solid_tumor` |
| `phase3_to_approval` | `numeric(4,3)` NOT NULL | CHECK ∈ [0,1] |
| `trial_design_adjustments` | `jsonb` NOT NULL DEFAULT `'{}'` | accelerated/non_inferiority/etc. |
| `notes` | `text` | |

### 3.2 Entity graph

**`entities`** — one row per tradeable issuer, keyed by FIGI.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `issuer_figi` | `text` UNIQUE | nullable when FIGI fails; populated by resolver later |
| `name` | `text` NOT NULL | |
| `primary_ticker` | `text` | |
| `primary_mic` | `text` | |
| `country` | `text` | ISO 3166-1 alpha-2 |
| `market_cap_usd` | `numeric(18,2)` | |
| `market_cap_as_of` | `date` | |
| `extensions` | `jsonb` NOT NULL DEFAULT `'{}'` | agent enrichment hooks |

Indexes: `entities_ticker_mic_idx (primary_ticker, primary_mic)`. RLS: SELECT `authenticated`; writes `service_role`.

**`entity_identifiers`** — prioritized fallback chain for entity resolution, implementing the v1 cascade (FIGI → ticker+MIC → codigo_cvm → id_empresa_biva → stock_code → CIK → CNPJ → ISIN → normalized name).

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `entity_id` | `uuid` NOT NULL FK → `entities(id)` ON DELETE CASCADE | |
| `id_type` | `text` NOT NULL | CHECK ∈ {ticker_mic, codigo_cvm, id_empresa_biva, stock_code, cik, cnpj, isin, name_normalized} |
| `id_value` | `text` NOT NULL | |
| `priority` | `smallint` NOT NULL DEFAULT 100 | lower = higher priority; FIGI=10, ticker+MIC=20, etc. |

Unique: `(id_type, id_value)`. Index: `entity_identifiers_entity_idx (entity_id)`. RLS: SELECT `authenticated`; writes `service_role`.

### 3.3 Raw evidence

**`filings`** — every filing a scanner ingests, content-hash-addressed. Raw bytes live in the `filings/` Storage bucket.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `source_id` | `uuid` NOT NULL FK → `sources(id)` | |
| `entity_id` | `uuid` FK → `entities(id)` | nullable until resolved |
| `source_content_hash` | `text` NOT NULL UNIQUE | SHA-256 of raw body |
| `storage_path` | `text` NOT NULL | `filings/<source>/<yyyy>/<mm>/<hash>` |
| `url` | `text` | |
| `fetched_at` | `timestamptz` NOT NULL DEFAULT `now()` | |
| `published_at` | `timestamptz` | |
| `filing_type` | `text` | e.g., `8-K`, `13D/A`, `PDUFA_AD` |
| `extensions` | `jsonb` NOT NULL DEFAULT `'{}'` | additive agent / deterministic enrichments only (`resolver_reasoning`, `legal_enrichment`, `biotech_enrichment`, etc.) |

Indexes: `filings_entity_published_idx (entity_id, published_at DESC)`; existing UNIQUE covers `source_content_hash`. RLS: SELECT `authenticated`; writes `service_role`.

### 3.4 Pipeline state

**`scanner_runs`** — audit of every Modal invocation.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `scanner_id` | `uuid` NOT NULL FK → `scanners(id)` | |
| `started_at` | `timestamptz` NOT NULL DEFAULT `now()` | |
| `completed_at` | `timestamptz` | |
| `status` | `text` NOT NULL | CHECK ∈ {running, ok, error, auth_required, partial, timeout} |
| `signals_emitted` | `int` NOT NULL DEFAULT 0 | |
| `errors` | `jsonb` NOT NULL DEFAULT `'[]'` | |
| `modal_invocation_id` | `text` | |
| `raw_log_path` | `text` | Storage path to scanner stdout/stderr capture |

Index: `scanner_runs_scanner_started_idx (scanner_id, started_at DESC)`. RLS: SELECT `authenticated`; writes `service_role`.

**`signals`** — the central event table. This table has the `signals_insert` database webhook pointing at the reactor.

| Column | Type | Notes |
|---|---|---|
| `signal_id` | `text` PK | scanner-assigned; globally unique |
| `entity_id` | `uuid` FK → `entities(id)` | nullable when resolution fails |
| `issuer_figi` | `text` | denormalized for fast convergence queries |
| `scanner_id` | `uuid` FK → `scanners(id)` | |
| `scanner_run_id` | `uuid` FK → `scanner_runs(id)` | |
| `scoring_profile` | `text` NOT NULL | |
| `rubric_version_id` | `uuid` NOT NULL FK → `rubrics(id)` | |
| `source_content_hash` | `text` NOT NULL | |
| `source_url` | `text` | |
| `source_date` | `timestamptz` NOT NULL | when the event happened |
| `scan_date` | `timestamptz` NOT NULL | when scanner ran |
| `signal_type` | `text` NOT NULL | e.g., `activist_13d`, `pdufa_approaching` |
| `thesis_direction` | `text` | CHECK ∈ {long, short, neutral} |
| `strength_estimate` | `smallint` | CHECK ∈ [1,5] |
| `imported` | `boolean` NOT NULL DEFAULT `false` | TRUE for historical-migrated rows |
| `dimensions` | `jsonb` NOT NULL DEFAULT `'{}'` | per-profile 1–5 scores |
| `score` | `numeric(5,2)` NOT NULL | pre-convergence |
| `band` | `signal_band` NOT NULL | pre-convergence; ENUM {immediate, watchlist, archive, discard} |
| `auto_caps_triggered` | `text[]` NOT NULL DEFAULT `'{}'` | `rule_id`s |
| `convergence_key` | `text` | populated by reactor |
| `convergence_bonus` | `smallint` NOT NULL DEFAULT 0 | 0/5/10 |
| `score_with_bonus` | `numeric(5,2)` | |
| `band_with_bonus` | `signal_band` | final band used for alerting |
| `convergence_evaluated_at` | `timestamptz` | reactor stamp |
| `raw_payload` | `jsonb` NOT NULL DEFAULT `'{}'` | opaque scanner blob |
| `extensions` | `jsonb` NOT NULL DEFAULT `'{}'` | |

Constraints: `UNIQUE (source_content_hash, scoring_profile)` — matches v1 dedup rule.
Indexes:
- `signals_entity_scan_idx (entity_id, scan_date DESC)`
- `signals_issuer_figi_scan_idx (issuer_figi, scan_date DESC)` — reactor hot path
- `signals_convergence_key_idx (convergence_key, scan_date DESC)`
- `signals_immediate_idx (scan_date DESC) WHERE band_with_bonus = 'immediate'`

Trigger/webhook: `signals_insert_wh` on `AFTER INSERT` → reactor edge function.
RLS: SELECT `authenticated`; INSERT `service_role` (Modal scanners, bridge mode); UPDATE of `convergence_*` and `convergence_evaluated_at` only by `service_role` (reactor); no DELETE.

**`candidates`** — promoted names with dossiers.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `ticker` | `text` NOT NULL | |
| `mic` | `text` | |
| `entity_id` | `uuid` FK → `entities(id)` | |
| `state` | `candidate_state` NOT NULL DEFAULT `'watch'` | ENUM {watch, active, killed, delivered} |
| `scoring_profile` | `text` | |
| `current_score` | `numeric(5,2)` | |
| `current_band` | `signal_band` | |
| `dossier_markdown` | `text` | full rendered dossier; convenience |
| `dossier_storage_path` | `text` | canonical copy in Storage |
| `thesis_approved_at` | `timestamptz` | when gate last accepted a thesis |
| `kill_conditions` | `jsonb` NOT NULL DEFAULT `'[]'` | **structured** kill conditions (see schema below) — the aging evaluator (§7.5) reads this, not the dossier prose. Authored by `thesis_writer` routine. |
| `next_catalyst_date` | `date` | parsed from thesis; used by aging sweep to detect elapsed catalyst windows. NULL when the thesis uses a band (`Q2 2026`) — in that case `next_catalyst_window` holds the range. |
| `next_catalyst_window` | `daterange` | ISO band ranges (e.g., `[2026-04-01, 2026-06-30]` for "Q2 2026"). Exactly one of `next_catalyst_date` / `next_catalyst_window` must be non-null (CHECK). |
| `last_aging_evaluated_at` | `timestamptz` | stamp from the most recent `candidate_aging` run. Drives dashboard "last check" column and operator visibility. |
| `extensions` | `jsonb` NOT NULL DEFAULT `'{}'` | |

Unique: `(ticker, mic)`. RLS: SELECT `authenticated`; INSERT/UPDATE `service_role` (via `thesis_writer` + `candidate_aging`).

**`candidates.kill_conditions` shape** (array of objects):

```json
[
  {
    "id": "kill_1",
    "description": "Board of directors formally rejects the offer",
    "observable": {
      "source_type": "filing",
      "filing_type": "SC 14D9",
      "search_pattern": "(?i)\\b(reject|oppose|inadequate)\\b",
      "url_pattern_hint": "sec.gov/.../sc14d9"
    },
    "date_bound": "2026-06-30",
    "status": "pending"
  }
]
```

Each entry requires `id` (stable within candidate), `description` (the human-readable kill prose), `observable` (structured-enough for `candidate_aging` to mechanically check against new signals in the 14/30d window for the entity), optional `date_bound`, and `status` ∈ {pending, triggered, cleared}. `source_type` ∈ {filing, price, news, regulator, clinical_readout}. Authored by the `thesis_writer` routine on promotion; updated by `candidate_aging` on evaluation.

**`candidate_events`** — append-only log.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `candidate_id` | `uuid` NOT NULL FK → `candidates(id)` ON DELETE CASCADE | |
| `event_type` | `text` NOT NULL | CHECK ∈ {created, state_changed, scored, note_added, thesis_drafted_by_claude, thesis_updated, thesis_approved_by_user, convergence, gate_rejected} |
| `payload` | `jsonb` NOT NULL DEFAULT `'{}'` | |
| `user_id` | `uuid` FK → `auth.users(id)` | nullable (system events) |

Index: `candidate_events_candidate_idx (candidate_id, created_at DESC)`. RLS: SELECT `authenticated`; INSERT `service_role` or `authenticated` with `user_id = auth.uid()`; no UPDATE/DELETE.

**`outcomes`** — realized results per candidate (minimum viable for v2; rich analytics deferred).

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `candidate_id` | `uuid` NOT NULL FK → `candidates(id)` | |
| `outcome_type` | `text` NOT NULL | CHECK ∈ {delivered, killed, expired} |
| `realized_return` | `numeric(6,3)` | decimal (e.g., 0.25 = +25%) |
| `notes` | `text` | |

RLS: SELECT/INSERT `authenticated`; UPDATE `authenticated` restricted to the creator.

**`alerts`** — Immediate-band trigger log + dedup. The fan-out webhook fires on INSERT.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `entity_id` | `uuid` FK → `entities(id)` | |
| `signal_id` | `text` NOT NULL FK → `signals(signal_id)` | |
| `signal_fingerprint` | `text` NOT NULL | `sha256(source_content_hash‖scoring_profile)` |
| `day_utc` | `date` NOT NULL DEFAULT `(now() AT TIME ZONE 'UTC')::date` | |
| `email_subject` | `text` | |
| `email_body_storage_path` | `text` | |
| `dispatched_at` | `timestamptz` | set by fan-out |
| `dispatched_to` | `text[]` NOT NULL DEFAULT `'{}'` | |

Constraints: `UNIQUE (entity_id, signal_fingerprint, day_utc)` — prevents same-day duplicate alerts on the same fingerprint. Reactor does `INSERT … ON CONFLICT DO NOTHING`.
Trigger/webhook: **none on `alerts` today** — `alerts_insert_wh` was dropped in migration `22_email_gating_pre_edge_only` (2026-04-20). Fan-out now triggers on `candidate_events.INSERT` for the pre-edge-promotion email path (§6.2).
RLS: SELECT `authenticated`; writes `service_role`.

**`alert_deliveries`** — one row per outbound attempt; audit + retry surface.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `alert_id` | `uuid` NOT NULL FK → `alerts(id)` ON DELETE CASCADE | |
| `channel` | `text` NOT NULL | CHECK ∈ {email, realtime} |
| `target` | `text` NOT NULL | email address or realtime channel name |
| `status` | `text` NOT NULL | CHECK ∈ {queued, sent, failed, bounced} |
| `resend_message_id` | `text` | |
| `response_body` | `jsonb` | |
| `attempt_count` | `smallint` NOT NULL DEFAULT 1 | |

RLS: SELECT `authenticated`; writes `service_role`.

**`failed_reactor_events`** — DLQ for reactor invocations that exhaust retries.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `signal_id` | `text` | |
| `payload` | `jsonb` NOT NULL | verbatim webhook envelope |
| `error_message` | `text` NOT NULL | |
| `attempt_count` | `smallint` NOT NULL DEFAULT 1 | |
| `last_attempted_at` | `timestamptz` NOT NULL DEFAULT `now()` | |
| `resolved_at` | `timestamptz` | |

RLS: SELECT/INSERT/UPDATE `service_role` only.

**`thesis_jobs`** — work queue for the Claude thesis-writer pipeline (§7.4). Immediate-band signals enqueue one row each; the `thesis_writer` Cowork skill polls this queue every 15 min, drafts the thesis, runs the gate, and either promotes to `candidates` or DLQs to `thesis_drafting_failures`.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `signal_id` | `text` NOT NULL FK → `signals(signal_id)` | |
| `alert_id` | `uuid` FK → `alerts(id)` | set once the fan-out alert is created; null in the narrow race window between reactor stamping band=immediate and alert INSERT |
| `status` | `text` NOT NULL DEFAULT `'queued'` | CHECK ∈ {queued, drafting, gate_failed_retrying, promoted, dlq} |
| `attempt_count` | `smallint` NOT NULL DEFAULT 0 | |
| `routine_run_ids` | `text[]` NOT NULL DEFAULT `'{}'` | Claude app routine run IDs, one per attempt |
| `drafted_thesis` | `jsonb` | last draft produced by the routine (5-field dict) |
| `gate_reasons` | `text[]` | rejection reasons from last `assess_thesis` call; empty on promote |
| `candidate_id` | `uuid` FK → `candidates(id)` | set on promote |
| `started_at` | `timestamptz` | set when worker claims the row |
| `completed_at` | `timestamptz` | set on terminal state |

Constraint: `UNIQUE (signal_id)` — one job per Immediate signal. Dedup handled at enqueue time; idempotent re-enqueue is a no-op.
Index: `thesis_jobs_status_idx (status, created_at)` — worker poll path.
RLS: SELECT `authenticated`; INSERT/UPDATE `service_role` only.

**`thesis_drafting_failures`** — DLQ for thesis jobs that exhaust the retry budget (one corrective retry → fail).

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `thesis_job_id` | `uuid` NOT NULL FK → `thesis_jobs(id)` | |
| `signal_id` | `text` NOT NULL FK → `signals(signal_id)` | |
| `final_reasons` | `text[]` NOT NULL | gate reasons from the final failed attempt |
| `all_drafts` | `jsonb` NOT NULL | full array of every draft Claude produced, for audit |
| `alerted` | `boolean` NOT NULL DEFAULT `true` | true when the `alerts` audit row was successfully inserted for the original signal; false only if the alerts insert itself failed. Historical semantic — pre-2026-04-20 this column also indicated whether an email went out; under the new gating emails fire from `candidate_events`, not `alerts`. |
| `resolved_at` | `timestamptz` | set when a human authors a thesis manually and the signal re-enters the pipeline |
| `resolved_candidate_id` | `uuid` FK → `candidates(id)` | |

RLS: SELECT `authenticated`; INSERT/UPDATE `service_role` only. Dashboard renders these as a "needs manual thesis" banner on the review queue.

**`operator_flags`** — structured operator-attention surface (replaces v1's `OPEN_QUESTIONS.md`). Any scheduled function that detects drift, anomaly, or decision-needed state writes here. Dashboard renders as a sorted, resolvable issue list.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `severity` | `text` NOT NULL | CHECK ∈ {info, warn, critical} |
| `source` | `text` NOT NULL | producing function: `translation_health`, `scanner_probe`, `convergence_qa`, `candidate_aging`, `pre_edge_monitor`, `thesis_writer`, `reactor`, `reporting_weekly`, `litigation_baselines`, `manual` |
| `kind` | `text` NOT NULL | free-form taxonomy token: `translation_confidence_trend`, `endpoint_drift`, `convergence_disagreement`, `orphan_alert`, `hallucinated_trigger`, `baseline_stale`, etc. |
| `scanner_id` | `uuid` FK → `scanners(id)` | nullable |
| `entity_id` | `uuid` FK → `entities(id)` | nullable |
| `signal_id` | `text` FK → `signals(signal_id)` | nullable |
| `candidate_id` | `uuid` FK → `candidates(id)` | nullable |
| `title` | `text` NOT NULL | one-line human-readable headline |
| `body` | `text` | optional markdown-ish longer note, <2kB |
| `evidence` | `jsonb` NOT NULL DEFAULT `'{}'` | diagnostic payload (sample sizes, URLs, deltas, routine_run_id) |
| `resolved_at` | `timestamptz` | set by operator or by the producing function when the condition clears |
| `resolved_by` | `uuid` FK → `auth.users(id)` | nullable |
| `resolved_note` | `text` | |

Constraints: partial unique index on `(source, kind, coalesce(scanner_id::text,''), coalesce(entity_id::text,''))` WHERE `resolved_at IS NULL` — prevents duplicate open flags for the same (source, kind, subject) tuple; producers `INSERT … ON CONFLICT DO UPDATE` to bump `evidence`/`updated_at` instead of inserting a duplicate.
Index: `operator_flags_open_idx (severity DESC, created_at DESC) WHERE resolved_at IS NULL` — dashboard hot path.
RLS: SELECT `authenticated`; INSERT `service_role`; UPDATE (resolution) `authenticated` with `resolved_by = auth.uid()`.

### 3.5 Human layer

`users` has no physical table; the dashboard queries `auth.users` directly. Access control for human-specific tables uses `auth.uid() = user_id`.

**`watchlists`**, **`notifications_prefs`**, **`annotations`** — per-user state.

| Table | Key columns | RLS |
|---|---|---|
| `watchlists` | `id uuid PK`, `user_id uuid FK→auth.users`, `name text`, `filter jsonb` | SELECT/INSERT/UPDATE/DELETE `user_id = auth.uid()` |
| `notifications_prefs` | `user_id uuid PK→auth.users`, `email_on_immediate bool`, `email_weekly_report bool`, `realtime_channels text[]` | SELECT/INSERT/UPDATE `user_id = auth.uid()`; no DELETE |
| `annotations` | `id uuid PK`, `user_id uuid FK→auth.users`, `candidate_id uuid FK→candidates`, `body text` | strict per-user: SELECT/INSERT/UPDATE/DELETE `user_id = auth.uid()` (no cross-user read, per PRD §11) |

**`candidate_rationales`** — hand-curated executive-summary cards (schema v2 per D-010/D-011).

| Column | Type | Notes |
|---|---|---|
| `ticker` | `text` PK | uppercase |
| `one_liner` | `text` NOT NULL | per D-011, must end with "→ Potential outcomes: …" |
| `hypothesis` | `text` NOT NULL | one sentence |
| `thesis` | `text` NOT NULL | plain-English, terms-of-art defined inline |
| `expected_outcome` | `text` NOT NULL | probabilistic |
| `price_targets` | `jsonb` NOT NULL | `{reference_price, upside_base, upside_best, downside}` |
| `time_sensitivity` | `text` NOT NULL | prefix ∈ {VERY HIGH, HIGH, MEDIUM-HIGH, MEDIUM, LOW} |
| `kill_watch` | `text` NOT NULL | |
| `catalyst_date_iso` | `date` | forward-looking |
| `archived` | `boolean` NOT NULL DEFAULT false | |
| `archived_meta` | `jsonb` | `{archived_date, archive_reason, outcome, former_one_liner, lesson?}` |

RLS: SELECT `authenticated`; writes `service_role` (Modal gate + dashboard server action).

---

## 4. Storage layout

Three private buckets. All objects accessed via signed URLs where exposure outside the service layer is needed.

- **`filings/`** — raw filing bodies, content-hash-addressed. Key shape: `<source>/<yyyy>/<mm>/<source_content_hash>`. Lifecycle: retained indefinitely (primary-source provenance is load-bearing). Signed URL issued only for reporting PDFs that embed source quotes; default access is server-side via service role.
- **`scanner-caches/`** — per-scanner state that must survive between Modal invocations. Prefixes:
  - `openfigi/<cache_key>.json` — 7-day TTL, enforced by `openfigi_resolver`.
  - `edgar/dedup.json` + `edgar/rotation.json` — 45-day dedup window + rotation cursor.
  - `esma/snapshots/<regulator>/<yyyy-mm-dd>.json` — daily per-regulator snapshots.
  - `lse/alldata/<yyyymmdd>.json` — pre-warmed by weekly `lse_cache_refresh` Modal function.
  - `asx/rotation.json` — rotation cursor.
  - `tdnet/`, `hkex/`, `kind/`, `bmv/`, `cvm/`, `sedar/`, `bse_nse/`, `courtlistener/`, `sec_enforcement/`, `takeover_candidate/`, `pre_phase3_readout/` — reserved prefixes; most scanners need none.
- **`reports/`** — generated PDFs. Key shape: `<yyyy>/<mm>/<yyyy-mm-dd>_executive_summary.pdf` and `<yyyy>/<mm>/<yyyy-mm-dd>/dossiers/<TICKER>.pdf`. Emails embed signed URLs with 7-day expiry.

Bucket RLS: SELECT via signed URL only (no anonymous reads); INSERT/UPDATE/DELETE by `service_role` only.

---

## 5. Extensions and setup

- **`pgvector`** enabled from day one (PRD §5) — no agent tables yet, but embeddings on `filings.extensions.embedding` allowed when agents arrive.
- **`pgcrypto`** for `gen_random_uuid()` and hash helpers.
- **`pg_cron`** — not enabled in v2; all scheduling is Modal. Reconsider if `failed_reactor_events` needs a cleanup sweep.
- **Supabase Auth** — magic-link only; invite via Supabase Studio adds to `auth.users` (no open signup).
- **Realtime** — enabled on `signals`, `alerts`, `candidates` for dashboard push.
- **Database webhooks** — two live triggers post-2026-04-20 email-gating migration: `signals_insert_wh` on `signals` → reactor, `candidate_events_fanout_wh` on `candidate_events` → fan-out. Both use the Supabase webhook HMAC secret stored in the function's env. The pre-directive `alerts_insert_wh` was dropped in migration `22_email_gating_pre_edge_only`.

---

## 6. Edge function contracts

All edge functions are Deno + TypeScript. Source lives in `supabase/functions/<name>/index.ts`. Deployed via `supabase functions deploy`. Every function validates its webhook secret header (`x-supabase-webhook-secret`) before processing.

### 6.1 Reactor — `/functions/v1/reactor`

**Trigger:** database webhook on `signals INSERT`.
**Purpose:** on every new signal, compute convergence scoped to the signal's entity window, apply auto-caps, classify band, optionally insert an alert, cross-update any prior group-winner whose status changed.

Request envelope (Supabase database-webhook payload):

```json
{
  "type": "INSERT",
  "table": "signals",
  "schema": "public",
  "record": {
    "signal_id": "edgar_8k_20260420_RPAY_001",
    "entity_id": "…uuid…",
    "issuer_figi": "BBG000B9XRY4",
    "scoring_profile": "activist_governance",
    "rubric_version_id": "…uuid…",
    "source_content_hash": "sha256:…",
    "source_date": "2026-04-20T14:03:00Z",
    "scan_date": "2026-04-20T14:05:12Z",
    "thesis_direction": "long",
    "dimensions": {"signal_strength": 4, "…": "…"},
    "score": 30.0,
    "band": "watchlist",
    "auto_caps_triggered": []
  },
  "old_record": null
}
```

Response:

```json
{
  "processed": true,
  "convergence_key": "figi:BBG000B9XRY4",
  "convergence_bonus": 5,
  "score_with_bonus": 35.0,
  "band_with_bonus": "immediate",
  "alert_inserted": true,
  "thesis_job_enqueued": true,
  "cross_updates": ["edgar_13d_20260416_RPAY_012"]
}
```

**Algorithm** (preserves `convergence_engine.py:44-202`):

1. Resolve `convergence_key`: if `record.issuer_figi` present, key = `figi:<figi>`. Else fall back to `ticker_mic`/`codigo_cvm`/`id_empresa_biva`/`stock_code`/`name_normalized` in that order via a SQL lookup against `entity_identifiers`. If no resolution, key = `unidentified:<signal_id>` (never groups with other signals).
2. Compute the window: 14 days standard, 30 days if any signal in the candidate group has `scoring_profile='litigation'`.
3. Query the group: `SELECT signal_id, signal_type, scoring_profile, thesis_direction, score, source_content_hash FROM signals WHERE convergence_key = $1 AND scan_date >= now() - interval '14 days'` (or 30d). Postgres query is sub-10ms with the `signals_issuer_figi_scan_idx` + `signals_convergence_key_idx`.
4. De-dup by `source_content_hash` (keep first occurrence). This collapses cross-listing echoes.
5. Classify: if directions include both `long` and `short`, type = `contradiction`, bonus = 0. Else 2 unique signals, same direction → +5; 3+ → +10. Different profiles (e.g., `activist_governance` + `short_positioning`) + same direction → `orthogonal`, same bonus scale.
6. Select the group winner: signal with highest `score`. If the new INSERT is the winner, stamp its row with `convergence_key`, `convergence_bonus`, `score_with_bonus = score + bonus`, `band_with_bonus = classify_band(score + bonus)`, `convergence_evaluated_at = now()`. If a prior row was the winner and is no longer, UPDATE that prior row to `convergence_bonus=0`, `score_with_bonus=NULL`, `band_with_bonus=NULL`.
7. Apply auto-caps via RPC to the Modal `rubric_engine.apply_auto_caps` (wrapper function documented in §7.1). Update `band_with_bonus` accordingly.
8. If `band_with_bonus = 'immediate'`:
   a. `INSERT INTO alerts (entity_id, signal_id, signal_fingerprint, day_utc) VALUES (…) ON CONFLICT DO NOTHING`. The ON CONFLICT catches same-day redundant alerts.
   b. `INSERT INTO thesis_jobs (signal_id) VALUES (…) ON CONFLICT (signal_id) DO NOTHING`. Enqueues the Claude thesis draft (§7.4). Independent of the alert path — alert fans out regardless of thesis job state.
9. Return response envelope. On any exception: insert into `failed_reactor_events` with payload + error; Supabase will replay up to 3× with backoff (60s / 300s / 900s), after which the row stays in DLQ for manual review.

**Idempotency:** keyed on `signal_id`. Repeat invocations re-compute deterministically; UPDATE is safe; `alerts` INSERT has unique constraint.

**Complexity:** O(n) in the window group size (typically ≤20 per entity over 14 days, ≤50 over 30 days). Reactor p95 target: 1.5s.

**Post-edge defense (three-layer, added 2026-04-20):** false-positive merger-clause signals (e.g., "board representation" in a merger-announcement 8-K — the QXO-TopBuild 2026-04-18 incident) are caught by three independent layers, not by extending one cap across all profiles:

1. **Scanner-level** (edgar only) — `edgar_filing_monitor._has_merger_sibling` suppresses an activist-category keyword hit on 8-K when the same CIK has a sibling 425 / PREM14A / SC TO-T filing within ±3 days. Scoped narrowly because SC 13D / PRER14A / DFAN14A / SC 14D9 activist hits remain unambiguous. Feature-flagged via `scanners.config.activist_merger_sibling_suppression` (default `true`).
2. **Profile-level** — `rubric_engine.apply_auto_caps` runs `takeover_candidate.post_edge_disqualified` → `band='discard'` when `raw_data.definitive_merger_agreement is True`. This is profile-scoped by design; activist_governance, mna, and other profiles legitimately overlap with merger contexts (target 8-K reactions, tender-offer positioning) and should not be blanket-capped. Extending the cap to all profiles would drop legitimate merger-arb and activist signals from takeover targets.
3. **Thesis-level** — `thesis_writer` treats post-edge reasoning as a terminal DLQ state (`thesis_drafting_failures.final_reasons` contains `'post_edge'`). This is the backstop for signals that pass both scanner and profile layers.

**Escalation rule:** if `thesis_drafting_failures` accumulates >3 rows with `final_reasons LIKE '%post_edge%'` in any 7-day window post-Phase-3, open an `operator_flags` row and consider either (a) widening the scanner-level check (additional forms, longer window) or (b) adding a new profile-level cap for the dominant false-positive pattern.

### 6.2 Fan-out — `/functions/v1/fanout`

**Email gating (locked 2026-04-20, memory `email_alert_gating.md`).** Email fires ONLY after AI review + pre-edge promotion. `alerts INSERT` no longer triggers email. The live wiring is:

- `signals INSERT` → `signals_insert_wh` → `/functions/v1/reactor` (§6.1). Reactor classifies band and inserts `alerts` row for audit.
- `alerts INSERT` → `alerts_insert_wh` → `/functions/v1/fanout` — **audit + Realtime only, no email.** Preserves dashboard Realtime feed + persists rendered body to Storage (`reports/alerts/…`) for dashboard server-render; `dispatched_to` closes as empty.
- `thesis_writer` skill (§7.4) polls `thesis_jobs` every 10 min (Cowork cron). On successful promotion it upserts `candidates` + inserts `candidate_events(event_type='created')`.
- `candidate_events INSERT` → `candidate_events_fanout_wh` → `/functions/v1/fanout`. Fanout filters on `event_type`: `{created, thesis_drafted_by_claude}` → pre-edge promotion email (Appendix D); `state_changed` with `payload.to ∈ {killed, delivered}` → feature-flagged transition email (default OFF per 2026-04-20 Q3 answer).

**Three entry points the edge function handles:**

| Trigger | Purpose | Email? |
|---|---|---|
| `alerts.INSERT` (via `alerts_insert_wh`) | audit + Realtime broadcast only; populates `alerts.email_body_storage_path` for dashboard render; `dispatched_to` closes as `'{}'` | NO |
| `candidate_events.INSERT` where `event_type ∈ {'created','thesis_drafted_by_claude'}` | pre-edge promotion (AI-reviewed candidate just landed) | YES |
| `candidate_events.INSERT` where `event_type='state_changed' AND payload.to ∈ {killed, delivered}` | candidate-transition email | Feature-flagged; default OFF (env `EMAIL_STATE_CHANGE_KILLED_DELIVERED`, Pedro's Q3 answer 2026-04-20: "email only for pre edge") |

**Purpose:** deliver AI-reviewed pre-edge candidate promotions via Resend and Realtime; write one `alert_deliveries` audit row per target. Realtime broadcasts still fan out for every alerts INSERT (via the audit-only path retained in code) so the dashboard signal stream stays live even though no email fires there.

Request envelope:

```json
{
  "type": "INSERT",
  "table": "alerts",
  "schema": "public",
  "record": {
    "id": "…uuid…",
    "entity_id": "…uuid…",
    "signal_id": "edgar_8k_20260420_RPAY_001",
    "signal_fingerprint": "sha256:…",
    "day_utc": "2026-04-20",
    "email_subject": null,
    "email_body_storage_path": null
  },
  "old_record": null
}
```

Response:

```json
{
  "processed": true,
  "email_recipients": 2,
  "realtime_channels": ["signals", "alerts"],
  "resend_message_ids": ["msg_…", "msg_…"]
}
```

**Flow (pre-edge promotion path — candidate_events trigger):**

1. Parse envelope; load the `candidates` row (via `candidate_id`) + entity (name, ticker, mic) + originating signal (via the signal_id stashed in `candidate_events.payload.signal_id`). Pull the thesis dossier fields — `dossier_markdown`, `current_score`, `current_band`, `kill_conditions`, `next_catalyst_date` — for rendering.
2. Render the email — HTML + plain-text — from the Appendix D pre-edge-promotion template. Store rendered body at `reports/candidates/<yyyy>/<mm>/<candidate_id>_created.html`.
3. Query `notifications_prefs WHERE email_on_immediate = true`. Resolve email addresses via `auth.users` admin API. Fall back to `FAN_OUT_DEV_RECIPIENTS` env list if empty.
4. For each recipient: insert `alert_deliveries(alert_id=NULL, channel='email', target, status='queued')`; call Resend API; update to `sent` + `resend_message_id`, or `failed` with response body.
5. Broadcast to Realtime on `candidates` channel and the per-candidate `candidate:<id>` channel so the dashboard's review queue updates live.

**Flow (alerts audit-only path — `alerts_insert_wh` → fanout):**

1. Render the legacy Immediate-band email body (Appendix D) and write to `reports/alerts/<yyyy>/<mm>/<alert_id>.html` for dashboard preview. Set `alerts.email_body_storage_path` + `email_subject`.
2. Broadcast Realtime on `alerts` / `entity:<id>` channels. **No Resend call, no `alert_deliveries` rows.**
3. `UPDATE alerts SET dispatched_at = now(), dispatched_to = '{}'::text[]` so the audit trail closes the row. Response envelope includes `email_gate: "pre-edge-promotion-required"` to make the gating explicit in logs.

**Retry:** Supabase webhook retries 3× on 5xx. Inside the function, Resend 429/5xx retries once with 3s backoff; failures persist as `alert_deliveries.status='failed'` for operator review. Resend free-tier 5 req/sec rate limit is no longer a practical constraint post-gating (pre-edge volume is 2-5/week).

### 6.3 Candidate-gate — deleted

Earlier drafts included a `/functions/v1/candidate-gate` edge function acting as a dashboard-facing thesis-submission proxy. **That endpoint is removed.** Per the 2026-04-20 directive, thesis authoring is Claude's job on behalf of all users, not a dashboard form field. The candidate_gate Python logic is preserved in `modal_workers/shared/candidate_gate.py` and called by the `thesis_writer` Cowork skill (§7.4) via Bash, never by users. The dashboard's candidate surface is a review queue, not an authoring form. See also §12 decision row "thesis authoring."

---

## 7. Worker signatures (Modal functions + Cowork skills)

**Surface split.** Modal hosts everything that's mechanical and source-code-driven: the 17 scanners (§7.2), the `rubric_apply_caps` RPC endpoint the reactor calls (§7.1), the weekly reporting job (§7.3), and the four observability/maintenance sweeps (§7.6). Modal workspace name: `conan-v2`. Secrets: `scanner-secrets` (OPENFIGI_API_KEY, COURTLISTENER_TOKEN, OPENDART_KEY, SEC_USER_AGENT), `supabase-secrets` (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, WEBHOOK_SECRET).

**Cowork skills** host the two Claude-mediated workloads: `thesis_writer` (§7.4) and `candidate_aging` (§7.5). They run as Claude skills under Pedro's Claude plan via scheduled Cowork tasks — not as Modal functions. No Anthropic API key is managed for them. Skill files (`.claude/skills/thesis_writer.md`, `.claude/skills/candidate_aging.md`) are authoritative for behavior; this spec documents the contract.

### 7.1 Shared modules

**`modal_workers/shared/openfigi_resolver.py`** — verbatim port of `tools/openfigi_resolver.py`. Public interface:

```python
def normalize_ticker(ticker: str, mic: Optional[str] = None) -> str: ...
def resolve_ticker_mic(ticker: str, mic: str) -> FigiResolution: ...
def resolve_ticker(ticker: str, exch_code: Optional[str] = "US") -> FigiResolution: ...
def resolve_isin(isin: str) -> FigiResolution: ...
def resolve_batch(queries: list[dict]) -> list[FigiResolution]: ...

@dataclass
class FigiResolution:
    ticker_local: Optional[str]; mic: Optional[str]; figi: Optional[str]
    issuer_figi: Optional[str]; name: Optional[str]; security_type: Optional[str]
    exchange_code: Optional[str]; isin: Optional[str] = None
    resolved: bool = False; error: Optional[str] = None
```

Cache location: `scanner-caches/openfigi/`. CACHE_TTL_SECONDS = 7 * 24 * 3600, unchanged. Rate-limiter unchanged (25/min unauth, 250/min auth).

**`modal_workers/shared/supabase_client.py`** — one HTTP client backed by `httpx.AsyncClient` + `postgrest` Python client.

```python
class SupabaseClient:
    def load_scanner_config(self, scanner_name: str) -> ScannerConfig: ...
    def open_scanner_run(self, scanner_id: UUID) -> UUID: ...
    def close_scanner_run(self, run_id: UUID, status: str, signals_emitted: int, errors: list) -> None: ...
    def upsert_filing(self, filing: Filing) -> UUID: ...
    def insert_signals(self, signals: list[Signal]) -> list[str]: ...  # returns signal_ids
    def update_signal_convergence(self, signal_id: str, **fields) -> None: ...
    def read_cache(self, prefix: str, key: str) -> Optional[bytes]: ...
    def write_cache(self, prefix: str, key: str, data: bytes, ttl_seconds: int | None) -> None: ...
    def resolve_or_create_entity(self, hints: EntityHints) -> UUID: ...
```

**`modal_workers/shared/scanner_base.py`** — the contract every scanner conforms to.

```python
@dataclass
class Signal:
    signal_id: str
    issuer_figi: Optional[str]
    scoring_profile: str           # set by scanner_base from registry if None
    source_content_hash: str
    source_url: Optional[str]
    source_date: datetime
    scan_date: datetime
    signal_type: str
    thesis_direction: Optional[Literal["long", "short", "neutral"]]
    strength_estimate: int         # 1-5
    dimensions: dict[str, int]     # 1-5 per dim
    raw_payload: dict[str, Any]
    extensions: dict[str, Any] = field(default_factory=dict)

@dataclass
class ScannerResult:
    scanner: str
    status: Literal["ok", "error", "auth_required", "partial", "timeout"]
    signals: list[Signal]
    warnings: list[str] = field(default_factory=list)
    fetched_records: int | None = None
    error: str | None = None

def run_scanner(scanner_name: str, scan_fn: Callable[[ScannerConfig], ScannerResult]) -> ScannerResult:
    """Wraps scan_fn with: load config, open run row, call scan_fn inside soft timeout,
       compute dedup on (source_content_hash, scoring_profile), enrich with rubric_version_id
       from active `rubrics` row for the profile, call supabase_client.insert_signals,
       close run row, return ScannerResult. Raises only on unrecoverable infra errors;
       scan_fn-level errors go into ScannerResult.error with status='error'."""
```

**`modal_workers/shared/rubric_engine.py`** — verbatim port of `run_post_scan.WEIGHTS`, `weighted_total`, `classify_band`, `score_signal`, `apply_auto_caps`. Exposed as a Modal function `rubric_apply_caps` so the reactor edge function can RPC it:

```python
@app.function(image=image, timeout=10)
@modal.web_endpoint(method="POST", label="rubric-apply-caps")
def rubric_apply_caps(payload: ApplyCapsPayload) -> ApplyCapsResponse: ...
```

`ApplyCapsPayload = {signal: SignalRecord, dimensions: dict, profile: str, band: str}` → `ApplyCapsResponse = {band: str, auto_caps_triggered: list[str]}`.

**`modal_workers/services/candidate_gate_service.py`** — in-process Python library called by `thesis_writer` (§7.4). Not a web endpoint; no HTTP surface. Pure function:

```python
def promote(signal: SignalRecord, thesis: ThesisDict, *,
            band: str, scoring_profile: str) -> PromoteResult:
    # runs assess_thesis_v2 + _render_candidate_md_v2;
    # returns {status, rendered_markdown, structured_kill_conditions, reasons}
    ...
```

**v2 validation schema (`assess_thesis_v2`)** — extends v1's 5-field check with three additional required fields. Ports v1's char-count + boilerplate-regex + ISO-date-parser logic verbatim; adds:

| Field | Required | Min chars | Additional checks |
|---|---|---|---|
| `situation` | ✓ | 80 | v1 verbatim |
| `why_underpriced` | ✓ | 100 | v1 verbatim |
| `next_catalyst` | ✓ | 40 | v1 verbatim |
| `next_catalyst_date` | ✓ | — | v1 verbatim (ISO / Q-band / month-band) |
| `kill_conditions` | ✓ | 60 (prose) | **v1 verbatim for prose.** Additionally validates `structured_kill_conditions`: ≥3 entries; each has `id`, `description` (≥40 chars), `observable.source_type`, and either `observable.search_pattern` OR `observable.url_pattern_hint`. At least one entry must have a `date_bound`. |
| `steelman` | ✓ (new) | 120 | The opposing view, seriously. Deep-dives skill's mandate: "Never skip the steelman. If you can't write it, the thesis isn't real." Same boilerplate regex as other fields. |
| `web_research` | ✓ (new) | — | Array ≥3 entries; each `{url, retrieved_at (ISO-8601), finding (≥40 chars), lean ∈ {strengthening, weakening, neutral}}`. At least one entry with `lean != strengthening` (enforces the steelman-in-practice: if all research points one way, re-check the steelman). |
| `reasoning_tag_coverage` | ✓ (new) | — | Parser sweeps `situation` + `why_underpriced` + `steelman` for the three-tag vocabulary (`[verified]`, `[inferred]`, `[speculated]`). Requires ≥5 tags total across those fields, with ≥1 `[verified]` anchor. Untagged sentences that contain load-bearing claims (heuristic: sentences with numbers, proper nouns, or dates) count as violations; >2 violations fails. |

`_render_candidate_md_v2` renders two new top-level sections (`## Steelman`, `## Web research`) into the dossier markdown between "Next catalyst" and "Kill conditions". The first five sections remain byte-identical to v1 for continuity with imported dossiers.

`assess_thesis_v1` stays importable for the historical-dossier import path (§9.3) — legacy AXSM/RPAY/VRDN/VERA/RGR dossiers must continue to parse under the v1 rules.

### 7.2 Scanner functions (17)

Each scanner is its own Modal function. Shared `image` bundles all `modal_workers/` code. Cadences map:

| Scanner | Cadence | Modal schedule | Hard timeout | Secrets needed |
|---|---|---|---|---|
| edgar_filing_monitor | 3h | `Period(hours=3)` | 120 | SEC_USER_AGENT, OPENFIGI_API_KEY |
| fda_pdufa_pipeline | 3h | `Period(hours=3)` | 120 | OPENFIGI_API_KEY |
| lse_rns_scanner | 3h | `Period(hours=3)` | 120 | OPENFIGI_API_KEY |
| tdnet_scanner | 3h | `Period(hours=3)` | 120 | OPENFIGI_API_KEY |
| asx_scanner | 3h | `Period(hours=3)` | 120 | OPENFIGI_API_KEY |
| esma_short_scanner | daily | `Cron("5 9 * * *")` | 120 | OPENFIGI_API_KEY |
| congressional_trading | daily | `Cron("10 9 * * *")` | 120 | — |
| sedar_plus_scanner | daily | `Cron("15 9 * * *")` | 120 | OPENFIGI_API_KEY |
| hkex_scanner | daily | `Cron("20 9 * * *")` | 120 | OPENFIGI_API_KEY |
| kind_scanner | daily | `Cron("25 9 * * *")` | 120 | OPENDART_KEY (auth_required when missing) |
| bse_nse_scanner | daily | `Cron("30 9 * * *")` | 120 | OPENFIGI_API_KEY |
| cvm_scanner | daily | `Cron("35 9 * * *")` | 120 | — |
| bmv_scanner | daily | `Cron("40 9 * * *")` | 60 | — |
| courtlistener_scanner | daily | `Cron("45 9 * * *")` | 120 | COURTLISTENER_TOKEN (auth_required when missing) |
| sec_enforcement_scanner | daily | `Cron("50 9 * * *")` | 60 | SEC_USER_AGENT |
| takeover_candidate_scanner | weekly | `Cron("0 10 * * 1")` | 180 | SEC_USER_AGENT, OPENFIGI_API_KEY |
| pre_phase3_readout_scanner | weekly | `Cron("5 10 * * 1")` | 180 | OPENFIGI_API_KEY |

Skeleton for every scanner (full example for edgar in Appendix C):

```python
@app.function(
    image=image,
    schedule=modal.Period(hours=3),
    timeout=120,
    secrets=[scanner_secrets, supabase_secrets],
)
def edgar_filing_monitor() -> ScannerResult:
    from modal_workers.scanners.edgar_filing_monitor import scan
    return run_scanner("edgar_filing_monitor", scan)
```

Auxiliary Modal functions:

- `lse_cache_refresh` — weekly, pre-warms `scanner-caches/lse/alldata/`.
- `reporting_weekly` — §7.3.
- `signal_log_compaction` — daily, prunes rows outside the 14/90-day retention window. (Mirrors `run_post_scan.save_signal_log` retention; v2 keeps data for audit but marks stale rows and drops from hot-path indexes.)

### 7.3 Reporting function

```python
@app.function(
    image=image,
    schedule=modal.Cron("0 12 * * 0"),  # Sunday 12:00 UTC
    timeout=600,
    secrets=[supabase_secrets],
)
def reporting_weekly() -> ReportingResult:
    # Read active candidates (state IN ('active','watch')) + candidate_rationales.
    # Render executive_summary.pdf and per-candidate dossiers with reportlab.
    # Upload to reports/<yyyy>/<mm>/<yyyy-mm-dd>_*.pdf.
    # INSERT notification row; fan-out emails the summary link.
    ...
```

Uses `tools/report_generator.py::publish_reporting` logic verbatim, with `PUBLISH_ROOT` rewired to the Storage bucket.

### 7.4 Thesis writer (Cowork scheduled task — runs as Claude skill under Pedro's account)

**Architecture pivot (2026-04-20 post-approval).** Thesis drafting is NOT a Modal function. It is a **Claude skill invoked by a Cowork scheduled task running under Pedro's account**. The skill has full tool access (WebSearch, Supabase MCP, Bash for gate validation) and uses Pedro's Claude quota directly rather than an Anthropic API key. This supersedes the earlier Modal-function design in prior revisions of this section.

**Authoritative implementation:** [`.claude/skills/thesis_writer.md`](.claude/skills/thesis_writer.md). The skill file is the single source of truth for: context loading, drafting rules, gate invocation path, promote / DLQ flow, and the 15/day quota check. This spec section documents the contract; the skill documents the execution.

**Purpose:** on every Immediate-band signal, draft a v2 thesis (§7.1 `assess_thesis_v2` schema: situation, why_underpriced, next_catalyst, next_catalyst_date, kill_conditions, steelman, web_research, structured_kill_conditions, confidence), run it through **two gates** (semantic challenger + syntactic validator), and promote to `candidates`. Two independent retry budgets: `attempt_count` for syntactic-gate retries (max 2 drafts) and `challenge_count` for challenger retries (max 2 challenges; migration `20260423000000_thesis_challenger.sql`). A challenger `kill` verdict skips retry and DLQs immediately. **Per 2026-04-20 email-gating directive:** this skill's successful promotion (`candidate_events.event_type='created'`) is what triggers the user-facing email — not the raw `alerts` INSERT. End-to-end SLA (signal → email) is now dominated by this skill's 15-min cron cadence rather than the reactor's 1.5s fan-out leg.

**Two-gate model (2026-04-21 amendment).** The syntactic gate (`assess_thesis_v2` — char counts, boilerplate regex, reasoning-tag coverage) catches sloppy output; it cannot catch sloppy *thinking*. A **semantic gate** runs parallel to it: a separate Claude app routine with an adversarial "skeptical IC reviewer" system prompt (different routine, different system prompt, no shared prior with the drafter). The challenger returns `{verdict: confirm|challenge|kill, reasons, required_fixes, strongest_counter, evidence_citations}`. Both gates must pass before promotion; a challenger `kill` verdict (structural failure — no named asymmetry, hallucinated catalyst, cosmetic kill conditions, "widely-watched deal with no edge" ITRK archetype) short-circuits the syntactic gate entirely. The challenger consumes the same 15/day promotion cap as the drafter (per-promotion metering, not per-Claude-call), so adding the second pass doesn't tighten throughput — it raises per-job compute from ~1 call to ~2 calls on the happy path and ~4 calls worst-case. Full verdict dispatch table is in `.claude/skills/thesis_writer.md` §8f.

**Trigger.** **Cron poll**, not webhook. A scheduled Cowork task (`conan-thesis-writer`, `*/15 * * * *` local time) queries `SELECT * FROM thesis_jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 5` and drains up to 5 jobs per run. Latency from `thesis_jobs INSERT` → `candidates` row → user email is bounded by the 15-min interval; in practice 0-15 min. Under the pre-edge-only email gating, this interval IS the end-to-end SLA (p95 ≤ 20 min).

The earlier `thesis_jobs_insert_wh` pg_net trigger was removed (migration 20) — there is no webhook on this table in v2.

**Why Cowork, not Modal RPC:**
- Runs as Claude (the model), not via the Anthropic SDK, so drafting uses Pedro's Claude plan rather than an `ANTHROPIC_API_KEY` secret.
- Full skill tool access: WebSearch (real web search, not simulated), Bash (for calling the Python gate validator), Read/Write (for scratch), the Supabase MCP (for all DB I/O).
- Session transcripts visible in the Claude Code UI for audit.
- Trade-off: up to 15-min latency from enqueue to candidate row and therefore to the user email (under the 2026-04-20 pre-edge-only email gating, these are the same event). Acceptable because the original 5-min alert SLA is no longer the load-bearing metric — users now receive AI-reviewed, gate-passed candidates rather than raw signals.

**Skill invocation (conceptual signature):**

```
# Pseudocode — actual implementation lives in .claude/skills/thesis_writer.md
skill thesis_writer():
    jobs = supabase.select("thesis_jobs", status="queued", limit=5)
    if today_promotions() >= 15:   return {processed: 0, quota_reached: true}
    for job in jobs:
        signal  = supabase.select("signals", signal_id=job.signal_id)
        entity  = supabase.select("entities", id=signal.entity_id)
        scanner = supabase.select("scanners", id=signal.scanner_id)
        for attempt in 1..2:
            thesis = draft_v2(signal, entity, scanner, retry_context=prior_reasons)
            ok, reasons = bash("python3 -c 'from modal_workers.shared.candidate_gate import assess_thesis_v2; …'")
            if ok:  promote(thesis, job); break
            if thesis.confidence == "low" or thesis.insufficient_signal:  dlq(job, reasons); break
        else:  dlq(job, final_reasons)
```

**Context payload sent to the routine** (the routine's system prompt carries the specialization — framework docs, rubric knowledge, exemplar theses; this payload is signal-specific data only):

```json
{
  "signal": {
    "signal_id": "...",
    "entity": {"name": "...", "ticker": "...", "mic": "...", "market_cap_usd_mm": 0},
    "scoring_profile": "activist_governance",
    "signal_type": "activist_13d",
    "score_with_bonus": 37.5,
    "auto_caps_triggered": ["..."],
    "source_url": "https://...",
    "source_date": "2026-04-20T14:03:00Z",
    "raw_payload": {...}
  },
  "filing_text": "...full primary-source body pulled from filings Storage...",
  "prior_signals_14d": [ {"signal_id": "...", "signal_type": "...", "score": 28, "direction": "long"} ],
  "existing_rationale": null,
  "retry_context": null
}
```

On the corrective retry, `retry_context` is populated:

```json
{
  "previous_draft": {"situation": "...", "why_underpriced": "...", "..."},
  "gate_reasons": ["situation too short (74 chars; need ≥80)", "why_underpriced: matches scanner boilerplate pattern"],
  "instruction": "Your prior draft was rejected. Fix the specific reasons above; do not resubmit near-identical prose."
}
```

**Routine output contract.** The routine must return the full v2 thesis schema (§7.1 `assess_thesis_v2`):

```json
{
  "thesis": {
    "situation": "On 2026-04-16 ITRK disclosed a Rule 2.4 statement naming EQT as a potential offeror [verified] ...",
    "why_underpriced": "The market is treating this as a standard PE take-private [inferred] ... but the trading spread implies the market-implied probability is ~65%, versus our estimate of ~80% [speculated] ...",
    "next_catalyst": "PUSU (put-up-or-shut-up) deadline 28 days from announcement under UK Takeover Code, 2026-05-14.",
    "next_catalyst_date": "2026-05-14",
    "kill_conditions": "Free-form prose summarizing the structured list below, rendered into the dossier."
  },
  "structured_kill_conditions": [
    {"id":"kill_1","description":"Board rejects the possible offer","observable":{"source_type":"filing","filing_type":"RNS 2.8","search_pattern":"(?i)(reject|inadequate|not proceed)"},"date_bound":"2026-05-14","status":"pending"},
    {"id":"kill_2","description":"No firm offer by PUSU deadline","observable":{"source_type":"regulator","search_pattern":"(?i)(walk\\s*away|lapsed|no\\s+firm\\s+offer)"},"date_bound":"2026-05-14","status":"pending"},
    {"id":"kill_3","description":"Competing bid emerges from strategic","observable":{"source_type":"news","search_pattern":"(?i)(counterbid|competing offer|rival approach)"},"status":"pending"}
  ],
  "steelman": "The contrary view: EQT has already approached once at a lower level and was rebuffed [inferred]. At $8B+ and TIC-industry regulatory overhang (ISO-17025 enforcement wave [speculated]), this is a widely-watched name with minimal information asymmetry [verified from search]. ...",
  "web_research": [
    {"url":"https://www.reuters.com/...","retrieved_at":"2026-04-20T14:07:03Z","finding":"Reuters confirms EQT is the sole named bidder; no competing strategic mentioned.","lean":"strengthening"},
    {"url":"https://www.ft.com/content/...","retrieved_at":"2026-04-20T14:08:12Z","finding":"FT notes the 2018 Carlyle approach was rebuffed at ~6000p; current implied level ~8500p.","lean":"neutral"},
    {"url":"https://www.reuters.com/breakingviews/...","retrieved_at":"2026-04-20T14:09:40Z","finding":"Breakingviews argues TIC comparables trade at 13-15x EV/EBITDA; EQT needs ≥9500p for LBO math.","lean":"weakening"}
  ],
  "confidence": "high",
  "primary_source_citations": ["https://www.investegate.co.uk/announcement/rns/intertek-group--itrk/response-to-possible-offer-announcement-by-eqt/9524268"]
}
```

If `confidence == "low"` OR the routine returns a structured `insufficient_signal` response, the job skips the gate and goes straight to DLQ with `final_reasons=['routine_declined: <reason>']` — no retry (the routine already evaluated and declined). The v2 schema intentionally makes `confidence: "low"` the honest path when a routine can't meet the steelman + web_research + tagging bar; that's preferred over hedging prose that passes char counts but fails asymmetry.

**Promote path** (gate passes):
1. `candidate_gate_service.promote(signal, thesis_full, band='immediate', scoring_profile=<from signal>)` — runs `assess_thesis_v2` + `_render_candidate_md_v2`; returns `{rendered_markdown, structured_kill_conditions}`.
2. Parse `next_catalyst_date` into either `candidates.next_catalyst_date` (ISO) or `candidates.next_catalyst_window` (range; Q2 2026 → `[2026-04-01, 2026-06-30]`, month-band → first-to-last of month).
3. UPSERT `candidates` keyed on `(ticker, mic)`: set `dossier_markdown`, `state='watch'`, `thesis_approved_at=now()`, `current_score=score_with_bonus`, `current_band='immediate'`, `kill_conditions=<structured array>`, `next_catalyst_date`/`next_catalyst_window`, `last_aging_evaluated_at=now()` (skip first aging check — we just wrote the thesis).
4. PUT canonical markdown copy to `candidates/<yyyy>/<mm>/<TICKER>_dossier.md`.
5. INSERT `candidate_events(event_type='created', user_id=NULL, payload={'source':'thesis_writer','thesis_job_id':...,'kill_condition_count':N})` on first creation; `event_type='thesis_drafted_by_claude'` on re-draft for convergence match.
6. UPDATE `thesis_jobs` SET `status='promoted'`, `candidate_id=…`, `completed_at=now()`.

**Re-draft on convergence.** If a new Immediate signal arrives for an entity that already has a candidate, a fresh `thesis_jobs` row is enqueued (signal_id is different even though entity matches). On promote, step 2 is an UPDATE to the existing candidate row; step 4 records `candidate_events(event_type='thesis_updated', payload={'previous_thesis':…})`. Old thesis text lives in the event payload, not overwritten silently.

**Cost + rate envelope.** 15 promotions per UTC day is a soft cap enforced in-skill (`.claude/skills/thesis_writer.md`) — not a Claude-side API quota. Immediate-band volume target is 2-5/week from v1 baselines, so steady-state usage is ~1/day on average; the 15/day ceiling covers convergence clusters. When the cap is hit: `COUNT(signals promoted today) ≥ 15` and the skill sets `gate_reasons=['daily_quota_reached']` on outstanding rows while leaving `status='queued'`. The next cron tick after 00:00 UTC drains them. Alert still fires; only the candidate row is delayed. Dashboard surfaces this with a "quota reached" banner.

**Failure + idempotency.** Re-invoking the skill on a `thesis_jobs` row already in `status='promoted'` or `status='dlq'` is a no-op (the `SELECT WHERE status='queued'` doesn't pick them up). Mid-session crashes (Cowork session terminates, browser closed, rate-limit 5xx mid-draft) leave `status='drafting'` with a populated `started_at`; the next cron tick sweeps `WHERE status='drafting' AND started_at < now() - interval '30 minutes'` and resets them to `queued`. No dedicated janitor function required — the same cron that drains new work handles stuck-drafting recovery.

**Specialization — what the skill file embeds** (`.claude/skills/thesis_writer.md` is authoritative; this list is traceability only):
- System prompt mandating named mispricing delta + specific kill triggers + primary-source citations + the full v2 output schema above.
- Framework docs (`framework/profile_*.md`) for the 6 scoring profiles.
- Exemplar theses from AXSM, RPAY, VRDN, VERA, RGR as "gold standard" references — each demonstrates the required 7-section structure.
- Anti-patterns drawn from `candidates/rejected_pending_thesis/` (boilerplate phrasing, hedging without specifics, deal-facts-without-edge, untagged claims, single-lean web-research tables).
- Reasoning-tag contract: every load-bearing claim in situation / why_underpriced / steelman must carry `[verified]` / `[inferred]` / `[speculated]` per INSTRUCTIONS.md Prime Directive.
- Refusal contract: when the signal lacks enough primary-source substance for a specific thesis, the skill must set `confidence: "low"` or `insufficient_signal: true` and DLQ rather than produce hedging prose.
- Tool access: the skill uses WebSearch for real web research (the `web_research[]` contract requires cited URLs with retrieval timestamps), Bash to call the Python gate validator, Read for scratch notes, and the Supabase MCP for all DB I/O.

### 7.5 Candidate aging (Cowork scheduled task — runs as Claude skill under Pedro's account)

**Architecture pivot (2026-04-20 post-approval).** Candidate aging is NOT a Modal function. Like `thesis_writer` (§7.4), it is a **Claude skill invoked by a Cowork scheduled task running under Pedro's account**. Same rationale: drafting uses Pedro's Claude plan rather than an `ANTHROPIC_API_KEY` secret, full skill tool access (WebSearch, Supabase MCP, Bash), session transcripts auditable in the Claude Code UI.

**Authoritative implementation:** [`.claude/skills/candidate_aging.md`](.claude/skills/candidate_aging.md). The skill file is the single source of truth; this spec section documents the contract.

**Purpose:** replaces the v1 `maintenance` skill's candidate-aging sweep (archived SKILL: steps 4-5) and `unified-operational`'s "monitor existing candidates against kill conditions" responsibility. Evaluates active and watchlist candidates for: (a) kill-condition triggers, (b) elapsed catalyst windows, (c) stale state decay. Writes state transitions to `candidate_events` and realized outcomes to `outcomes`.

**Ownership split (2026-04-26 amendment).** A deterministic Modal-side `pre_edge_monitor` runs between daily aging passes and is allowed to apply only **clear mechanical post-edge transitions** (for example: definitive deal announcement resolving a `takeover_candidate`, or FDA approval / CRL resolving a `binary_catalyst`). It writes through an atomic RPC so `candidates.state`, `candidate_events`, and `outcomes` stay in sync. `candidate_aging` remains the sole owner of `last_aging_evaluated_at`, date-based aging, ambiguous kill conditions, and every Claude-mediated decision.

**Trigger.** **Cron poll**, not webhook. A scheduled Cowork task (`conan-candidate-aging`, daily at 06:00 UTC — well before any US or APAC market open, after the overnight scanner wave) queries:

```sql
SELECT * FROM candidates
WHERE state IN ('active','watch')
  AND (last_aging_evaluated_at IS NULL OR last_aging_evaluated_at::date < current_date)
ORDER BY current_score DESC;
```

One run evaluates every due candidate in the result set. With ~5 active + ~10 watch = ~15 candidates per day, one daily session fits comfortably under the 15/day soft cap.

**Skill invocation (conceptual signature):**

```
# Pseudocode — actual implementation lives in .claude/skills/candidate_aging.md
skill candidate_aging():
    due = supabase.select("candidates", state IN ('active','watch'),
                          last_aging_evaluated_at::date < current_date,
                          order_by="current_score DESC")
    for candidate in due:
        # Stage A — mechanical date sweep (no Claude call)
        if mechanical_decision(candidate) is not None:
            apply(candidate, mechanical_decision); continue
        # Stage B — Claude-mediated evaluation of kill conditions
        signals_14d = supabase.select("signals", convergence_key=candidate.convergence_key,
                                       scan_date >= now() - interval '14 days')
        verdict = evaluate_kill_conditions(candidate, signals_14d)
        verdict = verify_evidence(verdict, signals_14d)   # integrity check
        apply(candidate, verdict)
        supabase.update("candidates", last_aging_evaluated_at=now())
```

**Algorithm** (per candidate where `state IN ('active','watch')`, ordered by `current_score DESC`):

1. **Load context.** Candidate row (kill_conditions, catalyst dates, last_updated, last_aging_evaluated_at) + all signals for the entity in the last 14/30 days (via convergence_key match, identical window rule to the reactor) + any candidate_events since `last_aging_evaluated_at`.

2. **Mechanical date sweep first (cheap, deterministic, no Claude call).**
   - If `state='active'` AND `next_catalyst_date IS NOT NULL` AND `next_catalyst_date < today - interval '7 days'` AND no convergence signals for the entity in the last 14 days referencing the catalyst → set `catalyst_elapsed=true` for stage 3.
   - If `state='watch'` AND `updated_at < now() - interval '60 days'` → set `state='killed'`, `kill_reason='aged_out'`, INSERT `outcomes(outcome_type='expired', realized_return=NULL)`, skip stage 3.
   - If `state='active'` AND `updated_at < now() - interval '30 days'` AND `next_catalyst_date IS NULL OR next_catalyst_date > now() + interval '30 days'` → demote to `state='watch'`. This is the "active without near catalyst" rule.

3. **Claude-mediated kill-condition evaluation.** The skill evaluates the candidate's structured `kill_conditions` against the recent-signals payload using its own reasoning plus the search-pattern regex as a grounding anchor. The evaluation prompt (embedded in `.claude/skills/candidate_aging.md`, documented here for traceability):

   ```json
   {
     "candidate": {"ticker":"...","scoring_profile":"...","state":"active","dossier_markdown":"..."},
     "kill_conditions": [/* from candidates.kill_conditions */],
     "recent_signals_14d": [/* signal payloads for this entity */],
     "catalyst_elapsed": true,
     "evaluation_date": "2026-04-20"
   }
   ```

   Skill output contract:

   ```json
   {
     "kill_condition_updates": [
       {"id":"kill_1","new_status":"triggered","evidence_url":"https://...","evidence_ts":"2026-04-18T09:32:00Z","reasoning":"SC 14D9 filed 2026-04-18 explicitly states 'board finds the offer inadequate'."},
       {"id":"kill_2","new_status":"pending","reasoning":"PUSU deadline 2026-05-14 not yet elapsed."}
     ],
     "recommendation": "kill" | "demote_to_watch" | "maintain" | "deliver",
     "recommendation_reasoning": "..."
   }
   ```

   **Integrity defense (same as in prior revisions).** Before committing any `new_status='triggered'`, the skill verifies the `evidence_url` maps to a signal in `recent_signals_14d` whose `raw_payload` actually matches the kill_condition's `observable.search_pattern` (Python regex, case-insensitive unless the pattern specifies). On mismatch: downgrade to `new_status='pending'`, insert `candidate_aging_failures(error_kind='hallucinated_trigger')`, preserve the original reasoning for audit.

   **Semantic gate — challenger pass (2026-04-21 amendment).** The regex integrity check catches hallucinations (no such match exists) but not cosmetic triggers (match exists but isn't load-bearing for the thesis). For every proposed `new_status='triggered'`, the skill additionally invokes the **challenger routine** — same adversarial Claude app routine used by `thesis_writer` (§7.4) but reframed: "is the matched signal load-bearing for this kill condition, or is it a cosmetic pattern hit?" The challenger returns `{verdict: confirm|challenge|kill, reasons, load_bearing_assessment, strongest_counter}`. On `challenge` or `kill` the specific kill_condition update downgrades to `pending` and an `candidate_aging_failures(error_kind='other', error_message='challenger_{challenge,kill_cosmetic}: …')` row is written; if the `recommendation` depended on that update, it downgrades to `maintain`. Runs PARALLEL to the regex check (both must pass). Shares the 15/day Stage B cap — per candidate with ≥1 proposed triggered update, adds 1 challenger call. Full contract in `.claude/skills/candidate_aging.md` §5.5.

4. **Apply actions.**
   - Update `candidates.kill_conditions` with the status transitions from stage 3 (the `kill_conditions` JSONB is fully replaced with the merged list).
   - Based on `recommendation`:
     - `kill` → set `state='killed'`, INSERT `outcomes(outcome_type='killed', notes=<reasoning>)`, INSERT `candidate_events(event_type='state_changed', payload={'from':'<prev>','to':'killed','reason':<triggered_kill_id>})`.
     - `demote_to_watch` → set `state='watch'`, INSERT `candidate_events(event_type='state_changed', payload={'from':'active','to':'watch','reason':'catalyst_elapsed_or_stale'})`.
     - `deliver` → set `state='delivered'`, INSERT `outcomes(outcome_type='delivered', realized_return=NULL)` — realized_return is filled manually by Pedro in a later annotation.
     - `maintain` → no state change; only `kill_conditions` status may have changed.
   - Always UPDATE `candidates.last_aging_evaluated_at = now()`.

5. **Fan-out on state change.** `candidate_events INSERT` with `event_type='state_changed'` where `new_state IN ('killed','delivered')` fires the fan-out edge function — Pedro and collaborators get an email "Candidate X transitioned to killed: <reason>". §6.2 fan-out is extended to handle this event type; Appendix D has the state-change template.

**Quota envelope.** ~15 candidates/day × 1 eval = 15 skill "calls" per day — exactly at the 15/day soft cap. In practice most candidates exit at stage 2 (mechanical date sweep, no Claude reasoning), so actual Claude-reasoning calls per run are usually much lower. Beyond the cap: lowest-score candidates defer to tomorrow; `last_aging_evaluated_at` reveals the backlog on the dashboard.

**Retry + failure.** Per-candidate evaluation is independent — one failure doesn't halt the sweep. Skill errors, decline-to-evaluate outputs, or integrity-check mismatches insert into `candidate_aging_failures (candidate_id, error_kind, error_message, routine_output, consecutive_failures)`. Next day's run retries. `consecutive_failures ≥ 3` on the same candidate surfaces an `operator_flags(source='candidate_aging', kind='aging_stuck', severity='warn')` row.

**Idempotency.** Two runs on the same calendar day are a no-op for candidates where `last_aging_evaluated_at::date = current_date`. Forces exactly-once semantics per UTC day without any lock.

**Specialization — what the skill file embeds** (`.claude/skills/candidate_aging.md` is authoritative):
- System prompt: "Evaluate whether any kill condition for this candidate has been triggered by the recent signals. Do NOT invent triggers; require explicit evidence (a signal whose URL, search_pattern match, or content-hash maps to the observable spec). Err on the side of `maintain` when evidence is ambiguous."
- Framework docs for each scoring profile's typical kill patterns.
- The dossier's structured `kill_conditions` list, including the `observable.search_pattern` regex — the skill applies the regex to signal payloads (via Bash `python3 -c 're.search(...)'`) and grounds its `triggered` claim in a specific match.
- Tool access: Supabase MCP (queries, updates, insert), Bash (regex validation, structured-output parsing), WebSearch (only when a kill_condition's observable type is `news` or `regulator` and the signal stream doesn't already contain the relevant item).

### 7.6 Observability and maintenance functions

The observability dispatcher now runs the original four scheduled maintenance sweeps plus the deterministic `pre_edge_monitor` and daily additive signal-enrichment sweeps (`legal_enrichment_sweep`, `biotech_enrichment_sweep`). All write to `operator_flags` (§3.4) or to additive JSONB overlays rather than to ad-hoc markdown files. No Claude routine calls — these are mechanical sanity sweeps.

#### 7.6.1 `translation_health` — daily 02:00 UTC

**Purpose:** guards the D-002 invariant. Translation drift silently corrupts scoring for non-English scanners.

```python
@app.function(image=image, schedule=modal.Cron("0 2 * * *"), timeout=120, secrets=[supabase_secrets])
def translation_health() -> HealthResult: ...
```

**Algorithm** (per scanner in {tdnet, kind, cvm, bmv, sedar_plus, hkex, bse_nse}):

1. Query `signals` for the last 30 days from this scanner where `raw_payload ? 'translation_confidence'` — extract the confidence values.
2. Compute rolling 30-day median, p25, count. Update a `scanner_translation_stats` view (or materialized view; spec leaves the choice to implementation).
3. If `median < 0.75` → UPSERT `operator_flags` with `severity='warn'`, `kind='translation_confidence_trend'`, `scanner_id=…`, title = "translation median {value} over 30d (threshold 0.75)", evidence = `{median, p25, count, window_days:30}`.
4. If any single day in the last 7 has `p50 < 0.70` AND the prior-day condition repeated for ≥7 consecutive days → UPSERT with `severity='critical'` instead. Auto-resolves when a day breaks the streak.
5. If the flag had been open and the 30d median has recovered ≥0.80 → UPDATE `resolved_at`, `resolved_note='auto-resolved: median recovered'`.

#### 7.6.2 `scanner_probe` — every 6 hours at :15

**Purpose:** active endpoint health. Catches drift between scheduled scanner runs.

```python
@app.function(image=image, schedule=modal.Cron("15 */6 * * *"), timeout=120, secrets=[supabase_secrets])
def scanner_probe() -> ProbeResult: ...
```

**Algorithm** (per scanner in `scanners` where `status='operational'`):

1. Load `scanners.endpoints` JSONB — canonical URL + documented fallbacks.
2. Issue lightweight GET (or HEAD if documented) to the primary endpoint. Capture {status_code, latency_ms, content_type, body_bytes (first 256)}.
3. Evaluate:
   - 2xx + expected content-type → CLEAR any open `operator_flags (source='scanner_probe', scanner_id=X)`.
   - 4xx/5xx → try each documented fallback once; if any succeeds, UPSERT flag with `severity='warn'`, `kind='endpoint_fallback_active'`. If all fallbacks fail → `severity='critical'`, `kind='endpoint_drift'`.
   - Unexpected content-type or body size >5σ from the 7-day baseline → `severity='warn'`, `kind='content_shape_drift'`.
4. UPDATE `scanners.last_probe_at`, `scanners.last_probe_status` (new columns — see Appendix A).

Does NOT attempt auto-repair. Auto-repair was a v1 maintenance-skill behavior (fix compile errors, rename endpoints); v2 treats scanner code as deployed-and-frozen, and endpoint-drift fixes are human-reviewed changes via Supabase Studio edits to `scanners.endpoints`.

#### 7.6.3 `convergence_qa` — daily 03:00 UTC

**Purpose:** catches reactor regressions. Samples recent convergence decisions and verifies against an offline reference implementation.

```python
@app.function(image=image, schedule=modal.Cron("0 3 * * *"), timeout=180, secrets=[supabase_secrets])
def convergence_qa() -> QAResult: ...
```

**Algorithm:**

1. Random-sample 20 signals from `signals` where `convergence_evaluated_at > now() - interval '24 hours'` AND `convergence_bonus > 0` (i.e., non-trivial groupings).
2. For each sample, re-compute convergence **offline** using the `rubric_engine.convergence_reference()` function — a standalone pure-Python re-implementation that takes the same inputs as the reactor and returns `{convergence_key, bonus, group_winner}`. Ported from `tools/convergence_engine.py` as an audit-only reference (distinct from the reactor's SQL-based implementation).
3. Compare: reactor's `convergence_bonus`/`score_with_bonus`/`convergence_key` vs reference. Tolerate ±0 on bonus (integer), ±0.5 on score_with_bonus (floating), exact-match on convergence_key.
4. On mismatch → UPSERT `operator_flags` with `severity='critical'`, `kind='convergence_disagreement'`, evidence = `{signal_id, reactor_output, reference_output, delta}`. Every mismatch is a reactor bug.
5. Additionally: scan for orphaned state — `alerts` rows whose underlying `signals.band_with_bonus` is no longer `'immediate'` (reactor changed the signal after alert already fanned out). Flag as `kind='orphan_alert'`, `severity='warn'` — informational, not a bug per se (expected on cross-UPDATE races), but trackable.

#### 7.6.4 `litigation_baselines_refresh` — weekly Sunday 04:00 UTC

**Purpose:** preserves the litigation sub-system's baseline freshness (party-resolution cache, DEF 14A executive lookups, Exhibit 21 subsidiary tables). Carried over from the archived `litigation-maintenance` skill.

```python
@app.function(image=image, schedule=modal.Cron("0 4 * * 0"), timeout=600, secrets=[scanner_secrets, supabase_secrets])
def litigation_baselines_refresh() -> BaselinesResult: ...
```

**Algorithm:**

1. **Party-resolution cache re-verification.** Scan `party_resolution_cache` (Storage-backed under `scanner-caches/litigation/parties/`) for entries with `last_verified > interval '180 days'`. Queue up to **50 per pass** (budget cap preserved from v1). For each: replay Stage 2 resolution (EDGAR exact → EDGAR fuzzy → Exhibit 21 → OpenFIGI NAME). If resolved → update `last_verified`; if still unresolved after 3 cumulative attempts across runs → UPSERT `operator_flags(source='litigation_baselines', kind='party_unresolvable_cold', severity='warn')` and move to cold-store.
2. **DEF 14A executive-lookup freshness check.** Read `baselines/executive_lookup.json` in Storage. If `last_refreshed > interval '90 days'` → UPSERT `operator_flags(kind='baseline_stale_def14a', severity='warn', title='DEF 14A refresh due')`. **Does NOT auto-refresh** — per v1 policy, baseline refresh is a scheduled manual job, not autonomous (too expensive; takes several hours of EDGAR pulls).
3. **Exhibit 21 subsidiary table freshness.** Same pattern as (2) with `kind='baseline_stale_exhibit21'`.
4. UPDATE `last_run_utc` on the litigation scanners' `scanners.config.baselines_last_checked_at` field.

Active only when litigation scanners are live in `scanners.status='operational'` (courtlistener, sec_enforcement). If both are `auth_required` or `deprecated`, the function short-circuits on entry and logs a no-op.

#### 7.6.5 `precision_auditor` — weekly Sunday 02:15 UTC (Phase 1d)

**Purpose:** Phase 1d precision-side companion to `coverage_auditor` (§7.6 recall side). Aggregates `emissions_ledger` over a 90-day window by `(profile × gate_decision × confidence)` and writes sparse-column rows into `accuracy_metrics`. Raises `operator_flags` when per-profile delivery rate drifts, post-edge miss rate spikes, confidence labels stop discriminating, bands collapse, or auto-caps invert (auto-capped cells outperform promoted — signals the rubric is binding the wrong way).

```python
# In observability.py; invoked in-process from dispatch_observability at Sunday 02:15 UTC.
def precision_auditor(client: Optional[SupabaseClient] = None) -> dict: ...
```

**Algorithm:**

1. GET `emissions_ledger` rows with `scored_at >= now() - interval '90 days'`, paginated.
2. For promoted rows, fetch `candidate_events.payload.thesis.confidence` per `candidate_id` (batch query on `event_type IN ('created','thesis_drafted_by_claude')`).
3. Cross-tabulate by `(profile, gate_decision)` primary, `(profile, 'promoted', confidence)` for discrimination, `(profile, band)` for band split.
4. Per cell compute: `sample_n`, `labeled_n`, `delivered/killed/expired`, `pre_edge_hit/post_edge_miss/dead_catalyst` counts + rates.
5. Per profile compute: `band_discrimination = delivery_rate(immediate) − delivery_rate(watchlist)`, `confidence_discrimination = delivery_rate(high) − delivery_rate(medium)`, `auto_cap_inversion = delivery_rate(auto_capped) − delivery_rate(promoted)`.
6. Compare current delivery_rate against prior accuracy_metrics baseline for the same cell; flag drift.
7. INSERT normalized rows into `accuracy_metrics` (sparse-column bulk insert; PostgREST requires uniform keys per batch so a `_normalize_metrics_row` helper fills missing columns with NULL).
8. UPSERT `operator_flags` per breach: `precision_drift` (warn ≥20pp drop, critical ≥40pp), `post_edge_miss_spike` (warn ≥0.30, critical ≥0.50 of labeled_n ≥20), `dead_catalyst_spike` (warn ≥0.40), `confidence_noise` (|discrimination|<0.05 with n≥30), `band_collapse` (discrimination <0.10 with n≥30), `auto_cap_inverted` (critical, inversion > 0 with n≥20 on both cells).

Thresholds tunable at module top: `_PRECISION_*` constants.

#### 7.6.6 `timing_auditor` — weekly Sunday 02:15 UTC (Phase 1d)

**Purpose:** Phase 1d companion to `precision_auditor`. Computes per-profile catalyst-date forecast accuracy and return-decay profile from the promoted + fully-labeled subset of `emissions_ledger`.

```python
# In observability.py; invoked in-process from dispatch_observability at Sunday 02:15 UTC.
def timing_auditor(client: Optional[SupabaseClient] = None) -> dict: ...
```

**Algorithm:** for `gate_decision='promoted'` emissions with both `predicted_catalyst_date` and `catalyst_hit_date` non-null, compute per-profile:

- `timing_error_median_days` (signed): `median(catalyst_hit_date − predicted_catalyst_date)` — positive = systematically early, negative = late.
- `timing_error_abs_p50 / _p90`: absolute error distribution.
- `emission_lead_days`: `median(catalyst_hit_date − promoted_at::date)` — how much runway we actually gave.
- `decay_ratio_30d_over_1d`: `mean(|realized_move_30d| / |realized_move_1d|)` — ≈1 = prompt; >>1 = slow-motion.
- Mean realized moves at 1d / 7d / 30d + realized_return.

Flags: `timing_drift` (warn at abs_p50 > 60d), `emission_too_late` (warn at lead_median < 3d), `decay_anomaly` (warn at decay_ratio > 3.0). All require `MIN_SAMPLE_N=10` before firing.

#### 7.6.7 `challenger_retro` — weekly Sunday 09:00 UTC (Phase 1d, Cowork)

**Purpose:** Detect drift in the static `thesis_challenger` routine by sampling historical promotions with labeled outcomes and re-invoking today's challenger on their theses. Catches two failure modes: challenger grew too aggressive (would `kill` a known `pre_edge_hit`) or too lenient (would `confirm` a known `dead_catalyst`).

**Architecture:** Cowork skill, NOT a Modal function. The challenger is a Cowork-resident Claude routine; Modal cannot invoke it. Sibling skill file at [`.claude/skills/challenger_retro.md`](.claude/skills/challenger_retro.md) is authoritative.

**Algorithm (skill summary):**

1. Sample ≤10 candidates with `outcome_label ∈ {pre_edge_hit, dead_catalyst, post_edge_miss}` in the rolling 90d window, stratified 3/3/2/2.
2. For each, fetch historical thesis from `candidate_events.payload.thesis` + signal + entity + scanner context.
3. Invoke `thesis_challenger` (drafting mode) with fresh context per sample — no cross-sample state leak.
4. Classify each by (outcome_label × verdict) — 8 buckets: `calibrated_hit / ambiguous_hit / miss / save / partial_save / pass_through / timing_catch / timing_miss`.
5. Aggregate `miss_rate`, `pass_through_rate`, `save_rate`, `calibrated_hit_rate`.
6. INSERT one `accuracy_metrics` row with `auditor='challenger_retro'`. Evidence JSONB carries per-sample records for Pedro's audit.
7. Flags: `challenger_retro_miss` (warn ≥0.10 / critical ≥0.25 on n≥5 hits), `challenger_retro_pass_through` (warn ≥0.25 on n≥5 dead catalysts), `challenger_retro_timing_blindspot` (warn on ≥2 timing misses out of n≥3).

**Scheduling:** Cowork cron `0 11 * * 0` local time (CEST UTC+2) = Sun 09:00 UTC in summer; rebind to `0 10 * * 0` after DST ends 2026-10-25. Placed after `coverage_auditor` (Sun 04:00 UTC) so its flags land in `reporting_weekly_cron` (Sun 12:00 UTC).

**Quota:** 10 challenger invocations per run. Shares Pedro's Cowork-routine budget; the stratified cap keeps total weekly cost bounded.

### 7.7 `reporting_weekly` augmentation — signal-log integrity sweep

The existing `reporting_weekly` function (§7.3) gains a pre-render integrity sweep:

1. Before rendering, query for orphans:
   - `alerts` rows referencing `signals` that no longer exist (FK violation would have rejected, but check anyway for index corruption).
   - `candidates` rows with `state='active'` whose most recent `candidate_events.created_at` is >45 days ago (stuck states — aging should have caught these; if not, the aging function is broken).
   - `thesis_jobs` in status `'drafting'` with `started_at < now() - interval '1 hour'` (janitor missed).
2. Each finding → UPSERT `operator_flags(source='reporting_weekly', kind='integrity_<flavor>', severity='warn')`.
3. Continue to the normal report render regardless.

Cost: negligible; all queries are indexed.

---

## 8. Event flow

### Happy path

```
┌───────────────────┐      ┌────────────────┐
│  Modal scanner    │      │  scanner_base  │
│  (on schedule)    │─────▶│  run_scanner() │
└───────────────────┘      └───────┬────────┘
                                   │ open scanner_runs row
                                   │ call scan() inside soft timeout
                                   │ fetch → parse → resolve FIGI
                                   ▼
                           ┌───────────────────┐
                           │ upsert filings    │──▶ Storage: filings/<source>/<hash>
                           └───────┬───────────┘
                                   │
                                   │ build Signal[] (dedup vs source_content_hash+profile)
                                   │ attach rubric_version_id + score via rubric_engine
                                   ▼
                           ┌───────────────────┐
                           │ INSERT signals    │
                           └───────┬───────────┘
                                   │ DB webhook (sync, <1s)
                                   ▼
                           ┌──────────────────────┐
                           │ /functions/v1/reactor │
                           │  • resolve convergence_key                    │
                           │  • query 14/30d window via signals_* indexes  │
                           │  • dedup source_content_hash                  │
                           │  • classify direction + group size            │
                           │  • pick winner, cross-UPDATE prior if needed  │
                           │  • rubric_engine.apply_auto_caps (Modal RPC)  │
                           │  • UPDATE signals SET convergence_*           │
                           │  • if band_with_bonus='immediate':            │
                           │      INSERT alerts ON CONFLICT DO NOTHING     │
                           │      INSERT thesis_jobs ON CONFLICT DO NOTHING│
                           └───┬────────────────┬─┘
                               │ alerts.INSERT  │ thesis_jobs.INSERT
                               │ (audit trigger │
                               │  → fanout      │
                               │  audit-only    │
                               │  path: storage │
                               │  + Realtime,   │
                               │  NO email)     │
                               ▼                ▼
                         audit-only fanout  ┌─────────────────────────────┐
                                           │ thesis_writer (Cowork skill │
                                           │  every 15 min, §7.4)        │
                                           │  • poll thesis_jobs queued  │
                                           │  • load signal + filing     │
                                           │  • draft v2 thesis (Claude) │
                                           │  • candidate_gate.v2 assess │
                                           │     ├ pass → promote        │
                                           │     │   UPSERT candidates   │
                                           │     │   INSERT candidate_   │
                                           │     │     events (created)  │
                                           │     └ fail: retry 1× →      │
                                           │        DLQ thesis_drafting_ │
                                           │        failures             │
                                           └───────────────┬─────────────┘
                                                           │ candidate_events.INSERT
                                                           ▼
                                           ┌─────────────────────────────┐
                                           │ candidate_events_fanout_wh  │
                                           │  → /functions/v1/fanout     │
                                           │  • render pre-edge email    │
                                           │  • notifications_prefs      │
                                           │  • Resend deliveries        │
                                           │  • Realtime broadcast       │
                                           │    on candidates channel    │
                                           └──────┬──────────────────────┘
                                                  ▼
                                         ┌────────┴────────┐
                                         ▼                 ▼
                                    ┌─────────┐      ┌───────────┐
                                    │ Resend  │      │ Realtime  │
                                    │ (email) │      │ WS        │
                                    └─────────┘      └───────────┘
```

Email dispatch is gated on the AI-reviewed pre-edge promotion (`candidate_events.event_type='created'`), not on raw `alerts` INSERT. End-to-end SLA is dominated by the 15-min `thesis_writer` cron: signal → reactor → thesis_writer poll window → draft → gate → `candidate_events` → fanout → Resend. p95 target: 20 min (was 5 min under the pre-directive wiring). Alerts rows still land (audit + Realtime broadcast for dashboard live signal stream), but no email off that path.

### Reporting path

```
Cron (Sun 12:00Z) ─▶ reporting_weekly Modal fn
                       │ read candidates + candidate_rationales
                       │ reportlab render
                       │ PUT Storage reports/<yyyy>/<mm>/…
                       │ INSERT notifications row (alert-like)
                       ▼
                     DB webhook ─▶ /fanout ─▶ Resend (signed URL, 7d expiry)
```

### Observability / maintenance path

```
Daily 02:00Z  translation_health   ─▶ scanner_translation_stats view
                                       + operator_flags (warn/critical)

Daily 03:00Z  convergence_qa       ─▶ offline reference re-compute
                                       + operator_flags (critical on mismatch)

6h cron :15   scanner_probe        ─▶ scanners.last_probe_* columns
                                       + operator_flags (endpoint drift)

Sun   04:00Z  litigation_baselines ─▶ party_resolution re-verify (50/pass)
                                       + operator_flags (baseline stale)

Sun   12:00Z  reporting_weekly     ─▶ integrity sweep + reportlab render
                                       + operator_flags (orphans/stuck state)
```

All five share a common sink: `operator_flags`. Dashboard renders one list, sorted by `severity DESC, created_at DESC WHERE resolved_at IS NULL`. Pedro resolves with a one-click + optional note.

### Candidate aging path

```
Cron (daily 06:00Z) ─▶ candidate_aging (Cowork skill, §7.5)
                         │ for each candidate state∈{active,watch} order by score DESC:
                         │   • mechanical: elapsed catalyst? 60d-stale watch? 30d-stale active?
                         │   • Claude-mediated: any kill_condition.observable matched recent signals?
                         │     (integrity check: verify evidence_url's regex match before commit)
                         │   • apply recommendation: kill | demote_to_watch | deliver | maintain
                         │   • UPDATE candidates.kill_conditions + state + last_aging_evaluated_at
                         │   • INSERT candidate_events (state transitions)
                         │   • INSERT outcomes on kill / deliver
                         ▼
                       candidate_events INSERT (event_type='state_changed' AND new_state∈{killed,delivered})
                         ▼ DB webhook
                       /functions/v1/fanout (extended for state_changed event type)
                         ▼
                       Resend — "Candidate RPAY transitioned to killed: board rejected at SC 14D9"
```

### Failure and retry paths

| Failure | Detection | Recovery |
|---|---|---|
| Reactor 5xx | Supabase webhook reports non-2xx | Auto-retry 3× (60s, 300s, 900s). If all fail, row persists in `failed_reactor_events`; Pedro sees it in scanner-health card. |
| Modal scanner crash (OOM, network) | `scanner_runs.status = 'error'`, `errors` populated | No signals written → no downstream work. Next cadence re-runs. Persistent failures surface on dashboard health card. |
| Modal scanner timeout | `scanner_runs.status='timeout'`, partial signals flushed | `partial=true` set on each Signal by `scanner_base` when soft-timeout is hit mid-scan; these are still scored and stored. |
| OpenFIGI 429 | `openfigi_resolver._post_batch` detects, sleeps 6s, retries once | If still 429, `FigiResolution(resolved=False)` flows through; signal writes with `issuer_figi=NULL`, reactor falls back to ticker+MIC key. |
| Auth-required (Q-017, Q-019) | `run_scanner` sees missing secret | `ScannerResult(status='auth_required', signals=[], warnings=[…])`. Scanner run row logged; dashboard marks scanner yellow. No retries until secret appears. |
| Dedup collision (same signal emitted twice) | Postgres unique `(source_content_hash, scoring_profile)` | INSERT fails with 23505; `scanner_base.insert_signals` catches and logs as `skipped`. |
| Bridge-mode race with Modal | Both try to INSERT same `source_content_hash` | Unique constraint rejects loser; no harm done. `signal_id` collisions are prevented by scanner-namespaced IDs. |
| Alert duplicate same day | Unique `(entity_id, fingerprint, day_utc)` | Reactor `ON CONFLICT DO NOTHING`; no alert, no email. |
| Resend 429/5xx | `alert_deliveries.status='failed'` + error body | Single retry 3s backoff; persistent failure surfaces on dashboard; no auto-retry beyond that (operator decision). |
| Thesis gate-fail (first attempt) | `candidate_gate.assess_thesis` returns reasons | `thesis_jobs.status='gate_failed_retrying'`; `thesis_writer` self-invokes once with `retry_context` populated. Alert already dispatched by fan-out in parallel. |
| Thesis gate-fail (second attempt) | Second `assess_thesis` call returns reasons | INSERT `thesis_drafting_failures` with all drafts + final reasons; `thesis_jobs.status='dlq'`. Dashboard shows "needs manual thesis" banner. Alert already fanned out. |
| Routine declines (`confidence:"low"`) | Routine returns structured refusal | Skip gate, go straight to DLQ with `final_reasons=['routine_declined: …']`. No retry — the routine already evaluated the signal and declined. |
| Claude routine quota exhausted (15/day) | Modal guardrail: count of today's non-queued `thesis_jobs` ≥ 15 | `thesis_jobs.status='queued'` persists; job resumes after 00:00 UTC. Alert unaffected. Dashboard shows "quota reached" banner. |
| Thesis worker OOM / crash mid-call | `thesis_jobs.status='drafting'` + `started_at > 10min ago` | Janitor re-enqueues (resets status to `queued`, increments nothing). Idempotent; routine calls aren't charged until response returns. |
| Aging routine declines or errors | `candidate_aging_failures` row inserted | Candidate skipped for this run; `last_aging_evaluated_at` NOT updated. Next day's run retries. If the same candidate fails 3 consecutive days → operator_flag (future: once §5 from gap analysis is built). |
| Aging routine falsely claims kill trigger (evidence regex mismatch) | Integrity check in `candidate_aging`: before applying a `triggered` transition, verify the routine's `evidence_url` actually matches the kill_condition's `observable.search_pattern` against a freshly-fetched filing | On mismatch, downgrade to `maintain`; log to `candidate_aging_failures` with `error='hallucinated_trigger'`. Defensive against routine drift. |
| Aging quota exhausted (15/day) | Routine call returns 429-equivalent | Candidates beyond the quota defer; `last_aging_evaluated_at` stays stale; dashboard shows "aging backlog N candidates." Does not trigger retry — tomorrow's run picks up. |

---

## 9. Migration plan

Goal: bring all state from v1 (JSON files + markdown dossiers) into Supabase without loss and without rescoring drift. Cutover is staged: Phases 0-2 keep v1 running; v2 starts receiving data in Phase 1 via the reference scanner; bridge mode fills the gap in Phase 2-3; v1 is retired only at Phase 6.

### 9.1 Registry seeding

Single-shot script `migrations/seed_registry.py`:

1. `scanner_registry.json` → one row per scanner in `scanners`. Map fields 1:1. `config` JSONB absorbs `notes`, `strategy_spec`, `filter_excluded_filers`, and any other scanner-specific keys. Additionally: seed `config.market_cap_floor_usd_mm = 215` on every row (canonical floor per §12 locked decision; scanner code reads from its config at startup — no hard-coded constant). Per-scanner overrides can be set later via Supabase Studio if needed.
2. Create one `sources` row per unique `endpoint_primary` host (auto-derive `kind` from URL).
3. `run_post_scan.WEIGHTS` → six `rubrics` rows (one per profile) at `rubric_version=1`, `effective_at=now()`, `superseded_at=NULL`.
4. `pe_filer_allowlist.json` → 45 rows.
5. `phase3_approval_base_rates.json` → 39 rows.
6. `candidates/_curated_rationales.json` → N rows keyed by ticker; `_archived` block maps to `archived=true` + `archived_meta`.

### 9.2 Signal log import (minimal scope per §12)

Script `migrations/import_signal_log.py`. Per Pedro's locked decision in §12, import is minimal: only signals referenced by the 5 active dossiers (RPAY, AXSM, VERA, VRDN, RGR) + the 3 archived-post-edge candidates (TVTX, AVNS, GSAT) + SEM. Roughly tens of rows total, not the full 734. The 565 `UNKNOWN`-tagged legacy rows are not imported; the rescue table from the original spec draft is dropped.

1. Read `signals/signal_log.json`.
2. Build the allow-set: parse frontmatter of every markdown under `candidates/`, `candidates/_archived_post_edge/`, `candidates/delivered/`; collect `ticker` + `mic` + any `signal_id` references in the dossier body. Union with the 9-ticker set in `_curated_rationales.json`.
3. Filter `signal_log.json` to rows whose `signal_id`, `issuer_figi`, or `ticker_plus_mic` matches the allow-set. If the legacy row carries only `ticker` + `raw_data`, match by ticker and keep only rows within the past 30 days (so we don't import stale pre-2026-04 noise).
4. For each surviving row, construct a `Signal` record:
   - `signal_id`, `source_content_hash`, `source_date`, `scan_date` preserved.
   - `imported = TRUE`.
   - `rubric_version_id` resolved by `scoring_profile` → `rubrics.id WHERE rubric_version=1`.
   - `scanner_id` resolved from `raw_data.source` / `signal_category` / `source_url` — a much simpler lookup than the original rescue table, since the allow-set is small enough to hand-verify.
   - `raw_payload` = original row's `raw_data`.
   - `dimensions`, `score`, `band`, `auto_caps_triggered` copied from `scoring` sub-object.
   - `convergence_*` fields left NULL (will be recomputed below).
5. Bulk INSERT; unique index on `(source_content_hash, scoring_profile)` rejects duplicates.
6. Invoke the reactor in backfill mode (`/reactor/backfill?signal_id=…`) over imported rows in `scan_date ASC` order so convergence results are temporally correct within the tiny set.
7. Snapshot convergence distribution and compare to v1's `working/convergence_report_2026-04-20*.json` filtered to the same allow-set. Exact parity expected given the small N.

### 9.3 Candidate dossier import

Script `migrations/import_candidates.py`:

1. Parse 5 active dossiers (`candidates/*.md`) via the same YAML-frontmatter+body reader used by `candidate_gate.audit()`. Map each to a `candidates` row; persist body into `candidates.dossier_markdown` and upload to `candidates/<yyyy>/<mm>/<TICKER>_dossier.md`.
2. Insert `candidate_events(event_type='created', payload={imported:true})` per candidate.
3. Import `candidates/_archived_post_edge/` (3 tickers) with `state='delivered'` and an `outcomes` row reflecting D-013 archive metadata.
4. Import `candidates/rejected_pending_thesis/` (66 files) into a lightweight `rejected_candidates` table (minimal schema: `ticker`, `rejection_path`, `reasons`, `imported_at`) OR skip — Pedro's call. Default: skip; these are informational and don't need the full candidates shape.
5. `candidates/_curated_rationales.json` already seeded in §9.1.

### 9.4 Dry-run strategy

1. Create Supabase branch `migration-dry-run` off `main`.
2. Apply schema migrations.
3. Run seeds (§9.1) and imports (§9.2, §9.3) against the branch.
4. Compute diff metrics:
   - **Signal count** per scanner, per band, per profile — must match v1 aggregates from `signals/signal_log.json` within ±2% (allowing for rescue-table imperfections on `UNKNOWN` rows).
   - **Score distribution** — per-profile mean, p50, p95 must match within 0.5 points.
   - **Convergence groups** — counts of {contradiction, same_direction, orthogonal, single} must match within ±1 group.
   - **Band distribution** — Immediate / Watchlist / Archive / Discard counts match within ±1.
5. If all diffs pass, merge branch to `main`. If not, investigate specific failures (most likely rescue table on `UNKNOWN` rows).

### 9.5 Rollback plan

- **Phase 0-2 (spec, foundation, reactor):** v1 cron keeps running untouched on Pedro's laptop. v2 is write-only for one scanner (edgar). Rollback = delete Supabase project; zero impact on v1.
- **Phase 3 (full scanner fleet + bridge mode):** bridge mode is additive — it runs scanners locally and writes to Supabase, but does not replace v1. v1 signal_log continues. Rollback = stop bridge mode; v1 continues.
- **Phase 6 (cutover):** v1 scheduled tasks disabled. Rollback plan = re-enable v1 cron + run `migrations/export_back_to_json.py` to rebuild `signal_log.json` from `signals` table. Recovery time <30 min, zero data loss.

---

## 10. Test strategy

### 10.1 Unit

- `normalize_ticker` golden vectors: `("AAPL", None) → "AAPL"`, `("7203", "XTKS") → "7203"`, `("469A0", "XTKS") → "469A"`, `("364A0", "XTKS") → "364A"`, `("469A0", "XNYS") → "469A0"` (no trim outside JP MICs).
- `score_signal` against 50 hand-picked v1 signals (one per profile × ~8 variations). Expect byte-identical score + band + auto_caps_triggered.
- `apply_auto_caps` edge tests: merger_arb Rule A at boundary (`annualized_return_pct = RISK_FREE_RATE*100 + 3 ± 0.01`), binary_catalyst EV floor, litigation party_confidence_cap, takeover_candidate post_edge_disqualified + below_triage_gate.
- `classify_band` boundary tests at 15/25/35.
- `candidate_gate.promote_candidate` on 10 valid + 10 boilerplate theses; reasons match v1.
- Entity resolver cascade: FIGI hit → ticker+MIC → codigo_cvm → … → unresolved.

### 10.2 Integration

- One mocked-HTTP scanner test per scanner (record real API responses once, replay from fixtures). Assert scanner emits N signals matching a golden JSON.
- Reactor on a curated INSERT batch (seed 20 signals across 3 entities, verify convergence groups + bonuses match v1 convergence_report for the same batch).
- Fan-out (pre-edge path): directly INSERT a `candidate_events(event_type='created', ...)` row with a pre-populated `candidates` row; assert email delivered to Resend sandbox inbox, `alert_deliveries` row = `sent`, Realtime broadcast received on `candidates` channel.
- Fan-out (audit-only path): INSERT an `alerts` row via a test trigger; assert **no** Resend call, no `alert_deliveries`, but the Realtime broadcast on `alerts` / `entity:<id>` fires.
- Thesis_writer skill dry-run: manual-invoke the skill against a seeded `thesis_jobs` row; assert gate v2 pass → `candidate_events.created` inserted → fanout chain fires.

### 10.3 End-to-end

- **Pre-edge promotion SLA (post-2026-04-20 gating):** synthetic EDGAR filing pushed through: scanner → filings + signals → reactor (Immediate band) → `thesis_jobs` enqueued → thesis_writer cron polls → v2 gate passes → `candidate_events.created` → fanout → Resend sandbox. Assert wall-clock from filing publish to inbox receipt ≤ 20 min (p95 over 20 runs — dominated by the 15-min thesis_writer cron interval; the reactor + fanout legs each contribute <30s).
- **Alerts audit path:** same scenario; assert the `alerts` row lands within 2 min of signal INSERT and that NO `alert_deliveries` row or email is generated off `alerts.INSERT`.
- Dashboard live view: a new signal on an entity with an existing Watchlist convergence triggers a bonus → dashboard pushes the updated row via Realtime within 1s of reactor completion.

### 10.4 Replay test (gating for migration cutover; narrower scope per §12)

Per the locked minimal-import decision in §12, the replay set is the tens of signals linked to active candidates + archived-post-edge + SEM, not the full 734. Smaller set ⇒ exact parity expected; smaller set also means fewer scoring-edge-case rows exercised, so §10.1 unit fixtures take on the complementary coverage role (synthetic signals exercising every profile × every auto-cap combination).

Inputs: the active-candidate-linked subset of `signal_log.json` (identified in §9.2 step 2) + v1 `working/convergence_report_2026-04-20*.json` filtered to the same subset.

Procedure:
1. Seed `rubrics` at version 1 from `WEIGHTS` (six profiles per §12).
2. Run `migrations/import_signal_log.py` (minimal filter) to populate `signals`.
3. Run the reactor in backfill mode across imported rows in `scan_date ASC` order.
4. For every imported row assert: `signals.score` matches `signal_log.json[i].scoring.score` exactly; `signals.band` matches `signal_log.json[i].scoring.band` exactly; `signals.auto_caps_triggered` matches (order-insensitive) exactly.
5. For every convergence group in the v1 filtered report: v2 produces the same `convergence_key`, same winner `signal_id`, same `convergence_bonus`.

Pass criterion: **0 score mismatches, 0 band mismatches, 0 auto-cap mismatches, 0 convergence-group mismatches** (the smaller set makes exact parity the bar, not ±1). Any failure blocks cutover until resolved.

Complementary coverage (new load-bearing: §10.1 unit fixtures): because the replay set is small, the unit-test fixture set must enumerate at least one signal per `(profile × auto-cap rule × band-threshold crossing)` combination — 6 profiles × ~15 rule/threshold cases ≈ 90 fixtures. Fixtures are hand-crafted synthetic signals plus golden-output JSON (score, band, auto_caps_triggered) from running v1 `score_signal` locally once.

### 10.5 Chaos

- Webhook 5xx simulation: pause the reactor for 60s; verify Supabase replays and all queued signals eventually process with correct convergence.
- Scanner OOM: crash a Modal run midway; verify `scanner_runs.status='error'`, next cadence retries cleanly.
- OpenFIGI 429: mock API returns 429 for 30s; verify resolver backoff + eventual success, no signal loss.
- Duplicate INSERT race: fire the same signal twice concurrently; verify unique index rejects the second, `scanner_runs.errors` records the skip, no side effects.
- Resend outage: mock 503 from Resend; verify `alert_deliveries.status='failed'` + operator-visible dashboard badge.

---

## 11. Phase 1 task list (with acceptance criteria)

Phase 1 runs after Pedro approves this spec. Each task has one observable acceptance check.

1. **Supabase project created** — Pro tier in chosen region (§12). AC: `supabase projects list` shows the project; service role JWT usable from CLI.
2. **Schema migrations applied** — Appendix A DDL run via `supabase db push`. AC: `\dt public.*` shows every table in §3; `\df public.*` shows the `set_updated_at` trigger function.
3. **RLS enabled + policies applied** — AC: anonymous JWT query to `signals` returns `[]`; authenticated JWT returns rows; unauthenticated `annotations` query returns 0 even with valid `candidate_id`.
4. **Storage buckets created** — `filings/`, `scanner-caches/`, `reports/`. AC: `supabase storage list` shows all three; signed URL round-trip works via service role.
5. **Database webhooks registered** (2026-04-20 email-gating wiring) — `signals_insert_wh` on `signals` → reactor, `candidate_events_fanout_wh` on `candidate_events` → fan-out. Both use the HMAC secret. (Historical: `alerts_insert_wh` existed pre-directive; dropped in migration `22_email_gating_pre_edge_only`.) AC: test INSERT on `signals` produces a request to reactor URL with valid signature; test INSERT on `candidate_events` with `event_type='created'` produces a request to fan-out URL.
6. **`openfigi_resolver` ported to Modal** — AC: Modal function `openfigi_ping` resolves `("AAPL","US")` + `("469A0","XTKS")` + `("7203","XTKS")` matching golden vectors. CI test vector file checked in under `modal_workers/tests/`.
7. **`scanner_base` + `supabase_client` + `rubric_engine` shared modules** — AC: unit tests pass (§10.1); one dry-run invocation successfully logs a fake `Signal` into the `signals` table against a non-prod project.
8. **`edgar_filing_monitor` ported as reference scanner** — on Modal with 3h schedule, writing to Supabase. AC: manual trigger produces ≥1 signal row for a known recent 13D/A filing within 35 seconds wall-clock.
9. **Reactor edge function deployed** — AC: manually INSERTing a test signal row produces `convergence_*` columns populated and, if band = Immediate, an `alerts` row inserted — all within 2s. A curated 20-signal batch test matches the convergence output of the same batch run through v1 convergence_engine.
10. **Fan-out edge function deployed** (email-gating revised 2026-04-20) — AC has two parts:
    (a) manually INSERTing a `candidate_events` row with `event_type='created'` (pre-populated `candidates` row for the FK) produces a Resend sandbox delivery to one test recipient + an `alert_deliveries` row with `status='sent'`;
    (b) manually INSERTing an `alerts` row produces NO email and NO `alert_deliveries` row (raw alerts are audit + Realtime only).
11. **End-to-end smoke test** — a recent real EDGAR filing → signal → reactor → thesis_writer skill (manually triggered once for test speed; skip the 15-min cron wait) → `candidate_events.created` → fanout → email in Pedro's test inbox. AC: timestamp evidence in logs + inbox receipt. Wall-clock target: ≤ 5 min excluding the thesis_writer cron wait, ≤ 20 min p95 including it in steady state.
12. **Scanner-health card (minimum viable)** — a read-only HTML at `/functions/v1/scanner-health` that returns JSON of each scanner's `last_run_utc`, `last_run_status`, `last_run_signals` from the `scanners` table. AC: `curl` returns fresh data post-test-run.

Exit criterion for Phase 1 per PRD: Pedro sees real EDGAR signals landing in Supabase within 5 minutes of a test filing. Tasks 8 + 11 cover this.

---

## 12. Decisions locked (PRD §11)

All items in this section were open at spec drafting time and were locked by Pedro on 2026-04-20. The table retains the original trade-off context; the final column shows what Pedro picked and any follow-on action.

| Decision | Locked answer | Implication |
|---|---|---|
| Modal region | **EU-West-3 (Paris)** — Supabase project region confirmed 2026-04-20. Modal scanner workers will co-locate in Paris to minimize DB round-trips. | Adds ~80-150ms per US-API request vs us-east-1. The 35s EDGAR soft budget (D-018) must be validated under EU-West latency in Phase 1; bump `timeout_soft_s` on edgar_filing_monitor, takeover_candidate_scanner, sec_enforcement_scanner if the P1 smoke test runs hot. |
| Resend sending domain | **`alerts.solutz.com`** (temporary) | Pedro provides DNS access to `solutz.com` for `resend._domainkey.alerts.solutz.com` TXT + SPF + DMARC records. Flagged as temporary — migration path to a permanent domain deferred. |
| Supabase project tier | **Pro** ($25/mo) | Webhook rates + database headroom guaranteed. No further action. |
| Vercel project + branch model | **`main → production`, PRs → preview** | Standard. Protected main; review required before merge. |
| Email template for Immediate alerts | **Appendix D stub as-is** | Ship Phase 2 fan-out with the stub template. Iterate wording after real sends. |
| RLS on `annotations` | **Strict per-user** (`auth.uid() = user_id` on all ops) | No cross-user annotations. Dashboard UX won't offer a shared-notes view. |
| Q-017 CourtListener + Q-019 OpenDART tokens | **Defer both; preserve graceful `auth_required`** | Scanners ship yellow on health card until tokens provided. No v2 scope change. |
| Reporting PDF distribution | **Email with 7-day signed URL + dashboard copy** | Signed URL auto-expires; canonical copy lives in dashboard reports panel. Matches PRD default. |
| Historical signal import scope | **Minimal — only active-candidate-linked signals** | Import only signals referenced by the 5 active dossiers + 3 archived-post-edge (~tens of rows). **Drops the 565 UNKNOWN rescue table from §9.2 and shrinks the §10.4 replay test.** Both sections have been edited in-place. |
| 6-vs-5 scoring profiles | **Seed 6; update PRD §2** | `takeover_candidate` seeded at `rubric_version=1`. PRD §2 needs a one-line edit to list 6 profiles (tracked as a PRD-update follow-up, not blocking Phase 1). |
| Thesis authoring ownership | **Claude drafts on behalf of all users, as a Cowork scheduled task running under Pedro's account (skill file `.claude/skills/thesis_writer.md`, §7.4). Polls `thesis_jobs` queue every 15 min. Immediate band only. One corrective retry on gate-fail, else DLQ. 15/day soft cap.** Locked 2026-04-20. **Architecture revised 2026-04-20 post-Phase-3:** originally specced as a Modal function calling a Claude app routine via API; moved to a Claude skill under Pedro's Claude plan. Rationale: no `ANTHROPIC_API_KEY` secret to manage, full skill tool access (WebSearch, Supabase MCP, Bash), auditable session transcripts in the Claude Code UI. | (a) `/functions/v1/candidate-gate` edge function removed (§6.3). (b) Two new tables: `thesis_jobs`, `thesis_drafting_failures`; `gate_rejections` removed. (c) Reactor enqueues a thesis job alongside the alert INSERT; the two paths are independent so alert SLA never blocks on drafting. (d) Dashboard candidate surface inverts to a review queue — no authoring form. (e) `candidate_events` gains `thesis_drafted_by_claude`, `thesis_approved_by_user`. (f) PRD §3 / §14 amended to except thesis drafting from the "no specialist agents in v2" exclusion. (g) Skill specialization (system prompt, framework docs, exemplars, anti-patterns from `rejected_pending_thesis/`) is configured in `.claude/skills/thesis_writer.md`, not out-of-band. |
| Thesis quality gate (v1 5-field → v2 7-section) | **Added 2026-04-20 post-approval** after skill-coverage audit against the archived `deep-dives` skill. v2 `assess_thesis_v2` extends v1's 5-field check with **steelman** (min 120 chars + boilerplate regex), **web_research** (≥3 cited entries with retrieval timestamps; ≥1 non-strengthening lean), and **reasoning-tag coverage** (≥5 total `[verified]`/`[inferred]`/`[speculated]` tags across situation + why_underpriced + steelman; ≥1 `[verified]` anchor; ≤2 load-bearing-claim tag-violations tolerated). | Closes the ITRK-archetype failure mode in `candidates/rejected_pending_thesis/` (correct prose, no asymmetry). `assess_thesis_v1` retained for historical dossier import (§9.3). New required routine output fields: `steelman`, `web_research[]`. Enforced inside candidate_gate_service (§7.1), not split across surfaces. |
| Candidate aging + kill-condition evaluation | **Added 2026-04-20 post-approval.** Cowork scheduled task `candidate_aging` (skill file `.claude/skills/candidate_aging.md`, §7.5) runs daily 06:00 UTC; mechanical date-based transitions first (60d watch → archive, 30d active with no near catalyst → watch, elapsed catalyst flag), then Claude-mediated kill-condition evaluation against recent 14/30d signals for each still-undecided entity. **Architecture revised post-Phase-3:** originally specced as a Modal function + second Claude app routine; moved to a Claude skill under Pedro's account, same rationale as §7.4. | (a) New `candidates.kill_conditions` JSONB column — structured list of `{id, description, observable, date_bound, status}`. (b) New `candidates.next_catalyst_date` + `next_catalyst_window` (daterange) — parsed from thesis on promotion, consumed by aging. (c) New `candidates.last_aging_evaluated_at` — drives dashboard "last check" column and the eligibility query. (d) New `candidate_aging_failures` DLQ table (now also linked to `operator_flags(kind='aging_stuck')` after 3 consecutive failures). (e) Fan-out edge function extended to email on `candidate_events.event_type='state_changed'` with `new_state ∈ {killed, delivered}` — Appendix D has a state-change email template. (f) Integrity defense: skill's `triggered` claim is verified against the kill_condition's `observable.search_pattern` via Bash regex before committing; hallucinated triggers get logged (`error_kind='hallucinated_trigger'`) and downgraded. |
| Observability & maintenance functions | **Added 2026-04-20 post-approval.** Four scheduled Modal functions (§7.6) replace the v1 `maintenance` skill's mechanical sweeps: `translation_health` (daily 02:00Z), `scanner_probe` (6h :15), `convergence_qa` (daily 03:00Z), `litigation_baselines_refresh` (weekly Sun 04:00Z). `reporting_weekly` gains an integrity-sweep pre-step (§7.7). | (a) New table `operator_flags` — common structured surface replacing v1's `OPEN_QUESTIONS.md`. Producers UPSERT on `(source, kind, subject)`; dashboard renders open flags sorted by severity. (b) `scanners` gains `last_probe_at`, `last_probe_status`, `last_probe_latency_ms` columns. (c) Convergence QA requires a pure-Python `rubric_engine.convergence_reference()` implementation distinct from the reactor's SQL-based path — an audit reference. (d) Scanner endpoint auto-repair is explicitly NOT carried forward from v1 — v2 treats scanner code/config as human-reviewed; `scanner_probe` only flags drift, it does not rewrite endpoints. |
| Scope calls from skill audit | **Pedro resolved 2026-04-20:** kill the 4h digest PDF (Immediate-band email + dashboard subsume), kill `candidates_index.json` (Supabase API + `candidates` table subsume), kill DOCX litigation briefs (markdown dossier + weekly PDF cover the use case), preserve litigation baseline refresh jobs as §7.6.4 (scanners remain operational in v2). | No spec-tables change beyond (a) the reporting path now has only one output artifact type — weekly PDF + email digest — and (b) `reporting_weekly` Modal function retains its Sunday 12:00 UTC schedule with no 4h sibling. DOCX generation code path is not ported. `candidates_index.json` is not produced. |
| Market-cap floor canonical value | **$215M USD confirmed 2026-04-20.** Matches PRD §3 and live INSTRUCTIONS.md. The $300M figure in archived non_us CLAUDE.md is legacy (pre-unification) and does NOT apply to v2. | Canonical value seeded into every `scanners.config` row as `market_cap_floor_usd_mm: 215` by `migrations/seed_registry.py` (§9.1). Per-scanner overrides remain possible through the JSONB (e.g., if Brazil CVM ever needs a floor raise for liquidity reasons); default is 215. Scanner code reads from its own config row at startup; no hard-coded constant. |
| Self-improvement posture — detect, don't correct (Phase 1d) | **Added 2026-04-21:** Phase 1d accuracy-loop auditors (`precision_auditor`, `timing_auditor`, `challenger_retro` — §7.6.5/6/7) are **passive miscalibration detectors only**. They surface drift via `accuracy_metrics` + `operator_flags`; they never auto-tune rubric weights, auto-edit challenger prompts, auto-adjust scanner thresholds, or auto-recompute `phase3_base_rates`. Every feedback loop still terminates at Pedro's manual edit. Rationale: determinism + auditability are load-bearing for the two-gate model; auto-tuning introduces drift on the tuner itself with no referee. v2+ can revisit once the MVP has shipped enough labeled outcomes to quantify whether auto-correction would beat hand-tuning. | (a) New migration `20260425000000_accuracy_metrics.sql` with sparse-column `accuracy_metrics` table (time-series rollup for all three auditors). (b) `precision_auditor` + `timing_auditor` added to `modal_workers/observability.py`, wired to `dispatch_observability` Sunday 02:15 UTC branch. (c) `challenger_retro` as Cowork skill at `.claude/skills/challenger_retro.md` (Modal can't invoke Cowork routines; has to be Cowork-side). Uses 10 challenger invocations/week, stratified on `outcome_label`. (d) Five new `operator_flag` kinds: `precision_drift`, `post_edge_miss_spike`, `dead_catalyst_spike`, `confidence_noise`, `band_collapse`, `auto_cap_inverted`, `timing_drift`, `emission_too_late`, `decay_anomaly`, `challenger_retro_miss`, `challenger_retro_pass_through`, `challenger_retro_timing_blindspot`. None collide with existing kinds. (e) Non-goals remain explicit: no rubric weight updates, no prompt auto-tuning, no scanner threshold learning, no auto-refresh of `phase3_base_rates`. |

Additional surfaced conflicts from exploration (retained for reference):

- **Convergence semantic shift (v1 async post-process → v2 per-INSERT)**. Same output, different latency profile. Reactor handles cross-updates to prior group-winners.
- **`takeover_candidate.post_edge_disqualified` returns `band='discard'`**. Signal row is still written for audit but never eligible for alerts. Explicit in `apply_auto_caps` (run_post_scan.py, ~line 153). v2 preserves this. Dashboard should surface discards as an audit view, not as alertable items.
- **`Scoring engine/` folder D-034 threshold shift (30/20/10) is not live Conan policy.** The Modal + Supabase runtime remains 35/25/15 in `rubric_engine.py` and `_shared/convergence.ts`; any future adoption must land as an explicit rubric/policy version change, not as a silent parity assumption.
- **Missing-dimension semantics intentionally diverged from the legacy file-bus copy.** v2 returns `score=NULL, band=NULL` when required dimensions are absent; it does not default missing dims to 3. This is a bug fix, not a regression.
- **Folder-only rubric criteria were reviewed rather than blindly ported.** The takeover-candidate sector-consolidation cap is deferred until the live scanner emits usable evidence for it; the short-positioning multi-regulator wording has now been aligned to the methodology's 2+ regulator bump while keeping the rest of the v2 short-positioning model intact.

### Methodology-derived additions adopted 2026-04-26

- **Short-positioning direct add adopted.** `dim_estimator.py` now treats the same name appearing across **2+ regulators** as a crowding bump, matching the methodology reference rather than the stricter temporary 3+ v2 threshold.
- **Additive enrichment expanded, still non-scoring.**
  - `signals.extensions.legal_enrichment` now includes structured `case_family`, `procedural_stage`, `procedural_stage_confidence`, `resolution_timeline_bucket`, `merits_hint`, `materiality_hint`, and `ticker_hint_present` on top of the existing severity/likelihood/risk fields.
  - `signals.extensions.biotech_enrichment` now includes `single_primary_endpoint`, `hard_endpoint_present`, `surrogate_endpoint_present`, `meaningful_enrollment`, `industry_sponsored`, `adcom_support_ratio`, `readout_timeline_bucket`, `ev_inputs_complete`, and `expected_value_pct` when raw inputs allow.
- **Scanner payload upgrades landed where current evidence supports them.**
  - `courtlistener_scanner.py` and `sec_enforcement_scanner.py` now emit litigation-stage hints directly in `raw_payload` (`case_family`, `procedural_stage`, `procedural_stage_confidence`, `resolution_timeline_bucket`, `ticker_hint_present`) so future scoring/resolver work can rely on structured data instead of only free text.
  - `pre_phase3_readout_scanner.py` now emits trial-shape helpers (`days_until_readout`, `single_primary_endpoint`, `industry_sponsored`, `meaningful_enrollment`, `matched_indications`) alongside the existing approval-probability/base-rate fields.
  - `fda_pdufa_pipeline.py` now emits `adcom_support_ratio`, `trial_status`, and `approval_history_count` so later binary-catalyst scoring can consume auditable support fields without reparsing nested enrichment blobs.

### Methodology-derived payload backlog retained for future work

- **`merger_arb`** still needs real structured deal economics before native mechanical scoring is honest at ingest: `spread_pct`, `annualized_return_pct`, expected-close timing, certainty/break-risk flags, and `adv_usd`.
- **`activist_governance`** still needs richer identity/catalyst fields to graduate from resolver-only scoring: `filer_name`, `filer_cik`, `subject_cik`, `next_catalyst_type`, `next_catalyst_date`, activist tiering, and `adv_usd`.
- **`binary_catalyst`** still lacks consistently auditable EV inputs on the FDA/PDUFA path (`upside_pct`, `downside_pct`, fully-modeled `approval_probability`), so `binary_catalyst.ev_floor` remains much more meaningful on some sources than others.
- **`litigation`** still needs damages/exposure, issuer-match confidence, and procedural-stage fields beyond the current deterministic hints before it can move from resolver-dominant scoring toward structured native scoring.
- **`takeover_candidate`** still lacks `valuation_cushion_pct`, `adv_usd`, and a stable sector-consolidation signal; the methodology's deferred watchlist cap for active-consolidation sectors remains intentionally unimplemented until those payloads exist.
- **EU-West + US-API latency budget**. Consequence of the locked Modal region. Phase 1 task 8 (edgar port + 35s wall-clock AC) explicitly verifies this; if the budget is tight, the fix is `timeout_soft_s` bump in the `scanners` row, not a scanner code change.

---

## Appendix A — Full DDL draft

```sql
-- Extensions
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- Shared enums
CREATE TYPE signal_band AS ENUM ('immediate','watchlist','archive','discard');
CREATE TYPE candidate_state AS ENUM ('watch','active','killed','delivered');

-- set_updated_at helper
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END; $$;

-- Registry
CREATE TABLE sources (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE,
  kind text NOT NULL CHECK (kind IN ('edgar','esma','fda','lse','tdnet','asx','sedar','hkex','kind','bse_nse','cvm','bmv','courtlistener','sec_enforcement','clinicaltrials')),
  base_url text,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE sources ENABLE ROW LEVEL SECURITY;
CREATE POLICY sources_select ON sources FOR SELECT TO authenticated USING (true);

CREATE TABLE scanners (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE,
  tool_path text,
  status text NOT NULL DEFAULT 'operational' CHECK (status IN ('operational','planned','deprecated','experimental')),
  geography text,
  cadence text NOT NULL CHECK (cadence IN ('3h','daily','weekly','on_demand')),
  default_scoring_profile text NOT NULL,
  signal_type_profile_map jsonb NOT NULL DEFAULT '{}'::jsonb,
  endpoints jsonb NOT NULL DEFAULT '{}'::jsonb,
  timeout_soft_s int NOT NULL DEFAULT 60,
  timeout_hard_s int NOT NULL DEFAULT 120,
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_run_utc timestamptz,
  last_run_status text,
  last_run_signals int,
  last_probe_at timestamptz,
  last_probe_status text CHECK (last_probe_status IS NULL OR last_probe_status IN ('ok','fallback','drift','content_shape_drift','timeout','error')),
  last_probe_latency_ms int,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX scanners_status_idx ON scanners(status);
CREATE TRIGGER scanners_updated BEFORE UPDATE ON scanners FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE scanners ENABLE ROW LEVEL SECURITY;
CREATE POLICY scanners_select ON scanners FOR SELECT TO authenticated USING (true);

CREATE TABLE rubrics (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile text NOT NULL,
  rubric_version int NOT NULL,
  dimension_weights jsonb NOT NULL,
  effective_at timestamptz NOT NULL DEFAULT now(),
  superseded_at timestamptz,
  notes text,
  UNIQUE (profile, rubric_version)
);
CREATE INDEX rubrics_active_idx ON rubrics(profile) WHERE superseded_at IS NULL;
ALTER TABLE rubrics ENABLE ROW LEVEL SECURITY;
CREATE POLICY rubrics_select ON rubrics FOR SELECT TO authenticated USING (true);

CREATE TABLE pe_filer_allowlist (
  filer_name text PRIMARY KEY,
  cik text,
  filer_type text NOT NULL CHECK (filer_type IN ('pe','activist_crossover')),
  notes text,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE pe_filer_allowlist ENABLE ROW LEVEL SECURITY;
CREATE POLICY pe_filer_select ON pe_filer_allowlist FOR SELECT TO authenticated USING (true);

CREATE TABLE phase3_base_rates (
  indication text PRIMARY KEY,
  phase3_to_approval numeric(4,3) NOT NULL CHECK (phase3_to_approval BETWEEN 0 AND 1),
  trial_design_adjustments jsonb NOT NULL DEFAULT '{}'::jsonb,
  notes text,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER phase3_updated BEFORE UPDATE ON phase3_base_rates FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE phase3_base_rates ENABLE ROW LEVEL SECURITY;
CREATE POLICY phase3_select ON phase3_base_rates FOR SELECT TO authenticated USING (true);

-- Entity graph
CREATE TABLE entities (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  issuer_figi text UNIQUE,
  name text NOT NULL,
  primary_ticker text,
  primary_mic text,
  country text,
  market_cap_usd numeric(18,2),
  market_cap_as_of date,
  extensions jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX entities_ticker_mic_idx ON entities(primary_ticker, primary_mic);
CREATE TRIGGER entities_updated BEFORE UPDATE ON entities FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE entities ENABLE ROW LEVEL SECURITY;
CREATE POLICY entities_select ON entities FOR SELECT TO authenticated USING (true);

CREATE TABLE entity_identifiers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_id uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  id_type text NOT NULL CHECK (id_type IN ('ticker_mic','codigo_cvm','id_empresa_biva','stock_code','cik','cnpj','isin','name_normalized')),
  id_value text NOT NULL,
  priority smallint NOT NULL DEFAULT 100,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id_type, id_value)
);
CREATE INDEX entity_identifiers_entity_idx ON entity_identifiers(entity_id);
ALTER TABLE entity_identifiers ENABLE ROW LEVEL SECURITY;
CREATE POLICY entity_identifiers_select ON entity_identifiers FOR SELECT TO authenticated USING (true);

-- Raw evidence
CREATE TABLE filings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id uuid NOT NULL REFERENCES sources(id),
  entity_id uuid REFERENCES entities(id),
  source_content_hash text NOT NULL UNIQUE,
  storage_path text NOT NULL,
  url text,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz,
  filing_type text,
  extensions jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX filings_entity_published_idx ON filings(entity_id, published_at DESC);
ALTER TABLE filings ENABLE ROW LEVEL SECURITY;
CREATE POLICY filings_select ON filings FOR SELECT TO authenticated USING (true);

-- Pipeline state
CREATE TABLE scanner_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scanner_id uuid NOT NULL REFERENCES scanners(id),
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  status text NOT NULL CHECK (status IN ('running','ok','error','auth_required','partial','timeout')),
  signals_emitted int NOT NULL DEFAULT 0,
  fetched_records int,
  errors jsonb NOT NULL DEFAULT '[]'::jsonb,
  modal_invocation_id text,
  raw_log_path text
);
CREATE INDEX scanner_runs_scanner_started_idx ON scanner_runs(scanner_id, started_at DESC);
ALTER TABLE scanner_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY scanner_runs_select ON scanner_runs FOR SELECT TO authenticated USING (true);

CREATE TABLE signals (
  signal_id text PRIMARY KEY,
  entity_id uuid REFERENCES entities(id),
  issuer_figi text,
  scanner_id uuid REFERENCES scanners(id),
  scanner_run_id uuid REFERENCES scanner_runs(id),
  scoring_profile text NOT NULL,
  rubric_version_id uuid NOT NULL REFERENCES rubrics(id),
  source_content_hash text NOT NULL,
  source_url text,
  source_date timestamptz NOT NULL,
  scan_date timestamptz NOT NULL,
  signal_type text NOT NULL,
  thesis_direction text CHECK (thesis_direction IN ('long','short','neutral')),
  strength_estimate smallint CHECK (strength_estimate BETWEEN 1 AND 5),
  imported boolean NOT NULL DEFAULT false,
  dimensions jsonb NOT NULL DEFAULT '{}'::jsonb,
  score numeric(5,2) NOT NULL,
  band signal_band NOT NULL,
  auto_caps_triggered text[] NOT NULL DEFAULT '{}',
  convergence_key text,
  convergence_bonus smallint NOT NULL DEFAULT 0 CHECK (convergence_bonus IN (0,5,10)),
  score_with_bonus numeric(5,2),
  band_with_bonus signal_band,
  convergence_evaluated_at timestamptz,
  raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  extensions jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_content_hash, scoring_profile)
);
CREATE INDEX signals_entity_scan_idx ON signals(entity_id, scan_date DESC);
CREATE INDEX signals_issuer_figi_scan_idx ON signals(issuer_figi, scan_date DESC);
CREATE INDEX signals_convergence_key_idx ON signals(convergence_key, scan_date DESC);
CREATE INDEX signals_immediate_idx ON signals(scan_date DESC) WHERE band_with_bonus = 'immediate';
ALTER TABLE signals ENABLE ROW LEVEL SECURITY;
CREATE POLICY signals_select ON signals FOR SELECT TO authenticated USING (true);

CREATE TABLE candidates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ticker text NOT NULL,
  mic text,
  entity_id uuid REFERENCES entities(id),
  state candidate_state NOT NULL DEFAULT 'watch',
  scoring_profile text,
  current_score numeric(5,2),
  current_band signal_band,
  dossier_markdown text,
  dossier_storage_path text,
  thesis_approved_at timestamptz,
  kill_conditions jsonb NOT NULL DEFAULT '[]'::jsonb,
  next_catalyst_date date,
  next_catalyst_window daterange,
  last_aging_evaluated_at timestamptz,
  extensions jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (ticker, mic),
  CONSTRAINT candidates_catalyst_exactly_one CHECK (
    (next_catalyst_date IS NULL) <> (next_catalyst_window IS NULL)
    OR (next_catalyst_date IS NULL AND next_catalyst_window IS NULL)
  ),
  CONSTRAINT candidates_kill_conditions_is_array CHECK (jsonb_typeof(kill_conditions) = 'array')
);
CREATE INDEX candidates_state_score_idx ON candidates(state, current_score DESC)
  WHERE state IN ('active','watch');
CREATE INDEX candidates_catalyst_date_idx ON candidates(next_catalyst_date)
  WHERE next_catalyst_date IS NOT NULL AND state IN ('active','watch');
CREATE INDEX candidates_aging_due_idx ON candidates(last_aging_evaluated_at NULLS FIRST)
  WHERE state IN ('active','watch');
CREATE TRIGGER candidates_updated BEFORE UPDATE ON candidates FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE candidates ENABLE ROW LEVEL SECURITY;
CREATE POLICY candidates_select ON candidates FOR SELECT TO authenticated USING (true);

CREATE TABLE candidate_aging_failures (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_id uuid NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
  attempt_at timestamptz NOT NULL DEFAULT now(),
  error_kind text NOT NULL CHECK (error_kind IN (
    'routine_error','routine_declined','hallucinated_trigger','quota_exhausted','gate_mismatch','other'
  )),
  error_message text,
  routine_output jsonb,
  consecutive_failures smallint NOT NULL DEFAULT 1
);
CREATE INDEX candidate_aging_failures_recent_idx
  ON candidate_aging_failures(candidate_id, attempt_at DESC);
ALTER TABLE candidate_aging_failures ENABLE ROW LEVEL SECURITY;
CREATE POLICY candidate_aging_failures_select
  ON candidate_aging_failures FOR SELECT TO authenticated USING (true);

CREATE TABLE operator_flags (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  severity text NOT NULL CHECK (severity IN ('info','warn','critical')),
  source text NOT NULL CHECK (source IN (
    'translation_health','scanner_probe','convergence_qa','candidate_aging',
    'thesis_writer','reactor','reporting_weekly','litigation_baselines','manual'
  )),
  kind text NOT NULL,
  scanner_id uuid REFERENCES scanners(id),
  entity_id uuid REFERENCES entities(id),
  signal_id text REFERENCES signals(signal_id),
  candidate_id uuid REFERENCES candidates(id),
  title text NOT NULL,
  body text,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  resolved_at timestamptz,
  resolved_by uuid REFERENCES auth.users(id),
  resolved_note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
-- Partial unique prevents duplicate open flags for the same (source, kind, subject) tuple.
-- Producers use INSERT … ON CONFLICT DO UPDATE to bump `evidence` instead of inserting duplicates.
CREATE UNIQUE INDEX operator_flags_open_uniq
  ON operator_flags (
    source,
    kind,
    coalesce(scanner_id::text, ''),
    coalesce(entity_id::text, ''),
    coalesce(signal_id, ''),
    coalesce(candidate_id::text, '')
  )
  WHERE resolved_at IS NULL;
CREATE INDEX operator_flags_open_idx
  ON operator_flags(severity DESC, created_at DESC) WHERE resolved_at IS NULL;
CREATE TRIGGER operator_flags_updated BEFORE UPDATE ON operator_flags FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE operator_flags ENABLE ROW LEVEL SECURITY;
CREATE POLICY operator_flags_select ON operator_flags FOR SELECT TO authenticated USING (true);
CREATE POLICY operator_flags_resolve ON operator_flags FOR UPDATE TO authenticated
  USING (true) WITH CHECK (resolved_by = auth.uid());

CREATE TABLE candidate_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_id uuid NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
  event_type text NOT NULL CHECK (event_type IN ('created','state_changed','scored','note_added','thesis_drafted_by_claude','thesis_updated','thesis_approved_by_user','convergence','gate_rejected')),
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  user_id uuid REFERENCES auth.users(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX candidate_events_candidate_idx ON candidate_events(candidate_id, created_at DESC);
ALTER TABLE candidate_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY candidate_events_select ON candidate_events FOR SELECT TO authenticated USING (true);

CREATE TABLE outcomes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_id uuid NOT NULL REFERENCES candidates(id),
  outcome_type text NOT NULL CHECK (outcome_type IN ('delivered','killed','expired')),
  realized_return numeric(6,3),
  notes text,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE outcomes ENABLE ROW LEVEL SECURITY;
CREATE POLICY outcomes_select ON outcomes FOR SELECT TO authenticated USING (true);

CREATE TABLE alerts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_id uuid REFERENCES entities(id),
  signal_id text NOT NULL REFERENCES signals(signal_id),
  signal_fingerprint text NOT NULL,
  day_utc date NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::date,
  email_subject text,
  email_body_storage_path text,
  dispatched_at timestamptz,
  dispatched_to text[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (entity_id, signal_fingerprint, day_utc)
);
CREATE INDEX alerts_created_idx ON alerts(created_at DESC);
ALTER TABLE alerts ENABLE ROW LEVEL SECURITY;
CREATE POLICY alerts_select ON alerts FOR SELECT TO authenticated USING (true);

CREATE TABLE alert_deliveries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  alert_id uuid NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
  channel text NOT NULL CHECK (channel IN ('email','realtime')),
  target text NOT NULL,
  status text NOT NULL CHECK (status IN ('queued','sent','failed','bounced')),
  resend_message_id text,
  response_body jsonb,
  attempt_count smallint NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE alert_deliveries ENABLE ROW LEVEL SECURITY;
CREATE POLICY alert_deliveries_select ON alert_deliveries FOR SELECT TO authenticated USING (true);

CREATE TABLE failed_reactor_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  signal_id text,
  payload jsonb NOT NULL,
  error_message text NOT NULL,
  attempt_count smallint NOT NULL DEFAULT 1,
  last_attempted_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz
);
ALTER TABLE failed_reactor_events ENABLE ROW LEVEL SECURITY;
-- service_role only; no authenticated policies.

-- gate_rejections removed — the /candidate-gate edge function is deleted (§6.3).
-- Thesis failures now land in thesis_drafting_failures (below); user-submitted
-- thesis payloads no longer exist in the v2 surface.

CREATE TABLE thesis_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  signal_id text NOT NULL REFERENCES signals(signal_id),
  alert_id uuid REFERENCES alerts(id),
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','drafting','gate_failed_retrying','promoted','dlq')),
  attempt_count smallint NOT NULL DEFAULT 0,
  routine_run_ids text[] NOT NULL DEFAULT '{}',
  drafted_thesis jsonb,
  gate_reasons text[],
  candidate_id uuid REFERENCES candidates(id),
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (signal_id)
);
CREATE INDEX thesis_jobs_status_idx ON thesis_jobs(status, created_at);
CREATE TRIGGER thesis_jobs_updated BEFORE UPDATE ON thesis_jobs FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE thesis_jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY thesis_jobs_select ON thesis_jobs FOR SELECT TO authenticated USING (true);

CREATE TABLE thesis_drafting_failures (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  thesis_job_id uuid NOT NULL REFERENCES thesis_jobs(id),
  signal_id text NOT NULL REFERENCES signals(signal_id),
  final_reasons text[] NOT NULL,
  all_drafts jsonb NOT NULL,
  alerted boolean NOT NULL DEFAULT true,
  resolved_at timestamptz,
  resolved_candidate_id uuid REFERENCES candidates(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX thesis_drafting_failures_unresolved_idx
  ON thesis_drafting_failures(created_at DESC) WHERE resolved_at IS NULL;
ALTER TABLE thesis_drafting_failures ENABLE ROW LEVEL SECURITY;
CREATE POLICY thesis_drafting_failures_select
  ON thesis_drafting_failures FOR SELECT TO authenticated USING (true);

-- Human layer
CREATE TABLE watchlists (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  name text NOT NULL,
  filter jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX watchlists_user_idx ON watchlists(user_id);
CREATE TRIGGER watchlists_updated BEFORE UPDATE ON watchlists FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE watchlists ENABLE ROW LEVEL SECURITY;
CREATE POLICY watchlists_user_rw ON watchlists FOR ALL TO authenticated
  USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

CREATE TABLE notifications_prefs (
  user_id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  email_on_immediate boolean NOT NULL DEFAULT true,
  email_weekly_report boolean NOT NULL DEFAULT true,
  realtime_channels text[] NOT NULL DEFAULT '{signals,alerts}',
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER notifications_prefs_updated BEFORE UPDATE ON notifications_prefs FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE notifications_prefs ENABLE ROW LEVEL SECURITY;
CREATE POLICY notifications_prefs_user_rw ON notifications_prefs FOR ALL TO authenticated
  USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

CREATE TABLE annotations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  candidate_id uuid NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
  body text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX annotations_user_candidate_idx ON annotations(user_id, candidate_id);
CREATE TRIGGER annotations_updated BEFORE UPDATE ON annotations FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE annotations ENABLE ROW LEVEL SECURITY;
CREATE POLICY annotations_user_rw ON annotations FOR ALL TO authenticated
  USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

CREATE TABLE candidate_rationales (
  ticker text PRIMARY KEY,
  one_liner text NOT NULL,
  hypothesis text NOT NULL,
  thesis text NOT NULL,
  expected_outcome text NOT NULL,
  price_targets jsonb NOT NULL,
  time_sensitivity text NOT NULL,
  kill_watch text NOT NULL,
  catalyst_date_iso date,
  archived boolean NOT NULL DEFAULT false,
  archived_meta jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER candidate_rationales_updated BEFORE UPDATE ON candidate_rationales FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE candidate_rationales ENABLE ROW LEVEL SECURITY;
CREATE POLICY candidate_rationales_select ON candidate_rationales FOR SELECT TO authenticated USING (true);

-- Storage buckets (created via Supabase Studio or supabase CLI, not SQL)
-- filings, scanner-caches, reports — all private
```

---

## Appendix B — Webhook payload samples

### Reactor request (from Supabase)

```http
POST /functions/v1/reactor HTTP/1.1
Host: <project>.supabase.co
Content-Type: application/json
x-supabase-webhook-secret: <shared_secret>

{
  "type": "INSERT",
  "table": "signals",
  "schema": "public",
  "record": { "signal_id": "…", "issuer_figi": "…", … },
  "old_record": null
}
```

### Reactor response

```json
{
  "processed": true,
  "convergence_key": "figi:BBG000B9XRY4",
  "convergence_bonus": 5,
  "score_with_bonus": 35.0,
  "band_with_bonus": "immediate",
  "alert_inserted": true,
  "thesis_job_enqueued": true,
  "cross_updates": ["edgar_13d_20260416_RPAY_012"]
}
```

### Fan-out request

```http
POST /functions/v1/fanout HTTP/1.1
x-supabase-webhook-secret: <shared_secret>

{
  "type": "INSERT",
  "table": "alerts",
  "schema": "public",
  "record": { "id": "…", "signal_id": "…", "signal_fingerprint": "…", "day_utc": "2026-04-20" },
  "old_record": null
}
```

### Fan-out response

```json
{
  "processed": true,
  "email_recipients": 2,
  "realtime_channels": ["alerts", "entity:uuid"],
  "resend_message_ids": ["msg_aaaa", "msg_bbbb"]
}
```

---

## Appendix C — Modal function skeleton

Full example: `modal_workers/scanners/edgar_filing_monitor.py`

```python
import modal
from modal_workers.shared.scanner_base import run_scanner, ScannerResult, ScannerConfig
from modal_workers.shared.supabase_client import SupabaseClient
from tools.edgar_filing_monitor import scan as legacy_scan   # preserved verbatim

app = modal.App("conan-v2")
image = (
    modal.Image.debian_slim()
    .pip_install("httpx", "postgrest", "pydantic", "requests")
    .add_local_python_source("modal_workers")
    .add_local_python_source("tools")
)

scanner_secrets = modal.Secret.from_name("scanner-secrets")
supabase_secrets = modal.Secret.from_name("supabase-secrets")

@app.function(
    image=image,
    schedule=modal.Period(hours=3),
    timeout=120,
    secrets=[scanner_secrets, supabase_secrets],
)
def edgar_filing_monitor() -> ScannerResult:
    def scan(cfg: ScannerConfig) -> ScannerResult:
        raw = legacy_scan(
            endpoints=cfg.endpoints,
            signal_type_profile_map=cfg.signal_type_profile_map,
            config=cfg.config,
            http_user_agent=cfg.secrets["SEC_USER_AGENT"],
        )
        return ScannerResult(
            scanner="edgar_filing_monitor",
            status="ok",
            signals=raw.signals,
            warnings=raw.warnings,
            fetched_records=raw.fetched_records,
        )
    return run_scanner("edgar_filing_monitor", scan)
```

Abbreviated template for the other 16 scanners — just substitute the scanner name, schedule, timeout, and the imported `legacy_scan`.

---

## Appendix D — Email template draft

**Gating matrix (locked 2026-04-20, memory `email_alert_gating.md`):**

| Template | Trigger | Sends email today? |
|---|---|---|
| Immediate-band alert (below) | `alerts.INSERT` | **NO** — `alerts_insert_wh` trigger was DROPPED in migration `22_email_gating_pre_edge_only`. Template retained for reference and for the dashboard's "render the email we would have sent" preview. |
| Pre-edge promotion (see `supabase/functions/fanout/index.ts::renderPromotion*`) | `candidate_events.INSERT WHERE event_type ∈ {'created','thesis_drafted_by_claude'}` | **YES** — primary path. AI-reviewed candidate just landed. |
| Candidate state-change (below, added with §7.5 aging) | `candidate_events.INSERT WHERE event_type='state_changed' AND payload.to ∈ {killed,delivered}` | **NO by default** — behind feature flag `EMAIL_STATE_CHANGE_KILLED_DELIVERED` (default `false` per Pedro's Q3 answer 2026-04-20). Set env var `true` to re-enable. |

### Immediate-band alert (subject + HTML + text)

**Subject:** `[IMMEDIATE] {TICKER}.{MIC} — {signal_type} — {band_with_bonus}`

**HTML body (simplified):**

```html
<!DOCTYPE html>
<html><body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 640px; margin: 0 auto; padding: 24px;">
  <h1 style="color:#8b0000; margin-bottom: 4px;">{TICKER}.{MIC} — {one_liner}</h1>
  <p style="color:#555; margin-top:0;">{company_name} · {geography} · {scoring_profile}</p>

  <table style="width:100%; border-collapse:collapse; margin: 16px 0;">
    <tr><td>Band</td><td><strong>{band_with_bonus}</strong> (score {score_with_bonus} = {score} + {convergence_bonus})</td></tr>
    <tr><td>Signal type</td><td>{signal_type}</td></tr>
    <tr><td>Source</td><td><a href="{source_url}">{source_url}</a></td></tr>
    <tr><td>Catalyst</td><td>{catalyst_date_iso}</td></tr>
  </table>

  <h3>Why this is immediate</h3>
  <p>{thesis_summary}</p>

  <h3>Kill watch</h3>
  <p>{kill_watch}</p>

  <p style="margin-top:24px;"><a href="{dashboard_url}/signals/{signal_id}" style="background:#111; color:#fff; padding:10px 16px; text-decoration:none;">Open in dashboard</a></p>
</body></html>
```

**Plain-text fallback:**

```
[IMMEDIATE] {TICKER}.{MIC} — {signal_type}

{one_liner}

Band: {band_with_bonus} (score {score_with_bonus} = {score} + {convergence_bonus})
Source: {source_url}
Catalyst: {catalyst_date_iso}

Why this is immediate:
{thesis_summary}

Kill watch:
{kill_watch}

Dashboard: {dashboard_url}/signals/{signal_id}
```

### Candidate state-change (added with §7.5 candidate_aging)

Triggered by `/functions/v1/fanout` on `candidate_events.INSERT WHERE event_type='state_changed' AND new_state IN ('killed','delivered')`.

**Subject:** `[CANDIDATE {new_state}] {TICKER}.{MIC} — {reason_short}`

**Plain-text body (HTML follows same shape):**

```
{TICKER}.{MIC} — {company_name}
State: {prev_state} → {new_state}
Reason: {reason_full}

Triggered kill condition: {kill_id} — {kill_description}
Evidence: {evidence_url}
  ({evidence_ts})

Catalyst date: {next_catalyst_date}
Last aging evaluated: {last_aging_evaluated_at}

For delivered candidates: realized_return is NULL; fill manually in dashboard.

Dossier: {dashboard_url}/candidates/{candidate_id}
```

Pedro reviews both templates before go-live.

---

## Appendix E — Migration script skeleton

```python
# migrations/import_signal_log.py
import json, os, uuid, hashlib, re, sys
from datetime import datetime, timezone
from pathlib import Path
from postgrest import SyncPostgrestClient

SIGNAL_LOG = Path("unified_system/unified_system/signals/signal_log.json")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
client = SyncPostgrestClient(f"{SUPABASE_URL}/rest/v1", headers={"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"})

SCANNER_RESCUE = [
    (re.compile(r"efts\.sec\.gov|sec\.gov/Archives"), "edgar_filing_monitor"),
    (re.compile(r"fda\.gov"), "fda_pdufa_pipeline"),
    (re.compile(r"capitoltrades\.com"), "congressional_trading"),
    (re.compile(r"londonstockexchange\.com"), "lse_rns_scanner"),
    (re.compile(r"release\.tdnet\.info"), "tdnet_scanner"),
    (re.compile(r"asx\.com\.au"), "asx_scanner"),
    (re.compile(r"sedarplus\.ca"), "sedar_plus_scanner"),
    (re.compile(r"hkexnews\.hk"), "hkex_scanner"),
    (re.compile(r"opendart\.fss\.or\.kr"), "kind_scanner"),
    (re.compile(r"nseindia\.com"), "bse_nse_scanner"),
    (re.compile(r"dados\.cvm\.gov\.br"), "cvm_scanner"),
    (re.compile(r"biva\.mx"), "bmv_scanner"),
    (re.compile(r"courtlistener\.com"), "courtlistener_scanner"),
    (re.compile(r"(fca|amf-france|afm|bafin|cnmv|consob)\."), "esma_short_scanner"),
]

def rescue_scanner_name(signal: dict) -> str:
    url = (signal.get("source_url") or signal.get("raw_data", {}).get("filing_url") or "")
    for rx, name in SCANNER_RESCUE:
        if rx.search(url):
            return name
    return "UNKNOWN_LEGACY"

def load_rubric_map() -> dict[str, str]:
    rows = client.from_("rubrics").select("id,profile").eq("rubric_version", 1).execute().data
    return {r["profile"]: r["id"] for r in rows}

def load_scanner_map() -> dict[str, str]:
    rows = client.from_("scanners").select("id,name").execute().data
    return {r["name"]: r["id"] for r in rows}

def main(dry_run: bool = False):
    signals = json.loads(SIGNAL_LOG.read_text())
    rubric_map = load_rubric_map()
    scanner_map = load_scanner_map()
    batch = []
    skipped = 0
    for s in signals:
        profile = s.get("scoring_profile") or s.get("scoring", {}).get("scoring_profile") or "activist_governance"
        rubric_id = rubric_map.get(profile)
        if rubric_id is None:
            print(f"! missing rubric for profile={profile}", file=sys.stderr); skipped += 1; continue
        scanner_name = s.get("scanner") or s.get("signal_category") or rescue_scanner_name(s)
        if scanner_name == "UNKNOWN" or scanner_name not in scanner_map:
            scanner_name = rescue_scanner_name(s)
        scanner_id = scanner_map.get(scanner_name)
        row = {
            "signal_id": s["signal_id"],
            "issuer_figi": s.get("issuer_figi"),
            "scanner_id": scanner_id,
            "scoring_profile": profile,
            "rubric_version_id": rubric_id,
            "source_content_hash": s["source_content_hash"],
            "source_url": s.get("source_url"),
            "source_date": s["source_date"],
            "scan_date": s["scan_date"],
            "signal_type": s.get("signal_type", "unknown"),
            "thesis_direction": s.get("thesis_direction"),
            "strength_estimate": s.get("strength_estimate"),
            "imported": True,
            "dimensions": s.get("scoring", {}).get("dimensions", {}),
            "score": s.get("scoring", {}).get("score", 0),
            "band": s.get("scoring", {}).get("band", "discard"),
            "auto_caps_triggered": s.get("scoring", {}).get("auto_caps_triggered", []),
            "raw_payload": s.get("raw_data", {}),
        }
        batch.append(row)
        if len(batch) >= 500:
            flush(batch, dry_run); batch = []
    if batch:
        flush(batch, dry_run)
    print(f"imported {len(signals) - skipped}, skipped {skipped}")

def flush(batch, dry_run):
    if dry_run:
        print(f"[dry-run] would insert {len(batch)} signals")
        return
    client.from_("signals").upsert(batch, on_conflict="source_content_hash,scoring_profile").execute()

if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
```

---

*End of spec.md. Pedro reviews end-to-end. Approval releases Phase 1 foundation work.*
