# Session 28 — Signal Triage

**Date:** 2026-04-10
**Signals this session:** 25 total (1 EDGAR, 0 Congressional, 11 ESMA, 0 Contract, 13 FDA)
**Convergences:** 0

## EDGAR (1 signal)

**LODE — Comstock Inc. ($234.9M mcap)** — "change of control" keyword hit in DEF 14A
- **Decision:** FALSE POSITIVE (boilerplate equity-plan acceleration language)
- **Verification:** Pulled `lode20260212_def14a.htm` directly. Context confirmed: "participant. The administrator may also shorten the vesting period of an award in connection with a participant's death, disability, retirement or termination by our company without cause **or a change of control**." — This is standard equity plan vesting language, not an M&A transaction.
- **Action:** Triaged out. No candidate creation.

## Congressional (0 signals)
Healthy. 266 trades fetched from Capitol Trades, filtered: 0 material signals after filter.

## ESMA Short (11 signals)

Same French cluster from S27 re-appearing in dedup:
- SW.PA (Sodexo) × 5
- UBI.PA (Ubisoft) × 2
- VIRI.PA × 1
- ELIOR.PA × 1
- 2 unresolved tickers

**Decision:** D-035 basket filter applies. Already triaged in S27 French Short Wave memo. No new basket members, no new fundamental disconfirmation, no new entry signals. Watchlist entry for VIRI.PA (23-24) is the only elevated name; rest remain informational only.

**Action:** Zero new candidates. No re-triage needed. Continue monitoring VIRI.PA for fundamental disconfirmation and price break <€110.

## Contract (0 signals)
Baseline. No new material defense contract signals.

## FDA PDUFA (13 signals)

Roster of upcoming PDUFA dates — **ALL TICKERS ALREADY KNOWN** to the system:

| Ticker | PDUFA | Status |
|--------|-------|--------|
| TVTX | Apr 13 | ACTIVE — provisional 29.75 |
| AXSM | Apr 30 | ACTIVE — 30.75 |
| ZLAB | May 10 | Excluded (NMPA false positive; S24) |
| MNKD | May 29 | Watchlist 26.75-27.75 |
| ARVN | Jun 5 | Watchlist 27.5 (T-50 re-score Apr 17) |
| PFE | Jun 15 | Not scored — mega-cap, zero info edge |
| ARQT | Jun 29 | Research queue |
| LNTH | Jun 29 | Research queue |
| IONS | Jun 30 | Research queue |
| AZN | Jun 30 | Not scored — mega-cap |
| VRDN | Jun 30 | Watchlist 26.0 (demoted) |
| ORCA | Jul 6 | Private — not scoreable |
| VERA | Jul 7 | Research queue |

**No new candidates.** All June 29-30 names need eventual evaluation but T-60 window opens April 30 for the earliest (VERA/IONS July 7/June 30). Will start research queue work for VERA, IONS, ARQT, LNTH as T-60 approaches.

## Convergence (0)
Zero directional convergences. Normal for early-catalyst window. S27 pattern continues.

## ARVN Pulse Check (T-56)
- Price $11.17, trading sideways in $10.00-$12.05 range
- No April 8-Ks (last 8-K Mar 18, known)
- No material news
- **Re-score still scheduled for Apr 17** (T-50, per S27 plan)
- Holds watchlist at 27.5

## Summary

- **0 new candidates** in S28
- **0 convergences**
- **2 active candidates** (TVTX, AXSM) both passing kill sweeps
- **All kill conditions clear** for both active candidates
- **Market narrative:** REPL PDUFA pending, TVTX T-0 decoupled and stable, AXSM breaking out on volume
- **Next significant actions:** REPL resolution (next session), TVTX Monday protocol (next session), ARVN T-50 re-score (Apr 17), AXSM T-7 deep dive (~Apr 21)
