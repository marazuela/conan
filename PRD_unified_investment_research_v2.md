# PRD — Unified Investment Research System v2

**Status:** draft for Claude Code session intake
**Date:** 2026-04-20
**Author:** Pedro (with architectural collaboration)
**Target repo:** `C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\unified_system\` (existing Python codebase)

---

## 1. Context

The Unified Investment Research System is a primary-source, event-driven discovery pipeline currently operating as 17 Python scanners, file-coupled batch scoring, convergence over a rolling JSON log, and PDF/markdown reporting on 3h/4h cron cadences. The system is fully operational, has shipped real candidates (RPAY, AXSM, VRDN, VERA, RGR active; TVTX and GSAT as archived wins), and its four-layer decomposition has proven sound.

This PRD defines the **v2 product upgrade**: a hardened, event-driven platform with shared multi-user access and real-time email alerting on Immediate-band signals. The core four-layer separation (discovery → scoring → convergence → reporting) is preserved. What changes is the substrate underneath it: JSON files become Postgres rows, subprocess-on-laptop becomes Modal workers, batch-only reporting becomes event-driven with a batch reporting layer alongside.

The architectural motivation is the AVNS miss of 2026-04-14 and the D-013 pre-edge mandate it drove. The current v1 system has the signal in hand 6–72 hours before the market, but the 3-hour batch cadence and desktop-only reach means a catalyst that hits at 2am JST is not surfaced until morning. v2 closes that gap.

## 2. Objective

Ship a production v2 in which signals from 17 scanners land in a shared Supabase workspace within minutes of filing publication, Immediate-band signals (alone or via convergence) trigger email alerts to Pedro and collaborators within 5 minutes, and the candidate lifecycle is managed through a Next.js dashboard. **Thesis authoring is Claude's responsibility on behalf of all users** (via a specialized Claude app routine called by API from Modal — see Phase 0 spec §7.4); users review and approve, they never draft. The current Python scanner code is preserved verbatim where possible — especially the OpenFIGI ticker normalizer — and migrated to Modal as scheduled functions.

## 3. Non-goals

Out of scope for this PRD:

- Replacing or retuning any scanner's scoring logic, rubric weights, or auto-cap rules. v2 is a substrate migration, not a rubric revision.
- Specialized analyst agents (medical-paper reviewer, financial-ratio agent). The data model is designed to accept these later, but no agents are built in v2. **Exception (added 2026-04-20, extended 2026-04-21):** three Claude-routine pipeline stages are in scope: (a) thesis drafting on Immediate-band signals (spec §7.4), (b) adversarial challenger review of every drafted thesis and every `new_status='triggered'` kill-condition claim — separate routine, "skeptical IC reviewer" system prompt, returns `confirm`/`challenge`/`kill` (spec §7.4 two-gate model; §7.5 challenger pass), (c) kill-condition evaluation on active candidates (spec §7.5). These are framed as pipeline stages, not as "specialist analyst agents," because they sit on the critical path and operate on every Immediate-band signal and every active candidate. Additional specialist agents (medical-paper review, financial ratios, litigation brief expansion) remain out of scope.
- Replacing reportlab, adding new primary sources, or changing the $215M market-cap floor.
- Back-testing framework, market commentary, or news aggregation. These remain explicit anti-goals per `docs/OBJECTIVES.md`.
- Migrating the Decisions register (`DECISIONS.md`) out of markdown into the database.

## 4. Users

Primary user: Pedro. Secondary users: 2–3 collaborators. All users share a single workspace — same signal stream, same candidates, same rubrics. Per-user state is limited to email preferences, watchlist filters, and private annotations on candidates.

Authentication via Supabase Auth (email + magic link). RLS policies enforce per-user isolation only on `annotations`, `watchlists`, and `notifications_prefs`. Everything else is readable and writable by any authenticated user.

## 5. Architecture — decisions already made

These are settled. The Claude Code session should not relitigate; if evidence during implementation suggests revisiting, surface the concern before making a unilateral change.

**Stack.** Supabase (Postgres + Auth + Storage + Realtime + Edge Functions) as state and bus. Modal for Python scanner and reporting workers. Vercel + Next.js for the frontend dashboard. Resend for transactional email.

**Event model.** Postgres is the event bus. INSERT into `signals` fires a database webhook to a Supabase Edge Function (the reactor). The reactor runs targeted convergence, updates the signal row, classifies the band, and on Immediate-band result inserts an `alerts` row. A second webhook on `alerts` INSERT fans out to Resend for email and to Realtime for dashboard push.

**Scanner hosting.** Cloud from day one. Each scanner is a Modal scheduled function. Scanner config (timeout, profile mapping, keyword sets) lives in Supabase tables and is loaded at function start. Schedules themselves are declared in Modal (cadence changes require a deploy — acceptable since cadence changes are monthly-or-less).

**Bridge mode.** A `bridge_mode.py` script exists to run scanners locally on Pedro's laptop and write to Supabase, for use during the 2–3 week migration window before all scanners are on Modal. Bridge mode is deprecated the day the last scanner lands on Modal.

**Convergence as a function, not a table.** Convergence is computed by the reactor edge function on signal INSERT, scoped to the affected entity's window. Results land as columns on the signal row (`convergence_key`, `convergence_bonus`). No persisted `convergence_groups` table.

**Rubric-as-data, auto-caps-as-code.** Dimension weights live in the `rubrics` table (versioned with `effective_at` / `superseded_at`). Auto-cap rules remain Python functions, each with a stable `rule_id`. Every scored signal records its `rubric_version_id` and the `rule_id`s that fired. This gives replayability without a rules-engine DSL.

**Forward-compat hooks, not forward-built tables.** `extensions` JSONB columns on `signals`, `filings`, and `candidates`. pgvector extension enabled from day one. No `agent_jobs` table yet — agents become webhook consumers when they arrive.

**Dashboard as a single thin page.** Signal stream, convergence view, candidate review queue (not a Kanban authoring board — Claude drafts theses, users review/approve; see §7.4 in spec), operator_flags panel (translation drift, endpoint drift, convergence QA, aging failures — replaces the v1 `OPEN_QUESTIONS.md`), scanner health card. No admin UI for rubrics or scanner config in v2 — edit rubrics in Supabase Studio, edit scanner configs in the same table. Admin tooling is built only when edit frequency justifies it.

**Alert latency target: 5 minutes.** Filing publication → email delivered ≤ 5 minutes at p95.

## 6. Preserved artifacts (load-bearing — do not break)

Claude Code must preserve the following during migration. If preservation appears to conflict with a clean v2 design, surface the conflict rather than silently re-implementing.

- **`tools/openfigi_resolver.py::normalize_ticker`** — the Japanese 5-character ticker fix (strip trailing `0` when `len(ticker)==5 and ticker[3].isalpha() and ticker[4]=='0' and MIC==JP`). Must be ported to the Modal worker package as a shared module with byte-for-byte equivalent behavior on the affected inputs. This is called out explicitly in `docs/DECISIONS.md` and Q-016 as non-reversible.
- **The 17 scanner implementations** in `tools/`. Their internal logic is preserved. What changes is their IO: reading config from Supabase instead of `scanner_registry.json`, writing filings to Supabase Storage instead of local disk, writing signals to the `signals` table instead of `signals/<scanner>_scanner_output.json`.
- **`candidate_gate.py::promote_candidate`** — the thesis-quality gate (D-008), including the minimum-character thresholds and the boilerplate regex detector. **Kept as a Modal-hosted Python library**, invoked in-process by the `thesis_writer` Modal function (spec §7.4), never called from the dashboard. **Validation schema is extended v1 → v2**: v1's 5 fields (situation, why_underpriced, next_catalyst, next_catalyst_date, kill_conditions) remain byte-identical and available via `assess_thesis_v1` for historical dossier import; v2's `assess_thesis_v2` additionally requires `steelman` (min 120 chars, boilerplate regex), `web_research` (≥3 cited entries with retrieval timestamps, ≥1 non-strengthening lean), and `[verified]`/`[inferred]`/`[speculated]` reasoning-tag coverage. The v2 expansion closes the "correct-prose, no-asymmetry" failure archetype documented in `candidates/rejected_pending_thesis/` (ITRK-style rejects).
- **`run_post_scan.py::WEIGHTS`** — the six scoring profiles. Seed the `rubrics` table from this dict as `rubric_version_id=1`. Every historical signal re-imported must reference this version.
- **`config/pe_filer_allowlist.json`** — 39 CIKs for the takeover_candidate scanner. Port into a `pe_filer_allowlist` table (rubric-adjacent config).
- **`config/phase3_approval_base_rates.json`** — 36 indication regex patterns with base rates. Port into a `phase3_base_rates` table.
- **Atomic-write invariants (D-052)** — the `.tmp` / `.bak` / rename-in-place pattern for the local `signal_log.json`. Not relevant after migration (Postgres transactions subsume this), but any residual file-writing code (bridge mode, Modal workers writing local cache) must preserve the pattern.
- **Scanner subprocess budget (D-014, D-018)** — the 35s EDGAR budget and 120s global scanner budget become Modal function timeouts. Same numbers.

## 7. Data model (sketch — column-level detail in Phase 0 spec)

Core tables, grouped by responsibility. Full column-level schemas live in `spec.md` §3 + Appendix A (authoritative; this PRD sketch is orientation only).

**Registry (config-as-data):** `sources`, `scanners`, `rubrics`, `pe_filer_allowlist`, `phase3_base_rates`.

**Entity graph:** `entities`, `entity_identifiers`. FIGI primary; fallback chain (ticker+MIC → codigo_cvm → id_empresa_biva → stock_code → normalized name) modeled as prioritized rows in `entity_identifiers`.

**Raw evidence:** `filings`. Content-hash-addressed, URL, fetched_at, FK to source, FK to entity (nullable until resolved). Raw bytes in Supabase Storage bucket `filings/`.

**Pipeline state:** `scanner_runs`, `signals`, `candidates`, `candidate_events`, `outcomes`, `alerts`, `alert_deliveries`. Signals are immutable once scored (UPDATE only allowed to set `convergence_key` / `convergence_bonus`). Candidates have a `state` enum, structured `kill_conditions` JSONB, parsed catalyst dates/windows, and an append-only `candidate_events` log.

**Thesis pipeline (added post-approval 2026-04-20):** `thesis_jobs` (queue for Claude-authored theses on every Immediate-band signal), `thesis_drafting_failures` (DLQ), `candidate_aging_failures` (DLQ for the daily kill-condition evaluator).

**Operator visibility:** `operator_flags` (structured drift/anomaly surface replacing v1's `OPEN_QUESTIONS.md`; written by `translation_health`, `scanner_probe`, `convergence_qa`, `candidate_aging`, `reporting_weekly`, `litigation_baselines_refresh`).

**Human layer:** `users` (via Supabase Auth), `watchlists`, `notifications_prefs`, `annotations`, `candidate_rationales`.

**Audit:** `alerts` + `alert_deliveries` + `candidate_events` cover audit for their domains. Supabase's built-in audit logs cover DDL. No separate audit tables.

JSONB fields of note: `signals.dimensions` (the per-profile scored dimensions), `signals.raw_payload` (what the scanner emitted), `signals.extensions` (for future agent enrichment), `filings.extensions`, `candidates.extensions`.

## 8. Event flow

A scanner fires on its Modal schedule. It reads its config from `scanners`, calls the primary source, for each new item writes the raw bytes to Storage and the filing metadata to `filings`, resolves the issuer via OpenFIGI (with the preserved normalizer), and for each qualifying hit inserts a row into `signals` with the matching `scoring_profile`, the scored dimensions (pre-convergence), and the `rubric_version_id` used.

The signal INSERT fires a database webhook to the **reactor** edge function. The reactor queries the signal log for the affected entity within the active window (14d standard, 30d if any signal is litigation), computes convergence, writes `convergence_key` and `convergence_bonus` back onto the row, applies auto-caps, and classifies the band. If the final band is Immediate, the reactor inserts into `alerts` with a dedup key.

The alert INSERT fires a second webhook to the **fan-out** edge function, which reads `notifications_prefs`, sends email via Resend to every opted-in user, and broadcasts to Realtime channels for connected dashboard clients. Dedup enforced by unique `(entity_id, signal_fingerprint, day)` on `alerts`.

Weekly, a **reporting** Modal job runs `reportlab` over the current candidate set, writes the PDF to Storage, and inserts a notification that goes out through the same fan-out path.

## 9. Deliverables

Claude Code produces, in order:

1. **`spec.md`** (Phase 0) — full column-level schema, edge function contracts, Modal function signatures, migration plan from v1 JSON log to v2 Postgres, test strategy.
2. **Supabase migrations** — schema, RLS policies, extensions (pgvector), storage buckets, webhooks.
3. **Modal worker package** — 17 scheduled functions, shared modules (openfigi_resolver, scanner_base, supabase_client), local dev harness.
4. **Edge functions** — reactor, fan-out, candidate-gate (if routed through edge).
5. **Next.js dashboard** — signal stream, convergence view, candidate board, scanner health.
6. **Bridge mode script** — `bridge_mode.py` for running scanners from Pedro's laptop against Supabase during migration.
7. **Migration script** — imports the existing `signal_log.json` (733 signals) and candidate markdown files into the new schema.
8. **README / ops runbook** — how to deploy, how to add a scanner, how to rotate keys, how to read scanner health.

## 10. Phased delivery with exit criteria

Each phase ends with a human checkpoint. Do not start the next phase without Pedro's explicit confirmation the exit criterion is met.

**Phase 0 — Spec and approval.** Write `spec.md`. Review with Pedro. Exit: Pedro approves schema, event contracts, migration plan.

**Phase 1 — Foundation.** Supabase project, schema migrations, RLS, Storage bucket, pgvector. One scanner (recommend `edgar_filing_monitor` as the highest-complexity reference implementation) ported to Modal, writing filings and signals. Exit: Pedro sees real EDGAR signals landing in Supabase within 5 minutes of a test filing.

**Phase 2 — Reactor and alerts.** Reactor edge function implementing targeted convergence, band classification, auto-cap application. Fan-out edge function with Resend integration. Pedro and one test collaborator receive a real email from a real Immediate-band convergence. Exit: end-to-end latency confirmed ≤ 5 minutes at p95 over a 48h observation window.

**Phase 3 — Full scanner migration.** Remaining 16 scanners ported to Modal. Bridge mode script available and documented. Historical `signal_log.json` imported. Exit: all 17 scanners running on Modal for a full 72h window with signal volume consistent with v1 baselines (±20%), no regressions on the open known-issues (Q-017 CourtListener, Q-018 sedar_plus CLI defect, Q-019 OpenDART — these remain documented blockers, not v2 regressions).

**Phase 4 — Dashboard.** Next.js app on Vercel. Signal stream (paginated, filterable by profile/region/band), convergence view (grouped by `convergence_key`), candidate board (Kanban by state), scanner health card. Auth via Supabase. Exit: Pedro and collaborators can authenticate and see live system state.

**Phase 5 — Candidate lifecycle.** `candidate_gate` (v2 schema — spec §7.1) ported to the Modal worker package. `thesis_writer` Modal function (§7.4) wired to the reactor via `thesis_jobs` queue — every Immediate-band signal triggers a Claude-authored draft → v2 gate → `candidates` row with full structured `kill_conditions`. `candidate_aging` Modal function (§7.5) wired to run daily with its own Claude app routine for kill-condition evaluation. Fan-out extended to email on `candidate_events.state_changed` when new state ∈ {killed, delivered}. Dashboard surface is a **review queue** — users accept/reject/override Claude drafts; no authoring form. State transitions logged to `candidate_events`. Five existing active candidates (RPAY, AXSM, VRDN, VERA, RGR) imported with their dossiers via `assess_thesis_v1` compatibility path. Exit: a new Immediate-band signal produces an emailed alert AND a Claude-drafted `state='watch'` candidate within 5 minutes at p95; the aging sweep demotes/kills candidates on kill-condition match or elapsed catalyst; existing five dossiers visible in review queue.

**Phase 6 — Reporting and cleanup.** Weekly reporting Modal job producing the executive PDF. Legacy cron tasks (unified-operational, unified-maintenance, unified-reporting) disabled on Pedro's desktop. Bridge mode removed. Archive v1 codebase. Exit: v1 fully retired, first v2 weekly PDF delivered.

## 11. Open decisions to surface (do not guess)

**Status (2026-04-20):** all items below locked by Pedro during Phase 0 review. See `spec.md` §12 for the authoritative locked-answers table; the list below is preserved for historical trace.

Claude Code must surface these to Pedro before making the call. Default suggestions are in parentheses but explicit confirmation is required.

- **Modal region.** (US-east, to minimize EDGAR latency.)
- **Resend sending domain.** (Confirm domain and DKIM setup.)
- **Supabase project tier.** (Pro tier — required for database webhooks at production rates.)
- **Vercel project and deploy branch model.** (Main → production, PRs → preview.)
- **Email template for Immediate-band alerts.** (Show a stub, get approval before final.)
- **RLS policy specifics on `annotations`.** (Per-user read/write, no cross-user read.)
- **How to handle the three blocked scanners (Q-017 CourtListener token, Q-019 OpenDART key).** v2 migration preserves their `status=auth_required` graceful-failure behavior; if Pedro wants to resolve the token blockers as part of v2, raise this explicitly.
- **Reporting PDF distribution.** (Email with Storage-signed URL, 7-day expiry.)
- **Historical signal import scope.** (Full `signal_log.json` — 733 signals — imported with original timestamps and a flag `imported=true` to distinguish from v2-native signals.)

## 12. Working conventions

Pedro's collaboration preferences — please follow them without reminder:

**Plan mode default.** Any non-trivial step enters plan mode. Phase 0 is explicitly a plan-mode deliverable. Within each phase, break the work into `tasks/todo.md` before starting implementation.

**Subagents liberally.** For research (Supabase webhook behavior, Modal scheduling semantics, Resend deliverability), parallel file analysis, or large codebase exploration, dispatch subagents. One tack per subagent.

**Self-improvement loop.** After any correction from Pedro, update `tasks/lessons.md` with the pattern and the rule. Review lessons at session start and before each phase.

**Verification before done.** No phase exits without evidence the exit criterion is met. Diff behavior against v1 where relevant (signal volume, scoring outputs, convergence grouping). Run tests, check logs, show proof.

**Elegance check.** Before presenting non-trivial work, pause and ask "is there a more elegant way?" Skip for obvious fixes. Challenge your own work before surfacing it.

**Autonomous bug fixing.** Point at logs, errors, or failing tests and resolve them without asking for permission. Zero context switching from Pedro.

**Task management cadence.** `tasks/todo.md` written and confirmed before implementation. Mark items complete as you go. `tasks/lessons.md` updated after any correction. High-level summary at each step.

**Simplicity first.** Minimal impact code. No temporary fixes. Root causes. If a simpler approach exists than what's in this PRD, surface it before implementing the PRD version.

## 13. Success criteria

v2 is considered successful when, for 14 consecutive days after Phase 6:

- All 17 scanners run on their declared Modal cadences with >98% success rate (excluding the three known auth-blocked scanners).
- Immediate-band signals trigger emails at p95 ≤ 5 minutes from filing publication.
- Pedro and collaborators access the dashboard daily; every Immediate-band signal produces a Claude-drafted candidate that appears in the review queue without user authoring; users review, annotate, or reject via the dashboard (not by editing markdown files).
- No data-integrity incident: no signal lost, no duplicate-alerted signal, no broken entity resolution on Japanese 5-char tickers.
- The weekly PDF ships automatically on schedule.
- The v1 codebase is archived and not touched by any scheduled task.

## 14. Out of scope / future phases

The following are deliberately deferred and referenced here only so Claude Code avoids accidentally pulling them into v2:

- Specialized analyst agents (medical papers, financial ratios, deeper litigation analysis). Hooks exist (`extensions` JSONB, pgvector) but these agents are not built. **In scope (carve-out per §3):** three Claude-routine pipeline stages — thesis drafting, adversarial challenger re-review of drafts and kill-condition claims (added 2026-04-21; closes the ITRK archetype "correct facts, no asymmetry" failure mode), and kill-condition evaluation on active candidates. All framed as pipeline stages, not specialist agents. Further thesis review (a third Claude pass beyond drafter + challenger) remains out of scope; the two-gate model is load-bearing but bounded.
- Admin UI for rubrics, scanner config, PE allowlist.
- Outcome-tracking analytics (win rate by profile, base-rate recalibration from archived outcomes).
- Mobile push notifications.
- Slack/Teams integrations beyond email.
- Multi-tenancy beyond shared workspace (if a second fund ever uses the system, that's a separate PRD).
- Replacing reportlab.
- New primary sources or new scoring profiles.

## 15. Existing code — locations

All under `C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\unified_system\`:

- `tools/` — 17 scanners plus `openfigi_resolver.py`, `run_post_scan.py`, `convergence_engine.py`, `candidate_gate.py`, `pipeline_runner.py`.
- `config/scanner_registry.json` — canonical scanner config (cadence, timeouts, signal_type_profile_map).
- `config/pe_filer_allowlist.json`, `config/phase3_approval_base_rates.json`.
- `framework/profile_*.md` — human-readable rubric docs (must match `WEIGHTS` dict).
- `framework/candidate_template.md` — 11-section dossier template.
- `candidates/` — active and archived dossiers.
- `candidates/_curated_rationales.json` — schema v2.2, hand-curated rationale cards.
- `signals/signal_log.json` — 733 signals to migrate.
- `docs/OBJECTIVES.md`, `docs/DECISIONS.md` — non-negotiable constraints.

Claude Code should begin by reading `docs/OBJECTIVES.md` and `docs/DECISIONS.md` in full before writing `spec.md`.
