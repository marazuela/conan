# Daily Signal Report — 2026-04-16

**Generated**: 2026-04-16 16:15 UTC
**Pipeline version**: v1.1

## Executive Summary

- **Total signals detected**: 34
- **Active scanners**: 5/5
- **Convergent entities**: 0

## Scanner Results

| Scanner | Status | Signals | Time (s) | Notes |
|---------|--------|---------|----------|-------|
| congressional | OK | 12 | 44.9 |  |
| contract | OK | 0 | 1.1 |  |
| edgar | OK | 1 | 31.3 |  |
| esma_short | OK | 11 | 14.7 |  |
| fda_pdufa | OK | 10 | 17.7 |  |

## Signals by Strategy

### Congressional (12 signals)

- **UHAL** (Amerco) — congressional_unusual_size — strength 3 — 2026-03-13
- **B** (Berkshire Hathaway IncBRK/) — congressional_unusual_size+timing_cluster:2_members — strength 3 — 2026-03-13
- **PDMLP** (Dorchester Minerals) — congressional_unusual_size — strength 3 — 2026-03-13
- **LPEPD** (Enterprise Products Partners) — congressional_unusual_size — strength 3 — 2026-03-13
- **AXP** (American Express Co) — congressional_timing_cluster:2_members — strength 3 — 2026-04-07
- **CARR** (Carrier Global Corp) — congressional_timing_cluster:2_members — strength 3 — 2026-04-07
- **PAYX** (Paychex Inc) — congressional_timing_cluster:2_members — strength 3 — 2026-04-07
- **AWK** (American Water Works Company Inc) — congressional_trade — strength 2 (low) — 2026-04-07
- **BR** (Broadridge Financial Solutions Inc) — congressional_trade — strength 2 (low) — 2026-04-07
- **CASY** (Casey's General Stores Inc.) — congressional_trade — strength 2 (low) — 2026-04-07
- ... and 2 more

### Edgar (1 signals)

*Rotation category: strategic*

- **RGR** (STURM RUGER & CO INC  (RGR)) — activist_keyword — strength 2 (low) — 2026-04-15

### Esma Short (11 signals, 6 high-strength)

- **BME.L** (B&M European Value Retail PLC) — short_crowded_short+new_position — strength 4 **HIGH** — 2026-04-15
- **MTLN** (Metlen Energy & Metals Plc) — short_crowded_short+new_position — strength 4 **HIGH** — 2026-04-15
- **VTY.L** (Vistry Group PLC) — short_crowded_short+new_position — strength 4 **HIGH** — 2026-04-15
- **TEP.PA** (TELEPERFORMANCE) — short_large_position+crowded_short — strength 4 **HIGH** — 2026-04-14
- **EXA.PA** (EXAIL TECHNOLOGIES) — short_crowded_short+new_position — strength 4 **HIGH** — 2026-04-14
- **BFIT.AS** (Basic-Fit N.V.) — short_crowded_short+new_position — strength 4 **HIGH** — 2026-04-15
- **AVON.L** (AVON PROTECTION PLC) — short_new_position — strength 3 — 2026-04-15
- **BCG.L** (BALTIC CLASSIFIEDS GROUP PLC) — short_position_increase+crowded_short — strength 3 — 2026-04-15
- **ITB.L** (IMPERIAL BRANDS PLC) — short_new_position — strength 3 — 2026-04-15
- **JSG.L** (Johnson Service Group Plc) — short_new_position — strength 3 — 2026-04-15
- ... and 1 more

### Fda Pdufa (10 signals)

- **AXSM** (Axsome Therapeutics) — pdufa_approaching — strength 2 (low) — 2026-04-30
- **MNKD** (MannKind Corporation) — pdufa_watchlist — strength 2 (low) — 2026-05-29
- **ARVN** (Arvinas) — pdufa_watchlist — strength 2 (low) — 2026-06-05
- **PFE** (Pfizer Inc.) — pdufa_watchlist — strength 2 (low) — 2026-06-29
- **LNTH** (Lantheus Holdings) — pdufa_watchlist — strength 2 (low) — 2026-06-29
- **ARQT** (Arcutis Biotherapeutics) — pdufa_watchlist — strength 2 (low) — 2026-06-29
- **IONS** (Ionis Pharmaceuticals) — pdufa_watchlist — strength 2 (low) — 2026-06-30
- **AZN** (AstraZeneca) — pdufa_watchlist — strength 2 (low) — 2026-06-30
- **VRDN** (Viridian Therapeutics) — pdufa_watchlist — strength 2 (low) — 2026-06-30
- **VERA** (Vera Therapeutics) — pdufa_watchlist — strength 2 (low) — 2026-07-07

## Convergence Alerts

*No convergent entities detected.*

## Strategy Health Check

All active scanners completed successfully.

## Active Candidates

- **Candidate: RPAY — Forager Fund vs Repay Holdings Poison Pill** — **Score**: ≈33.0 / 42.5 (S64 ↑ from 30 after Veradace 13D verification) — `RPAY_Forager_ActivistPoisonPill.md`
- **Candidate: SEM — Select Medical Holdings WCAS+Ortenzio Take-Private ($16.50 cash)** — **Score**: 22.5 / 42.5 — **WATCHLIST** (below 28 active threshold) — `SEM_Select_Medical_WCAS_TakePrivate.md`
- **Candidate: VRDN — Veligrotug PDUFA (Thyroid Eye Disease)** — ⚠️ **DEMOTED TO WATCHLIST — 2026-04-10 Session 26** — Score revised from 31.5 to **26.0** after REVEAL-1 elegrobart SC Phase 3 topline data (Mar 30) and subsequent -26% crash (Apr 6). Cumulative -43.8% from Mar 27. REVEAL-1 showed placebo-adj responder rates below expectations, weakening the SC franchise extension thesis. Veligrotug IV PDUFA June 30 is intact but franchise value is materially lower. See `working/session26_vrdn_reveal_impact.md` and DECISIONS.md D-034. The sections below are preserved for historical reference but scoring is stale. — `VRDN_veligrotug_PDUFA.md`

## PDUFA Watchlist

| Ticker | Drug | PDUFA Date | Status |
|--------|------|-----------|--------|
## Next Steps

- [ ] Score all new signals using 7-dimension rubric
- [ ] Full candidate writeup for any signal scoring 30+
- [ ] Check existing candidates against kill conditions
- [ ] Monitor active candidates for developments

---
*Report generated by run_post_scan.py v1.1 at 2026-04-16 16:15:09 UTC*