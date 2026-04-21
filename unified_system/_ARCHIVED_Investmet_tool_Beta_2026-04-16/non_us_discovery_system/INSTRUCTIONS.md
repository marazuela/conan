# INSTRUCTIONS — Non-US Primary-Source Discovery System (Tool 2)

---

## 1. Cold Start Protocol

Every session starts with zero memory. The relay files are the bridge.

**Step 1** — Concurrency check. Read `SESSION_LOCK.md`. If first line is `LOCKED` and timestamp is < 4 hours old, stop immediately. Otherwise, overwrite with `LOCKED / Timestamp: <UTC ISO> / Session: <session-id>` and proceed.

**Step 2** — Read `SESSION_STATE.md`. This is the relay baton.

**Step 3** — Read this file (`INSTRUCTIONS.md`) top to bottom.

**Step 4** — If `SESSION_STATE.md` flags blockers, read `OPEN_QUESTIONS.md`.

**Step 5** — Install dependencies:

```bash
pip install requests beautifulsoup4 lxml yfinance openpyxl pandas python-dateutil feedparser --break-system-packages
```

**Step 6** — Tool Validation Protocol. `py_compile` every file in `tools/`. Probe every active scanner's primary endpoint. Log results into `SESSION_STATE.md` Tool Health table.

**Step 7** — Read only the specific file(s) needed for the current task (strategy spec, scoring rubric, candidate template).

Do not read all files. `PROGRESS_LOG.md` is history — read only when tracing past decisions.

---

## 2. System Architecture (3 Layers)

### Layer 1 — Individual Scanners (9 tools)

Each strategy has a Python tool that queries its exchange's disclosure source and emits signals in the common JSON schema below. Tools are independent and parallelizable.

**Common signal schema (ALL scanners emit this):**

```json
{
  "upstream_system_id": "tool-2-non-us-primary",
  "signal_id": "<hash of exchange + local_id + filing_date>",
  "ticker_local": "7203",
  "mic": "XTKS",
  "ticker_plus_mic": "7203.XTKS",
  "isin": "JP3633400001",
  "figi": "BBG000BNWHK5",
  "issuer_figi": "BBG000BCZ5N6",
  "company_name_local": "トヨタ自動車株式会社",
  "company_name_en": "Toyota Motor Corporation",
  "market_cap_usd_mm": 245000,
  "exchange": "TDnet",
  "country": "JP",
  "signal_type": "guidance_revision",
  "signal_category": "earnings",
  "thesis_direction": "long|short|neutral|unknown",
  "strength_estimate": 3,
  "source_url": "https://...",
  "source_content_hash": "<sha256 of filing body>",
  "source_date": "2026-04-14",
  "scan_date": "2026-04-14T12:30:00Z",
  "translation_confidence": 0.92,
  "raw_data": { "filing_type": "Tanshin", "snippet": "..." }
}
```

### Layer 2 — Entity Resolution (OpenFIGI)

All signals pass through `tools/openfigi_resolver.py` which maps (ticker + MIC) → FIGI, and FIGI → issuer_figi (composite). Cross-listings (HSBC in LSE + HKEx; Rio Tinto in LSE + ASX + SEDAR) resolve to the same `issuer_figi`, which is the key used for convergence.

### Layer 3 — Convergence Engine + Cross-Listing Dedup

After entity resolution, `tools/convergence_engine.py` checks whether any `issuer_figi` has signals from 2+ strategies within a 14-day rolling window. Before flagging convergence, it checks `source_content_hash` pairwise — if two signals have highly similar content hashes (same underlying event echoed across listings), they are deduplicated to one signal and convergence is not claimed. This is the D-004 mitigation.

Convergent signals (after dedup) receive +4 bonus (2 strategies), +8 (3+). Auto-promotes to deep dive.

---

## 3. The 9 Strategies

| # | Strategy | Source | Tool | Frequency | Status |
|---|----------|--------|------|-----------|--------|
| 1 | LSE RNS | London Stock Exchange Regulatory News Service | `tools/lse_rns_scanner.py` | Daily | BUILD |
| 2 | TDnet | Japan Timely Disclosure Network | `tools/tdnet_scanner.py` | Daily | PENDING |
| 3 | ASX | Australian Securities Exchange Announcements | `tools/asx_scanner.py` | Daily | PENDING |
| 4 | SEDAR+ | Canadian consolidated filing platform | `tools/sedar_plus_scanner.py` | Daily | PENDING |
| 5 | HKEx | Hong Kong Exchange News | `tools/hkex_scanner.py` | Daily | PENDING |
| 6 | KIND | Korea Investor's Network for Disclosure | `tools/kind_scanner.py` | Daily | PENDING |
| 7 | BSE/NSE | Bombay + National Stock Exchange India | `tools/india_bse_nse_scanner.py` | Daily | PENDING |
| 8 | CVM | Comissão de Valores Mobiliários (Brazil) | `tools/cvm_scanner.py` | Daily | PENDING |
| 9 | BMV | Bolsa Mexicana de Valores | `tools/bmv_scanner.py` | Daily | PENDING |

