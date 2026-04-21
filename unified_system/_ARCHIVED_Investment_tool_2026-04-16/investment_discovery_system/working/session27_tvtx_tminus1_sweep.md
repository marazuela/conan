# TVTX T-1 Kill Condition Sweep — Session 27

**Date**: 2026-04-10 (Friday)
**PDUFA**: Monday, April 13, 2026 — **T-1 biz day**
**Ticker**: TVTX (Travere Therapeutics), CIK 1438533
**Drug**: Sparsentan (Filspari), supplemental NDA for FSGS
**Position status**: Hold-existing, provisional score 29.75 at $31.44

---

## 5 Kill Condition Checks

### Kill 1: Negative 8-K on EDGAR
**Status**: ✅ **CLEAR**
Primary-source pull of CIK 1438533 submissions. Zero 8-Ks since Feb 19, 2026 (Q4 2025 earnings + Item 2.02/9.01). April 2026 filings are entirely routine:
- Apr 6: DEF 14A, DEFA14A, ARS — annual proxy materials
- Apr 1-7: Multiple Form 4s (all 10b5-1 plan sales, see Kill 5)
- Apr 1-6: Form 144 notices (matching Form 4 sales)
- Mar 27: SC 13G/A (passive holder update)

No material adverse disclosure.

### Kill 2: FDA press release / adverse regulatory action
**Status**: ✅ **CLEAR**
No Replimune/FDA cross-signal. No FDA safety communication on sparsentan or FSGS. Filspari (same molecule, approved for IgAN since Feb 2023) has no new boxed warning updates. FDA REMS (Risk Evaluation and Mitigation Strategy) for hepatotoxicity and embryo-fetal toxicity remains standard — already in label.

### Kill 3: Price break below key levels ($27 floor, -10% single-day)
**Status**: ✅ **CLEAR — STRONGLY BULLISH**

10-day price history (verified via yfinance):
| Date | Close | Volume | Day Chg | Note |
|------|-------|--------|---------|------|
| Mar 26 | $27.92 | 851K | +1.3% | |
| Mar 27 | $27.25 | 634K | -2.4% | |
| Mar 30 | $27.66 | 1.03M | +1.5% | |
| Mar 31 | $29.71 | 1.85M | **+7.4%** | Breakout on 2x volume |
| Apr 1 | $31.42 | 2.28M | **+5.8%** | Continuation |
| Apr 2 | $30.44 | 1.15M | -3.1% | |
| Apr 6 | $31.83 | 2.95M | +4.6% | Intraday high $33.78 |
| Apr 7 | $31.67 | 988K | -0.5% | |
| Apr 8 | $31.68 | 1.20M | +0.0% | |
| **Apr 9** | **$31.44** | **1.85M** | -0.8% | |

**10-day move: +12.6%** (Mar 26 → Apr 9). **No single-day drop >5%.** Floor well above $27. Volume elevated but not panic (largest day 2.95M = ~2.5x avg). This is institutional **accumulation**, not distribution.

Contrast with REPL which crashed -30% on 6-8x volume in same window → TVTX price action completely decoupled from REPL sentiment = market treats them as independent, which is correct (different drugs, different mechanisms, different review paths).

### Kill 4: Analyst sell-side downgrade to <$25 PT
**Status**: ✅ **CLEAR** (no fresh downgrades in web search)
Last known sell-side consensus PT well above current $31. No reports of material cuts.

### Kill 5: Unusual insider selling (opportunistic, not 10b5-1)
**Status**: ✅ **CLEAR — all sales verified 10b5-1 plan**

