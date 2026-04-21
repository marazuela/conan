# Phase 0 — `spec.md` Spec-Writing Checklist

Derived from the approved plan at `~/.claude/plans/kickoff-unified-abundant-conway.md`. I mark items complete as I draft each section of `spec.md`. Pedro should skim this before I start drafting to confirm the scope and order.

## Pre-drafting (confirm before writing)

- [ ] Pedro confirms this checklist reflects what he expects in `spec.md`.
- [ ] Pedro confirms target repo layout: `spec.md` at `/Users/Pico/Documents/Claude/Projects/Conan/spec.md` (alongside the PRD).
- [ ] Pedro confirms length target (~4000 words; DDL + webhook samples in appendices push total closer to 6000).
- [ ] Pedro acknowledges the 6-vs-5 scoring profile discrepancy (`takeover_candidate` exists in `WEIGHTS` dict as a 6th profile — I will seed it; flag it as a PRD update candidate).

## Section 1 — Context & scope

- [ ] One-paragraph framing: v2 = substrate migration, not behavior change.
- [ ] One-line reading map: who reads which sections (Pedro end-to-end; any collaborator onboarding only §§2, 8, 11).
- [ ] Explicit list of what's out of scope for v2 per PRD §3 + §14.

## Section 2 — Preserved artifacts (PRD §6 coverage)

- [ ] Table with every PRD §6 item → v2 destination.
- [ ] Rows: `openfigi_resolver.normalize_ticker` → `shared/openfigi_resolver.py` Modal module + regression test.
- [ ] Rows: 17 scanners → `modal_workers/scanners/<name>.py` + `scanner_base`.
- [ ] Rows: `candidate_gate.promote_candidate` → Modal service `modal_workers/services/candidate_gate_service.py` + edge-function proxy.
- [ ] Rows: `run_post_scan.WEIGHTS` (6 profiles) → `rubrics` table at `rubric_version_id=1`.
- [ ] Rows: `config/pe_filer_allowlist.json` (45 filers) → `pe_filer_allowlist` table.
- [ ] Rows: `config/phase3_approval_base_rates.json` (39 indications) → `phase3_base_rates` table.
- [ ] Rows: Atomic-write pattern (D-052) → noted as irrelevant for Postgres, relevant for openfigi_cache under bridge-mode.
- [ ] Rows: 35s EDGAR budget + 120s scanner budget → Modal function `timeout=` kwargs.
- [ ] Column on each row: deviation flag (yes/no) + one-line why.

## Section 3 — Data model (PRD §7 coverage — column-level)

- [ ] **3.1 Registry tables**
  - [ ] `sources`: columns, types, indexes, RLS (read-all, write-admin).
  - [ ] `scanners`: columns (name, tool_path, status, geography, cadence, default_scoring_profile, signal_type_profile_map JSONB, endpoints JSONB, timeouts, last_run_*), indexes, RLS.
  - [ ] `rubrics`: columns (id, profile, dimension_weights JSONB, effective_at, superseded_at, rubric_version), unique(profile, rubric_version), RLS (read-all, write-admin).
  - [ ] `pe_filer_allowlist`: columns (filer_name PK, cik nullable, type, notes).
  - [ ] `phase3_base_rates`: columns (indication PK, phase3_to_approval numeric, trial_design_adjustments JSONB, notes).

- [ ] **3.2 Entity graph**
  - [ ] `entities`: columns (id UUID, issuer_figi unique, name, primary_ticker, primary_mic, country, market_cap_usd, updated_at, extensions JSONB), indexes on (issuer_figi), RLS.
  - [ ] `entity_identifiers`: columns (id, entity_id FK, id_type enum (ticker_mic, codigo_cvm, id_empresa_biva, stock_code, cik, cnpj, isin, name_normalized), id_value, priority smallint, created_at), unique(id_type, id_value), index on (entity_id).

- [ ] **3.3 Raw evidence**
  - [ ] `filings`: columns (id UUID, source_id FK, entity_id FK nullable, source_content_hash unique, storage_path, url, fetched_at, published_at, filing_type, extensions JSONB), indexes on (entity_id, published_at) and (source_content_hash).

