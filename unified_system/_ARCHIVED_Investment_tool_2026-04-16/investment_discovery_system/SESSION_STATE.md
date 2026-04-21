# Session State — The Relay Baton

**Last updated**: 2026-04-16 16:30 UTC by Scheduled Session 68
**Next session**: Next operational / maintenance run (~2026-04-16 19:00 UTC)

---

## TOP HEADLINE — S68 OPERATIONAL PIPELINE COMPLETE. ACTIVE CANDIDATE ROSTER UNCHANGED.

**Key outcomes this session**:

1. **Daily scanner pipeline** — 5/5 scanners OK at 16:15 UTC: edgar 1, congressional 12, contract 0, esma_short 11 (6 strength-4 HIGH), fda_pdufa 10 → 34 total signals, 0 convergences (2 pre-suppressed: AMT, FBLG). Daily report: `reports/2026-04-16_daily_report.md`.

2. **AXSM T-13 kill-sweep #30 — ALL CLEAR at T-14**. SEC submissions API CIK 0001579428: latest filing still 2026-04-01 8-K (pre-catalyst window); no 4/144/8-K Apr 14–16. Price $184.79 (+0.33% vs S67). Thesis intact. Next intensive check T-12 (Apr 18).

3. **VERA CEO Fordyce Apr 14 Form 4 — re-parsed from primary XML**. Three sale tranches (14,130 + 7,921 + 900 = 22,951 sh) at weighted $43.66 / $44.58 / $45.51. **`<aff10b5One>1</aff10b5One>` flag confirms 10b5-1 plan affirmation** (same Jan 9 2026 plan as S67 reported). Form 144 Apr 14 is the prospective-sale notice. Routine. No thesis change. Price $42.19 (-2.9% vs S67) reflects selling pressure.

4. **SEM entity-resolution correction logged**. S67 queried SEM via CIK 0001320350 — which is actually **LENSAR, Inc. (ticker LNSR)**, NOT Select Medical Holdings. Correct SEM CIK = **0001320414** (verified via EFTS 10-K search). Re-query on correct CIK: only filings post-Apr 13 are the Apr 15 PREM14A + SC 13E3 already logged in S67. Candidate file deal-specs match. Added to Warning #0r.

5. **GSAT 8-K termination fee confirmed from body text**: **$419,832,000** (Company owed to Amazon under breach / alternative-acquisition-within-12-months scenarios per Merger Agreement). No other new GSAT filings Apr 16. Schedule 14C filing watch continues.

6. **AVNS, RGR, RPAY, SEM — all CLEAN on Apr 16 SEC submissions sweep**. AVNS annual meeting postponement (Apr 21 → special meeting for merger vote) already logged S61. RGR: no new filings since Apr 15 PRER14A. RPAY: no new filings since Apr 15 Veradace 13D. SEM: no new filings since Apr 15 PREM14A + SC 13E3.

7. **Fresh prices** (yfinance fast_info 16:20 UTC): AXSM $184.79 (+0.33%), RPAY $3.17 (+0.6%), RGR $41.93 (-0.99%), VERA $42.19 (-2.9%), AVNS $24.64 (-0.32%), GSAT $79.80 (-1.04%), SEM $16.38 (unch), MNKD $2.71 (-2.2%), ARVN $10.91 (-0.55%), VRDN $14.89 (+0.07%).

### Key Findings This Session

- **Zero new material thesis changes.** Every active candidate status sustained. The AVNS and GSAT "8-K Apr 14" filings I initially saw as new are the original deal-signing 8-Ks already captured in their respective candidate files (0001606498-26-000046 / 0001140361-26-014528). The AVNS DEFA14A filings (0001606498-26-000050 + 0001104659-26-043093) were already logged in S61/S64 — no new info.
- **Entity-resolution lesson reinforced**: S67's SEM CIK was wrong. The only reason it didn't cause a thesis error was because S67 still retrieved a valid 8-K — but from the wrong company (LENSAR). This is exactly the failure mode Warning #0r exists to catch. Re-verification via EFTS 10-K name-search should be default when a CIK returns a filing whose ticker doesn't match the candidate's ticker.
- **10b5-1 primary-source verification method validated**: Parsing `<aff10b5One>` directly from Form 4 XML is the cleanest method for confirming plan-affirmation. This is stronger evidence than inferring routine-ness from dates alone.

---

## What's Done

- ✅ Concurrency lock acquired (SESSION_LOCK.md @ 2026-04-16T16:08:10Z).
- ✅ Python dependencies installed (requests, beautifulsoup4, lxml, yfinance, openpyxl, pandas, python-docx).
- ✅ S68 orient reads complete (SESSION_STATE, INSTRUCTIONS).
- ✅ Tool validation: all 5 scanners compile OK; 4/5 external sources reachable (ClinicalTrials.gov 403 — persistent egress restriction, pipeline uses EDGAR PDUFA discovery as primary).
- ✅ All 5 scanners run sequentially (edgar --rotate, congressional, esma_short, contract, fda_pdufa).
- ✅ run_post_scan.py aggregation complete → `reports/2026-04-16_daily_report.md`.
- ✅ AXSM T-13 kill-sweep #30 ALL CLEAR at T-14.
- ✅ VERA CEO Apr 14 Form 4 re-parsed from XML → aff10b5One=1 affirmed.
- ✅ SEM correct CIK verified via EFTS: 0001320414 (not 0001320350).
- ✅ GSAT 8-K termination fee confirmed: $419,832,000.
- ✅ Fresh prices collected via yfinance for all 10 tracked tickers.
- ✅ TIME_SENSITIVE.md updated with S68 changes.
- ✅ Potential_Opportunities.docx regenerated (3 paths: root, outputs/, parent).
- ✅ Report Summary/{system_status_report.md, candidate_pipeline.md} updated with S68 snapshot.

