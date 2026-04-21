# PROGRESS_LOG — Non-US Primary-Source Discovery System (Tool 2)

Append-only session log. Never edit past sessions. Growing is fine; read only when tracing specific past decisions.

Format per session:

```
## Session N — YYYY-MM-DD
✅ Completed:
🔄 In progress:
⏭️ Next:
⚠️ Blockers:
```

---

## Session 0 — 2026-04-14 — Phase 0 Scaffold

✅ Completed:
- Adaptation conversation with operator — confirmed 9-scanner non-US scope, UK-first build order, deliverable shape identical to Tool 1, full structural independence from Tool 1.
- Folder structure created: `non_us_discovery_system/` + `reporting_layer/` + all subfolders per template Part 2.
- `PROJECT_INSTRUCTIONS.md` — full 11-section charter.
- `OBJECTIVES.md` — primary goal, mandate, 9-strategy table, success criteria, definition of done.
- `INSTRUCTIONS.md` — 10-section architecture with common signal schema, pipeline, session rules, scheduled tasks.
- `CONTEXT.md` — strategy rationale + endpoint planning table (all 9 endpoints marked UNVERIFIED pending per-phase probe).
- `README.md` — entry point, cold-start read order, common commands.
- `DECISIONS.md` — D-000 (founding architecture) + D-001 through D-004 (cross-listing dedup, translation-direction honesty, ticker+MIC entity resolution, content-similarity threshold).
- `OPEN_QUESTIONS.md` — initialized empty.
- `SESSION_STATE.md` — Build mode, Phase 1 priority queue populated.
- `SESSION_LOCK.md` — UNLOCKED.

🔄 In progress:
- Remaining Phase 0: framework/ files, 9 strategy spec stubs, 4 SKILL.md files, port of OpenFIGI resolver + convergence engine + pipeline runner stubs.

⏭️ Next:
- Complete remaining Phase 0 scaffolding.
- Begin Phase 1: validate UK LSE RNS endpoints live, flesh out UK strategy spec, build LSE scanner.

⚠️ Blockers:
- None.

---

## 2026-04-14 — non-us-maintenance cycle

✅ Completed:
- Acquired SESSION_LOCK at 14:40Z.
- Scanner endpoint probes: lse_rns 200/ok, tdnet 200/ok, asx 200/ok with expected `data` schema. 6 pending scanners (sedar, hkex, kind, bse_nse, cvm, bmv) have no module file yet — expected given Phase 4+ pending.
- Universe freshness: ASX universe 0.03 days old (TTL 7d) — no rebuild needed.
- Signal log audit: 71 entries, 0 duplicate `source_content_hash`, 0 orphan hashes. Noted: `exchange` field null on all entries (low-priority metadata gap; `mic` is populated).
- OpenFIGI cache: 76 cache files on disk, last ASX scan 59/59 (100%) resolved — well above 80% threshold.
- Boilerplate audit: 90/149 ASX headlines dropped in last scan; `substantial_holder_change` dominates (≈88 of 90). Recommend adding holder-size threshold filter in `tools/asx_scanner.py`.
- Candidate drift: 5 active candidates scanned; 0 new strength≥3 signals on any tracked issuer_figi — no candidate markdowns updated.
- Dependencies: `requests`, `pandas`, `yfinance` all importable after install.
- Wrote `HEALTH_LOG.md` with full probe results.

⏭️ Next:
- Phase 4: build `tools/sedar_scanner.py` (Canada SEDAR+) covering XTSE + XTSX.
- Consider holder-size threshold filter refinement in `tools/asx_scanner.py`.

⚠️ Blockers:
- None.

---

## 2026-04-15 — non-us-maintenance cycle (skipped)

✅ Completed:
- Cold start: read CLAUDE.md, task SKILL.md spec, PROGRESS_LOG.md.
- Checked SESSION_LOCK: LOCKED by `non-us-operational-2026-04-15` at 2026-04-15T00:00:00Z, TTL 4h.
- Lock is fresh — operational cycle is holding it within TTL window.
- Per task spec: "if LOCKED and TTL < 4 h old, exit cleanly." Exiting without writes to `working/`, `signals/`, `candidates/`, or `tools/`.

⏭️ Next:
- Next scheduled maintenance run will re-check lock and proceed if released.

⚠️ Blockers:
- SESSION_LOCK held by operational cycle — normal coordination, not a failure.

---

## 2026-04-15 — non-us-maintenance cycle