- [ ] **3.4 Pipeline state**
  - [ ] `scanner_runs`: columns (id, scanner_id FK, started_at, completed_at, status, signals_emitted, errors JSONB, raw_log_path).
  - [ ] `signals`: full column spec — the biggest table.
    - Universal: `signal_id` PK, `entity_id` FK, `issuer_figi`, `scanner_id` FK, `scoring_profile`, `rubric_version_id` FK, `source_content_hash`, `source_url`, `source_date`, `scan_date`, `signal_type`, `thesis_direction` enum (long/short/neutral), `strength_estimate` smallint, `imported` boolean default false, `rule_ids_fired` text[] array, `raw_payload` JSONB, `extensions` JSONB.
    - Scoring: `dimensions` JSONB (per-profile), `score` numeric, `band` enum (immediate/watchlist/archive/discard).
    - Convergence (populated by reactor): `convergence_key` text, `convergence_bonus` smallint, `score_with_bonus` numeric, `band_with_bonus` enum.
    - Indexes: unique(signal_id); unique(source_content_hash, scoring_profile); index(entity_id, scan_date); index(convergence_key, scan_date); partial index where band_with_bonus='immediate'.
    - Trigger: database webhook → reactor on INSERT.
    - RLS: read-all (authenticated); INSERT from service role only; UPDATE only to convergence_* columns via service role.
  - [ ] `candidates`: columns (id UUID, ticker, mic, entity_id FK, state enum (watch/active/killed/delivered), scoring_profile, current_score numeric, current_band, created_at, updated_at, dossier_markdown text, dossier_storage_path, thesis_approved_at, extensions JSONB).
  - [ ] `candidate_events`: columns (id, candidate_id FK, event_type enum (created/state_changed/scored/note_added/thesis_updated), payload JSONB, user_id FK nullable, created_at). Append-only; no UPDATE/DELETE.
  - [ ] `outcomes`: columns (id, candidate_id FK, outcome_type enum (delivered/killed/expired), realized_return numeric nullable, notes, created_at).
  - [ ] `alerts`: columns (id, entity_id FK, signal_id FK, signal_fingerprint, day_utc date, email_subject, email_body_storage_path, dispatched_at, dispatched_to text[], dedup_key generated as `entity_id||signal_fingerprint||day_utc::text`), unique(dedup_key).

- [ ] **3.5 Human layer**
  - [ ] `users`: thin view or reference to `auth.users`; no separate table.
  - [ ] `watchlists`: columns (id, user_id FK, name, filter_jsonb, created_at). RLS: strict per-user.
  - [ ] `notifications_prefs`: columns (user_id PK, email_on_immediate boolean, email_weekly_report boolean, realtime_channels text[]). RLS: per-user.
  - [ ] `annotations`: columns (id, user_id FK, candidate_id FK, body text, created_at). RLS: strict per-user read + write.
  - [ ] `candidate_rationales`: columns matching `_curated_rationales.json` schema v2 (ticker PK, one_liner, hypothesis, thesis, expected_outcome, price_targets JSONB, time_sensitivity, kill_watch, catalyst_date_iso, archived boolean default false, archived_meta JSONB).

- [ ] Every table has `created_at timestamptz default now()`, most have `updated_at timestamptz` with trigger.
- [ ] Every table has an RLS block: pseudocode `CREATE POLICY` statements for SELECT/INSERT/UPDATE/DELETE.

## Section 4 — Storage layout

- [ ] `filings/` bucket: content-hash-addressed, private, signed URL for authenticated access. Lifecycle: retained indefinitely.
- [ ] `scanner-caches/` bucket: prefixes per scanner (`openfigi/`, `edgar/dedup/`, `edgar/rotation/`, `esma/snapshots/YYYY-MM-DD/`, `lse/alldata/`, `asx/rotation/`). TTLs noted per cache.
- [ ] `reports/` bucket: signed URL access, 7-day expiry for emailed links.
- [ ] RLS for each bucket explicitly.

## Section 5 — Extensions and setup

- [ ] pgvector (from day one, per PRD §5).
- [ ] pgcrypto (for content-hash utilities).
- [ ] pg_cron (if used for cleanup of old failed_reactor_events; defer).
- [ ] Supabase Auth: magic link enabled; invite-only (allowlist in auth.users).
- [ ] Realtime: channels per candidate, per user.

## Section 6 — Edge function contracts

- [ ] **6.1 Reactor (`/reactor`)**
  - [ ] Supabase database-webhook payload envelope documented verbatim (table, type='INSERT', record, old_record, schema).
  - [ ] Request validation: JWT / webhook secret header.
  - [ ] Convergence routine pseudocode: entity-window query → group → classify (same-direction/contradiction/orthogonal) → apply caps → select winner → write back (INSERT row + cross-UPDATE prior winner if displaced).
  - [ ] Response envelope: `{processed: true, convergence_bonus, band_with_bonus, alert_inserted}`.
  - [ ] Error handling: 3 attempts exponential backoff; DLQ row into `failed_reactor_events`; idempotent by `signal_id`.
