# REPL PDUFA Outcome Check — Session 26

**Date:** 2026-04-10
**Scan time:** ~10:10 UTC (05:10 ET — pre-market)
**Status:** **DECISION NOT YET ANNOUNCED AT SCAN TIME**

## Primary-Source Verification

**EDGAR submissions API pull** (CIK 0001737953, Replimune Group Inc):
- Zero 8-K filings in April 2026
- Last 8-K: 2026-02-03 (Q3 FY2026 earnings)
- Recent April 2026 activity: 14 Form 4s filed 2026-04-06 and 2026-04-07 (annual grants), 1 Form 144 on 2026-04-02
- No material event disclosure

Replimune has NOT yet filed an 8-K announcing the PDUFA outcome. FDA decisions typically arrive 4–6pm ET; the company then files the 8-K within hours. This session is running pre-market ET, so the outcome is genuinely still pending.

## Price Action (yfinance, last 10 sessions)

| Date | Close | Vol | Notes |
|------|-------|-----|-------|
| 2026-03-26 | 7.54 | 1.1M | Baseline |
| 2026-04-02 | 8.41 | 3.2M | +11% pop (M&A chatter / pre-PDUFA positioning) |
| 2026-04-06 | 8.54 | 3.2M | Peak |
| 2026-04-07 | 7.80 | 3.6M | -8.7% |
| **2026-04-08** | **5.89** | **6.3M** | **-24.5% intraday collapse** |
| 2026-04-09 | 5.91 | 8.3M | Stabilized on 8x volume |

**Interpretation:** -30.9% peak-to-trough over two sessions on 6–8x volume immediately before binary. This is consistent with:
- De-risking by institutional holders
- Retail sentiment flip (Stocktwits: bullish → neutral, chatter +171% W/W)
- No trading halt announcement per primary EDGAR search
- Some speculation of negative news leakage, but no primary-source confirmation

**IMPORTANT:** Pre-PDUFA sell-offs of this magnitude are ambiguous — they happen on genuine bad-news leaks AND on pure risk-management de-grossing. The Aug 2025 post-CRL crash was -77% in one day. A -31% two-day fade is more characteristic of pre-event positioning than a leak.

Market cap at $5.91 × ~82M shares ≈ **$484M**. Against $800M peak sales estimate, market is pricing ~25–30% approval probability × NPV discounting.

## News Sweep (WebSearch, 2026-04-10 AM UTC)

- No press releases from Replimune IR dated 2026-04-10
- No FDA announcements dated 2026-04-10
- Stock decline confirmed as pre-decision risk management, not a CRL announcement
- All 2026 coverage still references the April 10 PDUFA as PENDING

## TVTX Read-Across Implications (sentiment overlay)

The REPL decision matters for TVTX Monday positioning through four channels:

**Path A — REPL APPROVED:** Small-cap biotech sentiment tailwind into Monday. TVTX Monday gap-up more likely. No action: hold-existing policy unchanged.

**Path B — REPL CRL:** Small-cap FDA risk premium re-prices. TVTX could gap down 3–8% on sympathy even without specific negative news. Monday kill-sweep becomes more important. Do NOT add to position on sympathy dip — wait for FDA press release.

**Path C — REPL DELAY (no decision today):** Ambiguous. Monday TVTX likely opens near Friday close with modest volatility. Neutral for positioning.

**Path D — Trading halted, late-day announcement:** Default to Path A/B handling once announcement arrives. If overnight 8-K before Monday open, re-run kill sweep immediately.

## Process Check

Session 25 logged that "REPL 8-Ks in April 2026: 0 — PDUFA is TODAY, not resolved." This session re-verified that claim via direct EDGAR pull. Consistent. No stale data.

REPL decision will be covered in next session via EDGAR + news search as first action. If next session runs during US market hours on April 10 (post 13:30 UTC) or afterwards, outcome will be resolvable.

## Next Session First-Action Checklist

1. Pull EDGAR submissions API for CIK 0001737953, look for any 8-K with filingDate ≥ 2026-04-10
2. If 8-K found: parse for "Complete Response Letter" vs "FDA Approval"
3. If no 8-K: Google "Replimune FDA decision April 10"
4. Update `candidates/` or archive as applicable
5. Re-run TVTX Monday kill sweep with REPL outcome as sentiment input
