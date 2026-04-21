# TVTX Decision Watch — Session 45

**Date**: 2026-04-15 ~09:30 UTC (Tuesday Apr 15 morning ET; S44 ran on Apr 14)
**PDUFA date**: April 13, 2026 (Monday)

## Checks Performed

1. **yfinance**: Last trading data Apr 10, close $28.96. No Apr 13/14 data. Short interest now 14.61% (up from 13.1%).
2. **WebSearch (3 queries)**: No FDA approval or CRL announcement found.
3. **SEC EDGAR (CIK 0001438533)**: No 8-K since Apr 7. No FDA decision filing.
4. **Travere IR page**: Redirect error on fetch. No new press releases found via search.

## Assessment

**NO FDA DECISION ANNOUNCED** — consistent with S43 and S44 findings.

S44 noted a $38.21 price snippet that could indicate Apr 14 intraday approval-driven gap-up, but this was unconfirmed. yfinance did not return Apr 14 data in either S44 or S45.

## S46 Protocol

Same as S44 protocol:
1. yfinance for Apr 14+ close data
2. WebSearch for TVTX/Travere/sparsentan/FILSPARI FDA decision
3. SEC EDGAR for 8-K
4. If approval confirmed: execute post-approval protocol from SESSION_STATE
5. If CRL: archive protocol
6. If still pending by Apr 16: flag possible extended review