- [ ] **6.2 Fan-out (`/fanout`)**
  - [ ] alerts.INSERT webhook envelope.
  - [ ] Reads `notifications_prefs` → Resend → Realtime broadcast → writes `alert_deliveries` sub-audit.
  - [ ] Dedup: `ON CONFLICT DO NOTHING` on `alerts.dedup_key`.
- [ ] **6.3 Candidate-gate-proxy (`/candidate-gate`)**
  - [ ] Request: `{signal_id, thesis: {situation, why_underpriced, next_catalyst, next_catalyst_date, kill_conditions}, band, scoring_profile}`.
  - [ ] Forwards to Modal `candidate_gate_service`; persists `status=promoted|rejected`; writes markdown to `candidates` table + Storage.
  - [ ] Response: `{status, candidate_id?, rejection_reasons?}`.

## Section 7 — Modal function signatures

- [ ] **7.1 Shared modules** (each with type-hint-style signatures):
  - [ ] `shared/openfigi_resolver.py` — `resolve_ticker_mic`, `resolve_ticker`, `resolve_isin`, `resolve_batch`, `normalize_ticker`. Cache in `scanner-caches/openfigi/`.
  - [ ] `shared/supabase_client.py` — `upsert_filing`, `insert_signal`, `update_signal_convergence`, `load_config`, `append_scanner_run`, `save_cache`, `load_cache`.
  - [ ] `shared/scanner_base.py` — `ScannerResult` dataclass; `run_scanner(scanner_name, scan_fn)` decorator handling timeouts + envelope + signal writeback.
- [ ] `shared/rubric_engine.py` — `score_signal`, `apply_auto_caps`, `classify_band`, `weighted_total`. Live Conan keeps the 35/25/15 contract even though the separate `Scoring engine/` folder later experimented with 30/20/10 on a legacy file-bus branch.
  - [ ] `services/candidate_gate_service.py` — FastAPI or Modal webhook endpoint wrapping `promote_candidate`.
- [ ] **7.2 17 scanner functions**
  - [ ] One Modal decorator per scanner with cadence mapping:
    - `@app.function(schedule=Period(hours=3))` for 3h cadence (edgar, fda_pdufa, lse_rns, tdnet, asx, takeover_candidate[weekly])
    - `@app.function(schedule=Cron("0 9 * * *"))` for daily (esma_short, congressional, sedar_plus, hkex, kind, bse_nse, cvm, bmv, courtlistener, sec_enforcement)
    - `@app.function(schedule=Cron("0 9 * * 1"))` weekly for takeover_candidate, pre_phase3_readout
  - [ ] Each declares `timeout=` (hard, from registry), secrets, Storage mounts.
  - [ ] Each wraps a scanner-internal `scan()` function that returns `list[Signal]`.
- [ ] **7.3 Reporting function**
  - [ ] `@app.function(schedule=Cron("0 12 * * 0"))` weekly Sunday noon UTC.
  - [ ] Reads active candidates + rationales; renders via reportlab; uploads to `reports/`; inserts notification row.

## Section 8 — Event flow (ASCII diagrams)

- [ ] Happy path: Modal scanner → `filings` INSERT → `signals` INSERT → reactor webhook → (convergence + caps + band + alerts INSERT) → fan-out webhook → Resend + Realtime → email + dashboard.
- [ ] Reporting path: weekly Modal → `reports/` Storage → fan-out → email with signed URL.
- [ ] Failure/retry paths:
  - Webhook 5xx replay (Supabase built-in retry)
  - Modal scanner OOM or crash (signal not emitted; next cadence retry; alert to Pedro)
  - OpenFIGI 429 (backoff in resolver)
  - auth_required scanner (idle gracefully, no error)
  - Dedup collision (DB unique-index rejection; no error surfaced)
  - Bridge-mode race (Modal and bridge both trying to INSERT same source_content_hash; loser drops)

## Section 9 — Migration plan

- [ ] **9.1 Registry seeding** scripts (one per config → table).
- [ ] **9.2 Signal log import**:
  - [ ] 734 signals, dedup by (source_content_hash, scoring_profile).
  - [ ] `imported=true`.
  - [ ] `scanner_name` rescue rules for 565 UNKNOWN rows (source_url patterns → scanner name).
  - [ ] `rubric_version_id=1`.
  - [ ] Convergence re-run once across imported set.
