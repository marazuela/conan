# VERA T-86 Deep-Dive — PARTIAL (Session 32, 2026-04-12)

**Status**: INCOMPLETE — research layer started, 7-dim scoring + candidate file writeup pending S33 due to session context budget prioritizing clean shutdown over continued analysis.

## Context

VERA T-86 deep-dive was pulled forward from May 8 per SESSION_STATE directive. Catalyst: Vera Therapeutics atacicept IgAN PDUFA ~July 7, 2026. S31 flagged VERA +10.04% on Apr 10 on 2.23× vol — suspected momentum from Wolfe Research upgrade + reported $200M institutional investment.

## Research findings (WebSearch layer — 2 queries executed)

### Query 1: Clinical & regulatory base
- **PDUFA target date**: July 7, 2026 (confirmed)
- **Indication**: IgA nephropathy (IgAN), first B-cell modulator targeting both BAFF and APRIL
- **Pivotal trial**: ORIGIN Phase 3, interim analysis MET primary endpoint at week 36
- **Efficacy**: 46% proteinuria reduction from baseline, 42% reduction vs placebo (p<0.0001)
- **Regulatory pathway**: BLA filed via Accelerated Approval Program
- **Designations**: Breakthrough Therapy Designation granted

### Query 2: Market narrative & catalysts
- **Wolfe Research upgrade**: March 11, 2026 — upgraded from "peer perform" to "outperform" with $88 price target
- **$200M institutional investment**: Reports of sizable $200M institutional backing Vera's clinical pipeline
- **April 10 event**: Inducement Grants under Nasdaq Listing Rule 5635(c)(4) — routine employee option grants (NON-MATERIAL, standard Nasdaq-required disclosure)
- **Apr 10 price action**: +7.1% momentum per secondary sources; S31 recorded +10.04% on 2.23× vol (combined with broader sector tape)

## Preliminary 7-dim score estimate (NOT FINAL — deferred to S33)

Based on research layer only (no EDGAR 13G/13G-A verification, no ORIGIN full protocol review, no competitive landscape analysis):

| Dimension | Weight | Provisional Score | Rationale |
|-----------|--------|-------------------|-----------|
| Signal Strength | 2.0 | ~4.0 | Phase 3 interim met + BTD + Accelerated Approval + analyst upgrade + institutional flows |
| Catalyst Clarity | 1.0 | ~4.5 | Hard PDUFA July 7 binary |
| Information Asymmetry | 1.5 | ~2.5-3.0 | Wolfe upgrade already reflected; $200M investment partially priced |
| Risk/Reward | 1.0 | ~3.5 | Unknown — needs market cap + implied move calc |
| Edge Decay | 1.0 | ~3.5 | 86 days out, more edge window than TVTX/AXSM |
| Liquidity | 1.0 | TBD | Need confirmed MCap + ADV |
| Catalyst Timeline | 1.0 | ~4.0 | Hard date |

**Rough estimate**: 27-30 range, consistent with SESSION_STATE expected band of 27-31.

## Open verification tasks for S33

1. **EDGAR CIK lookup**: Find Vera Therapeutics CIK (need ticker → CIK resolution)
2. **13G/13G-A filings**: Verify which institutional investor filed for the $200M position (likely >5% if material)
3. **Recent 8-K review**: Scan for any FDA communication, AdCom, or material adverse filings post-BLA
4. **Market cap & ADV**: Confirm for Liquidity dimension scoring
5. **Implied move calc**: Option chain IV for Risk/Reward dimension
6. **Competitive landscape**: Sibeprenlimab (Otsuka, approved 2025), iptacopan (Novartis, approved), sparsentan (TVTX FSGS different indication but IgAN approved 2023) — positioning vs existing SoC
7. **ORIGIN full protocol**: Duration of response, renal function changes, safety profile
8. **Cross-read with TVTX FSGS outcome**: Monday's decision shapes positioning sentiment for nephrology space broadly

## Decision tree for S33

- If all verification layers clean AND score ≥ 28 → write full `candidates/VERA_IGAN_PDUFA.md` writeup
- If score 22-27 → promote to watchlist with active monitoring
- If score < 22 OR kill signal found → archive with rationale

## Sources (partial — needs expansion S33)

- ORIGIN Phase 3 interim: (to be cited from primary press release)
- Wolfe Research March 11 upgrade: (to be cited)
- $200M institutional investment: (to be cited from primary source)
- Apr 10 Inducement Grants: Nasdaq Listing Rule 5635(c)(4) disclosure (routine)

## Framework note

The partial completion of VERA is by design — the project instructions hierarchy ((1) handoff quality, (2) output quality, (3) output volume) mandates clean shutdown when context budget is tight. A partial, well-documented handoff beats an incomplete candidate file that leaves S33 with unclear state.

S33 can resume VERA from this file using the 8 open verification tasks above as a direct worklist.
