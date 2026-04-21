# INSTRUCTIONS — Litigation & Docket Signal System (Tool 3)

The architectural reference. Every session reads this after `SESSION_STATE.md` during cold start. It does not replace `PROJECT_INSTRUCTIONS.md` (the charter) — it is the implementation-layer complement.

---

## 1. Cold Start Protocol

Run these in order at the start of every session:

1. **Concurrency check** — read `SESSION_LOCK.md`.
   - If `LOCKED` and timestamp < 4h old: STOP. Abort the session. Do nothing else.
   - Otherwise: overwrite with `LOCKED / Timestamp: <UTC ISO> / Session: <identifier>`.
2. **Install deps** — `pip install requests beautifulsoup4 lxml yfinance openpyxl pandas pypdf rapidfuzz reportlab python-docx --break-system-packages`. Dependencies reset between sessions; never skip.
3. **Read SESSION_STATE.md** — current phase, actives, warnings, next queue.
4. **Read this INSTRUCTIONS.md** (if cold-starting; may be skipped on subsequent reads in-session).
5. **Read OPEN_QUESTIONS.md** — only if `SESSION_STATE.md` flags blockers referring to open questions.
6. **Tool Validation Protocol** — see section 14 below.
7. Read the ONE task-specific file needed for the current work block (a strategy spec, the scoring rubric, a failure mode note). Do NOT read all files.

After these steps, you MUST be able to state in one minute: "Here is what I will do next." If you cannot, `SESSION_STATE.md` has failed; the first work action of this session is to fix it.

---

## 2. System Architecture

Six layers, stacked, each gating the one below. Derived from the architecture diagram in `project set up template/_scratch_diagram/`.

```
LAYER 0 — GOVERNANCE (PROJECT_INSTRUCTIONS.md — Prime Directive, Creativity, Reasoning, 12-point Self-Review)
          ↓
LAYER 1 — SESSION CONTINUITY (SESSION_STATE ↔ SESSION_LOCK ↔ INSTRUCTIONS ↔ DECISIONS ↔ OPEN_QUESTIONS ↔ PROGRESS_LOG ↔ INDEX)
          ↓
LAYER 2 — DATA & SOURCE DISCIPLINE (endpoints verified live; every fact labeled VERIFIED/INFERRED/SPECULATED)
          ↓
LAYER 3 — ANALYTICAL PIPELINE (scan → triage → party-resolve → entity-resolve → converge → score → deep dive → deliver)
          ↓
LAYER 4 — EXECUTION (File tools, shell/Python, WebFetch/WebSearch, scheduled-tasks MCP, memory)
          ↓
LAYER 5 — SKILL LIBRARY (docx, pdf, pptx, xlsx, data:*, skill-creator, schedule)
          ↓
LAYER 6 — AUTONOMOUS MODE (interactive: AskUserQuestion OK; scheduled: log to OPEN_QUESTIONS, fail forward)
```

---

## 3. Common Data Format (Signal JSON Schema)

Per D-004, the outer schema matches Tool 1 / Tool 2 verbatim. Litigation-specific fields live inside `raw_data`.

```json
{
  "entity_id": "0000320193",
  "entity_aux_id": "AAPL",
  "entity_name": "Apple Inc.",
  "entity_size_metric": 3000000000000,
  "signal_type": "motion_to_dismiss_denied",
  "signal_category": "federal_civil",
  "strength_estimate": 4.2,
  "source_url": "https://www.courtlistener.com/docket/...",
  "source_date": "2026-04-12",
  "scan_date": "2026-04-14T10:00:00Z",
  "raw_data": {
    "court": "N.D. Cal.",
    "case_number": "3:25-cv-01234",
    "case_caption": "Foo Corp. v. Apple Inc.",
    "docket_entry": "Order denying motion to dismiss",
    "docket_entry_id": "1234567",
    "party_role": "defendant",
    "party_raw_name": "Apple Inc.",
    "resolution_method": "sec_edgar_exact",
    "resolution_confidence": 0.95,
    "document_status": "in_recap",
    "pacer_cost_estimate_cents": 0
  }
}
```

Dedup key across all scanners: `(court, case_number, docket_entry_id)` per F-06. Never caption-based.

---

## 4. The Six Channels Table