- [ ] **9.3 Candidate dossier import**: markdown front-matter + body → `candidates` table + Storage; sidecar → `candidate_rationales`.
- [ ] **9.4 Dry-run strategy**: Supabase branch `migration-dry-run`; diff metrics (score distribution, band counts, convergence group counts) against v1; promote only if within ±2%.
- [ ] **9.5 Rollback plan**: v1 keeps running through Phase 0-2; v1→v2 cutover only at Phase 6; rollback = point cron back; no data loss.

## Section 10 — Test strategy

- [ ] **10.1 Unit tests**: normalize_ticker golden vectors; score_signal frozen fixtures; candidate_gate thesis validation; entity-resolution cascade.
- [ ] **10.2 Integration tests**: scanner → Supabase (HTTP mocked); reactor on curated INSERT batch; fan-out with Resend test mode.
- [ ] **10.3 End-to-end**: synthetic EDGAR filing → scanner → signal → reactor → alert → email sandbox; assert p95 ≤ 5 min.
- [ ] **10.4 Replay test (load-bearing)**: 734 historical signals re-imported under `rubric_version_id=1` must reproduce byte-identical `score`, `band`, `auto_caps_triggered`. Convergence output must match v1's `convergence_report_*.json`.
- [ ] **10.5 Chaos**: webhook 5xx for 60s; scanner OOM; OpenFIGI 429; duplicate INSERT race.

## Section 11 — Phase 1 task list (with acceptance criteria)

- [ ] Supabase project (Pro tier) created in chosen region — AC: can create a test table and query it.
- [ ] Migrations applied — AC: schema-diff against spec shows zero drift.
- [ ] RLS policies active — AC: unauthenticated query returns 0 rows on all sensitive tables.
- [ ] openfigi_resolver ported — AC: golden-vector test passes (incl. 469A0 → 469A).
- [ ] scanner_base + supabase_client modules — AC: unit tests pass; dogfood by edgar scanner.
- [ ] edgar_filing_monitor on Modal — AC: manual trigger produces ≥1 signal row in Supabase within 35s.
- [ ] Reactor edge function — AC: INSERT on test signal triggers convergence + band + optional alert within 2s.
- [ ] Fan-out edge function — AC: alert INSERT produces a Resend sandbox delivery + Realtime broadcast.
- [ ] End-to-end smoke test — AC: synthetic EDGAR filing → email in ≤5 min.

## Section 12 — Open decisions surfaced (PRD §11)

- [ ] Modal region: default US-east; Pedro confirms.
- [ ] Resend sending domain: Pedro provides.
- [ ] Supabase tier: Pro.
- [ ] Vercel deploy branch model: main → prod, PR → preview.
- [ ] Email template: appendix draft; Pedro reviews copy.
- [ ] RLS on annotations: strict per-user (`auth.uid() = user_id`).
- [ ] Q-017 / Q-019 tokens: out of scope for v2 unless Pedro provides; preserve graceful auth_required.
- [ ] Reporting PDF distribution: 7-day signed URL + dashboard; Pedro confirms.
- [ ] Historical signal import scope: all 734 with `imported=true`; Pedro confirms.
- [ ] 6-vs-5 profiles: seed 6; Pedro confirms PRD edit.

## Section 13 — Appendices

- [ ] **A. Full DDL draft** (copy-paste-ready SQL for every table, RLS policy, index, trigger).
- [ ] **B. Webhook payload samples** (reactor request/response, fan-out request/response; both Supabase envelope + internal shape).
- [ ] **C. Modal function skeleton** (one full example: edgar_filing_monitor; abbreviated template for others).
- [ ] **D. Email template draft** (HTML + plain-text for Immediate alert; weekly report digest template).
- [ ] **E. Migration script skeleton** (Python, runs against `migration-dry-run` branch).

## Post-drafting checks (before handing spec.md to Pedro)

- [ ] Every PRD §6 artifact named with v2 destination.
- [ ] Every PRD §7 table fully column-specified.
- [ ] Every PRD §11 open decision has a proposed default + note on what Pedro must confirm.
- [ ] Replay test noted as gating acceptance for migration cutover.
- [ ] Cross-check against DECISIONS.md D-001..D-013 + the carried-over D-014/D-018/D-047/D-052 — no decision violated.
- [ ] Elegance pass: anywhere the PRD approach has a cleaner alternative, proposed with trade-off (not silently substituted).
- [ ] Word count ≤ 6000 total incl. appendices; body ≤ 4500.
- [ ] Scan for any internal todo/placeholder strings; delete before handing over.

