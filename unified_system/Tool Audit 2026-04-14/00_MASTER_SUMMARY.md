# Tool Audit — Master Summary
**Date**: 2026-04-14
**Auditor**: Claude (Cowork session, user = Pedro)
**Scope**: Originally `investment_discovery_system` (5 scanners + shared infra). **Expanded 2026-04-14** to include `Investmet tool Beta / non_us_discovery_system` — see `09_beta_non_us_diagnostic.md`. Still to cover: Gamma (silence scanner, blueprint), Delta (litigation, Phase 2 partial, PTAB Apr 20 deadline), Independent review (cross-system analyzer, blueprint).
**Purpose**: Tell Pedro honestly how each tool is doing, what to fix, what to build next.

---

## Multi-tool scope note (added 2026-04-14)

The portfolio at `Conan/` root contains five tool folders, not one:

| Folder | Status | Audit |
|--------|--------|-------|
| `investment_discovery_system/` | Live, 5 scanners | Files 01-08 in this folder |
| `Investmet tool Beta/non_us_discovery_system/` | Live, 3 of 9 scanners; Phase 4 code-complete | File 09 (this audit) |
| `Investment tool Gamma/` | Blueprint only (silence scanner concept) | **Not yet audited** |
| `Investment tool Delta/` | Phase 2 partial, litigation-focused; PTAB deadline 2026-04-20 | **Not yet audited — may be time-sensitive** |
| `Independent review/` | Blueprint only (cross-system analyzer) | **Not yet audited** |

Decision on ordering remains open — per user's 2026-04-14 directive, Beta is delivered first; Delta/Gamma/Independent review to be decided separately.

---

## Headline

**The pipeline is working.** On 2026-04-14 (today), the scheduled S56 run caught two real M&A deals on announcement day — AVNS (American Industrial Partners at $25, 72% premium) and GSAT (Amazon at $90). AVNS became a new active candidate (28.5 score), GSAT a watchlist entry. This is exactly the intended output of the EDGAR M&A rotation. Similar validated wins in recent history: TVTX PDUFA approval (Apr 13), REPL CRL pre-read (Apr 8–10), VRDN timely demotion (Apr 10).

That said, the system has **accumulated technical debt** from ~56 scheduled sessions of continuous operation. The debt is small per item but adds up. The priorities in this audit are (a) **hygiene** (easy fixes with outsized downside if ignored), (b) **signal quality** (filters and downgrades identified in OPEN_QUESTIONS but not yet applied), (c) **coverage gaps** (low-yield scanners that deserve tuning or retirement).

---

## Tool-by-tool grade (one-line summaries)