| # | Channel | Strategy file | Scanner file | Cadence | Primary endpoint |
|---|---------|--------------|--------------|---------|------------------|
| 1 | Federal Civil | `strategies/strategy_federal_civil.md` | `tools/pacer_recap_scanner.py` | 6h | CourtListener RECAP API |
| 2 | ITC 337 | `strategies/strategy_itc_337.md` | `tools/itc_337_scanner.py` | 12h | edis.usitc.gov |
| 3 | PTAB IPR | `strategies/strategy_ptab_ipr.md` | `tools/ptab_ipr_scanner.py` | 24h | developer.uspto.gov/api-catalog/ptab-api-v2 |
| 4 | Delaware Chancery | `strategies/strategy_delaware_chancery.md` | `tools/delaware_chancery_scanner.py` | 12h | courts.delaware.gov + RSS |
| 5 | SEC Enforcement | `strategies/strategy_sec_enforcement.md` | `tools/sec_enforcement_scanner.py` | 6h | sec.gov/litigation/litreleases + EDGAR |
| 6 | DOJ/FTC Antitrust | `strategies/strategy_doj_ftc_antitrust.md` | `tools/doj_ftc_antitrust_scanner.py` | 12h | justice.gov/atr + ftc.gov |

Per-channel cadence is tracked in `baselines/scanner_last_run.json` so the operational dispatcher (6h cron) only fires scanners whose cadence is due.

---

## 5. Signal Pipeline (Stages)

Every raw signal from any scanner traverses these stages in order. A signal can be dropped at any stage; the drop is logged.

```
1. SCAN          — scanner fetches raw docket entries within its rolling window
                   (window = cadence × 1.5 per F-08 anti-fencepost rule)
2. TRIAGE        — whitelist signal_type; confirm docket_entry_id uniqueness
                   (dedup against signals/); discard ministerial noise (F-07)
3. PARTY-RESOLVE — two-stage: normalize party string → classify → resolve to CIK
                   (per CONTEXT Entity Resolution Protocol)
4. ENTITY-RESOLVE — CIK → ticker → OpenFIGI; apply $300M market-cap floor
                    (reject if < $300M)
5. CONFIDENCE-GATE — require resolution_confidence ≥ 0.85 to admit to scoring;
                     0.70–0.85 admitted only if corroborated by another channel
                     within 30d; <0.70 dropped (logged to working/unresolved_parties.md)
6. CONVERGE      — check 30d rolling window for same issuer_figi across channels;
                   emit convergence bonus annotation (D-005)
7. SCORE         — apply 7-dim rubric (framework/scoring_system.md); compute final
8. PROMOTE       — score ≥ 28: candidate writeup in candidates/
                   22–27: watchlist entry (working/watchlist.json)
                   14–21: archive to candidates/archive/ (log only)
                   <14 : discard (log only)
9. DEEP DIVE     — for score ≥ 28 only: web research (NARRATIVE layer), kill-condition
                   check, self-review checklist (12 items)
10. DELIVER      — operational session writes daily report to reports/;
                   deep-dive scheduled task writes docx+pdf to reporting_layer/
11. MONITOR      — existing candidates: check kill conditions on every operational
                   pass; move to candidates/delivered/ on resolution
```

Stage 9 is the mandatory NARRATIVE web-research layer. No candidate leaves the system without it.

---

## 6. Execution Model

- **In-process**: party resolution, entity resolution, scoring, convergence detection, kill-condition checks.
- **Spawned subprocess (hard-killed at 120s)**: every scanner. Scanner has internal soft-budget of 45s (F-15).
- **Per-endpoint rate-limit logic**: every probe, validation call, and production scan call goes through a single `http_client.py` with backoff and 429 handling. No one-off `requests.get` (F-16).
- **PDF parsing**: in-process with `pypdf`. PTAB FWD claim-outcome parsing specifically wrapped in try/except with `outcome_parse_status` recorded per F-09.

---

## 7. Daily Session Flow (Operational Mode)

Ordered. Scanning is step 1 of 11, not the whole job. See PROJECT_INSTRUCTIONS §7 and PROJECT_TEMPLATE Part 8 (anti-early-stop).

```
1. Acquire lock, install deps, read SESSION_STATE + this file.
2. Tool Validation Protocol (§14).
3. Determine mode — Build / Operational / Blocked — from SESSION_STATE phase.
4. For Operational mode: dispatch only scanners whose per-channel cadence is due
   (consult baselines/scanner_last_run.json). Each scanner runs as subprocess.
5. Aggregate raw signals into signals/<YYYY-MM-DD>/.
6. Run triage + party-resolve + entity-resolve across all new signals.
7. Run convergence engine across the rolling-30d signal inventory.
8. Score surviving signals; promote to candidates/ or watchlist as rubric dictates.
9. Monitor actives (candidates/) for kill conditions.
10. Regenerate daily report: reports/YYYY-MM-DD.md.
11. Shutdown protocol (§11).
```

Anti-early-stop reminder (quoted verbatim from PROJECT_TEMPLATE Part 8):
> Running scanners is step 1 of many. There is ALWAYS more work. The only valid stop is ALL work genuinely blocked, in which case document blockers in OPEN_QUESTIONS.md and SESSION_STATE.md.

