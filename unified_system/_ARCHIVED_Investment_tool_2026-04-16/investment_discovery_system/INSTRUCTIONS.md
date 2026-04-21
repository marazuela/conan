# Execution Instructions

---

## Cold Start Protocol

Every session — interactive or scheduled — starts with zero memory of previous sessions. This protocol is the bridge.

**Step 1** — Read `SESSION_STATE.md`. This is the relay baton — a small, always-current file that tells you: what phase the project is in, what was last completed, what is in progress, what comes next, and any active warnings or blockers. This is the fastest path to full orientation.

**Step 2** — Read this file (`INSTRUCTIONS.md`) top to bottom. It contains: architecture, pipeline, daily session flow, execution environment, folder structure, session rules, and implementation priority queue.

**Step 3** — If SESSION_STATE.md references blockers or open questions, read `OPEN_QUESTIONS.md`.

**Step 4** — Read ONLY the specific file needed for your current task:
- What is this project? → `README.md`
- Goals and success criteria? → `OBJECTIVES.md`
- Why was X decided? → `DECISIONS.md`
- History of all past sessions? → `PROGRESS_LOG.md`
- Building/running a tool? → Read its strategy spec in `strategies/`
- Scoring a signal? → `framework/scoring_system.md`
- API endpoints? → `CONTEXT.md`
- Writing a candidate? → `framework/candidate_template.md`

**Do NOT read all files.** SESSION_STATE + this file gives full working context. PROGRESS_LOG is history — read it only when you need to understand past decisions, not for current state.

---

## System Architecture (3 Layers)

### Layer 1: Individual Scanners (5 tools)
Each strategy has a Python tool that queries its data source and produces standardized signal objects. Tools run independently and can execute in parallel.

**Common signal output format** (all tools must produce this):
```json
{
  "ticker": "ACME",
  "isin": "US0000000000",
  "company_name": "Acme Corp",
  "market_cap_mm": 1500,
  "signal_type": "distress_keyword",
  "signal_category": "edgar",
  "strength_estimate": 3,
  "source_url": "https://...",
  "source_date": "2026-04-09",
  "scan_date": "2026-04-09",
  "raw_data": {"keyword": "going concern", "filing_type": "10-K", "passage": "..."}
}
```

### Layer 2: Entity Resolution (OpenFIGI)
All signals pass through OpenFIGI normalization to map company names, tickers, ISINs, CUSIPs to a canonical entity. This enables cross-strategy matching. OpenFIGI API: free, no auth, use v3 endpoint (v2 sunsets July 1, 2026).

### Layer 3: Convergence Engine
After entity resolution, checks whether any entity has signals from 2+ strategies within a rolling 14-day window. Convergent signals get bonus scores (+4 for 2 strategies, +8 for 3+) and automatically trigger a full candidate writeup. Maintains a rolling signal log (`signals/signal_log.json`).

---

## The 5 Strategies

| # | Strategy | Source | Tool | Frequency |
|---|----------|--------|------|-----------|
| 1 | EDGAR Keyword Scanning | SEC EFTS + data.sec.gov (free, no auth) | `tools/edgar_filing_monitor.py` ✅ v2.4 | Every 3h |
| 2 | ESMA Short Aggregation | FCA + AMF + AFM + BaFin (free, multi-regulator) | `tools/esma_short_scanner.py` ✅ v2.0 | Daily |
| 3 | Congressional Trading | Capitol Trades HTML scraping (free, no auth) | `tools/congressional_trading.py` ✅ v2.0 | Daily |
| 4 | Contract Award Monitoring | USAspending.gov API (free, no key needed) | `tools/contract_monitor.py` ✅ v1.1 | Every 3h |
| 5 | FDA PDUFA Calendar | ClinicalTrials.gov API + openFDA API (free) | `tools/fda_pdufa_pipeline.py` ✅ v2.0 | Every 3h |

Each strategy has a full spec in `strategies/` with: data source details, API endpoints, signal filters, deep dive analysis checklist, and output format.

---

## Three-Stage Signal Pipeline