✅ Completed:
- Cold start: read CLAUDE.md, task SKILL.md spec, SESSION_STATE, PROGRESS_LOG.
- SESSION_LOCK: previous lock (non-us-operational-2026-04-15, 00:00Z) was 4.77h old — past 4h TTL. Took lock at 04:46Z as `non-us-maintenance-2026-04-15`.
- Scanner health probes (HTTP 200 + expected schema):
  - lse_rns (Investegate): 200, 112KB payload, t=1.25s
  - tdnet: 200, 22KB payload, t=0.83s
  - asx (markitdigital): 200, `data.items` present (5 items on WBC probe), t=0.64s
  - sedar_plus: 200, t=0.61s (code complete per Phase 4 memory, not yet in production registry)
  - 5 pending scanners (hkex, kind, bse_nse, cvm, bmv) still have no module file — expected.
- Universe freshness: `working/asx_universe.json` as_of=2026-04-14T16:08:27Z, age 0.53 days, 426 tickers. Well under 6-day refresh trigger. No rebuild.
- Signal log audit: 90 entries (+19 since last cycle), 0 duplicate `source_content_hash`, 0 orphan FIGI, 0 missing hash. Distribution: tdnet=15, lse_rns=16, asx=59. Routing: immediate=11, watchlist=42, archive=37. Schema reminder: `signal_log.json` entries store venue in `ticker_plus_mic` (e.g. `CBA.XASX`) — no separate `mic`/`exchange` fields by design; prior "null" warning from 2026-04-14 was misread.
- OpenFIGI cache: 79 files. Latest scan FIGI resolve rate: asx 59/59, lse_rns 2/2, tdnet 3/3 → 100% across the board. Above 80% target.
- Boilerplate audit: ASX raw 149 → processed 59 (90 dropped). Breakdown by signal_type — dropped entirely: `substantial_holder_initial` (45), `substantial_holder_ceasing` (44). Survives triage: `substantial_holder_change` (48/49). The surviving category still dominates processed volume; holder-size threshold filter in `tools/asx_scanner.py` remains the right refinement (carried forward from 2026-04-14 audit).
- Candidate drift: 5 active candidates scanned against last 7d of signal_log. 7 matching entries found; all reconcile to existing `related_signal_ids` or in-candidate `Source traceability` (Sankei 2 signals = tender + supporting; PTSB 2 signals = recommended cash offer + merged sibling 45min later). No new uncaptured high-strength signals → no candidate markdown appends required.
- Dependencies: `pandas`, `requests` already present. `yfinance` was missing — installed via `pip install --break-system-packages yfinance` (v1.2.2). All three now importable.

🔄 In progress:
- Phase 4 (SEDAR+) code complete per memory, end-to-end run still blocked per prior note.

⏭️ Next:
- Unblock and validate end-to-end SEDAR+ run (first XTSE/XTSX candidate).
- Implement holder-size threshold filter in `tools/asx_scanner.py` at emit time to reduce `substantial_holder_change` noise before triage.

⚠️ Blockers:
- None for maintenance. Carrying forward: Phase 4 end-to-end run sandbox blocker (see `PHASE4_PROGRESS.md`).

---

## 2026-04-15 — non-us-maintenance cycle (09:11Z)

✅ Completed:
- Cold start: read CLAUDE.md, task SKILL.md, CLAUDE project doc, SESSION_STATE, PROGRESS_LOG.
- SESSION_LOCK: prior operational lock (05:00Z) had TTL-expired at 09:00Z (11 min past); took lock as `non-us-maintenance-2026-04-15T0911`.
- Scanner endpoint probes (all HTTP 200 + expected schema): lse_rns 113KB/0.97s, tdnet 63KB/1.11s, asx 5 data items/0.65s, sedar 7KB/0.61s. Pending scanners (hkex/kind/bse_nse/cvm/bmv) still module_missing — expected.
- py_compile clean on all active scanner modules + shared utilities.
- Universe freshness: `working/asx_universe.json` age 0.71 days (426 tickers). Well under 6-day refresh trigger.
- Signal log audit: 101 entries (+11 since last cycle from new lse_rns 2026-04-15 scan). Zero duplicate content-hashes, zero orphan FIGIs, zero missing hashes. By scanner tdnet=15 / lse_rns=27 / asx=59. By routing immediate=11 / watchlist=53 / archive=37.
- Signal log schema confirmed slim-index (no `signal_type`/`strength_estimate`) — drift checks use `score_total>=28` as strong-signal proxy. Prior cycles' reliance on `strength_estimate` was unreachable from the log alone.
- OpenFIGI cache: 87 files on disk (+8). Sample 20/20 = 100% resolved. Last-scan FIGI resolve: asx 59/59, lse_rns 11/11, tdnet 3/3 → 100% across the board.
- Boilerplate audit (unchanged raw scan; no new ASX scan since 2026-04-14): raw 149 → processed 59. `substantial_holder_initial` and `substantial_holder_ceasing` dropped 100%; `substantial_holder_change` survives at 98% (48/49) and still dominates processed volume. Holder-size threshold filter recommendation carried forward (3rd cycle in a row).
- Candidate drift: 5 candidates reviewed against signal_log. All 7 score≥28 matches on tracked issuer_figis reconcile to existing `related_signal_ids` — no uncaptured high-strength signals. No markdown updates required.
- Dependencies: yfinance 1.2.2, pandas 2.3.3, requests 2.33.1 importable after `pip install --break-system-packages`.

