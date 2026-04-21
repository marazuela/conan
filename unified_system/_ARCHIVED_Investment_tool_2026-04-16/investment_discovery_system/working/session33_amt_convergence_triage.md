# S33 AMT Convergence Triage

**Date**: 2026-04-12 (S33)
**Result**: HARD FALSE POSITIVE — archive

## The Signal

Post-scan aggregation raised a convergence alert on **AMT / American Tower Corp** — score 26.0, 2 strategies (congressional + edgar), 5 signals rolling across the 14-day window.

Market cap: **$84.2B** (mega-cap, REIT).

## Component Breakdown

### Signal A — Congressional
- **Representative**: Ro Khanna (D-CA, House)
- **Transaction**: Sell
- **Range**: $1K–$15K (midpoint $8K)
- **Owner**: **Child** (not the member)
- **Date**: 2026-03-30
- **Committee alignment**: null
- **Cluster count**: 1 (solo)
- **Signal flags**: none

### Signal B — EDGAR activist_keyword
- **Keyword match**: "strategic alternatives"
- **Filing type**: **ARS** (Annual Report to Shareholders)
- **CIK**: 0001053507 (American Tower Corp)
- **Accession**: 0001193125-26-146429
- **Date**: 2026-04-08
- **Index verified**: Status 200, filing labelled "Annual Report" / "ARS" in the EDGAR index

## Why This Is a False Positive

Two independent reasons, either of which alone is sufficient to archive:

### 1. D-029-A — Activist keyword in routine proxy/annual boilerplate

AMT is an $84B S&P 500 REIT. Its Annual Report to Shareholders is a standard glossy-brochure filing that invariably contains forward-looking language about "evaluating strategic alternatives," "pursuing strategic opportunities," "strategic investments in towers and infrastructure," etc. This is boilerplate. D-029-A explicitly classifies activist-keyword matches inside ARS/DEF 14A/10-K risk-factor sections at mega-caps (>$10B) as automatic archives absent corroboration. No corroboration exists here — see point 2.

### 2. Ro Khanna's child made a trivial $8K sale

The congressional signal has zero predictive content:
- Trade size: $1K–$15K range (midpoint $8K). Mega-cap congressional trades at this level are noise; a $8K trade against AMT's $84B market cap is effectively rounding-error.
- Owner was **Child**, not the member herself — signal value collapses further (family members trade for reasons unrelated to the member's committee access).
- No committee alignment flag (Khanna sits on House Oversight / Armed Services, not a committee with AMT-specific information advantage).
- Solo trade (cluster count 1) — no co-trading cluster indicating shared information.
- Direction is a **sell** ($8K), which in a convergence that the engine labeled "bullish" is itself inconsistent. The only "bullish" component is the EDGAR keyword (falsely tagged) — the congressional half is actually a mild bearish noise trade. Convergence direction classification is misleading.

The congressional trade cannot rescue the edgar false positive; it is itself noise.

## Strategy Scoring If We Tried

Even if we took the signal seriously and scored the 7-dim rubric:
- Signal Strength: 1 (×2 = 2) — both components are weak/boilerplate
- Catalyst Clarity: 0 — no catalyst
- Info Asymmetry: 0 (×1.5 = 0) — public proxy text, tiny child trade
- Risk/Reward: 1 — mega-cap, no mispricing thesis
- Edge Decay: 0 — no edge to decay
- Liquidity: 5 — AMT is highly liquid (not relevant if no thesis)
- Catalyst Timeline: 0 — no catalyst

**Max score**: ~8. Far below the 14 threshold for even archive-logging, let alone the 28 candidate threshold. Convergence bonus (+4) would lift it to 12 — still discarded.

The engine's 26.0 score comes from the bonus-heavy scoring path that inflates score on mere *presence* of 2+ strategies. This is why human triage is the final filter.

## Action

**Archive.** Not a candidate, not even a watchlist item.

This triage adds to the running log of D-029-A confirmations. The pattern remains: mega-cap + ARS/DEF 14A/10-K + activist keyword = boilerplate.

## Framework Note

The AMT case illustrates a gap in convergence direction classification: the engine called the AMT convergence "bullish" because it aggregated `direction_summary: B:1 R:0 N:4` — but the lone bullish signal was the false-positive edgar keyword, and the other 4 were all neutrals or a *sell* side congressional trade. Future refinement: when the "bullish" side of a convergence is entirely composed of D-029-A-pattern matches, the convergence should be flagged for mandatory human review rather than auto-categorized bullish.

No DECISIONS.md change required yet — this is a first observation of the specific pattern. Logged here for accumulation.

## Sources

- Convergence JSON: `signals/convergence_20260412_031417.json`
- EDGAR filing index (verified): https://www.sec.gov/Archives/edgar/data/1053507/000119312526146429/0001193125-26-146429-index.htm
- Capitol Trades: https://www.capitoltrades.com/trades
