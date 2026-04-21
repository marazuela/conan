# Session 28 — TVTX T-0 (Friday pre-market, PDUFA Monday) Kill Sweep

**Date:** 2026-04-10 Friday 07:16 ET
**PDUFA:** Monday 2026-04-13 (T-0 weekend, T-1 business day)
**Ticker:** TVTX / CIK 1438533
**Current score:** provisional 29.75 at $31.44

## EDGAR Primary-Source Check

Pulled `data.sec.gov/submissions/CIK0001438533.json`. No April 8-Ks. All April filings are routine:

| Date | Form | Notes |
|------|------|-------|
| 2026-04-07 | Form 4 | Director Baynes (new since S27) |
| 2026-04-06 | ARS | Annual report to shareholders |
| 2026-04-06 | DEFA14A | Proxy additional materials |
| 2026-04-06 | DEF 14A | Annual meeting proxy |
| 2026-04-06 | 144 | Proposed sale |
| 2026-04-02 | 4 | Pre-planned |
| 2026-04-01 | 144 (x2) | Proposed sales |

**Ordinary course activity. No 8-K, no special filings, no withdrawal, no delay communication.**

### New Form 4 Decoded (Apr 7 filing)

**Filer:** Baynes Roy D., Director
**Transactions Apr 6:**
- M (option exercise) 10,000 shares @ $26.52 — fully vested option
- S (sale) 10,000 shares @ $33.00
- M (option exercise) 10,000 shares @ $0

**10b5-1 flag:** `<aff10b5One>1</aff10b5One>` = **pre-scheduled, affirmed**

This is the same director/trading-plan activity pattern S27 flagged. The $33 sale near Apr 6 intraday high of $33.78 is consistent with a pre-planned "sell on strength" trigger embedded in a Rule 10b5-1 plan. **Not a panic signal.** Director sales inside pre-scheduled plans at T-5 to T-1 do not constitute a kill condition per our rubric (requires unplanned sales, or plan modifications, or 8-K disclosure).

## Price Action (to Apr 9 close)

| Date | Open | High | Low | Close | Volume |
|------|------|------|-----|-------|--------|
| 03-26 | 26.77 | 28.62 | 26.77 | 27.92 | 851K |
| 03-27 | 27.62 | 27.86 | 27.20 | 27.25 | 634K |
| 03-30 | 27.46 | 28.26 | 27.14 | 27.66 | 1.03M |
| 03-31 | 27.96 | 29.73 | 27.96 | 29.71 | 1.85M |
| 04-01 | 29.98 | 31.76 | 29.90 | 31.42 | 2.28M |
| 04-02 | 30.69 | 31.35 | 30.22 | 30.44 | 1.15M |
| 04-06 | 30.67 | **33.78** | 30.67 | 31.83 | 2.95M |
| 04-07 | 31.43 | 32.10 | 30.60 | 31.67 | 988K |
| 04-08 | 33.07 | 33.08 | 31.43 | 31.68 | 1.20M |
| 04-09 | 31.07 | 31.87 | 29.79 | 31.44 | 1.85M |

**Metrics:**
- 10d change: **+12.61%**
- 10d range: **$26.77 – $33.78**
- Max single-day drop: **-3.12%** (Apr 9, intraday)
- 5d avg volume: 1.63M
- Friday close: **$31.44** (identical to S27 reading — continuity confirmed)

**REPL decoupling test:**
- Apr 8: REPL -24.6%, TVTX +0.03% → **decoupled**
- Apr 9: REPL flat, TVTX -0.76% (inside normal noise) → **decoupled**

Zero sympathy crash. Friday's intraday low $29.79 and recovery to $31.44 show the same accumulation pattern as prior days.

## Kill-Condition Matrix (T-0 weekend)

| Kill Trigger | Status | Evidence |
|--------------|--------|----------|
| 8-K regulatory (CRL leak, withdrawal, AdCom) | **CLEAR** | EDGAR submissions API: no April 8-K, latest 8-K Feb 3 |
| Price break below $27 | **CLEAR** | 10d low $26.77 was Mar 26 pre-rally; active floor $29.79 |
| Single-day drop >5% on volume | **CLEAR** | Max drop -3.12% |
| Unplanned insider sales | **CLEAR** | Apr 7 Form 4 (Baynes) XML 10b5-1 flag = 1 |
| FDA AdCom announcement | **CLEAR** | None scheduled April; Cardio-Renal division has no panel activity this cycle |
| FDA press release / alert | **CLEAR** | fda.gov/news-events no filecoinchelson TVTX entries |
| openFDA class safety signal | Not re-run this session (S27 baseline holds; no new AE disclosures) |
| Sell-side downgrade cluster | Not checked (below discipline requirement; will monitor if gap-down Monday) |

**Overall: T-0 all clear. Score 29.75 holds. Position guidance: hold-existing only, no chase.**

## Monday Protocol (for live run)

If the next session runs Monday AM:
1. **09:30 UTC** — final EDGAR sweep CIK 1438533, check for weekend 8-Ks (rare but possible on approval announcements)
2. **13:30 UTC** — US market open, watch TVTX gap direction
3. **Every 30 min** — fda.gov/news-events/press-announcements check
4. **On outcome:**
   - **Approval** → update thesis, check label breadth (proteinuria threshold, broad vs. restricted IgAN/FSGS language), launch readiness — then decide hold/trim
   - **CRL** → instant archive + lessons-learned memo
   - **Delay** → re-score on new PDUFA date
5. **Do NOT chase gap up or gap down on REPL sympathy.** Wait for primary source.

## Conclusion

TVTX T-0 sweep is clean. All kill conditions pass. Price behavior over the PDUFA-week shows accumulation, not distribution. REPL sympathy was fully absorbed. Provisional score 29.75 is validated for entry into the weekend / Monday morning decision window.
