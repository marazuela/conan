# Reporting Hub — Performance Report Task Prompt

**Task ID:** `reporting-hub-performance`
**Cron:** `30 2 * * *` (daily, 02:30 UTC)
**Role:** Cross-tool consumer. Generates per-tool performance PDFs into `Performance/per_tool/<tool>/` and one integrated cross-tool dashboard PDF at `Performance/ALL_TOOLS_PERFORMANCE.pdf`. Never writes outside `Reporting Hub/`.

---

## Phase 0 — Acquire hub lock

1. Read `Reporting Hub/REPORTING_SESSION_LOCK.md`.
2. If `LOCKED` and `Acquired` < 4h old, log `abort: hub lock held by <session>` to stdout and EXIT without any writes.
3. Otherwise (UNLOCKED or stale) overwrite it with:
   ```
   LOCKED
   Session: reporting-hub-performance-<YYYY-MM-DDTHHMM>
   Acquired: <ISO8601 UTC>
   Expected release: <ISO8601 UTC, +60 min>
   ```
4. Open a run log at `Reporting Hub/working/performance_report_<YYYY-MM-DD>_<HHMM>.log`. Every action in this task is appended there.

## Phase 1 — Orient

1. Read `Reporting Hub/REPORTING_INSTRUCTIONS.md` — confirm the contract.
2. Read `Reporting Hub/SOURCES.md` — this is the authoritative path manifest.
3. Build an `availability[]` map for each tool listed in SOURCES.md:
   - `operational`: the tool's system folder exists AND its `SESSION_STATE.md` exists AND its `SESSION_LOCK.md` does NOT show `LOCKED` within the last 4h.
   - `busy`: folder exists but SESSION_LOCK is ACTIVE (<4h old) → skip this cycle, log reason.
   - `pre-launch`: the tool's availability flag in SOURCES.md is `pre-launch` OR its SESSION_STATE.md is missing → skip silently, log once.
   - `missing`: the tool's operational folder does not exist at all → log warning, skip.

## Phase 2 — Read producer state (strictly read-only)

For each tool in `availability[] == operational`:

