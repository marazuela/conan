# Signal Analysis — Session 35

**Date**: 2026-04-13
**Scanner results**: EDGAR 0, Congressional 0, ESMA 2, Contract 0, FDA PDUFA 13

---

## AMT Convergence Alert — DISMISSED (Noise)

The convergence engine flagged AMT (American Tower Corp, $84B REIT) with signals from 2 strategies:

1. **Congressional trade**: Ro Khanna (D-CA) SOLD $1K–$15K AMT from a child's account on 2026-03-30
   - This is an $8K midpoint sell from a dependent minor's account
   - No committee alignment (Khanna is on Armed Services + Oversight)
   - Single trade, not a cluster
   - **Signal quality: NOISE** — sub-$15K dependent account trades have zero predictive value

2. **EDGAR activist keyword**: "strategic alternatives" found in an ARS (Annual Report to Shareholders) filing dated 2026-04-08
   - Filing type: ARS — this is the annual report itself, not an activist filing
   - "Strategic alternatives" is boilerplate language that appears in virtually every large-cap annual report's governance/strategic discussion sections
   - **Signal quality: FALSE POSITIVE** — the EDGAR scanner correctly detects the keyword but cannot distinguish boilerplate from actionable context

**Assessment**: Both signals are independently low-quality (strength 2). Their convergence is coincidental, not meaningful. AMT is a mega-cap REIT with no visible catalyst, no activist campaign, no unusual insider activity. **No further investigation warranted.**

**Improvement note**: The EDGAR scanner needs a filing-type filter to deprioritize ARS filings for activist keywords (ARS is the annual report, not an activist proxy or 13D). Logged in OPEN_QUESTIONS.md.

---

## ESMA Short Signals — Low Priority

### IQE (IQX.DE) — Crowded Short
- ISIN: GB0009619924
- 3 holders: Qube (0.5%), Two Sigma (1.09%), Walleye (0.95%)
- Market cap: NULL (likely below threshold, could not resolve via yfinance)
- **Assessment**: UK semiconductor company. Market cap unresolvable — likely micro/small cap. Three quant funds all at minimum disclosure threshold (0.5%). This is routine quant factor positioning, not conviction. **DISCARD** — fails market cap triage.

### Spire Healthcare (SPI.L) — Crowded Short
- ISIN: GB00BNLPYF73
- 4 holders: GLG (0.6%), Qube (0.5%), Two Sigma (0.5%), Walleye (0.5%)
- Market cap: £610M (~$770M)
- **Assessment**: UK private healthcare provider. Four holders all near minimum threshold. No escalation (all positions stable). This is quant-driven factor exposure, not conviction short-selling. **DISCARD** — no edge, no catalyst.

---

## FDA PDUFA Scanner — Known Entries

13 signals, all matching existing watchlist entries. No new PDUFA dates discovered (EFTS was returning 500 errors on keyword queries today). Scanner confirmed AXSM approaching (T-17).

**Note**: ZLAB and CORT reappear in scanner output despite S34 disqualification. The scanner doesn't have a disqualification filter — this is a known enhancement needed (D-039 PDUFA scanner filters). S35 confirms these remain DISQUALIFIED per S34 analysis.

---

## Active Candidate Status Update

| Ticker | Score | Price | Short% | Status | Notes |
|--------|-------|-------|--------|--------|-------|
| TVTX | 29.75 | $28.96* | — | DECISION PENDING | No FDA decision as of 12:30 PM ET |
| AXSM | 30.75 | $178.11 | 6.41% | ALL CLEAR | UBS PT raised to $259 |
| VERA | 30.50 | $45.04 | 13.1% | ALL CLEAR | No new developments |
| VRDN | 26.00 | $14.95 | 8.34% | WATCHLIST | Stabilized post-REVEAL |
| MNKD | 26.75 | $2.57 | 7.48% | WATCHLIST | No new data |
| HROW | 22.5 | $35.93 | 21.1% | WATCHLIST | Short interest very high |

*TVTX price is Apr 10 close — market closed Apr 11 weekend, reopening today.

## New Pipeline Addition

| Ticker | Score | PDUFA | Notes |
|--------|-------|-------|-------|
| ARVN | ~22.5 | Jun 5 | First PROTAC, Pfizer partner, modest PFS data. Watchlist. |