### Stage 1: Signal Triage (automated, inside each tool)
Hard filters — signals must pass ALL checks:
- Publicly traded on major exchange
- Market cap ≥ $215M / €200M (via yfinance library)
- Signal is novel (not recurring boilerplate)
- Data is fresh (within scan window)

### Stage 2: Opportunity Scoring (7 dimensions)
Signals that pass triage are scored on: Signal Strength (×2), Catalyst Clarity, Info Asymmetry (×1.5), Risk/Reward, Edge Decay, Liquidity, Catalyst Timeline.
- **Max score**: 42.5 (+ convergence bonus up to +8)
- **28+**: Immediate candidate → full deep dive
- **22–27**: Watchlist → condensed analysis, monitor
- **14–21**: Archive → log, check periodically
- **<14**: Discard
Full rubric with worked example: `framework/scoring_system.md`

### Stage 3: Deep Dive Analysis (Claude reasoning, strategy-specific + web research)
Each strategy has its own deep dive checklist (see strategy specs). Common elements across all:
- Read the actual source document (not just keyword match)
- Company context (market cap, sector, analyst coverage, recent price action)
- Thesis statement (what the market is missing)
- **Web research layer** — mandatory for all candidates. Search for recent news, analyst activity, litigation, regulatory actions, social sentiment. Assess whether findings strengthen, weaken, or leave the thesis neutral. If they reveal a kill condition, flag immediately. Full checklist and template in `framework/scoring_system.md` and `framework/candidate_template.md`.
- Kill conditions (explicit, measurable invalidation criteria)
- Catalyst map (event, date/window, entry/exit triggers)
- Source links (every claim traceable)

---

## Execution Model: Hybrid Architecture

**Python tools** handle data collection (API calls, file parsing, entity resolution, triage filtering).
**Cowork sessions** handle analysis (scoring, cross-referencing, deep dives, candidate writeups, daily reports).

### Daily Session Flow
1. Run all active scanner tools → collect raw signals
2. Triage filter → discard sub-threshold signals
3. OpenFIGI entity resolution → normalize all signals
4. Convergence check → flag any entity with 2+ strategy signals in 14-day window
5. Score surviving signals → apply 7-dimension composite
6. Deep dive on new 28+ scores and convergences → write/update candidate files
7. Update watchlist candidates (22-27) → check for developments
8. Produce daily signal report → save to `reports/`
9. Monitor all existing candidates against kill conditions
10. Append to `PROGRESS_LOG.md`

### Daily Report Contents
- New signals detected (count by strategy, brief summary of each)
- New candidates (28+ scores with one-paragraph thesis)
- Updated candidates (material developments on existing candidates)
- Watchlist movements (signals that crossed thresholds up or down)
- Convergence alerts (highest priority)
- Expired/killed candidates (kill conditions triggered)
- Strategy health check (any scrapers broken, API errors, etc.)

---

## Execution Environment

**Python tools**: All scripts are in `tools/`. Before running:
```bash
pip install requests beautifulsoup4 lxml yfinance openpyxl pandas --break-system-packages
```
- **yfinance**: Required for market cap triage ($215M / €200M floor). Yahoo Finance REST APIs (v7/v10) now require auth — the yfinance library works without auth for US, UK (.L), DE (.DE), FR (.PA), NL (.AS) and other European stocks.
- EDGAR script (`edgar_filing_monitor.py`): requires User-Agent header with valid email. Edit `USER_AGENT` variable.
- USAspending.gov API: free, no auth, no key required.
- Capitol Trades: free, no auth. HTML scraping (Quiver Quantitative now requires auth — see D-013).
- ClinicalTrials.gov API v2: free, no auth required. Use `query.term` for searches (not `filter.phase`).
- openFDA API: free, no auth required.
- OpenFIGI API: free, no auth, use v3 endpoint.
- Output: each tool writes JSON signal files to `signals/`. Sessions read these for analysis.

**New tool pattern**: When building a new strategy tool, follow this structure:
1. Configuration block at top (API URLs, filters, thresholds)
2. Data collection functions (API calls, file parsing)
3. Triage filter function (apply $215M / €200M market cap + other gates)
4. Signal output function (produce common JSON format)
5. CLI entry point

