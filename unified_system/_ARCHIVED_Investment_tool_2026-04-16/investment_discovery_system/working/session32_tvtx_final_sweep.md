# TVTX Final Pre-PDUFA Kill Sweep — Session 32 (2026-04-12)

## Context

**TVTX PDUFA = Monday April 13, 2026 (T-0).** This is the final pre-decision session before the market opens for the decision day. Active candidate score 29.75 at $28.996 (last close Friday April 10). Position: **hold-existing only** per TVTX_FSGS_PDUFA.md candidate file.

This sweep is the 6th consecutive clean sweep (S27–S32).

## Kill Sweep Checklist

### 1. EDGAR CIK 1438533 (Travere Therapeutics) — filings since Feb 19

Filings observed, ordered newest first:
- 2026-04-07: Form 4 (insider, routine 10b5-1)
- 2026-04-06: DEFA14A + DEF 14A (annual proxy — routine shareholder meeting)
- 2026-04-06: Form 144 (proposed sale, 10b5-1)
- 2026-04-02: Form 4 ×2 (insider, routine)
- 2026-04-01: Form 144 ×2 (10b5-1 plan sales)
- 2026-03-18: Form 4 (insider)
- 2026-03-16: Form 144
- 2026-02-24: Form 4
- 2026-02-20: Form 144
- 2026-02-19: 10-K (annual report)
- 2026-02-19: Form 4
- 2026-02-19: 8-K items 2.02+9.01 (Q4 earnings release)
- 2026-02-17: Form 144

**CRITICAL FINDING**: Zero 8-Ks since Feb 19 (which was Q4 earnings, non-adverse). Zero SC 13D or activist filings. Zero material definitive agreement disclosures. Zero FDA-triggered item 8.01 disclosures.

All Form 4/144 activity is consistent with S31 analysis: Baynes Nov 17 2025 10b5-1 plan with 5-month cooling-off period executing mechanically. The DEFA14A/DEF 14A dated April 6 is a normal April annual shareholder meeting proxy — timing typical for calendar-year-end filers.

**CIK 1438533: CLEAN.**

### 2. Company press release feed (StockTitan)

Latest TVTX news items on StockTitan feed:
- 2026-02-19: Travere Therapeutics Reports Fourth Quarter and Full Year 2025
- 2026-02-12: Travere Therapeutics to Report Fourth Quarter and Full Year 2025
- 2026-02-11: Travere Therapeutics Reports Inducement Grants Under Nasdaq Listing
- 2026-02-04: Travere Therapeutics to Present at the Guggenheim Emerging Outlook
- 2026-01-13: FDA Extends Review of sNDA for FILSPARI (sparsentan) in FSGS *(the extension announcement)*
- 2026-01-12: Travere Therapeutics Provides Corporate Update and 2026 Outlook

**No press releases since Feb 19. Zero April 2026 press releases. Zero weekend press releases.**

### 3. FDA press announcements

Queried: https://www.fda.gov/news-events/fda-newsroom/press-announcements (URL corrected from old /news-events/press-announcements which now 404s — S32 finding).

Search results for keywords: Travere, FILSPARI, sparsentan, FSGS, focal segmental.

**All keywords: NOT FOUND.** Most recent April 2026 FDA press release listed is April 1 (First New Molecular Entity NPV). No TVTX/FSGS-related announcement. Confirms FDA has not pre-announced a Monday decision.

### 4. Broad news search (April 11–12 weekend)

WebSearch queries:
- `Travere Therapeutics TVTX FSGS PDUFA April 13 2026 FDA decision news`
- `Travere Therapeutics TVTX FILSPARI April 11 12 2026 news`
- `"TVTX" OR "Travere" news "April 2026" approval rejection sparsentan`

All results returned only:
- The original Jan 13 2026 extension announcement
- The May 2025 sNDA acceptance announcement
- Secondary analyst commentary (all pre-existing, dated before Apr 11)
- AInvest article: "Travere (TVTX) Hinges on April 13 FDA Decision—FSGS Approval Could Unlock Wide Moat or Trigger Sharp De-Rating" (pre-existing speculation)