Four Form 4 sales decoded from XML `<aff10b5One>` flag:
| Date | Insider | Title | Code | Shares Sold | Avg Price | 10b5-1 Flag |
|------|---------|-------|------|-------------|-----------|-------------|
| Mar 16 | Reed Elizabeth | CLO/GC | M+S | 10,000 | $28.09 | ✅ 1 |
| Apr 1 | Dube Eric | **CEO** | M+S | 60,000 | $30.55-$31.46 | ✅ 1 |
| Apr 1 | Reed Elizabeth | CLO/GC | M+S | 10,000 | $30.00 | ✅ 1 |
| Apr 6 | Baynes Roy D. | Director/Officer | M+S | 10,000 | $33.00 | ✅ 1 |

All sales flagged `<aff10b5One>1</aff10b5One>` — indicates pre-scheduled 10b5-1 trading plan. 10b5-1 plans must be established at least 90 days before the first trade (per FINRA/SEC rules effective Feb 2023), meaning these plans were set up no later than ~early January 2026, well before any recent PDUFA positioning.

**Interpretation**: These are affirmative-defense pre-scheduled sales. They are NOT opportunistic dumps on MNPI. HOWEVER, it is worth noting that the **CEO sold 60,000 shares on T-8 biz day** (Apr 1, 8 business days before Apr 13 PDUFA). The timing is notable even if legally compliant. A CEO who expected a CRL Monday would likely not have structured a plan to dump just before the event — this is either (a) routine tax-optimization diversification or (b) neutral-to-mildly positive evidence (no MNPI concerns at plan-setup time in January).

**Verdict**: Not a kill. Informational only.

---

## Additional Data Points

### Sparsentan FSGS context
- Filspari already approved for IgA nephropathy (primary IgAN indication) since Feb 17, 2023.
- FSGS sNDA uses same molecule, same safety profile, same REMS, same sponsor, same FDA review division.
- Key evidence base: DUPLEX Phase 3 trial (sparsentan vs irbesartan in FSGS); PROTECT Phase 3 trial (sparsentan vs irbesartan in IgAN).
- DUPLEX missed primary eGFR slope endpoint at 108 weeks but showed clinical meaningful proteinuria reduction (primary surrogate endpoint FDA accepted for IgAN).
- **Historical read**: Filspari IgAN approval was initial accelerated approval that converted to full (traditional) approval Sept 2024. Establishes strong regulatory trust with FDA's Division of Cardiovascular and Renal Products.

### REPL read-across (pending resolution)
REPL and TVTX are not directly comparable — different drug class, different FDA division (REPL is oncology, TVTX is CVRP). If REPL gets CRL today, Monday's TVTX open may gap down 3-8% on sympathy, but this is **sentiment noise** and should NOT trigger any protocol action (still hold-existing).

### Convergence check
No convergence between TVTX and any other pipeline signal this session. TVTX is a standalone long-duration thesis.

---

## Final Assessment — T-1 Kill Sweep

**ALL 5 KILL CONDITIONS CLEAR.** Score **provisional 29.75 confirmed** at $31.44 close.

**Monday Protocol Staged**:
1. 09:00 UTC (pre-market ET check): pull CIK 1438533 submissions, scan for any overnight 8-K
2. 13:30 UTC (US market open): monitor TVTX gap direction. If gap-down >10% → investigate for leak; if gap-up >15% → early resolution (news wire)
3. Every 30 min through afternoon: check FDA press releases (fda.gov/news-events/fda-newsroom/press-announcements)
4. Outcome resolution:
   - **Approval** → update thesis, mark as "Approved" in candidates/, monitor for label specifics and launch readiness
   - **CRL** → instant archive, lessons-learned memo
   - **Deferral / extended review** → hold 48h, re-score

**Stop rules for live action**: None beyond hold-existing. Do not chase gap up or gap down on REPL sympathy moves.

---

## Sources

- [SEC EDGAR CIK 0001438533 submissions](https://data.sec.gov/submissions/CIK0001438533.json) — primary
- [Travere Therapeutics IR](https://ir.travere.com) — reference
- [FDA Filspari label](https://www.accessdata.fda.gov/drugsatfda_docs/label/2024/216403s000lbl.pdf) — reference for prior approval
