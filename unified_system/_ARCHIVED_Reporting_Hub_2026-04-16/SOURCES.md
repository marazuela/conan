# Reporting Hub — SOURCES manifest

**Version:** 1.0
**Purpose:** Authoritative list of read-only input paths per producer tool. Hub tasks resolve all paths through this file.

All paths are relative to the project root: `C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\`.

---

## Tool 1: investment_tool

**Status:** operational
**Folder:** `Investment tool/investment_discovery_system/`
**Deliverable type:** US equities investment theses

### Read paths

| Path | Purpose |
|---|---|
| `Investment tool/investment_discovery_system/SESSION_STATE.md` | Latest pipeline state, active candidates, funnel counts |
| `Investment tool/investment_discovery_system/PROGRESS_LOG.md` | Change log for trend/activity views |
| `Investment tool/investment_discovery_system/TIME_SENSITIVE.md` | Upcoming catalysts (PDUFA, earnings, court) |
| `Investment tool/investment_discovery_system/SESSION_LOCK.md` | Concurrency check — skip this tool if ACTIVE |
| `Investment tool/investment_discovery_system/candidates/` | Candidate markdown files (deep-dive inputs) |
| `Investment tool/investment_discovery_system/signals/` | Triage queue (funnel-count inputs) |
| `Investment tool/investment_discovery_system/reports/` | Per-run operational reports |
| `Investment tool/investment_discovery_system/Report Summary/` | Rolling summaries |
| `Investment tool/investment_discovery_system/outputs/` | Potential_Opportunities.docx |

### Hub output targets

- Performance: `Performance/per_tool/investment_tool/<YYYY-MM-DD>_performance.pdf`
- Deep dives: `Candidates/deep_dives/{docx,pdf}/<TICKER>_<YYYY-MM-DD>.{docx,pdf}`
- Registry: `Candidates/candidates_index.json` (schema 3.0, with `source_tool: "investment_tool"`)
- Per-tool candidate summary: `Candidates/per_tool/investment_tool_candidates.pdf`

---

## Tool 2: non_us

**Status:** operational (mid-build — Phase 3 complete, Phase 4 partial)
**Folder:** `Investmet tool Beta/non_us_discovery_system/`
**Deliverable type:** Non-US equities candidate deep dives

### Read paths

| Path | Purpose |
|---|---|
| `Investmet tool Beta/non_us_discovery_system/SESSION_STATE.md` | Pipeline state |
| `Investmet tool Beta/non_us_discovery_system/PROGRESS_LOG.md` | Change log |
| `Investmet tool Beta/non_us_discovery_system/SESSION_LOCK.md` | Concurrency check |
| `Investmet tool Beta/non_us_discovery_system/candidates/` | Candidate markdown files |
| `Investmet tool Beta/non_us_discovery_system/signals/` | Triage queue (if present) |
| `Investmet tool Beta/non_us_discovery_system/reports/` | Operational reports |

### Hub output targets

- Performance: `Performance/per_tool/non_us/<YYYY-MM-DD>_performance.pdf`
- Deep dives: `Candidates/deep_dives/{docx,pdf}/<TICKER>_<MIC>_<YYYY-MM-DD>.{docx,pdf}`
- Registry: `Candidates/candidates_index.json` (schema 3.0, `source_tool: "non_us"`)
- Per-tool candidate summary: `Candidates/per_tool/non_us_candidates.pdf`

---

## Tool 3: litigation

**Status:** pre-launch (system folder exists, producer scheduled tasks not yet created)
**Folder:** `Investment tool Delta/litigation_system/`
**Deliverable type:** Litigation-driven investment briefs

### Read paths (when operational)

| Path | Purpose |
|---|---|
| `Investment tool Delta/litigation_system/SESSION_STATE.md` | Pipeline state |
| `Investment tool Delta/litigation_system/PROGRESS_LOG.md` | Change log |
| `Investment tool Delta/litigation_system/SESSION_LOCK.md` | Concurrency check |
| `Investment tool Delta/litigation_system/candidates/` | Candidate files |

### Hub output targets

- Performance: `Performance/per_tool/litigation/<YYYY-MM-DD>_performance.pdf`
- Deep dives: `Candidates/deep_dives/{docx,pdf}/<CASE>_<YYYY-MM-DD>.{docx,pdf}`
- Registry: `Candidates/candidates_index.json` (schema 3.0, `source_tool: "litigation"`)
- Per-tool candidate summary: `Candidates/per_tool/litigation_candidates.pdf`

### Availability flag

`pre-launch` — hub tasks log "skipped: litigation pre-launch" and continue.

---

## Tool 4: silence

**Status:** pre-launch (bootstrap template only, no operational files)
**Folder:** `Investment tool Gamma/silence_tool_bootstrap/`
**Deliverable type:** Silence-dimension probabilistic reports on Russell 1000

### Read paths (when operational)

TBD — populate when bootstrap completes.

### Hub output targets

- Performance: `Performance/per_tool/silence/<YYYY-MM-DD>_performance.pdf`
- Deep dives: `Candidates/deep_dives/{docx,pdf}/<TICKER>_silence_<YYYY-MM-DD>.{docx,pdf}`
- Registry: `Candidates/candidates_index.json` (schema 3.0, `source_tool: "silence"`)
- Per-tool candidate summary: `Candidates/per_tool/silence_candidates.pdf`

### Availability flag

`pre-launch` — skip silently.

---

## Cross-tool outputs (hub-generated, no specific producer source)

- `Performance/ALL_TOOLS_PERFORMANCE.pdf` — single canonical fleet dashboard: availability states, aggregated funnel, active candidate counts, cross-tool catalyst calendar, producer task health. Rendered by `working/build_scripts/render_all_tools_performance.py`.
- `Candidates/ALL_CANDIDATES.pdf` — single canonical cross-tool candidate summary: ticker, source tool, hypothesis, next key dates, status, conviction. Rendered by `working/build_scripts/render_all_candidates.py`.

---

## How to add a new tool

1. Create the tool's producer folder under `Conan/<tool_name>/<domain>_system/` following the PROJECT_TEMPLATE pattern (but **without** a `reporting_layer/` subfolder).
2. Add a new section here with status, folder path, read paths, hub output targets, and availability flag.
3. Update both hub task prompts' tool-iteration loops only if the new tool needs custom handling (usually not — the generic loop reads SOURCES.md directly).
4. No changes to scheduled tasks needed — the hub tasks pick up the new entry on next run.