---

## 8. Daily Report Contents (`reports/YYYY-MM-DD.md`)

1. **Headline** — single sentence: highest-score new candidate, or "no new 28+ signals today" with top score seen.
2. **New candidates (score ≥ 28)** — one block each: entity, score breakdown, source URL(s), proposed thesis.
3. **Watchlist additions (22–27)** — compressed one-liners.
4. **Convergences detected** — issuer_figi across ≥ 2 channels in 30d window.
5. **Known-to-Tool-1 callouts** — candidates whose underlying event may also appear in Tool 1's pipeline (F-20 informational).
6. **Active-litigation tags** — cases with > 5 signals in 7d (F-17).
7. **Active candidate monitoring** — kill conditions checked, status deltas.
8. **Scanner health** — per-scanner: compiled? reachable? signal count. Mirrors SESSION_STATE Tool Health.
9. **Warnings** — any new warnings from this session.

---

## 9. Execution Environment

```bash
pip install requests beautifulsoup4 lxml yfinance openpyxl pandas pypdf rapidfuzz reportlab python-docx --break-system-packages
```

Path mapping (Cowork sandbox):
- `C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\Investment tool Delta\litigation_system\` → `/sessions/.../mnt/Investment tool Delta/litigation_system/`
- Working scratch: the session's outputs folder (ephemeral, not this tool's durable state).

User-Agent header for all SEC/EDGAR calls: `"Tool3-Litigation-Scanner (javiergorordo13@hotmail.com)"`. SEC requires a valid-email User-Agent.

---

## 10. Folder Structure

```
litigation_system/
├── PROJECT_INSTRUCTIONS.md     # charter
├── README.md                   # cold-start entry
├── INSTRUCTIONS.md             # this file
├── OBJECTIVES.md               # goals, mandate, success criteria
├── CONTEXT.md                  # domain background, endpoints, schema
├── SESSION_STATE.md            # relay baton — rewritten every session
├── SESSION_LOCK.md             # concurrency gate — LOCKED/UNLOCKED
├── PROGRESS_LOG.md             # append-only per-session log
├── INDEX.md                    # map of every file
├── DECISIONS.md                # D-000..D-013 (and onward)
├── OPEN_QUESTIONS.md           # Q-001..Q-003 (and onward)
│
├── framework/
│   ├── scoring_system.md       # 7-dim rubric, thresholds, worked example
│   └── candidate_template.md   # shape of every candidate writeup
├── strategies/
│   ├── strategy_federal_civil.md
│   ├── strategy_itc_337.md
│   ├── strategy_ptab_ipr.md
│   ├── strategy_delaware_chancery.md
│   ├── strategy_sec_enforcement.md
│   └── strategy_doj_ftc_antitrust.md
├── tools/
│   ├── pipeline_runner.py      # dispatcher
│   ├── http_client.py          # shared rate-limited HTTP wrapper
│   ├── party_resolver.py       # two-stage D-003 protocol
│   ├── openfigi_resolver.py
│   ├── convergence_engine.py
│   ├── scoring.py
│   ├── executive_lookup_builder.py
│   ├── build_exhibit21_map.py
│   ├── pacer_recap_scanner.py
│   ├── itc_337_scanner.py
│   ├── ptab_ipr_scanner.py
│   ├── delaware_chancery_scanner.py
│   ├── sec_enforcement_scanner.py
│   └── doj_ftc_antitrust_scanner.py
├── signals/                    # raw signal JSON by date
├── candidates/
│   ├── delivered/              # resolved outcomes
│   ├── archive/                # superseded writeups
│   └── pending_pacer/          # needs user-directed PACER pull (D-008, F-05)
├── reports/                    # daily operational reports
├── working/                    # scratch + pacer_pulls_requested.md, watchlist.json, unresolved_parties.md
├── research/                   # persistent investigative notes that outlive a session
├── baselines/                  # party_resolution_cache, executive_lookup, exhibit21_map, scanner_last_run
├── archive/                    # superseded files, per date+reason
└── skills/                     # authored SKILL.md source of truth (D-013)
    ├── litigation-operational/SKILL.md
    ├── litigation-maintenance/SKILL.md
    ├── litigation-performance-report/SKILL.md
    └── litigation-deep-dives/SKILL.md