🔄 In progress:
- Phase 4 (SEDAR+) end-to-end run still blocked per `PHASE4_PROGRESS.md`.

⏭️ Next:
- Unblock SEDAR+ end-to-end run; first XTSE/XTSX candidate markdown.
- Implement holder-size threshold filter in `tools/asx_scanner.py` at emit time.
- Phase 5 prep — HKEx scanner module.

⚠️ Blockers:
- None for maintenance. Carrying forward: Phase 4 end-to-end run blocker.

---

## 2026-04-15 — non-us-operational cycle (09:25Z)

✅ Completed:
- Cold start: read CLAUDE.md, INSTRUCTIONS.md, SESSION_STATE, PROGRESS_LOG, SESSION_LOCK.
- SESSION_LOCK: acquired at 09:25Z as `non-us-operational-2026-04-15T0925` (prior maintenance session released at 09:14Z).
- Dependencies re-installed via `pip install --break-system-packages`.
- Scanners run (window=7):
  - `lse_rns` → 11 raw / 11 triaged / 11 resolved / 11 watchlist (all score=23.0, no immediates).
  - `tdnet` → 27 raw / 27 triaged / 27 resolved / **17 immediate** + 10 watchlist. Major TOB / MBO cluster.
  - `asx` → attempted twice at throttle=0.05 and 0.1 with 35s and 40s wall-clock budgets; scanner did not finish within budget. No asx output this cycle. Carried forward.
  - `sedar` → no Canada universe file available (same blocker noted in PHASE4_PROGRESS); scan exited cleanly with 0 signals. Carried forward.
- Wrote 13 new candidate markdowns for TDnet immediates:
  - Hand-authored: `6197_XTKS_solasto-mbo-tender-offer.md`
  - Scripted stubs (12): 3391 Tsuruha (TOB), 6027 Bengo4 (impairment), 8267 Aeon (TOB), 1882 Toa Road (litigation), 4206 Aica Kogyo (TOB), 7085 Curves (profit upgrade), 6135 Makino Milling (TOB), 6367 Daikin (litigation), 2692 Itochu-Shokuhin (TOB), 8001 Itochu Corp (TOB), 8934 Sun Frontier (TOB), 2540 Yomeishu (TOB).
  - All include frontmatter, source signals, translation notes, thesis, steelman, kill conditions, catalyst map, sizing, traceability per template.
  - 2972 Sankei (existing candidate) — 1 additional signal landed; no re-write needed since existing markdown already covers the TOB arc.
- Wrote 21 watchlist JSON entries to `candidates/watchlist/` (11 lse_rns + 10 tdnet).
- `signals/signal_log.json`: 90 → 128 entries (+38). By routing: immediate 11→28 (+17), watchlist 42→63 (+21), archive 37→37.

🔄 In progress:
- ASX scan not completed this cycle — needs a wall-clock budget greater than what a single bash call allows, or chunked universe scan.
- SEDAR+ still blocked on missing Canada universe file.

⏭️ Next session priority (operational + maintenance):
1. **ASX scan wall-clock:** either split ASX universe into 3 chunks of ~140 tickers and run 3 separate pipeline invocations, or add a `--max-tickers N` flag to `tools/asx_scanner.py` so operational cycles can cover the universe across several runs. Per-bash-call budget in this sandbox is ≤45s effective.
2. **SEDAR+ universe:** `tools/ca_universe.py` needs a real run (same blocker as Phase 4). If universe builder is slow, chunked / batch approach per (1) applies here too.
3. **Deep-dive backlog:** 13 new TDnet candidates all stub-status. At current `non-us-deep-dives` cron (45 */4 * * *) this is ~3 days of deep-dive capacity. Prioritize the 8 tender_offer / MBO signals over the 3 impairment/litigation (where tc is 0.85–0.88, direction=short, and edge decay is faster post-filing).
4. **Direction verification on 8001 Itochu:** Japanese trading companies often file as the TOB *buyer* (acquirer), not the target. Scanner tagged direction=long on a $87B mcap ticker — deep-dive must confirm buyer vs. target role before any sizing.
5. Holder-size threshold filter in `tools/asx_scanner.py` — still carried forward from prior 3 cycles.