---

## Folder Structure

```
investment_discovery_system/
├── README.md               ← Project entry point: what, status, how to start
├── OBJECTIVES.md           ← Primary goal, mandate, success criteria, definition of done
├── INSTRUCTIONS.md         ← THIS FILE — execution rules, architecture, pipeline
├── CONTEXT.md              ← API reference table, strategy rationale, scoring quick ref
├── DECISIONS.md            ← All decisions with rationale + alternatives rejected
├── OPEN_QUESTIONS.md       ← Unresolved issues, blockers, items needing user input
├── SESSION_STATE.md        ← Relay baton — overwritten each session with current state
├── SESSION_LOCK.md         ← Concurrency lock — exists only while a session is active
├── INDEX.md                ← Live file inventory (update when files change)
├── PROGRESS_LOG.md         ← Append-only session log (current status + next actions)
├── strategies/             ← 5 strategy specs (APIs, filters, deep dive checklists)
├── framework/
│   ├── scoring_system.md   ← 7-dimension scoring rubric + triage gates + worked example
│   └── candidate_template.md ← Standardized candidate writeup template
├── signals/                ← JSON signal files from tools (rolling)
├── candidates/             ← Candidate writeups (TICKER_description.md)
├── reports/                ← Daily signal reports
├── tools/                  ← Python scripts (.py)
├── outputs/                ← Deliverables (.docx, .xlsx)
├── working/                ← In-progress drafts
├── research/               ← Raw findings, source material, API test outputs
├── Report Summary/         ← Tool performance, candidate pipeline, status reports
└── archive/                ← Superseded versions (never delete, move here)
```

---

## Session Rules