Each strategy has a spec in `strategies/` detailing: data source URL, filing types of interest, signal filters, entity resolution pathway, deep dive checklist, output examples.

---

## 4. Three-Stage Signal Pipeline

### Stage 1 — Triage (inside each scanner)

Hard filters; a signal must pass all:

- Issuer is publicly traded on a major exchange (the target exchange or a cross-listing).
- Market cap ≥ USD $300M (via yfinance library with appropriate exchange suffix).
- Signal is novel — not a duplicate of one already in `signals/signal_log.json` within 30 days.
- Data is fresh — filing date within last 7 days.
- For non-English sources: translation confidence above the per-strategy threshold (default 0.70).

### Stage 2 — Scoring (7 dimensions)

Signals that pass triage are scored on:

- Signal Strength (×2)
- Catalyst Clarity (×1)
- Info Asymmetry (×1.5)
- Risk/Reward (×1)
- Edge Decay (×1)
- Liquidity (×1)
- Catalyst Timeline (×1)

Max score: 42.5. Convergence bonus: +4 (2 strategies), +8 (3+).

Thresholds: **28+ Immediate**, **22–27 Watch**, **14–21 Archive**, **<14 Discard**.

Full rubric: `framework/scoring_system.md`.

### Stage 3 — Deep Dive

Each strategy has its own deep dive checklist. Common elements:

- Read the actual source document (not just headline).
- For non-English: emit translation with confidence score. If confidence < 0.85 on direction-relevant passages, direction = `unknown`.
- Company context (market cap, sector, recent price action, cross-listings).
- Thesis statement with explicit verified/inferred/speculated tags.
- Web research layer — search for recent news, analyst activity, litigation.
- Kill conditions — explicit, measurable invalidation criteria.
- Catalyst map — event, date/window, entry/exit triggers.
- Cross-listing check — is this signal echoing the same event across multiple exchange listings?

---

## 5. Execution Model

**Python tools** collect data, resolve entities, triage.
**Cowork sessions** do scoring, cross-referencing, deep dives, candidate writeups, daily reports.

### Daily Session Flow

1. Acquire lock.
2. Install deps.
3. Tool Validation Protocol.
4. Run all healthy scanner tools → raw signals.
5. Apply triage filters (market cap, novelty, freshness, translation confidence).
6. OpenFIGI entity resolution → normalize.
7. Cross-listing dedup — collapse echoes of same event across listings.
8. Convergence check on `issuer_figi` within 14-day window.
9. Score surviving signals.
10. Deep dive on new 28+ scores and convergences.
11. Update watchlist candidates (22–27).
12. Daily report to `reports/YYYY-MM-DD_daily_report.md`.
13. Monitor existing candidates against kill conditions.
14. Update `INDEX.md`, `PROGRESS_LOG.md`, `SESSION_STATE.md`.
15. Release lock.

### Daily Report Contents

- New signals (count by strategy + brief summary).
- New candidates (28+ with one-paragraph thesis).
- Updated candidates (material developments).
- Watchlist movements.
- Convergence alerts (highest priority).
- Cross-listing echoes detected and deduplicated.
- Expired/killed candidates.
- Tool health (broken scanners, API errors).

---

## 6. Execution Environment

**Path mappings (Cowork sandbox):**

- Windows: `C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\Investmet tool Beta\non_us_discovery_system`
- Sandbox: `/sessions/intelligent-youthful-tesla/mnt/Investmet tool Beta/non_us_discovery_system`

Always use absolute paths.

**Pip install:**

```bash
pip install requests beautifulsoup4 lxml yfinance openpyxl pandas python-dateutil feedparser --break-system-packages
```

Dependencies reset between sessions. Never skip reinstall.

**Rate limits & politeness:**

- User-Agent header with email on every request.
- Minimum 1-second delay between requests to the same host.
- Batch OpenFIGI requests at 10 per call (unauthenticated tier).
- Per-scanner wall-clock budget: 60 seconds. Subprocess hard-kill at 150 seconds.

