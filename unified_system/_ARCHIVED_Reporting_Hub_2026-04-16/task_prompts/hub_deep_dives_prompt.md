# Reporting Hub — Deep Dives Task Prompt

**Task ID:** `reporting-hub-deep-dives`
**Cron:** `30 */4 * * *` (every 4 hours at :30 — 00:30, 04:30, 08:30, 12:30, 16:30, 20:30 UTC)
**Role:** Cross-tool consumer. For each tool, scans for new/updated candidates and produces deep-dive thesis docx+pdf into the shared `Candidates/deep_dives/` folder. Maintains the unified `Candidates/candidates_index.json` (schema 3.0). At the end of each run, regenerates `Candidates/ALL_CANDIDATES.pdf` and `Candidates/per_tool/<tool>_candidates.pdf`. Never writes outside `Reporting Hub/`.

---

## Phase 0 — Acquire hub lock

Same as `reporting-hub-performance` Phase 0 — acquire `Reporting Hub/REPORTING_SESSION_LOCK.md` with 4h stale-window rule. Session tag: `reporting-hub-deep-dives-<YYYY-MM-DDTHHMM>`. Open run log at `Reporting Hub/working/thesis_run_<YYYY-MM-DD>_<HHMM>.log`.

## Phase 1 — Orient

1. Read `Reporting Hub/REPORTING_INSTRUCTIONS.md`.
2. Read `Reporting Hub/SOURCES.md`.
3. Build `availability[]` map for each tool (same logic as performance task).

## Phase 2 — Per-tool candidate scan

For each `operational` tool, in order:

### Tool: investment_tool
- Read `Investment tool/investment_discovery_system/candidates/*.md` — list all candidate files with their MD5 hashes.
- Load registry: `Reporting Hub/Candidates/candidates_index.json` (schema 3.0).
- For each candidate, decide regeneration per Phase 2a rules below.
- For each ticker requiring regeneration, produce:
  - docx: `Reporting Hub/Candidates/deep_dives/docx/<TICKER>_<YYYY-MM-DD>.docx`
  - pdf:  `Reporting Hub/Candidates/deep_dives/pdf/<TICKER>_<YYYY-MM-DD>.pdf`
- Atomic write pattern (write to `<path>.tmp`, fsync, rename).
- Update registry entry with: `source_tool: "investment_tool"`, `source_md5`, `thesis_version`, `docx_path`, `pdf_path`, `pdf_bytes`, `prose_word_count`, **`hypothesis`** (2-3 sentence plain-English thesis distilled from the deep dive), **`next_key_dates`** (list of `{date, event, nature}`), **`catalyst_category`**, **`conviction`**, **`status`**, `last_updated`.

### Tool: non_us
- Read `Investmet tool Beta/non_us_discovery_system/candidates/*.md`.
- Target output: `Reporting Hub/Candidates/deep_dives/{docx,pdf}/<TICKER>_<MIC>_<YYYY-MM-DD>.{docx,pdf}` — same flat folder as investment_tool so they share the canonical deep_dives tree.
- Registry entry goes in the same `Candidates/candidates_index.json` with `source_tool: "non_us"`.
- Use `Reporting Hub/working/build_scripts/pdf_gen.py` / `build_all.py` as the rendering helpers.
- When regenerating, clear any `needs_backfill: true` flag and populate `hypothesis` / `next_key_dates` / `catalyst_category` / `conviction` properly.

### Tool: litigation
- Availability is `pre-launch` until Delta has its first operational producer run. Skip silently, log once.
- Future: read from `Investment tool Delta/litigation_system/candidates/`, write to `Reporting Hub/Candidates/deep_dives/{docx,pdf}/`, register in `Reporting Hub/Candidates/candidates_index.json` with `source_tool: "litigation"`.

### Tool: silence
- `pre-launch`. Skip silently.
- Future: same pattern — write deep-dives into `Reporting Hub/Candidates/deep_dives/{docx,pdf}/`, register with `source_tool: "silence"`.

### Phase 2a — Regeneration decision rules (per ticker)

A thesis is regenerated when ANY of:
- (a) **No prior thesis** for this ticker in the registry (first-time generation).
- (b) **Source file MD5 changed** since the last thesis AND the change is not monitoring-log-only (inspect diff; if only trailing log entries added, mark `PATCHED` not `REGENERATED`).
- (c) **Thesis older than freshness_window_days** (default 7) AND catalyst is still active (not `delivered`).
- (d) **Material external news** surfaced by WebSearch during kill-sweep that contradicts or strengthens the existing thesis.
- (e) **`needs_backfill: true` flag set on the registry entry** (e.g., legacy non-US entries migrated without hypothesis/key dates). Regenerate and clear the flag.