### Start
1. **Concurrency check** (scheduled sessions only): Read `SESSION_LOCK.md`. If the first line says `LOCKED` and the timestamp is less than 4 hours old, another session is still active — stop immediately, do nothing. If the first line says `UNLOCKED`, or the timestamp is stale (>4 hours), overwrite the file with `LOCKED` + current UTC timestamp and proceed. Interactive sessions with Pedro present skip this check. **Important**: `SESSION_LOCK.md` uses overwrite-only semantics (never delete — sandbox can't delete files). At shutdown, overwrite it with `UNLOCKED` + current timestamp.
2. Read `SESSION_STATE.md` → this file → `OPEN_QUESTIONS.md` (if blockers flagged) → task-specific file.
3. Install dependencies: `pip install requests beautifulsoup4 lxml yfinance openpyxl pandas --break-system-packages` (sandbox resets every session).
4. **Tool Validation Protocol** (MANDATORY every session): After installing dependencies, run a quick health check on ALL 5 scanner tools. For each tool, verify: (a) the script compiles without errors (`python -c "import tools.X"`), (b) the external data source is reachable (test API call or HEAD request), (c) the tool matches the current design spec (correct thresholds, correct data sources, correct output format). If any tool fails validation, analyze why and proactively fix it before proceeding with the scan pipeline. Log validation results in SESSION_STATE.md under "Tool Health". This prevents silent tool degradation across sessions.
5. State your understanding of current status and proposed next action before executing.

### During
6. **Work until usage limit — NO EXCEPTIONS. This is the most important rule.** Do not stop early. Do not "wrap up" when there is remaining capacity. If a task completes, immediately start the next task from the priority queue or SESSION_STATE's next actions. The ONLY acceptable reason to stop before the limit is if ALL actionable work is genuinely blocked AND the shutdown protocol has been completed. "I've completed the main task" is NOT a reason to stop — there is ALWAYS a next task. This rule exists because sessions have consistently stopped early, wasting capacity. Read the anti-pattern warnings below.
7. **Save after every discrete unit of work** — every completed tool, every finished analysis, every file created. Never hold significant work only in context.
8. **Update `INDEX.md`** when files are created, renamed, or deleted.
9. **Update this file** when Tool Status changes or Priority Queue items complete.
10. **Update `DECISIONS.md`** when any meaningful decision is made.
11. **Update `OPEN_QUESTIONS.md`** when a blocker is found or resolved.
12. **Never delete files** — move superseded work to `archive/` with a dated suffix.
13. **One concept per file** — no monolithic documents.
14. **Cross-link using relative paths** — update all references when files move.

### ANTI-EARLY-STOP RULES (READ CAREFULLY)
These rules exist because sessions have repeatedly stopped early despite explicit instructions not to. This wastes the user's scheduled session capacity.

**Common early-stop anti-patterns — DO NOT DO ANY OF THESE:**
- "I've completed the daily scan pipeline, let me wrap up" → NO. Run the scan, then score signals, then write candidates, then check existing candidates, then update the Report Summary, then review and improve tools.
- "All scanners ran successfully, writing SESSION_STATE" → NO. Running scanners is step 1 of 10. Keep going.
- "I'll save my progress and the next session can continue" → NO. Continue NOW. Save progress continuously, but do not stop working.
- "The main objective has been achieved" → NO. The main objective is to maximize productive output until the usage limit. There is always more to do.
- "Let me update the tracking files and close out" → NO. Update tracking files ONLY when context pressure forces shutdown.

**What to do instead after completing a task:**
1. Check SESSION_STATE.md's work queue for the next task
2. If the queue is empty, look at the priority list below
3. If priorities are done, improve existing tools, expand datasets, research candidates
4. If candidates exist, check their kill conditions, update their analysis
5. If everything is truly current, run a full pipeline scan to look for new signals
6. Generate or update the Report Summary documents

### Signal Pipeline Rules
15. **When a signal scores 28+**: create `candidates/TICKER_short_description.md` using `framework/candidate_template.md`. Include full deep dive per the strategy-specific checklist.
16. **Cross-strategy convergence** (same entity from 2+ strategies within 14 days) = highest priority — deep dive immediately, regardless of individual signal scores.
17. **Daily report**: save to `reports/YYYY-MM-DD_daily_report.md`. Keep last 30 days; archive older ones.
18. **Kill condition monitoring**: every session checks existing candidates against their kill conditions. If triggered, mark candidate as killed with explanation.
19. **Report Summary maintenance**: Update `Report Summary/` folder each session with current tool status, candidate pipeline, and performance metrics.

### End (CRITICAL — do this BEFORE running out of context)
20. **Detect context pressure early.** When you sense you are approaching the usage limit — or after completing a major work block and before starting another large one — execute the shutdown protocol below. Better to shut down cleanly one step early than to lose work by running into the wall.
21. **Shutdown protocol** (execute all 5 steps in order):
    1. **Flush all working state to files.** Any analysis, partial tool, or findings that exist only in context must be written to files in `working/` (if incomplete) or their final location (if complete).
    2. **Overwrite `SESSION_STATE.md`** with current state: what was completed this session, what is in progress, what comes next, active warnings, active blockers. This is the relay baton — the next session reads this first.
    3. **Append to `PROGRESS_LOG.md`** with: done, in progress, next, blockers.
    4. **Update `INDEX.md`** if any files were created or changed.
    5. **Overwrite `SESSION_LOCK.md`** with `UNLOCKED` + current UTC timestamp to release the lock for the next scheduled session. (Never delete — use overwrite-only.)
22. **The next session must be able to continue seamlessly from SESSION_STATE.md alone.** If a future session reads SESSION_STATE and cannot determine exactly what to do next, the handoff has failed.

---

## Scheduled Session Behavior

This project runs on scheduled Cowork sessions. Each session starts cold with zero memory. The entire continuity mechanism is the files in this folder.

**Operating principles for scheduled sessions:**
- **Concurrency lock.** Two scheduled tasks share this project: an operational scanner (`investment-tool-project`, cron `0 */3 * * *`) and a maintenance task (`investment-tool-maintenance`, cron `50 */3 * * *`). Before doing any work, check `SESSION_LOCK.md`. If another session is active (first line says "LOCKED", timestamp <4 hours old), stop immediately — do nothing. If the lock is stale (>4 hours), override it (the previous session likely crashed). On shutdown, overwrite `SESSION_LOCK.md` with "UNLOCKED" (never delete the file). This prevents two sessions from writing to the same files simultaneously.
- **No human present.** Scheduled sessions run autonomously. Do not ask questions in the chat — write them to `OPEN_QUESTIONS.md` and continue with whatever work is not blocked.
- **Work until the limit — NO EXCEPTIONS.** Use every available token productively. Follow the priority queue. When one task completes, immediately start the next. Never stop because "the main task is done" — there is always a next task. Stopping early is a failure mode. The only valid stop is when ALL work is genuinely blocked.
- **Flawless continuity.** The handoff between sessions must be seamless. A new session should be indistinguishable from a continuation of the previous one. No repeated work, no forgotten context, no re-litigating decisions already made.
- **SESSION_STATE.md is the contract.** If it says something is done, it's done — don't re-verify unless there's a specific reason. If it says something is in progress, pick it up. If it says something is next, start it.
- **Dependencies reset every session.** The sandbox resets between sessions. Always reinstall Python packages at the start (`pip install ...`).
- **Fail forward.** If an API is down, a tool is broken, or something unexpected happens — log the issue in `OPEN_QUESTIONS.md`, note it in `SESSION_STATE.md` warnings, and move to the next productive task. Don't spend the session debugging a transient failure.

---

## Scheduled Tasks

Two Cowork scheduled tasks share this project folder:

1. **Operational Scanner** (`investment-tool-project`): Runs the 5-scanner pipeline, scores signals, monitors candidates, regenerates reports. Cron: `0 */3 * * *` (every 3 hours, every day including weekends).

2. **Maintenance Task** (`investment-tool-maintenance`): Checks structural health (missing/corrupt files, broken JSON), audits signal quality (stale data, orphan files), detects and fixes bugs in Python tools (syntax, import, logic). Cron: `50 */3 * * *` (10 minutes before each operational cycle). Never runs scanners, never modifies candidates/scoring.

Both tasks use the same `SESSION_LOCK.md` concurrency protocol. The maintenance task identifies itself as `maintenance-[YYYY-MM-DD]` in the lock; the operational task uses `scheduled-[date]`. If one task finds the lock held by the other, it aborts gracefully. See `DECISIONS.md` D-047 for full rationale.

---

## Implementation Priority Queue

1. ~~Design common signal JSON schema~~ → defined above
2. ~~Build OpenFIGI entity resolution module~~ → `tools/openfigi_resolver.py` v1.1 ✅ (23/23 tests)
3. ~~Refine EDGAR tool~~ → `tools/edgar_filing_monitor.py` v2.4 ✅ (18/18 tests + wall-clock budget)
4. ~~Build Congressional trading client~~ → `tools/congressional_trading.py` v2.0 ✅ (Capitol Trades HTML scraping — Quiver now requires auth)
5. ~~Build ESMA short position tool~~ → `tools/esma_short_scanner.py` v2.0 ✅ (multi-regulator: FCA UK, AMF France, AFM Netherlands, BaFin Germany)
6. ~~Build FDA PDUFA calendar pipeline~~ → `tools/fda_pdufa_pipeline.py` v2.0 ✅ (39/40 tests — needs watchlist population)
7. ~~Build contract award monitor~~ → `tools/contract_monitor.py` v1.1 ✅ (31/31 tests)
8. ~~Build convergence engine~~ → `tools/convergence_engine.py` v1.0 ✅ (31/31 tests)
9. ~~Build `tools/pipeline_runner.py`~~ → `tools/pipeline_runner.py` v1.1 ✅ (subprocess-isolated scanners + OpenFIGI + convergence + daily report) + `run_scanner.py` + `run_post_scan.py`
10. ~~Create initial PDUFA watchlist~~ → `signals/pdufa_watchlist.json` ✅ (9 entries: TVTX Apr 13, MNKD May 29, ACHV Jun 20, ARQT Jun 29, LNTH Jun 29, IONS Jun 30, AZN Jun 30, PFE Jun 15, VERA Jul 7)
11. Run first full integrated daily scan → generate first candidates
12. Set up scheduled Cowork session for autonomous daily pipeline
