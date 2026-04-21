# Session 29 — Signal Triage

**Session**: 2026-04-10 12:21 UTC
**Raw signals**: 24 across 5 scanners
**Convergences**: 0
**New candidates**: 0
**New research queue additions**: 1 (DHER.DE — watchlist only)

## Scanner Summary

| Scanner | Signals | Outcome |
|---------|---------|---------|
| EDGAR (activist category) | 10 | All strength-2 keyword FPs (Corgi ETF, HON, Dorian, Lumida, MHO×2, TMDX, PSFE, HBNB, HON — all activist-keyword only, low conviction). Noted: TMDX is a legit name but the activist keyword-only match is insufficient for elevation. No new candidate. |
| Congressional | 0 (timeout, S28 data at 1h20m old used for convergence) | Yfinance enrichment budget exceeded. Known pattern — see warnings. |
| ESMA Short | 1 | **DHER.DE (Delivery Hero SE) — HIGH strength-4** |
| Contract | 0 | Baseline |
| FDA PDUFA | 13 | All known tickers — TVTX, MNKD, PFE, ARQT, LNTH, IONS, AZN, VRDN, ORCA, VERA, + 3 more. No new names. |

## Signal Deep Dive: DHER.DE — Delivery Hero SE

### Raw data
- **ISIN**: DE000A2E4K43
- **Market cap**: €5.21B (well above $215M floor)
- **Signal**: crowded_short + new_position
- **Strength**: 4 (HIGH)
- **New position**: Capital Fund Management SA 0.5% dated 2026-04-09
- **Crowded stack** (9 holders total):
  1. D. E. Shaw 1.69% (2026-02-27)
  2. Two Sigma 1.41% (2026-03-31)
  3. AQR Capital 1.21% (2026-04-08) ← recent
  4. Caisse de dépôt et placement du Québec 0.93% (2025-12-19)
  5. PDT Partners 0.80% (2026-04-08) ← recent
  6. WorldQuant 0.64% (2026-03-31)
  7. BlackRock Financial Mgmt 0.62% (2026-04-01) ← recent
  8. AHL Partners 0.60% (2025-12-04)
  9. Capital Fund Management 0.50% (2026-04-09) ← NEW

**Total disclosed short interest**: ~8.40% of shares out
**Concentration**: Primarily systematic/quant funds (7 of 9 are quant names). This is a heavy quant pile-on, not a fundamental-driven short.

### D-035 Basket Check
- DHER.DE is a single-name signal, not part of a sector/country same-day basket. No other German food-delivery or consumer-discretionary crowded shorts detected today.
- **D-035 does NOT apply**. Signal can be evaluated on its own merits.

### Fundamental Context (web layer)
- **FY25 earnings** reported 2026-03-26: Taiwan Foodpanda sale to Grab for $600M announced, near-term cash and margin relief → stock rallied on the day
- **FY26 guidance**: 8-10% LFL GMV growth, 14-16% revenue growth, adj EBITDA €910-960M
- **Guidance miss**: consensus was €992.7M → **guided BELOW consensus** (-3% to -8% miss at midpoint)
- **Analyst sentiment**: 10 buy / 7 hold / 1 sell (net positive)
- **Profit margin**: -5.57% (still loss-making)
- **Forward PE**: 23.7

### Price Context — 15 Sessions

| Date | Close | Volume | Note |
|------|-------|--------|------|
| 2026-03-19 | 15.28 | 1.52M | Pre-earnings base |
| 2026-03-20 | 15.29 | 1.79M | |
| 2026-03-23 | 16.50 | 2.04M | |
| 2026-03-24 | 15.65 | 1.69M | |
| 2026-03-25 | 15.84 | 1.66M | |
| 2026-03-26 | 15.73 | 2.50M | **Earnings day + Taiwan sale** |
| 2026-03-27 | 16.62 | 3.44M | Post-earnings rally |
| 2026-03-30 | 16.32 | 2.00M | |
| 2026-03-31 | 15.45 | 1.78M | Digestion |
| 2026-04-01 | 16.70 | 2.12M | |
| 2026-04-02 | 16.57 | 1.23M | |
| 2026-04-07 | 15.69 | 1.57M | Pullback |
| 2026-04-08 | 16.75 | 1.98M | |
| 2026-04-09 | 16.66 | 1.38M | |
| 2026-04-10 | **17.36** | 447k | **+4.2% intraday (European session)** |