## Hand-off

- [ ] Draft complete; summary of what's in spec.md posted to Pedro.
- [ ] Pedro reads end-to-end.
- [ ] Feedback captured; edits made.
- [ ] Pedro explicit approval received → Phase 1 foundation work may begin.


---

# Phase 5 — Skill refinement (2026-04-21)

Scope: cross-reference the three `.claude/skills/*.md` files against the new Modal-runtime Python modules (`modal_workers/shared/*`), the reactor/fanout edge functions, and the latest Supabase migrations. Fix drift only — no behavior changes.

## Plan

- [x] Read migrations (`20260420200000_initial_schema.sql`, `20260420210000_post_approval_amendments.sql`, `20260421000000_allow_null_score_band.sql`, `20260422000000_signal_resolver_queue.sql`) and confirm column/enum/index references cited by the skills.
- [x] Verify runtime symbols referenced by the skills exist: `rubric_engine.rescore_with_dims`, `rubric_engine.window_days`, `candidate_gate.assess_thesis_v2`, `candidate_gate.render_candidate_markdown_v2`, `dim_estimator.estimate_dimensions` (returns None for activist_governance / merger_arb / litigation), `reactor.enqueueNeedsScoring` (INSERT `thesis_jobs{status:'needs_scoring'}`), reactor UPDATE path (score NULL→non-NULL only).
- [x] Fix `candidate_aging.md` line 82: `rubric_engine.py:265` → `rubric_engine.py:306` (true line of `window_days`).
- [x] Fix `thesis_writer.md` line 39: drop redundant `AND status NOT IN ('needs_scoring','scoring','scoring_complete_below_immediate')` — `WHERE status='queued'` is strictly more restrictive.
- [x] Re-grep all `.claude/skills/*` for remaining `:NNN` line refs; confirm each still resolves.

## Review

**Drift found:**
1. `candidate_aging.md:82` pointed to `rubric_engine.py:265` but `window_days` moved to line **306** when the convergence audit reference block was added above it. Fixed.
2. `thesis_writer.md:39` carried a belt-and-suspenders `NOT IN` over three new statuses from migration `20260422000000_signal_resolver_queue.sql`. Since `status = 'queued'` is strict equality, the NOT IN can never fire. Removed — the query is now clean and matches the one-line promise in §1.

**Verified in-place (no edit needed):**
- `thesis_writer.md:201` → `fanout/index.ts:101` (`created` / `thesis_drafted_by_claude` email trigger) — ✅ correct.
- `thesis_writer.md:260` → `fanout/index.ts:89-94` (`alerts.INSERT` audit-only path) — ✅ correct.
- `candidate_aging.md:183` → `initial_schema.sql:284-293` (`operator_flags_open_uniq` partial index) — ✅ correct.
- `candidate_aging.md:273` → `fanout/index.ts:108-118` (feature-flagged `state_changed → killed/delivered` email path behind `EMAIL_STATE_CHANGE_KILLED_DELIVERED`) — ✅ correct.
- `signals.extensions` JSONB column used in `signal_resolver.md` step 7 exists (initial_schema §Pipeline state, line 199).
- `candidate_aging_failures.error_kind` enum includes `'other'` — the streak-reset sentinel insert in `candidate_aging.md` step 6 is valid.
- `candidates_catalyst_exactly_one` CHECK permits both NULL — matches the `thesis_writer.md` catalyst parsing logic.
- Reactor's `enqueueThesisJob` collides on `UNIQUE (signal_id)` with the row `signal_resolver` is already holding in `status='scoring'` and returns false — the inline-draft flow in `signal_resolver.md` step 10 is safe.

**Follow-ups flagged (not fixed — out of scope for a drift pass):**
- `signal_resolver.md` step 8's "poll `signals.band_with_bonus` for ~3s" has no fallback if the reactor DLQs the event into `failed_reactor_events`. Today the skill would fall through both branches (neither `IN (...)` nor `= 'immediate'` matches NULL). Candidate fix: after the poll, if `band_with_bonus` is still NULL, check `failed_reactor_events` by `signal_id` and mark the job `dlq` with a specific reason. Not touching here because it changes behavior.
- `litigation.party_resolution_confidence` auto-cap fires when `prc < 3` (i.e. 1 OR 2) — the skill rubric in `signal_resolver.md` only names the 1-value as triggering. Wording is conservative; not a bug, but a candidate tightening.
