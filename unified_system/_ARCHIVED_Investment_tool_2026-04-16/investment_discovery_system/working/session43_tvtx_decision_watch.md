# Session 43 — TVTX Decision Watch

**Date**: 2026-04-14 (Tuesday)
**PDUFA**: April 13, 2026 (Monday — decision window is the business day itself)

## Decision Status: PENDING — NO ANNOUNCEMENT FOUND

### Evidence Gathered

1. **yfinance**: Data only available through Apr 10 (Friday close $28.96). No Apr 14 (Monday) data returned by yfinance API. Cannot infer decision from price action.

2. **WebSearch**: Multiple searches for "TVTX FDA decision", "Travere FSGS sparsentan approval", "FILSPARI FSGS FDA approved". No press release announcing approval or CRL found. Most recent Travere IR press release found is the Jan 2026 review extension announcement.

3. **SEC EDGAR**: CIK 0001438533. Most recent filings:
   - 2026-04-07: Form 4 (insider transaction)
   - 2026-04-06: ARS, DEFA14A, DEF 14A
   - No 8-K filed since Apr 7 — no decision-related 8-K visible yet.

4. **FDA Novel Drug Approvals 2026 page**: Last updated Apr 6. Does NOT include FILSPARI — but this is expected because FSGS sNDA is a supplemental approval, not a novel drug. The sNDA would not appear here.

5. **Web search snippets**: Some sources mention TVTX at $38.21 (above $37.04 "10-year high") and $36.40. These snippets lack clear dates and may reflect:
   - Pre-PDUFA anticipatory trading
   - Different time windows (e.g., around AdCom waiver news)
   - Possibly Apr 14 intraday trading if approval announced during market hours
   
   **CAUTION**: These price snippets are inconclusive without confirmed timestamps. The $38.21 figure alongside "major technical breakout above $37.04 10-year high" is suggestive of a large move, but I cannot confirm this is from Apr 14.

### Assessment

**INCONCLUSIVE — DECISION MAY HAVE BEEN ANNOUNCED ON MONDAY OR DELAYED**

The PDUFA date was Monday Apr 13. FDA issues decisions on business days, so the expected decision window was Monday Apr 13. However, it's now Tuesday Apr 14. Given:
- No explicit press release found
- No 8-K filed
- yfinance not returning Apr 14 data clearly

It's possible that:
- (a) The decision hasn't been announced yet (FDA may issue on Tuesday or later)
- (b) The decision was announced very recently (during Apr 14 market hours) and hasn't propagated to our searchable sources yet
- (c) The web snippets showing ~$36-38 prices could indicate approval was announced and market has reacted on Apr 14

### S44 Protocol

S44 MUST immediately re-check:
1. yfinance for Apr 14 closing price — if available and >$35, approval is very likely
2. WebSearch for Travere press release
3. SEC EDGAR for 8-K filing
4. If approval confirmed: follow S42 protocol for post-decision documentation
5. If CRL: follow S42 archive protocol
6. If still pending by S44: FDA may take 1-2 extra business days — note in SESSION_STATE but do not downgrade
