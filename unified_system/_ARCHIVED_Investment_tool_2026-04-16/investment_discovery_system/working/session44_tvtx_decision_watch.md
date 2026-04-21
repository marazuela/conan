# TVTX Decision Watch — Session 44

**Date**: 2026-04-14 ~14:00 UTC (Tuesday, Apr 14 afternoon ET)
**PDUFA date**: April 13, 2026 (Monday — decision window was Monday business hours)
**Status**: Checking for decision that should have come Monday April 13

## Checks Performed

1. **yfinance**: Only returns data through Apr 10 (Friday close $28.96). No Apr 14 data available yet via library.
2. **WebSearch (multiple queries)**: No FDA approval announcement or CRL found. No press release from Travere Therapeutics announcing decision.
3. **SEC EDGAR (CIK 0001438533)**: Most recent 8-K filed 2026-02-19 (earnings). No FDA decision 8-K filed.
4. **Travere IR page**: Could not fetch (redirect block), but search results show most recent PR is the Jan 13 PDUFA extension announcement.
5. **drugs.com new approvals**: FILSPARI/sparsentan not listed for FSGS approval.

## Ambiguous Price Signal

- Web search snippet showed TVTX at $38.21 (one source) — this would be ~32% above Friday close of $28.96
- Another snippet showed $28.63 (likely stale/Friday)
- If $38.21 is real Apr 14 intraday trading, it strongly suggests APPROVAL — a gap up of this magnitude on PDUFA day is the canonical approval signal
- However: yfinance has not captured Apr 14 data yet, and no formal announcement has been found

## Assessment

**NO DECISION FORMALLY ANNOUNCED as of S44 data access (~14:00 UTC Apr 14)**

Most likely scenarios:
1. FDA issued decision today (Mon Apr 14) — announcement may come after market close or hasn't been indexed yet. The $38.21 price (if real) would suggest approval was communicated to the company pre-market.
2. FDA will issue decision tomorrow (Tue Apr 15) — not uncommon for PDUFA dates that fall on weekends.

## Protocol for S45

1. yfinance: Check for Apr 14 close data — this will be definitive:
   - Close ~$37-42 = APPROVAL (high confidence)
   - Close ~$20-23 = CRL (high confidence)
   - Close ~$28-30 = No decision yet
2. WebSearch: "Travere" OR "TVTX" OR "FILSPARI" approval April 2026
3. SEC EDGAR: Check for new 8-K (CIK 0001438533)
4. If APPROVAL confirmed: execute post-approval documentation (see SESSION_STATE protocol)
5. If CRL confirmed: execute archive protocol
6. If still pending: note FDA has taken extra business day(s), do not downgrade score