---

## 7. Folder Structure

```
non_us_discovery_system/
├── PROJECT_INSTRUCTIONS.md   ← charter
├── README.md                 ← entry point
├── OBJECTIVES.md             ← primary goal, mandate, success criteria
├── INSTRUCTIONS.md           ← this file
├── CONTEXT.md                ← API reference, validated endpoints
├── DECISIONS.md              ← D-000, D-001, …
├── OPEN_QUESTIONS.md         ← Q-001, Q-002, …
├── SESSION_STATE.md          ← relay baton — overwritten each session
├── SESSION_LOCK.md           ← concurrency gate
├── INDEX.md                  ← file inventory
├── PROGRESS_LOG.md           ← append-only session log
├── framework/
│   ├── scoring_system.md     ← 7-dimension rubric
│   └── candidate_template.md ← per-candidate writeup template
├── strategies/               ← 9 strategy specs
├── tools/                    ← Python scanners + shared resolvers/engines
├── signals/                  ← JSON signal logs
├── candidates/               ← TICKER_description.md files
│   ├── delivered/            ← resolved outcomes
│   └── archive/              ← superseded
├── reports/                  ← daily reports
├── working/                  ← in-progress scratch
└── archive/                  ← superseded work, never deleted
```

---

## 8. Session Rules

### Start

1. Concurrency check.
2. Read SESSION_STATE, INSTRUCTIONS, OPEN_QUESTIONS (if flagged), task-specific file.
3. Install dependencies.
4. Tool Validation Protocol.
5. State understanding of current status and proposed next action before executing.

### During

6. Work until usage limit — no exceptions. Do not stop early.
7. Save after every discrete unit of work.
8. Update INDEX.md when files change.
9. Update DECISIONS.md when decisions are made.
10. Update OPEN_QUESTIONS.md for new blockers.
11. Never delete — move to archive/.
12. One concept per file.

### Signal Pipeline Rules

13. Signal scores 28+ → create `candidates/TICKER_description.md` using template.
14. Cross-strategy convergence (after dedup) = highest priority.
15. Daily report → `reports/YYYY-MM-DD_daily_report.md`.
16. Kill condition monitoring every session.

### End

17. Detect context pressure early. Shut down cleanly one step early if uncertain.
18. Shutdown: flush state → overwrite SESSION_STATE → append PROGRESS_LOG → update INDEX → overwrite SESSION_LOCK with UNLOCKED (last step).

---

## 9. Scheduled Tasks

This system has two producer tasks (coordinated via SESSION_LOCK). Reader/reporting tasks were retired on 2026-04-15 and consolidated into the project-root `Reporting Hub/`:

| # | Task | Cron (local) | Write scope | Concurrency |
|---|------|-------------|-------------|-------------|
| 1 | `non-us-operational` | `20 */3 * * *` | `non_us_discovery_system/` | SESSION_LOCK |
| 2 | `non-us-maintenance` | `40 */3 * * *` | `non_us_discovery_system/` | SESSION_LOCK |

Offsets are 20/40 to give 20-minute separation from both sides of Tool 1's `0 */3` and `50 */3` schedule. Independent systems — separate locks — but operator-side UI is calmer if they don't fire simultaneously.

**Reporting tasks (moved to Reporting Hub):** Performance reports and deep-dive theses for this system are produced by the project-root `Reporting Hub/` tasks `reporting-hub-performance` (daily 02:30 UTC) and `reporting-hub-deep-dives` (every 4h at :30 UTC). See `Reporting Hub/REPORTING_INSTRUCTIONS.md` and `Reporting Hub/SOURCES.md` for the read contract. This system is producer-only: it does not write outside `non_us_discovery_system/`.

Register tasks only after Phase 1 proves one scanner end-to-end.

---

## 10. Implementation Priority Queue

1. Phase 0: Scaffold (folders, relay files, framework, strategy stubs, SKILLs, shared tools) — IN PROGRESS.
2. Phase 1: UK LSE RNS scanner — first end-to-end validation. Canary for the whole architecture.
3. Phase 2: Japan TDnet scanner.
4. Phase 3: Australia ASX scanner.
5. Phase 4: Canada SEDAR+ scanner.
6. Phase 5: Hong Kong HKEx scanner.
7. Phase 6: Korea KIND scanner.
8. Phase 7: India BSE/NSE scanner.
9. Phase 8: Brazil CVM scanner.
10. Phase 9: Mexico BMV scanner.
11. Phase 10: Register scheduled Cowork tasks. Run 7 days autonomous.