**No breaking news, no AdCom announcement, no FDA action leak, no company pre-announcement.**

### 5. Sympathy name cross-check

REPL CRL was Apr 10. REPL decoupling from TVTX reached 40.5pp by end of S31. As of Friday close:
- TVTX: -7.8% on Apr 10 (diagnosed as mechanical sympathy risk-off from REPL CRL + IOVA melanoma -7.7% on same day, not thesis damage)
- REPL: continued decline (peak-to-trough -44.2%)

**TVTX Apr 8 price action was DIFFERENT from REPL Apr 8.** REPL Apr 8 was a -24.6% CRASH on 2.0× volume = D-036 candidate leading indicator. TVTX Apr 8 closed UP intraday. This is a critical distinction for the D-036 draft rule.

## Verdict

**TVTX kill sweep: 6th consecutive clean sweep (S27, S28, S29, S30, S31, S32). CLEAN. Score 29.75 holds into Monday open.**

No new information warranting score change. Hold-existing protocol. Execute Section 7 of `candidates/TVTX_FSGS_PDUFA.md` on decision.

## Monday Apr 13 Execution Protocol

The SESSION_STATE.md protocol is the operative plan. Repeated here for S33 continuity:

1. **13:00 UTC pre-market**: re-run EDGAR CIK 1438533 sweep, WebSearch overnight news
2. **13:30 UTC market open**: Monday gap direction is first diagnostic
   - Gap up or flat: D-036 inverse confirmed (absence of bearish pre-positioning → approval-biased). Hold 29.75.
   - Gap down ≥5% with no news: inverse D-036 warning (bearish pre-positioning). Immediate downgrade 29.75 → 24-25, demote to watchlist.
   - Gap down with news: execute Section 7 based on news content
3. **Every 30 min**: poll https://www.fda.gov/news-events/fda-newsroom/press-announcements (NEW URL — S32 correction)
4. **On decision**: execute Section 7 of `candidates/TVTX_FSGS_PDUFA.md`
   - **Approval** → score finalized, candidate moves to Delivered, P&L logged
   - **CRL** → immediate archive, loss attribution, document as second CRL-miss case. **Note**: TVTX Apr 8 was NOT a D-036 bearish pre-position signal (closed up), distinguishing this from REPL.
   - **AdCom delay** → reassess timeline, hold lower score
   - **3-month extension** → hold position, update PDUFA date, score likely unchanged

## Framework note: D-036 pre-PDUFA price-action rule

This session adds one more datapoint to D-036 observations:

**S32 TVTX baseline (T-1)**:
- 10d: REPL -34.1% (confirmed CRL)
- 10d: TVTX +6.4% (pending decision)
- 10d divergence: 40.5pp

If TVTX approves Monday → D-036 gets another inverse confirmation (absence of bearish pre-position → positive outcome).
If TVTX CRLs Monday → D-036 remains in 1-confirming + 1-counter-example status (TVTX didn't have the pre-crash, so inverse rule wasn't triggered, so outcome is consistent with rule: no bearish signal → unpredictable but no inverse confirmation either).

D-036 observation log: `working/d036_evidence_log.md` to be created S33 with TVTX outcome as 2nd datapoint.

## Sources

- EDGAR Travere filings: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001438533
- StockTitan TVTX feed: https://www.stocktitan.net/news/TVTX/
- FDA press announcements (CORRECTED URL): https://www.fda.gov/news-events/fda-newsroom/press-announcements
- Travere IR press releases: https://ir.travere.com/press-releases/
- Original Jan 13 extension: https://ir.travere.com/press-releases/news-details/2026/Travere-Therapeutics-Announces-FDA-Extends-Review-of-sNDA-for-FILSPARI-sparsentan-in-FSGS/default.aspx