Otherwise: `SKIP` with reason logged.

## Phase 3 — Thesis generation

For each ticker requiring regeneration:

1. Read the source candidate .md in full.
2. Read adjacent SESSION_STATE / TIME_SENSITIVE context from the source tool.
3. Run WebSearch kill-sweep (6-month window for drug PDUFAs, shorter for merger arb).
4. Compose thesis following the canonical template. Target 2500–4500 prose words; complex situations may go longer.
5. Render docx via python-docx, convert to PDF via libreoffice (preferred) or reportlab (fallback).
6. **Extract the registry-level metadata from the deep dive**: a 2-3 sentence `hypothesis` (distilled from the thesis summary), `next_key_dates` (pulled from catalyst calendar in the thesis), `catalyst_category`, and `conviction`. This is what the master summaries render from, so it must be honest and non-empty.
7. Archive any superseded prior thesis to `Reporting Hub/archive/<YYYY-MM-DD>_<TICKER>_superseded/` before overwriting.

## Phase 4 — Update registry atomically

1. Read `Reporting Hub/Candidates/candidates_index.json`.
2. Add/update entry for each regenerated ticker, including all schema 3.0 fields (`hypothesis`, `next_key_dates`, `catalyst_category`, `conviction`, `status`).
3. Append a `run_history` entry summarizing this run (timestamp, decisions per ticker, housekeeping).
4. Write to `candidates_index.json.tmp`, validate JSON parses, fsync, `os.replace(tmp, candidates_index.json)`.
5. If parse fails after write, restore previous version from in-memory backup and log critical error.

**Corruption-prevention upgrade:** Do not stream writes into `candidates_index.json` directly.

## Phase 5 — Render master summaries

After the registry is fully updated:

1. Run `python3 "Reporting Hub/working/build_scripts/render_all_candidates.py"` → writes `Candidates/ALL_CANDIDATES.pdf` and `Candidates/ALL_CANDIDATES.md`.
2. Run `python3 "Reporting Hub/working/build_scripts/render_per_tool_candidates.py"` → writes `Candidates/per_tool/<tool>_candidates.pdf` for each of the 4 tools (empty tools still get a placeholder PDF).

Each script reads ONLY from `Candidates/candidates_index.json`. If the registry parse fails, skip this phase and log a critical error rather than writing stale masters.

## Phase 6 — Self-audit

1. Grep run log for writes outside `Reporting Hub/`. If any, write violation file, don't release lock.
2. Confirm `Candidates/candidates_index.json` parses.
3. Confirm every new docx/pdf exists and is non-zero-byte.
4. Confirm `ALL_CANDIDATES.pdf` was regenerated this run (mtime within last 10 min).
5. Append `SELF_AUDIT: OK` to run log.

## Phase 7 — Release lock

Same as performance task.

---

## Expected per-run volume

| Tool | Typical new regenerations | Time |
|---|---|---|
| investment_tool | 0–1 per run (most SKIP) | 5–15 min |
| non_us | 0–3 per run during active Japan TSE cycles (+ backfill cycles until all `needs_backfill` flags cleared) | 5–20 min |
| litigation | 0 (pre-launch) | — |
| silence | 0 (pre-launch) | — |

Master-summary rendering: < 30 seconds.

Total expected runtime: 10–40 minutes. Well within 4-hour cadence.

## Never-delete rule

When a thesis is superseded (new MD5, PATCH, etc.), **move** the old docx/pdf to `archive/<YYYY-MM-DD>_<TICKER>_superseded/` — never delete. Keep the archive directory under the hub's `archive/` tree for audit parity.

## Failure modes quick-reference

Same table as performance task. Plus:

| Condition | Action |
|---|---|
| candidates_index.json parse failure after write | Restore in-memory backup, log critical, don't release lock |
| docx→pdf conversion fails | Retry with reportlab; if both fail, log error, skip ticker, continue |
| Source candidate .md read fails | Skip ticker, continue |
| WebSearch timeout | Log, proceed with cached context |
| Master-summary render script fails | Log critical, leave previous master in place, do NOT release lock until investigated |