⚠️ Blockers:
- ASX scanner budget: single-shot pipeline run exceeds the 45s bash tool ceiling. Needs chunking or background-with-polling. Noted above.
- SEDAR+: missing `working/ca_universe.json`. Blocks all sedar runs.
- `TodoWrite` tool schema unavailable this session (ToolSearch lookup failed); operated without todo-list.

Shutdown adjustments for future scheduled operational sessions:
- **Avoid running `lse_rns` then `tdnet` then `asx` back-to-back inside one bash call.** Each scanner should be its own bash call; TDnet and ASX both take >30s and will hang the sandbox if chained.
- **Order by expected wall-clock ascending:** sedar (fast, often 0-sig) → lse_rns (~20s) → tdnet (~60s) → asx (>90s). This session hit the TDnet wall because it started in a hung bash state.
- **ASX specifically:** this session could not complete ASX. Next operational should either (a) set `--max-tickers 150 --throttle 0.05` and run twice, or (b) run asx with a 60s `timeout` in its own bash call.

## 2026-04-15T10:00Z — non-us-operational run
Scanners: lse_rns (62→0 imm/0 watch), tdnet (197→4 imm), sedar (blocked on universe), asx chunked-4×100+30 (426 tickers → 153 raw → 11 routed → 1 imm/2 watch).
New candidates: 4343 AEON Fantasy, 7512 AEON Hokkaido, CCL Cuscal.
Watchlist (ASX): TWR, A4N. TDnet: 4 immediates — 8001 Itochu (flagged as scanner direction defect — buyer-side TOB; existing stub), 6197 Solasto (existing stub), 4343 and 7512 (new stubs).
No SEDAR+ universe still; same blocker as last cycle.
ASX chunking worked end-to-end this cycle (4 chunks × ~45s each → finalize stage).

---

## 2026-04-15T11:15Z — non-us-maintenance cycle

**Scanner health (HTTP + module smoke):** lse_rns OK (24 raw/1d), tdnet OK (20 raw/1d), asx OK (WBC 5 items), sedar OK (yfinance path 10 news items on SHOP.TO). All 9 registered endpoints return HTTP 200. Phase 5-9 scanners not yet built (expected).

**Universe freshness:** ASX universe fresh (age 0d, 426 tickers). CA universe still missing — Phase 4 blocker unchanged.

**Signal log:** 143 entries, 0 duplicate content-hashes, 0 orphans.

**OpenFIGI cache:** 117 cached entries, 100% hit rate on latest ASX processed scan.

**Boilerplate filter:** top raw discard prefixes (ASX substantial-holder family @ 138, LSE TR1 & trading updates @ single digits) are already handled or correctly retained. No new rule needed.

**Candidate drift:** 21 candidates audited vs. 7-day signal log window. Zero drift — no new high-strength signals missing from candidate markdowns.

