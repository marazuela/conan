# Cross-Strategy Synergy Opportunities
How to get more out of the 5 scanners by combining their outputs intelligently.

---

## The convergence engine is working, but it's narrow

The current convergence engine (in `convergence_engine.py`) flags entities with ≥2 signals from different strategies within a 14-day rolling window. This is the right primitive but today's reality is **0 convergences for many consecutive sessions**. Not because the market is quiet — because the 5 strategies rarely touch the same ticker in 14 days.

Here's how to loosen the match while preserving quality.

---

## Tier A: Stackable pairs with clear directional logic

### A1 — EDGAR M&A + ESMA Shorts (most asymmetric)
**Setup**: Company announces deal (EDGAR strength-5 M&A) AND is on crowded-short list (ESMA strength ≥3).
**Interpretation**: Forced short cover = squeeze. Short thesis is broken.
**Action**: Immediate candidate writeup. Target: spread capture + potential topping-bid chatter.
**Currently**: convergence engine catches it if both signals fire within 14 days. Today's AVNS + GSAT had no ESMA hits (both are US-listed; ESMA covers UK/EU) — so this pair works only for EU names or for UK ADRs of EU companies.

### A2 — Congressional (Armed Services) + Contract (Defense) + EDGAR Earnings Surprise
**Setup**: Armed Services member buys defense prime; USAspending posts ≥$100M award to same company; 8-K earnings guidance raise.
**Interpretation**: Triple-confirm defense bull thesis.
**Action**: Candidate-grade if all three in 21 days.
**Currently**: not specifically coded as a triple-signal. Would require extending convergence engine beyond 2-strategy minimum.

### A3 — FDA PDUFA + ESMA/US short pressure
**Setup**: PDUFA within 30 days + crowded short (≥3 holders) OR US short interest >15%.
**Interpretation**: Binary catalyst priced pessimistically = maximum upside asymmetry on approval.
**Action**: Auto-boost PDUFA signal strength if short data coincides.
**Currently**: ESMA is EU-listed only. For US names (most PDUFA candidates), we'd need a US short-interest feed. **Gap**: FINRA's ShortSaleVolumeDaily (CSV, free) would give daily short-sale volume; combined with free-float it approximates short interest. Worth building.

### A4 — EDGAR Distress + FDA PDUFA (highest-binary-risk stack)
**Setup**: Company has PDUFA upcoming AND 8-K mentions going concern / substantial doubt.
**Interpretation**: Single-asset biotech + cash runway problem = approval-or-bust. High binary risk.
**Action**: Auto-flag as highest-priority deep dive.
**Currently**: convergence engine would catch if both within 14 days, but Q-010 filter intentionally suppresses S-1/S-4 distress boilerplate. Need to ensure the filter doesn't mask real 10-K/10-Q going-concern mentions for PDUFA candidates.

### A5 — Congressional (HELP) + FDA PDUFA
**Setup**: HELP committee member trades biotech before PDUFA.
**Interpretation**: Weak signal alone (HELP is legislative oversight, not FDA approval). But combined with another positive signal (Phase 3 readout, AdCom favorable) can tip score.
**Action**: Include as tiebreaker, not a driver.
**Currently**: committee alignment logic handles this; just needs to land in convergence_engine.py directionality.

---

## Tier B: Pattern-mining (don't auto-trigger candidates, but log)

### B1 — Soft convergence (15–28 day window)
**Setup**: 1 signal today + 1 signal 15–28 days ago on same ticker.
**Current**: convergence_engine only looks 14 days.
**Value**: Post-hoc pattern discovery — helps refine what signal combinations actually lead to candidates. Cheap to implement.

### B2 — Sector convergence
**Setup**: ≥3 tickers in the same sector show signals from 2+ strategies in 14 days.
**Interpretation**: Macro theme forming (e.g., defense stack in April 2026).
**Action**: Log as "sector cluster"; not a candidate itself, but context for individual names.

### B3 — Cross-regulator short anomaly
**Setup**: Same ISIN crowded in multiple ESMA regulators (e.g., FCA AND BaFin).
**Interpretation**: Pan-European conviction short; more likely to be fundamental vs. single-fund.
**Action**: Auto-boost strength by +1.
**Currently**: crowded detection groups by ISIN regardless of regulator, but doesn't specifically boost for multi-regulator presence.

---

## Tier C: Speculative extensions

### C1 — Options-flow scanner (not yet built)
Would cover the missing "implied volatility mispricing" leg from the FDA strategy spec. Requires paid data. Biggest single edge enhancement if budget allows.

### C2 — Social sentiment (not yet built)
Reddit/Twitter/StockTwits. Noisy but correlates with retail squeeze setups. A lightweight version (keyword-hit counts per ticker per day, no NLP) could be a soft-convergence input.

### C3 — Insider Form 4 scanner (not yet built)
SEC EDGAR has Form 4 filings (open/free). A CEO buying $5M of stock 30 days before PDUFA is *not* insider trading in the legal sense but is a strong conviction signal. Also: heavy selling by multiple insiders pre-catalyst is a kill-condition input. This is probably the highest-ROI next scanner to build.

### C4 — Patent-expiry scanner (biotech-adjacent)
USPTO has free patent-term-extension data. Applies to pharma IP cliffs — large-cap pharma losing exclusivity on blockbusters creates predictable revenue cliffs. Out-of-scope for current 5 strategies but could be a sixth.

---

## Implementation priorities for synergy

**P1** (before adding new scanners):
1. Make the convergence engine's directional classifier trustworthy — audit edge cases (bull vs. bear interpretation for specific signal pairs).
2. Add soft-convergence logging (B1) for post-hoc pattern discovery.

**P2** (1–2 weeks):
3. Build FINRA short-volume loader for US tickers → enable A3 for US-listed PDUFA candidates.
4. Add cross-regulator short anomaly boost (B3).

**P3** (1–3 months, in priority order):
5. Insider Form 4 scanner (C3) — highest expected value.
6. Options flow (C1) — requires paid data decision.
7. Social sentiment (C2) — lightweight first pass.

---

## Verification notes
- All pair setups (A1–A5) are INFERRED from the five strategy specs + SESSION_STATE candidate history. Empirical validation would require backtesting, which the current pipeline doesn't support.
- Tier B and C suggestions are SPECULATED based on financial-research literature + data-source availability.
- FINRA ShortSaleVolumeDaily availability VERIFIED from public-data reputation; the actual URL and format require a feasibility check before building.
