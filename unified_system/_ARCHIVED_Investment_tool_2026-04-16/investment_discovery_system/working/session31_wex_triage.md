# WEX 3-Hit EDGAR Convergence Triage (S31)

**Date:** 2026-04-12 S31
**Status:** PENDING VERIFICATION — DEFERRED TO S32

## Signal

EDGAR scanner (edgar_20260412_020017.json) flagged WEX Inc. with a **3-hit single-name convergence** across all activist keywords scanned:
- "strategic alternatives"
- "board representation"
- "maximize shareholder value"

All 3 signals at strength=2. Single-name 3-hit convergence on activist keywords is unusual (most signals are isolated keyword hits).

## Price Action — DISCONFIRMING

yfinance 10d through Apr 10 2026:
- Range: $147–$159 (flat)
- Apr 10 close: $159.29
- 10d change: ~0% (sideways)
- Market cap: $5.47B
- Float: 31.6M
- Short interest: 7.9%

**Genuine activist situations almost always carry price confirmation within 10 trading days of filing** — accumulating positioning, dealer hedging, or leak-driven drift. A flat $147–$159 range strongly suggests these keyword hits are **boilerplate language** (D-029 false positive pattern), not real activist activity.

## Most Likely Explanation

WEX is a payments/fintech company. "Strategic alternatives," "board representation," and "maximize shareholder value" are routine phrases in:
- Proxy statements (DEF 14A) — director nomination procedures
- Annual reports (10-K) — risk factor boilerplate
- Bylaw amendments
- Routine governance disclosures

The fact that all three keywords hit the same company on the same scan day suggests this is a single proxy/10-K filing whose language triggered all three keyword matches — a structural FP, not 3 independent activist signals.

## Verification Required (S32 if tripwire triggers)

Before treating as candidate:
1. Pull the actual EDGAR filings that produced the 3 hits — check form type (13D/A = real, DEF 14A = boilerplate)
2. Check filer identity — known activist fund (Elliott, Starboard, ValueAct, JANA, Trian, etc.) = real, company itself as filer = boilerplate
3. Check if there are any 13D/A or 13D filings for WEX in the last 30 days
4. If all 3 hits are company-filed proxy language and no activist 13D exists, this is a **confirmed D-029 FP** and should be used as a calibration case

## Tripwire for S32 Priority

**Only escalate WEX verification to priority status in S32 if:**
- Monday Apr 13 WEX opens >$170 (+7% gap) = price-action confirmation
- OR if news search surfaces a named activist
- OR if EDGAR shows a fresh 13D/13D-A filing

**Otherwise, WEX stays as routine D-029 candidate FP, document in activist-keyword calibration log and move on.**

## Not Scored

WEX is not being written to candidates/ — failure of price-action confirmation blocks advancement per D-036 companion logic (price action as confirming signal). The 3-hit convergence alone is not sufficient without the market corroborating.

## Sources

- signals/edgar_20260412_020017.json (3 raw hits)
- yfinance WEX 10d price data Apr 1–10 2026