**Dependencies:** yfinance 1.2.2 / pandas 2.3.3 / requests 2.33.1 installed cleanly. tools/*.py py_compile OK.

**Open issues carried forward:**
1. CA universe still missing → Phase 4 SEDAR+ still blocked on end-to-end validation.
2. 8001 Itochu buyer-side TOB direction classifier defect (per prior operational run) — not addressed this cycle (out of maintenance scope).

Next maintenance cycle: 2026-04-15T14:40Z (cron `40 */3 * * *`).

---

## Migration — 2026-04-15 — Reporting Hub consolidation

✅ Completed:
- Per-tool `reporting_layer/` sibling folder retired. Contents migrated to project-root `Reporting Hub/` (see `Reporting Hub/archive/2026-04-15_pre_hub_migration/` for preserved originals).
- Scheduled tasks `non-us-performance-report` and `non-us-deep-dives` retired; replaced by consolidated `reporting-hub-performance` (daily 02:30 UTC) and `reporting-hub-deep-dives` (every 4h at :30 UTC) that operate on the project-root hub.
- `non_us_discovery_system/` is now producer-only; this system does not write outside its own folder.
- INSTRUCTIONS.md §9 updated; DECISIONS.md D-000 implications updated; performance-report SKILL.md updated to remove cross-folder writes.

⚠️ Note for future sessions:
- Do NOT recreate `reporting_layer/` next to this folder. Reporting is the hub's responsibility.
- The Phase 4 freeze on Japan TDnet exchange scanners is independent of this migration and remains open.

## Session — 2026-04-15 — Maintenance cycle (non-us-maintenance)

✅ Completed:
- Took SESSION_LOCK (none in place at start; no stale lock to break).
- Scanner endpoint smoke tests: lse_rns, tdnet, asx, kind, nse, cvm, bmv-root all 200/healthy. SEDAR/HKEx/BMV-detail pages 404 or timed out (probe URLs, not canonical scanner endpoints — see HEALTH_LOG).
- BSE endpoint returns 200 but non-JSON via plain GET (will need Referer/cookie when Phase 7 scanner is built).
- Universe freshness: ASX universe `as_of=2026-04-14`, 426 tickers — no refresh needed (TTL 6d).
- Signal log audit: 145 entries, 0 duplicate content_hashes, 0 orphans.
- OpenFIGI cache audit: 118 entries, 100% hit rate on sample.
- Boilerplate audit: no triage-dropped persistence on disk → 0 patterns surfaced; flagged as gap.
- Candidate drift check: 21 candidates, 0 with new high-strength signals on the same FIGI in the last 7 days. Drift report `working/candidate_drift_2026-04-15.json`.
- Dependencies: yfinance/pandas/requests reinstalled into maintenance sandbox; all importable.

🔄 In progress:
- (none — read-only maintenance cycle)

⏭️ Next:
- Operational cycle continues per `SESSION_STATE.md`: run `tools/ca_universe.py` to unblock SEDAR+, then patch 8001 Itochu buyer-side TOB classifier in `tools/tdnet_scanner.py`, then continue deep-dive backlog (21 stubs).
- Maintenance gap to address opportunistically: persist triage-dropped headlines so future maintenance can mine boilerplate patterns.

⚠️ Blockers:
- SEDAR+ end-to-end still blocked on `working/ca_universe.json` (re-flagged).
- Phases 5–9 scanners not yet built (hkex/kind/bse_nse/cvm/bmv).

## Operational 2026-04-15T14:30Z (non-us-operational)
- sedar: 0 raw (ca_universe.json still missing)
- lse_rns: 62 raw → 2 watchlist (SEIT 23.0 major_shareholder_change, ROR 25.0 buyback_initiation), 0 immediate
- tdnet: 197 raw → 4 immediate, all already-stubbed (8001 Itochu*buyer-side defect still open*, 6197 Solasto, 4343 AEON Fantasy, 7512 AEON Hokkaido)
- asx: 153 raw → 2 archive (GYG, PPT both substantial_holder_change), 0 immediate, 0 watchlist
- Wrote 2 LSE watchlist JSONs: SEIT_XLON, ROR_XLON
- No new candidate markdowns this cycle (all immediates had existing stubs)

## Operational cycle 2026-04-15T22:30Z (non-us-operational)
✅ Completed: lse_rns (1 watchlist SEIT pre-existing), tdnet (197 raw → 2 triaged → 0 routed; all dedup), asx chunked (153 raw → 0 routed: 68 dedup + 85 boilerplate), sedar (still blocked: ca_universe.json missing).
🔄 In progress: none — quiet cycle, 0 new candidate markdowns this cycle.
⏭️ Next: build/run tools/ca_universe.py to unblock SEDAR; patch tdnet 8001 buyer-side TOB direction classifier; continue stub deep-dive backlog.
⚠️ Blockers: SEDAR+ universe missing (Phase 4 e2e); TDnet 8001 direction defect persists (not surfaced this cycle since dedup'd, but still in scanner code).

---
## 2026-04-16T22:46Z — non-us-maintenance

**Type:** maintenance cycle  
**Lock:** acquired UNLOCKED → LOCKED → UNLOCKED  
**Outcome:** clean cycle, no actionable issues.

| Check | Result |
|---|---|
| Scanner smoke tests | 4/4 active scanners (lse_rns, tdnet, asx, sedar) return 200 + import OK |
| ASX universe freshness | 1 day old (426 tickers), within TTL |
| CA universe | still missing — Phase 4 SEDAR+ blocker unchanged |
| Signal log | 148 entries, 0 dup hashes, 0 orphans |
| OpenFIGI cache | 100% hit rate over last 7 days (117/117 tickers) |
| Boilerplate audit | no new patterns proposed |
| Candidate drift | 3 candidates have predecessor same-issuer signals — all originating cluster, no genuine new drift |
| Dependencies | yfinance 1.2.2 / pandas 2.3.3 / requests 2.33.1 reinstalled (sandbox cold) |

**Open items unchanged:** (1) build `working/ca_universe.json`; (2) patch `tools/tdnet_scanner.py` thesis_direction for buyer-side TOB (8001 Itochu defect); (3) Phase 5–9 scanner modules unbuilt.


## 2026-04-16T23:00Z — non-us-operational run
Scanners: lse_rns (0 raw → 0 routed), tdnet 3-day (114 raw → 1 triaged → 0 resolved/routed), asx chunked 50+40×8+50 (426 tickers → 149 raw → 5 triaged → 1 imm / 3 watchlist / 1 archive), sedar (still blocked: ca_universe.json missing).
New candidate: PDI_XASX merger-agreement (Predictive Discovery & Robex) score 28.0.
New watchlist: VEA (trading_halt, 22), FLT (share_buyback, 25), DGT (substantial_holder_change, 22.5).
Archive: COL (substantial_holder_change, 21).
Blockers: ca_universe.json still missing; tdnet resolve dropped 1 triaged signal (FIGI lookup failure); yfinance absent in sandbox (jpx mcap enricher warned, fell back).
Next: unblock ca_universe, investigate tdnet triage→resolve gap (1 dropped at resolve).

---

## 2026-04-16T23:20Z — non-us-maintenance

**Lock:** acquired (prior session released cleanly at 23:00Z).

**Scanner health:** 8 of 10 endpoints respond 200. HKEx and BMV probe URLs returned 404 — those modules aren't yet built (Phases 5 and 9), probes were speculative and not operational failures. All active scanners (lse_rns, tdnet, asx) responded green. `tools/*.py` all compile clean.

**Dependencies:** cold sandbox missing `yfinance` / `pandas` / `requests`; installed via `pip --break-system-packages` (yfinance 1.2.2).

**Universe freshness:** `working/asx_universe.json` as_of 2026-04-14, 1 day old, 426 tickers — under the 6-day refresh threshold. No refresh triggered.

**Signal log audit:** 153 entries, 0 duplicate content hashes, 0 orphans missing issuer_figi. By exchange: asx 77, tdnet 46, lse_rns 30.

**OpenFIGI cache:** 122 entries, 100% hit rate on last 40 resolved signals (target >80%).

**Boilerplate audit:** top discarded prefixes (last 10 raw scans) are all already handled by existing rules — `Becoming/Ceasing to be a substantial holder` (ASX), 通期業績予想の修正 / 決算短信 (Japan). No new rule needed.

**Candidate drift:** 22 candidates reviewed; none show a newer >=22-score signal post-dating the candidate file. No candidate notes appended.

**Open issues (unchanged):** SEDAR+ still blocked on missing `working/ca_universe.json`; awaiting universe build.

**Shutdown:** SESSION_STATE refreshed with next concrete maintenance action. Lock released.

---

## 2026-04-16T04:47Z — non-us-maintenance cycle

**Lock:** prior operational lock (00:05Z) was 4h42m old — expired. Took lock as `non-us-maintenance-2026-04-16T04:47`.

**Scanner health:** 4 active scanners all HTTP 200 (lse_rns, tdnet, asx, sedar) — latencies 0.5–0.9s. Module import + `fetch_raw_signals` presence OK on lse_rns, tdnet, asx, sedar, sedar_chrome_supplement. `py_compile tools/*.py` clean. Phases 5–9 (hkex, kind, bse_nse, cvm, bmv) remain unbuilt.

**Universe freshness:** `working/asx_universe.json` age 1.53 days / 426 tickers — fresh (no refresh). `working/ca_universe.json` still missing — Phase 4 blocker unchanged.

**Signal log:** 153 entries, 0 duplicate `source_content_hash`, 0 orphans, 0 missing hashes. Scanner split asx=77, tdnet=46, lse_rns=30. Routing split immediate=34, watchlist=71, archive=48.

**OpenFIGI cache:** 122 files; 20/20 sampled = 100% resolved; 34/34 last-40 unique tickers = 100% hit rate.

**Boilerplate audit:** 567 signals surveyed across last 8 raw files. Top headlines (Japanese earnings/disclosure boilerplate + LSE TR-1 major-holdings notifications) all correctly handled by existing filters. No new rule proposed.

**Candidate drift:** 22 candidates scanned vs. 7-day signal_log window at score≥22 threshold. Zero drift. Snapshot: `working/candidate_drift_2026-04-16_maint_0447Z.json`.

**Dependencies:** yfinance 1.2.2 / pandas 2.3.3 / requests 2.33.1 installed on cold sandbox via `pip install --break-system-packages`. All importable.

**Open issues carried forward (unchanged):**
1. `working/ca_universe.json` absent — Phase 4 SEDAR+ end-to-end blocked.
2. Phases 5–9 scanner modules not built.
3. Itochu (8001) tdnet buyer-side TOB thesis_direction classifier — did not resurface.

Next maintenance cycle per cron `40 */3 * * *`.

## Session 2026-04-16T06:50Z — non-us-operational (scheduled)

One-line summary: **lse_rns 0 immediate / 9 watchlist (score 23 across prelim/trading-updates); tdnet 1 immediate (1878 Daito Trust tender-offer correction for 3271, score 35); asx already completed this cycle at 01:38 (PDI immediate retained).** New candidate markdown written: `candidates/1878_XTKS_daito-trust-global-tender-offer-correction.md`. 9 XLON watchlist JSONs written to `candidates/watchlist/`. SEDAR skipped (still blocked on `working/ca_universe.json`). HKEx/KIND/BSE_NSE/CVM/BMV skipped (STUB, no scanner modules yet).

---

## 2026-04-16T23:50Z — non-us-maintenance

**Lock:** acquired UNLOCKED (prior released at 23:30Z) → LOCKED → UNLOCKED at end.

**Scanner health:** 4 active scanners all healthy. lse_rns 200 (454 ms HEAD). tdnet 200 (62K bytes, 4.4 s). asx 200 (WBC announcements, 958 ms). sedar yfinance primary source (SHOP.TO .news) returned 10 items in 1.4 s. HEAD on Yahoo Finance landing returned 500 as expected (Yahoo rejects HEAD; `Ticker.news` works). `py_compile tools/*.py` clean.

**Dependencies:** cold sandbox — installed `yfinance 1.2.2` / `pandas 2.3.3` / `requests 2.33.1` via `pip --break-system-packages`.

**Universe freshness:** `working/asx_universe.json` age 1.78 days (as_of 2026-04-14T16:08:27Z), 426 tickers — fresh, no refresh. `working/ca_universe.json` still missing (standing Phase 4 blocker).

**Signal log audit:** 163 entries (+10 since 04:47Z), 0 duplicate content hashes, 0 orphans. `exchange` field still null on all entries (low-priority metadata gap, `mic` is sufficient).

**OpenFIGI cache:** 132 files (+10). Per-scanner last-scan hit rates: asx 52%, lse_rns 44%, tdnet 14%, sedar n/a (no raw signals — ca_universe missing). Rates look low vs prior "100% on last-40 unique" but this is the expected per-scan pattern: the resolver only persists cache when `resolved=True`, so one-off small-caps that fail resolve are re-probed every scan. Not a cache regression — see HEALTH_LOG.md for explanation.

**Boilerplate audit:** recurring discarded headlines are legitimate signal categories (guidance revision, decisional results, dividend revision, trading update). All already handled by existing rules; no new boilerplate rule recommended. If the intent is to raise throughput for these signal types, the lever is market-cap floor / novelty dedup, not new boilerplate filters.

**Candidate drift:** 23 candidates scanned vs 7-day signal_log at strength≥3 threshold. 0 drifts, 0 append notes.

**Open issues (unchanged):**
1. `working/ca_universe.json` missing — Phase 4 SEDAR+ end-to-end blocked. Operational task's responsibility.
2. Phases 5–9 scanner modules (hkex, kind, bse_nse, cvm, bmv) still unbuilt. Endpoint probes deferred until modules land.
3. Itochu (8001) tdnet buyer-side TOB thesis_direction defect — did not resurface this cycle (dedup'd).

**Shutdown:** SESSION_STATE refreshed with next concrete maintenance action. Lock released as final step.

---

## 2026-04-16T23:56Z — non-us-operational

One-line summary: **lse_rns window=1 → 1 NEW immediate (ITRK.XLON takeover_possible_offer by EQT, score 28.0) + 1 11:37Z sibling dedup-merged into the same candidate; tdnet window=3 → 135 raw / 1 triaged / 0 resolved (FIGI lookup dropped the single survivor again); asx already completed earlier this cycle at 01:38Z (chunked state cursor 426/426, finalize stage=done); sedar blocked (ca_universe still missing).** New candidate: `candidates/ITRK_XLON_eqt-possible-offer.md`. LSE window=3/7 timed out in-sandbox; window=1 succeeded at 44s — used that scope. No new XLON watchlist JSONs this cycle (all candidates novel-deduped from 06:50Z cycle). Phases 5–9 (hkex, kind, bse_nse, cvm, bmv) skipped — STUB, no scanner modules.

Blockers unchanged: `working/ca_universe.json` still missing; tdnet post_triage→resolve still drops 1 survivor (same symptom as 23:00Z cycle); LSE window=3 timeout in cold-sandbox wall-clock budget (window=1 works reliably).

Next: deep-dive ITRK — PUSU deadline ~2026-05-14 (28d from 2026-04-16); confirm EQT approach details, undisturbed price, and competing-bidder risk before promoting from stub.

---

## Session — 2026-04-16T13:47Z — non-us-maintenance run

✅ Completed:
- Scanner health: lse_rns / tdnet / asx / sedar all 200 OK; dependency versions unchanged (yfinance 1.2.2, pandas 2.3.3, requests 2.33.1). `py_compile tools/*.py` clean.
- Universe freshness: ASX 1.9 days old (fresh); ca_universe.json still missing (standing blocker).
- Signal log audit: 166 entries (+3 since prior maintenance), 0 duplicate hashes, 0 orphan FIGIs.
- OpenFIGI cache audit: 133 files on disk, all populated. Per-scan hit-rate still unobservable — low-priority instrumentation gap.
- Boilerplate filter audit: 699 raw signals reviewed across 30 files. No new boilerplate rules recommended. JP tanshin / guidance-revision patterns are load-bearing, NOT boilerplate.
- Candidate drift: 23 candidates re-queried against 7-day signal_log window; 0 true drift after filtering self-matches.

🔄 In progress:
- None.

⏭️ Next (maintenance):
- Re-probe HKEx and BMV endpoints with real URLs once Phase 5 / Phase 9 scanner modules land (unchanged).
- Once `working/ca_universe.json` exists, add SEDAR cache-hit-rate to per-scanner audit.
- Optional: stamp `resolver_cache_hit: bool` on resolved signals in `tools/openfigi_resolver.py`.

⚠️ Blockers:
- Prior operational session at 07:30Z did not release SESSION_LOCK on exit; lock was stale (>4h TTL) and taken over cleanly by this maintenance run. Not currently blocking; flag if recurrence on next cycle.
- `working/ca_universe.json` still missing — SEDAR scanner cannot produce non-zero raw signals. Operational-task responsibility per SESSION_STATE.

---

## 2026-04-16T16:26Z — Operational cycle (non-us-operational)

Lock: UNLOCKED → LOCKED (holder=non-us-operational, ttl_until=2026-04-16T20:23Z) → UNLOCKED at end.

Scanners run:
- **lse_rns --window 1** → 36 raw / 4 triaged / 2 resolved / 2 dedup survivors / 0 immediate / 2 watchlist. Wrote `candidates/watchlist/ATYM_XLON_2026-04-16.json` (major_shareholder_change, score 23.0) and `candidates/watchlist/SEIT_XLON_2026-04-16.json` (major_shareholder_change, score 23.0).
- **tdnet --window 3** → 135 raw / 1 triaged / 0 resolved. FIGI-resolve defect recurred (4th cycle in a row). One survivor dropped at OpenFIGI resolve — 404 on `364A0.T` alphanumeric mapping, same pattern as the `469A0` gotcha documented in CLAUDE.md. Patch needed in `tools/openfigi_resolver.py` or `tools/tdnet_scanner.py` ticker-local normalization.
- **asx** → Skipped (already finalized earlier this UTC date at 01:38Z, chunked cursor 426/426, finalize stage=done, 5 processed signals: PDI immediate candidate + VEA/FLT/DGT watchlist + COL archive). Confirmed via `asx_chunked_scan --window 7 --throttle 0.1` returning `DONE: 426 tickers processed`.
- **sedar --window 7** → 0 raw signals (expected: `working/ca_universe.json` still missing — standing #1 blocker).

No new candidate markdowns this cycle. No new watchlist files beyond ATYM + SEIT. Novelty dedup working as designed.

Errors: none returned from pipeline_runner. tdnet resolve-drop is a known recurring defect, not a fatal error.