reporting_layer/
├── performance_reports/        # PDFs, daily
├── litigation_briefs/
│   ├── docx/
│   ├── pdf/
│   └── index.json              # dedup registry
├── working/                    # scratch for reporting tasks
└── archive/                    # superseded deliverables
```

---

## 11. Shutdown Protocol

Run in this exact order. Step 5 is last — if the session dies before step 5, the 4-hour stale-lock window recovers automatically but clean release is always preferred.

```
1. Flush all working state to files. Incomplete work → working/. Complete → final location.
2. Overwrite SESSION_STATE.md — relay baton for next session.
3. Append session block to PROGRESS_LOG.md:
   ## Session N — YYYY-MM-DD
   ✅ Completed: ...
   🔄 In progress: ...
   ⏭️ Next: ...
   ⚠️ Blockers: ...
4. Update INDEX.md if any file was created or substantially changed.
5. Overwrite SESSION_LOCK.md: UNLOCKED / Timestamp: <UTC> / Session: completed.
```

Hierarchy under context pressure: handoff quality > output quality > output volume.

---

## 12. Session Rules

- **No chat questions in scheduled sessions** — append to `OPEN_QUESTIONS.md` and continue.
- **No `rm` on SESSION_LOCK.md**, ever (D-011). Overwrite only.
- **4-hour stale-lock window** (D-011).
- **Write-scope isolation** — `litigation_system/` is written only by operational/maintenance; `reporting_layer/` is written only by performance-report/deep-dives. Never cross.
- **Settled decisions are not re-litigated** — override via new numbered decision; never edit old ones.
- **Signal dedup key is `(court, case_number, docket_entry_id)`** — never caption-based (F-06).
- **Convergence keys on `issuer_figi`** — never on party-name string (D-003).
- **Per-scanner subprocess hard-kill at 120s**, soft-budget 45s (F-15).

---

## 13. Scheduled Tasks

Per D-012 and PROJECT_TEMPLATE Part 4. Cron offsets are in LOCAL time.

| # | Task | Cron | Write scope | Concurrency |
|---|------|------|-------------|-------------|
| 1 | `litigation-operational` | `0 */6 * * *` | `litigation_system/` | SESSION_LOCK |
| 2 | `litigation-maintenance` | `50 */6 * * *` | `litigation_system/` (audit-only) | SESSION_LOCK |
| 3 | `litigation-performance-report` | `30 1 * * *` | `reporting_layer/performance_reports/` | independent |
| 4 | `litigation-deep-dives` | `30 */8 * * *` | `reporting_layer/litigation_briefs/` | independent |

SKILL.md files are authored at `skills/<task-id>/SKILL.md` per D-013. At registration time the content is copied into the MCP store.

---

## 14. Tool Validation Protocol

Run at the start of every operational and every maintenance session. Log results to `SESSION_STATE.md` Tool Health table.

```python
# Step 1 — py_compile every tool
import py_compile, pathlib, json, datetime
results = {}
for p in sorted(pathlib.Path("litigation_system/tools").glob("*.py")):
    try:
        py_compile.compile(str(p), doraise=True)
        results[p.name] = {"compiles": True, "error": None}
    except py_compile.PyCompileError as e:
        results[p.name] = {"compiles": False, "error": str(e)}

# Step 2 — reachability probe for each endpoint
# (use the production http_client; never a one-off requests.get — F-16)
from tools.http_client import head_probe
endpoints = json.loads(pathlib.Path("litigation_system/baselines/endpoints.json").read_text())
for name, url in endpoints.items():
    results[name] = head_probe(url)

# Step 3 — write Tool Health block into SESSION_STATE.md
```

Silent file truncation is the primary bug this catches — a `.py` that imports fine but is missing its tail will fail `py_compile`'s parser (PROJECT_TEMPLATE Part 14). Running on every session means the bug can't live longer than one cron interval.

---

## 15. Implementation Priority Queue (current)

Phase 0 complete as of 2026-04-14 Session 1. Current priority queue:

1. Phase 1 — endpoint validation live-probe (all 15 planned endpoints). Upgrade CONTEXT table from ⚠️ UNVERIFIED to ✅ VERIFIED.
2. Phase 1 — build `tools/http_client.py` (shared rate-limited HTTP + 429 backoff per F-16).
3. Phase 1 — build `tools/party_resolver.py` implementing D-003.
4. Phase 1 — build `tools/build_exhibit21_map.py`, run once to populate `baselines/exhibit21_map.json`.
5. Phase 1 — build `tools/executive_lookup_builder.py`, run once to populate `baselines/executive_lookup.json`.
6. Phase 1 — validate resolver on 100-case manually-labeled test set. Target ≥ 80% precision at confidence ≥ 0.85.
7. Phase 2 — build first scanner (`pacer_recap_scanner.py`). Gate: Phase 1 complete.

Each Phase is gated by the success criteria in PHASING (`project set up template/litigation_tool_bootstrap/LITIGATION_PHASING.md`).