1. Read (do NOT write, do NOT modify):
   - `SESSION_STATE.md`
   - tail of `PROGRESS_LOG.md` (last 400 lines)
   - list of `candidates/` files with their mtime
   - list of `signals/` files with their mtime (if present)
   - `TIME_SENSITIVE.md` (if present)
   - latest file under `reports/` (the producer's own internal daily report, if present — Tool 2 produces these)
2. Compute per-tool metrics: active candidate count, watchlist count, killed-in-last-7d, signal volume last 7d, API uptime if tracked, convergence events if tracked.
3. Record everything in the run log as you go.

**If any read path resolves outside the tool's system folder → abort and write `Reporting Hub/working/violation_<timestamp>.md`.**

## Phase 3 — Generate per-tool PDFs

For each `operational` tool, write:

- `Reporting Hub/Performance/per_tool/<tool>/<YYYY-MM-DD>_performance.pdf`

(Pre-launch tools: still emit a minimal placeholder PDF to `Performance/per_tool/<tool>/<YYYY-MM-DD>_performance.pdf` noting `pre-launch, no producer data yet` so downstream consumers always see a file.)

Use direct reportlab generation (no docx→pdf chain — known failure mode). Sections:

1. Header: tool name, date, hub session ID
2. Health: current phase, active producers, last producer run timestamps
3. Pipeline volume (last 7 days): signals → triage → dedup → scored candidates
4. Active candidates: ticker, score, catalyst date, stance, conviction
5. Watchlist digest: counts and recent promotions/demotions
6. Recently killed / archived
7. Convergence events (if applicable)
8. Open questions / incidents (tail from system's OPEN_QUESTIONS.md or incident logs if present)
9. Producer task status (was today's run successful? any errors?)

**Atomic write pattern:** write to `<path>.tmp`, fsync, rename. Do NOT leave half-written PDFs.

## Phase 4 — Render integrated cross-tool dashboard

After all per-tool PDFs are written, regenerate the single canonical cross-tool PDF:

1. Run `python3 "Reporting Hub/working/build_scripts/render_all_tools_performance.py"` → writes `Reporting Hub/Performance/ALL_TOOLS_PERFORMANCE.pdf` (and `ALL_TOOLS_PERFORMANCE.md` sidecar).
2. This script is READ-ONLY against producer state. It probes each producer's `SESSION_STATE.md`, `SESSION_LOCK.md`, `candidates/`, and `PROGRESS_LOG.md` and renders a fleet-wide snapshot.
3. Atomic write: script writes to `<path>.tmp` then `os.replace` — no half-written masters.

Sections the script emits:

1. Fleet status: each tool's availability state (operational/busy/pre-launch/missing) + last producer run mtime
2. Aggregated funnel across operational tools: total raw signals → total candidates
3. Active candidate count per tool
4. Catalyst calendar merged across tools (next 30 days, any type) — sourced from `Candidates/candidates_index.json` `next_key_dates` fields
5. Producer task health summary: which tasks ran successfully in the last 24h, which errored
6. Pointer block to per-tool PDFs under `Performance/per_tool/<tool>/`

If the render script fails, log critical, leave previous `ALL_TOOLS_PERFORMANCE.pdf` in place, and do NOT release lock until investigated.

## Phase 5 — Registry updates

No registry file for performance reports. Skip.

(Note: this task READS `Candidates/candidates_index.json` to pull catalyst dates for the merged calendar in Phase 4 — it never writes to it. Registry writes are owned exclusively by `reporting-hub-deep-dives`.)

## Phase 6 — Self-audit (mandatory)

1. Grep the run log for any write path that does NOT start with `Reporting Hub/`. If any hit:
   - Write `Reporting Hub/working/violation_<timestamp>.md` with the violating path.
   - Do NOT release the lock. Exit so the operator investigates.
2. Confirm every per-tool PDF written this run exists and is non-zero-byte.
3. Confirm `Performance/ALL_TOOLS_PERFORMANCE.pdf` mtime is within the last 10 min.
4. Grep run log for any reference to the retired paths `performance_reports/`, `investment_theses/`, `non_us_candidates/`, `litigation_briefs/`, `silence_reports/`. If any hit, flag as a stale-path regression and abort.
5. Otherwise append to run log: `SELF_AUDIT: OK — all writes within Reporting Hub/`.

## Phase 7 — Release lock

Overwrite `Reporting Hub/REPORTING_SESSION_LOCK.md`:

```
UNLOCKED
Last session: reporting-hub-performance-<YYYY-MM-DDTHHMM>
Released: <ISO8601 UTC>
```

Done. Total expected runtime: 3–10 minutes depending on tool count and volume.

---

## Never-delete rule

When a per-tool performance PDF is superseded by a newer one on the same day (rare — same task shouldn't run twice), **move** the old file to `Reporting Hub/archive/<YYYY-MM-DD>_<tool>_performance_superseded/` — never delete. `ALL_TOOLS_PERFORMANCE.pdf` is always overwritten in place (it's a single canonical file), but the prior version is staged to `archive/performance_master/<YYYY-MM-DDTHHMM>_ALL_TOOLS_PERFORMANCE.pdf` before overwrite so history is preserved.

## Failure modes quick-reference

| Condition | Action |
|---|---|
| Hub lock held <4h | Abort, no writes |
| Hub lock stale (>4h) | Overwrite, proceed |
| Producer lock held <4h | Skip that tool, log, continue |
| Producer SESSION_STATE missing | Mark `pre-launch`, skip silently (still emit placeholder PDF) |
| Producer folder missing | Warn, skip |
| Per-tool PDF render error | Log error, write `<path>.error` instead of `.pdf`, continue other tools |
| `render_all_tools_performance.py` fails | Log critical, leave previous master in place, do NOT release lock until investigated |
| Write path outside hub | Abort, write violation file, don't release lock |
| Stale retired path referenced in run log | Flag as regression, abort before release |