## What's In Progress

- 🔄 S68 shutdown protocol execution (SESSION_STATE + PROGRESS_LOG + INDEX + LOCK release).

## Next Session Priorities (S69)

**High priority (any session)**:
1. **AXSM T-12 kill-sweep #31** (Apr 18). SEC CIK 0001579428: 8-K, 4, 144. Price. Intensive daily cadence active through Apr 30.
2. **GSAT Schedule 14C filing watch** — primary expected formal filing (~late Apr – early May). Keyword monitor: "Schedule 14C", "Information Statement", "14C". Also watch for S-4 (stock-election mechanics) and HSR.
3. **RGR monitoring for cooperation agreement 8-K** — T-41 to AM; settlement 8-K could drop any day. Watch for DEF 14A filing.
4. **SEM watchlist monitoring (weekly)** — spread check vs $16.50; DEF 14A filing (expected 20-45 days after Apr 15 PREM14A = early-to-mid May); HSR/litigation news. Use correct CIK **0001320414**.
5. **RPAY activist response watch** — Forager/Veradace follow-on 13D/A, RPAY 8-K board response, PREC14A preparation.

**Medium priority**:
6. **MNKD T-20 evaluation (~May 8)** — promote from watchlist to active if signal strengthens.
7. **AVNS DEF 14A filing watch** — expected late May.
8. **VERA T-30 intensive activation (~Jun 7)**.

**Lower priority / backlog**:
9. ESMA short signals (6 strength-4 new today: BME.L, MTLN, VTY.L, TEP.PA, EXA.PA, BFIT.AS) — standalone short signals rarely promote to active without convergence; check back if same ticker reappears in a different strategy.
10. EDGAR rotation schedule — next categories: governance (strategic rotated Apr 16), distress, mna.

## Active Warnings / Known Issues

- **Warning #20 (persistent)**: EDGAR strength-5 mna rotation has ~7:1 false-positive ratio. Always fetch primaryDocument 8-K body before assuming MNA category is real.
- **Warning #23 (yfinance cache)**: yfinance fast_info can be stale; use `fast_info.last_price` preferentially; if price is identical to prior session, verify with alternate query.
- **Warning #0r (entity resolution)** — **UPDATED S68**: Some CIKs from older tool outputs resolve to wrong companies. Verified S68: **SEM correct CIK = 0001320414 (NOT 0001320350 which is LENSAR)**. Previously verified: VERA CIK 0001831828 (not 0001851657/Vaxxinity); AVNS CIK 0001606498 (not 0001644440/GCP Applied). Method: when a CIK returns a filing whose ticker doesn't match the candidate, re-verify via EFTS `search-index?q="<Company Name>"&forms=10-K`.
- **Rule (from S66)**: Distinguish tax-withholding Form 144s (acquisition nature = "Restricted Stock Vesting", payment = "Compensation") from voluntary open-market sales.
- **S67 lesson**: Always cross-check candidate file before treating a filing as novel.
- **S68 lesson**: Primary-source XML parse of `<aff10b5One>` is stronger evidence for 10b5-1 plan than date-inference alone. Use for all future insider-sale clarifications.
- **Egress restriction (persistent)**: ClinicalTrials.gov v2 API returns 403 from sandbox (all user-agent variants). Not a tool bug — pipeline uses EDGAR PDUFA discovery as primary pathway. No action required.
- **Maintenance note (persistent)**: `rm -rf tools/__pycache__` returns "Operation not permitted" in sandbox. Individual .pyc writes via py_compile succeed — no stale-bytecode issue for operational pipeline.

## Active Blockers

- **None** — no blockers for S69.

## Files Modified This Session

- `SESSION_LOCK.md` (acquired LOCKED @ 2026-04-16T16:08:10Z; will release UNLOCKED at end).
- `TIME_SENSITIVE.md` — rewritten with S68 state (timestamp, fresh prices, S68 changes summary prepended; S67/S64 preserved).
- `Potential_Opportunities.docx` (root) + `outputs/Potential_Opportunities.docx` + parent `Potential_Opportunities.docx` — regenerated with S68 headline, candidate table, 60-day calendar.
- `Report Summary/system_status_report.md` — S68 headline + external-source reachability note prepended.
- `Report Summary/candidate_pipeline.md` — S68 snapshot table prepended.
- `SESSION_STATE.md` — this file (relay baton for S69).
- `PROGRESS_LOG.md` — will append S68 block.
- `INDEX.md` — unchanged (no new files created).
- `signals/*` — scanner outputs refreshed (12 congressional, 11 esma_short, 10 fda_pdufa, 1 edgar, 0 contract + aggregations).

## Execution Environment

- Bash timeout: 45s hard limit (use backgrounded nohup for long-running).
- Outputs folder writable at `/sessions/dreamy-happy-pasteur/mnt/outputs/`.
- Working folder `working/` exists at project root.
- yfinance works for all candidate tickers; fast_info is preferred price source.
- **Scheduled-task dependency list** (reinstall every session): requests, beautifulsoup4, lxml, yfinance, openpyxl, pandas, python-docx.

---

*S68 handoff complete. S69 should cold-start by reading SESSION_STATE.md (this file) → INSTRUCTIONS.md → then begin Priority #1 (AXSM T-12 kill-sweep #31).*