**52w range**: €14.80-€29.89 (currently 42% off high, 17% above low)
**Trend**: UP over 3 weeks (+13.6% from Mar 19 low). Shorts adding into strength.

### Thesis Analysis

**Bear case (what the shorts see)**:
1. FY26 EBITDA guidance miss vs consensus (~5% below)
2. Still unprofitable at net margin level
3. Crowded quant pile-on suggests systematic factor signal (likely growth-at-scale + momentum + quality factor)
4. 42% below 52w high signals broken franchise narrative
5. Taiwan divestiture reduces revenue base

**Bull case (what the price is telling)**:
1. Taiwan sale for $600M is margin-accretive and delevers
2. Price rallying INTO the shorts over 3 weeks
3. Analyst sentiment net positive (10B/7H/1S)
4. Quant shorts are systematic, not fundamentally informed
5. Guidance miss was modest and already digested (stock rallied through the print)

### Score Estimate (provisional, band)

Using 7-dimension rubric:
1. **Information asymmetry** (0-6): ~3.0 (quant crowd vs fundamental; moderate asym)
2. **Catalyst conviction** (0-6): ~2.5 (no binary catalyst — Q1 earnings TBD, probably May)
3. **Timing specificity** (0-6): ~2.0 (no hard date)
4. **Risk/reward asymmetry** (0-6): ~3.5 (€14.80 floor near, €29.89 ceiling well above)
5. **Signal quality** (0-6): ~4.0 (9-holder stack is material but quant-weighted)
6. **Crowding / contrarian edge** (0-6): ~3.5 (crowded = risk but also provides squeeze asymmetry)
7. **Disconfirmation robustness** (0-6): ~3.0 (price conflicts with thesis — a warning)

**Preliminary band**: 21.5-23.5 — **BELOW watchlist floor (22-24 minimum)** or **barely at watchlist floor**.

### Verdict

**WATCHLIST — LIGHT MONITORING** at provisional score ~22-23.

**Critical caveat**: This signal has the HROW/VIRI.PA/TEP.PA/ZAL.DE pattern — crowded short + rising price + fundamental conflict. Historically our watchlist has treated these as **do-not-elevate without price break + fundamental disconfirmation**.

**Trigger to elevate**: Break below €15.00 + Q1 earnings miss (likely early-mid May) + negative analyst revision cluster → elevate to formal scoring.
**Trigger to archive**: Break above €19.00 on heavy volume (short squeeze) → archive.

### Cross-Reference

DHER.DE joins the growing "quant-heavy crowded short into rallying price" bucket:
- ZAL.DE (29.5 watchlist — also German consumer, Zalando, 6.44% SI)
- HROW (22.5 watchlist — US pharma, Walleye + 21.1% SI)
- TEP.PA (22-24 watchlist — French engineering + AI disruption)
- VIRI.PA (23-24 watchlist — French consumer, basket short)
- **DHER.DE (NEW S29 — ~22-23 watchlist — German food delivery)**

**Pattern observation**: Five names now in this cluster. If 2+ break on the same day (e.g., Zalando and Delivery Hero both <key level), consider sector-rotation framing for the German consumer complex — not a convergence signal per se but a meaningful meta-pattern.

## EDGAR False Positives (strength-2)

All 10 EDGAR activist-keyword signals are low-conviction. Quick triage:
- **TMDX, HON ×2, MHO ×2, PSFE, HBNB**: known large caps (TMDX $2-3B medtech), activist keyword only — no elevation
- **Corgi ETF Trust III, Dorian Inc., Lumida Inc.**: ETF/private/sub-scale — no tickers resolved — drop
- **HON** (Honeywell): mega-cap FP — drop

No elevations. D-029 rubric down-weights keyword-only properly.

## FDA PDUFA Watchlist Maintenance

All 13 FDA signals are known. Only TVTX is strength-3 (pdufa_imminent T-3). VERA correctly flagged for T-60 research queue (prelim 29-33.5 band).

## Action Items from This Triage

1. Add DHER.DE to watchlist at 22-23 band.
2. Monitor DHER.DE Q1 earnings date (likely early-mid May 2026) for elevation trigger.
3. Watch for 2+ member moves in the German consumer short cluster (ZAL.DE + DHER.DE simultaneous break = meta-signal).
4. No new candidate writeups. No convergences.
