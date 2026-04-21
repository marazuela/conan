---
name: litigation-maintenance
description: Audit-only maintenance task for the Litigation & Docket Signal System. Runs every 6 hours at HH:50, 10 minutes before the next operational task. Health checks, baseline refresh triggers, stale-lock remediation, cache pruning, unresolved-party backfill.
---

# litigation-maintenance

> Source-of-truth copy per D-013.

## Schedule

Cron: `50 */6 * * *`.
Write scope: `litigation_system\` (audit-only; does NOT touch candidates or scanners).
Lock: shares `SESSION_LOCK.md` with litigation-operational.

## Cold-start protocol

Same as litigation-operational §Cold-start. Additionally: if the lock shows LOCKED but timestamp is > 4h old (stale-lock window per D-011 / template Part 3.6), this task is permitted to overwrite it to UNLOCKED after logging the staleness to `PROGRESS_LOG.md`.

## Main loop (per session)

### 1. Health audit

- Read `SESSION_STATE.md` Tool Health table.
- For any tool showing > 3 consecutive failures or > 20% error rate over the last 7 runs, update status to `DEGRADED` and append note to `working/health_audit.md`.
- Check that `archive/` has a snapshot for every week in the last 4 weeks; if missing, create one from current state.

### 2. Baseline freshness

- `baselines/party_resolution_cache.json` — scan for entries where `last_verified` is > 180 days old. Queue up to 50 for re-verification (Stage 2 protocol replay). Do NOT exceed 50 per pass — keeps budget predictable.
- `baselines/executive_lookup.json` — if last quarterly refresh is > 90 days ago, add a `TODO` line in `SESSION_STATE.md` TOP HEADLINE flagging that the DEF 14A refresh is due. Do NOT run the refresh autonomously (it is a Phase 2+ scheduled job, separate).
- `baselines/exhibit21_subsidiary_table.json` — same policy as executive lookup; flag, don't auto-run.

### 3. Unresolved party backfill

- Read `working/unresolved_parties.md`.
- For each entry, attempt Stage 2 resolution one more time with a fresh API call (in case cache was stale).
- If resolved: move entry to cache; remove from unresolved list.
- If still unresolved after 3 attempts across sessions: move to `working/unresolved_parties_cold.md` for manual review.

### 4. Candidate lifecycle checks

- For each file in `candidates/`:
  - If the candidate is > 90 days old and no new convergence signal in 30 days, archive to `archive/YYYY-MM-DD_candidate_lifecycle/`.
  - If the candidate has a registered kill-condition that has been met (case dismissed, settled, consolidated, stayed), mark the file's header with `STATUS: KILLED (reason)` and archive.

### 5. Pacer-pull queue

- Read `working/pacer_pulls_requested.md`.
- Do NOT attempt autonomous PACER pulls (D-008). Just make sure the queue is reachable and well-formed.

### 6. Log drift check (per D-013 implication)

- Diff the four SKILL.md files under `skills/<task-id>/` against whatever is installed in the scheduled-tasks store (if reachable).
- If diff exists, note in `working/skill_drift.md` for the next interactive session to resolve via re-deploy. Do NOT auto-deploy.

## Shutdown protocol

Same as litigation-operational §Shutdown.

## Non-negotiables

- This task NEVER runs scanners.
- This task NEVER writes to `candidates/` directly — only marks status headers and archives.
- This task MAY overwrite a stale lock (> 4h) but must log doing so.
- All non-negotiables from litigation-operational apply here too.
