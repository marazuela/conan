# Reporting Hub — Instructions

**Version:** 1.0
**Created:** 2026-04-15
**Scope:** This is the project-root consolidated reporting folder. It replaces the per-tool `reporting_layer/` folders that existed prior to 2026-04-15.

## Mandate

This hub is the sole consumer and sole reporter for every producer tool in this project. It:

1. Generates per-tool performance reports by reading each tool's operational state files.
2. Generates deep-dive investment thesis PDFs by reading each tool's candidate files.
3. Produces cross-tool integrated views (daily digest, comparative performance) that are impossible to produce from inside a single tool's folder.

## Read-only contract (non-negotiable)

- This hub's scheduled tasks **read** from each producer's folder and **write only** inside `Reporting Hub/`.
- Writing to any producer folder (`Investment tool/`, `Investmet tool Beta/`, `Investment tool Delta/`, `Investment tool Gamma/`) is a P0 violation. If your own run log shows any path outside `Reporting Hub/`, abort and log to `working/violation_<timestamp>.md`.
- Producers do not write here. Ever.

## Authoritative sources

Per-tool read paths are enumerated in `SOURCES.md`. The hub's task prompts resolve paths only through that file. When a new tool comes online, add one entry to `SOURCES.md` — no task-prompt edits needed.

## Concurrency model

Single lock: `REPORTING_SESSION_LOCK.md` at the root of this hub. Hub tasks (`reporting-hub-performance`, `reporting-hub-deep-dives`) acquire it at start and release at end. 4-hour stale window applies — a lock older than 4h is considered dead and may be overwritten.

Producer tools have their own `SESSION_LOCK.md` inside their operational folder. The hub **reads** those locks to decide whether a producer is mid-write — if locked, skip that tool for this cycle and note it in the run log. The hub never modifies producer locks.

## Failure-mode rules

- **Producer locked** → skip that tool this cycle; log `skipped: <tool> locked until <expiry>`; continue with other tools.
- **Producer pre-launch** (folder exists but operational artifacts missing, or `availability: pre-launch` in SOURCES.md) → skip silently; log once.
- **Producer folder missing** → log warning; continue.
- **Hub crash mid-write** → next hub task sees stale `REPORTING_SESSION_LOCK.md` (>4h), overwrites, proceeds. Check `working/` for half-written files and clean them up before generating new ones.
- **index.json corruption** → always write via temp file + atomic rename: `index.json.tmp` → fsync → rename. Back up any pre-existing corrupted file to `archive/<date>_index_json_corrupted_backup/` before overwriting.

## Output conventions (restructured 2026-04-15)

The hub has **two top-level output purposes**. Everything the hub emits lands under one of them.

### Candidates/ — "what are we looking at?"

| Output | Path |
|---|---|
| Master summary of all candidates (cross-tool) | `Candidates/ALL_CANDIDATES.pdf` (+ `.md` sidecar) |
| Per-tool candidate summary | `Candidates/per_tool/<tool>_candidates.pdf` |
| Deep-dive docx (all tools, flat) | `Candidates/deep_dives/docx/<TICKER>[_<MIC>]_<YYYY-MM-DD>.docx` |
| Deep-dive pdf (all tools, flat) | `Candidates/deep_dives/pdf/<TICKER>[_<MIC>]_<YYYY-MM-DD>.pdf` |
| Unified registry | `Candidates/candidates_index.json` (schema 3.0) |

Master summaries carry, for each candidate: ticker, source tool, **hypothesis** (2–3 sentence thesis distillation), **next key dates**, status, conviction. They are rendered **only** from `Candidates/candidates_index.json` — never ad-hoc from source files.

### Performance/ — "how are the tools running?"

| Output | Path |
|---|---|
| Integrated cross-tool dashboard | `Performance/ALL_TOOLS_PERFORMANCE.pdf` (+ `.md` sidecar) |
| Per-tool performance PDF (daily) | `Performance/per_tool/<tool>/<YYYY-MM-DD>_performance.pdf` |

### Support paths

| Output | Path |
|---|---|
| Run log | `working/<task>_<YYYY-MM-DD>_<HHMM>.log` |
| Render helper scripts | `working/build_scripts/*.py` |
| Archived superseded output | `archive/<YYYY-MM-DD>_<reason>/...` |

### Retired top-level folders (do not write to these)

The following folders existed pre-2026-04-15-restructure and are now RETIRED. Any hub task that references them is a regression:

- `performance_reports/` → replaced by `Performance/`
- `investment_theses/` → absorbed into `Candidates/deep_dives/`
- `non_us_candidates/` → absorbed into `Candidates/deep_dives/`
- `litigation_briefs/` → absorbed into `Candidates/deep_dives/` (future)
- `silence_reports/` → absorbed into `Candidates/deep_dives/` (future)

Their snapshots live under `archive/2026-04-15_pre_restructure/` for audit. Self-audit must grep the run log for these paths and flag any hit.

### Registry ownership

`Candidates/candidates_index.json` is the single source of truth for candidates.
- **Writer:** `reporting-hub-deep-dives` only (via atomic tmp+fsync+rename).
- **Readers:** `reporting-hub-performance` (for catalyst calendar), both render scripts (`render_all_candidates.py`, `render_per_tool_candidates.py`), and humans.

## Self-audit step (mandatory, end of every run)

Before releasing the lock, every hub task must:
1. Grep its own run log for any write path that does **not** start with `Reporting Hub/`.
2. If any match found, write `working/violation_<timestamp>.md` describing the attempted path, then abort without releasing the lock (so operator investigates).
3. Otherwise append `SELF_AUDIT: OK — all writes within Reporting Hub/` to the run log and release the lock.

## Scheduled tasks

| Task ID | Cron | Purpose |
|---|---|---|
| `reporting-hub-performance` | `30 2 * * *` | Daily per-tool + cross-tool performance PDFs |
| `reporting-hub-deep-dives` | `30 */4 * * *` | Deep-dive theses for new/updated candidates across all tools |

Task prompts live in `task_prompts/`.

## Migration note

The pre-hub per-tool `reporting_layer/` folders and their historical content are preserved under `archive/2026-04-15_pre_hub_migration/` for audit. Do not delete.
