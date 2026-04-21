# Development Roadmap — 2-6 Week Sequence
A concrete, ordered plan for improving signal accuracy across the tool system.

---

## Week 1 (this week + next scheduled session)

**Day 0–1 (now)**
- [ ] Clean `convergence_engine.py` trailing duplicate code (L560–569). 5 min.
- [ ] Enable `openfigi_resolver.py` persistent file cache. 15 min.
- [ ] Fix empty PDUFA-watchlist table in `run_post_scan.py`. 30 min.
- [ ] Trace why today's FDA signals are all strength 2 (verify `_assess_strength` reading enrichment). 30 min.

**Day 2–3**
- [ ] Apply Q-014 Ro Khanna spouse/child/mega-cap downgrade in `congressional_trading.py`. 30 min.
- [ ] Add ETF/fund + non-US ticker reject list in `congressional_trading.py`. 1 h.
- [ ] Align `contract_monitor.py` mcap floor to $215M. 1 min.

**Day 3–5**
- [ ] Expand `CONTRACTOR_TICKER_MAP` from last 10 runs of UNMATCHED logs. 2–3 h.
- [ ] Apply Q-009 EDGAR proxy-season whitelist (activist + governance). 1 h.
- [ ] Apply Q-010 EDGAR SPAC-issuer blacklist + $500M floor for distress. 1 h.

**End of Week 1 checkpoint**: Rerun pipeline. Expected outcomes:
- Contract signals rise from 0 → 3–5 per week (INFERRED based on unmatched-log pattern).
- Congressional strength-4 mega-cap FPs drop ~80%.
- EDGAR activist category becomes usable again for May 2026+.

---

## Week 2

- [ ] ESMA: add historical crowded-short tracking (14-day rolling state file). 4 h.
- [ ] ESMA: snapshot retention + archive policy. 1 h.
- [ ] Convergence: add soft-convergence logging (15–28 day window). 2 h.
- [ ] Convergence: directional classifier audit — write unit tests for bull/bear/conflicting across representative signal pairs. 4 h.
- [ ] FDA PDUFA: add staleness flag for watchlist entries ≥60 days without enrichment. 1 h.
- [ ] run_post_scan: add "Newly Emerged Watchlist Items" section to daily report. 1 h.

**End of Week 2 checkpoint**: Soft convergences should show up 2–5× per week and be a useful pattern-mining input for future decisions.

---

## Week 3–4

**FINRA short-volume loader (enables A3 synergy for US tickers)**
- [ ] Feasibility: fetch FINRA ShortSaleVolumeDaily CSV, validate schema. 2 h.
- [ ] Build `tools/finra_short_loader.py` — daily load + rolling 20-day average per ticker. 1 day.
- [ ] Integrate into convergence engine: boost FDA PDUFA signals when US short-volume / float > 15%. 2 h.

**Parallel investments**
- [ ] Watchlist spring cleaning: review `pdufa_watchlist.json` for stale entries; archive confirmed-approved/rejected.
- [ ] Expand DISQUALIFIED_TICKERS with expiry dates. 1 h.
- [ ] Trace empty-table and other silent bugs by adding pre-shutdown checksum to SESSION_STATE writes.

---

## Week 5–6

**New scanner: Insider Form 4 (C3 in synergy doc)**
Highest-ROI additional strategy based on free-data availability + signal quality.
- [ ] Data-source validation: SEC EDGAR Form 4 feed, parse ownership.xml. 1 day.
- [ ] Build `tools/form4_scanner.py` — CEO/CFO/10% buys ≥$1M, multi-insider clusters, pre-catalyst windows. 2 days.
- [ ] Integrate into pipeline_runner + convergence engine. 4 h.
- [ ] Add to daily report. 1 h.

**Calendar watch**
- [ ] FCA ANSP regime change (June 1 2026) — prepare adapter for aggregate-format ESMA data. 1 week.

---

## Beyond week 6 (backlog, prioritized)

1. Candidate news monitor (Q-011) — weekly per-candidate kill-condition scan.
2. Contract materiality-vs-revenue scoring.
3. Options IV-mispricing scanner (requires paid data — budget decision).
4. AdCom calendar integration.
5. CNMV Spain access (Q-002) — Pedro's home market.
6. CONSOB Italy access.
7. Patent-expiry scanner (new 6th strategy).
8. Weekly rollup reports.

---

## Quality gates before each week ends

- All scanners `py_compile` clean (already a session-start check — keep).
- Daily report generated without silent row-drops.
- Convergence engine run clean.
- At least one candidate-grade signal per week OR a documented "quiet week" rationale.
- SESSION_STATE.md and PROGRESS_LOG.md updated.

---

## How to measure improvement

**Leading indicators** (per-week):
- Number of strength-4+ signals across all scanners.
- Number of soft-convergences logged.
- Number of candidate-grade (28+ score) setups.

**Lagging indicators** (per-month):
- Candidates that resolved favorably (TVTX +34% approval, AVNS deal spread) vs. kill-condition hits (REPL CRL demoted, VRDN REVEAL-1 demoted).
- Accuracy of the kill-condition calls.
- Time from signal to candidate writeup.

---

## Verification notes
- All effort estimates are INFERRED from reading the code and knowing the size of the needed changes.
- FINRA / Form 4 feasibility: data availability is widely VERIFIED at the industry level; specific URL + format requires validation before building.
- June 2026 FCA ANSP date VERIFIED via OPEN_QUESTIONS.md (claim sources trace to FCA publications — not re-verified in this audit).
- "Week 1 checkpoint" yield projections are SPECULATED based on current FP-rate patterns.