| Tool | Grade | Status | Top priority |
|------|-------|--------|--------------|
| `edgar_filing_monitor.py` | **A** | Working well; caught 2 live deals today | Apply Q-009 + Q-010 form whitelists (proxy + SPAC) |
| `fda_pdufa_pipeline.py` | **A−** | Working, with watchlist auto-discovery + early-approval cross-check | Watchlist freshness audit; improve strength scoring |
| `esma_short_scanner.py` | **B+** | All 4 regulators live; good signal flow | Add crowded-short historical tracking; plan for FCA June 2026 ANSP transition (Q-008) |
| `congressional_trading.py` | **B** | Live, Capitol Trades stable | Apply Q-014 Ro Khanna FP filter; ticker noise (ETFs, non-US) |
| `contract_monitor.py` | **C** | Running but zero signals for many runs | Expand CONTRACTOR_TICKER_MAP; review mapping gaps logged at INFO |
| `convergence_engine.py` | **B** | Working but **has dead duplicate code** (L560–569) | Clean trailing garbage; add partial-convergence surfacing |
| `openfigi_resolver.py` | **A−** | v3 resolver stable, 37/42 resolved last run | Enable persistent file cache (currently `CACHE_FILE = None`) |
| `mcap_cache.py` | **A** | Shared cross-scanner cache, 24h TTL, clean | Add cache-purge helper; persist last-good value on yfinance errors |
| `pipeline_runner.py` | **A−** | Subprocess isolation + 120s hard-kill working | None — keep as-is |
| `run_post_scan.py` | **A** | Reports generated cleanly | Minor: auto-truncate daily watchlist table (see today's report bug L88–91) |

**Verification note**: Grades above reflect state as of 2026-04-14 based on reading source code, SESSION_STATE.md (updated 16:55 UTC today), the most recent daily report, and the signal log artifacts. No live scanner runs were executed during this audit — the Cowork sandbox was unavailable when I started. The scheduled task itself is running every 3 hours and its output is the empirical evidence.

---

## Priority-ordered action list

### P0 — Do this session
1. **Clean `convergence_engine.py` trailing duplicate code** (lines 560–569). Harmless but signals the file-truncation bug warned about in SESSION_STATE active-warning #1. Code hygiene. 5-minute fix.
2. **Enable persistent OpenFIGI file cache** (`openfigi_resolver.py` `CACHE_FILE = None` → set to `signals/openfigi_cache.json`). ESMA scanner already maintains its own `esma_ticker_cache.json` — redundant. Unifying saves ~40 API calls/session. 15-minute fix.
3. **Fix watchlist table bug in `run_post_scan.py`**. Today's report (2026-04-14) shows `## PDUFA Watchlist` header followed by empty table, then `## Next Steps`. The watchlist generator silently emitted zero rows. Inspect the watchlist emit path.

### P1 — Next 1–2 sessions
4. **Apply Q-009 EDGAR proxy-season whitelist**. Activist + governance categories blind during March–May. Form whitelist: {8-K, SC 13D, 13D/A, SC 14D9, PRER14A, DFAN14A}. Already drafted in OPEN_QUESTIONS.md; just needs to land in `edgar_filing_monitor.py`.
5. **Apply Q-010 EDGAR SPAC/de-SPAC filter** for distress + M&A categories. Form blacklist already partially implemented via `SPAC_IPO_FORM_BLACKLIST` — extend with issuer-name blacklist ("Acquisition Corp", "Blank Check") and lift mcap floor for distress to $500M.
6. **Apply Q-014 Ro Khanna spouse/mega-cap tech downgrade** in `congressional_trading.py`. Rule: owner ∈ {Spouse, Child} AND trade ≤$50k AND mcap ≥$100B AND committee = Commerce → downgrade 4 → 2. Pattern confirmed across ≥2 sessions.
7. **Contractor map expansion**: scrape INFO-level log entries from last 10 runs for "UNMATCHED HIGH-VALUE" and triage. Today's run had 0 contract signals — the bottleneck is the mapping table, not USAspending.

### P2 — Next 1–2 weeks
8. **ESMA crowded-short history tracking**: persist `esma_snapshots/` longer than 1 day so we can detect *newly* crowded names (today 6-holder; yesterday 2-holder is a real signal; today 6-holder that has been 6 for weeks is not).
9. **Partial-convergence surfacing in `convergence_engine.py`**: today's run had 0 convergences (2-strategy within 14 days). Useful variant: surface 1-strategy signals where a *prior* signal from a different strategy fell 15–21 days ago. Soft-convergence logging for pattern mining.
10. **Disqualification-list expiry**: `DISQUALIFIED_TICKERS` in `fda_pdufa_pipeline.py` is static. Add optional expiry dates so e.g. CORT can auto-re-enable if a new PDUFA appears 12+ months from the last event.

### P3 — Strategic / calendar-driven
11. **FCA June 2026 ANSP regime transition** (Q-008). Scanner will lose per-holder resolution in UK when aggregate-anonymized replaces named disclosures. 6-week runway to adapt.
12. **CNMV (Spain) access** (Q-002). Pedro's home market. Returns 403 from Python sandbox. Browser-automation path or periodic manual check.
13. **CONSOB (Italy) access** (spec note). Bot protection via Radware. Same browser-automation pattern.
14. **Candidate news monitor** (Q-011): lightweight weekly scan per active candidate for kill-condition events. Currently manual. Automation would catch competitor events (VRDN/REVEAL-1 was caught manually but barely).

---

## Cross-cutting observations

**Synergy opportunities (strategic, speculative)**:
- EDGAR M&A + ESMA shorts — a company with high short interest AND a new M&A filing is either a squeeze setup (bullish) or a confirmed-bad-news short (bearish). The convergence engine already supports this, but directional classification in `convergence_engine.py` (bullish/bearish/conflicting) needs a targeted test.
- Congressional + Contract + EDGAR — defense sector. Ro Khanna's committee alignment is Commerce (FP per Q-014), but Armed Services members (e.g., Tommy Tuberville) trading defense primes in the same week USAspending awards post is a high-signal stack. Worth filtering convergence output by sector match.
- FDA PDUFA + EDGAR — 8-K auto-discovery is already the bridge. Worth adding the reverse: if EDGAR flags a distress signal on a ticker with an approaching PDUFA, auto-raise its strength (PDUFA-before-distress = high-binary setup).

**Gaps that are unlikely to close without investment**:
- No options-flow scanner (mentioned in FDA PDUFA spec as #5 but never built). Would catch IV mispricing — the strongest form of pre-catalyst edge.
- No social-sentiment scanner (Reddit, StockTwits, X). Trade-offs around noise vs. signal; deferred.
- No insider-transaction scanner (Form 4). High-value, relatively easy to build, not yet on the roadmap.

**What NOT to change**:
- Subprocess isolation in `pipeline_runner.py` — this is doing its job (preventing cascading scanner failures).
- 120s hard-kill timeout — EDGAR is the fastest to hit it; the 35s wall-clock budget inside EDGAR is the right defense.
- Capitol Trades fallback for congressional — Quiver API now requires auth (Q-006). Don't revisit unless Quiver offers free tier.
- OpenFIGI v3 migration — already done (v2 sunsets Jul 1 2026).

---

## Verification / speculation labeling

The per-tool diagnostic files (`01_...` through `09_...`) follow a consistent labeling convention per the project's Prime Directive:

- **VERIFIED** — statements directly traceable to source code, SESSION_STATE, DECISIONS, or recent signal artifacts. The majority of claims.
- **INFERRED** — reasonable conclusions drawn from combining multiple verified facts. Always tagged.
- **SPECULATED** — forward-looking or hypothetical. Tagged and kept minimal.

The audit did not run any scanners live (Cowork sandbox was unavailable when I started); all health claims are based on reading artifacts. The S56 scheduled run (2026-04-14 16:55 UTC) is the most recent live-run evidence and confirms all 5 scanners + orchestrator as healthy today.

---

## Files in this audit folder

- `00_MASTER_SUMMARY.md` — this file
- `01_edgar_diagnostic.md`
- `02_esma_short_diagnostic.md`
- `03_congressional_diagnostic.md`
- `04_contract_diagnostic.md`
- `05_fda_pdufa_diagnostic.md`
- `06_shared_infra_diagnostic.md` — openfigi_resolver, convergence_engine, mcap_cache, pipeline_runner, run_post_scan
- `07_synergy_opportunities.md` — cross-strategy signal-stacking ideas
- `08_roadmap.md` — proposed 2–6 week development sequence

Sources:
- [SESSION_STATE.md](computer://C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\Investment tool\investment_discovery_system\SESSION_STATE.md)
- [OPEN_QUESTIONS.md](computer://C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\Investment tool\investment_discovery_system\OPEN_QUESTIONS.md)
- [2026-04-14_daily_report.md](computer://C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\Investment tool\investment_discovery_system\reports\2026-04-14_daily_report.md)
