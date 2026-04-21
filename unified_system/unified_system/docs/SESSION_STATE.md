# SESSION_STATE — Unified Investment Discovery System

**Last updated**: 2026-04-17 by Pedro + Claude (full scan + pre-edge recalibration)
**Next session**: Build the takeover-candidate and pre-Phase-3 scanners (specs in `strategies/`).

---

## TOP HEADLINE

**2026-04-17**: Pre-edge mandate (D-013) enforced. 4 candidates archived as post-edge (TVTX resolved, AVNS/GSAT/SEM deals signed). Full scanner sweep produced 156 new signals; 1 crossed 35 (HKEX 02427 GUANZE MEDICAL tender) but was post-edge-disqualified. Active pipeline is 5 candidates (RPAY, AXSM, RGR, VERA, VRDN), all verified pre-edge today. Summary + 5 dossiers republished. Two new pre-edge scanner specs filed: `strategies/pre_edge_takeover_candidate.md` and `strategies/pre_edge_phase3_readout.md`.

---

## Current Phase

**Phase 0–1 complete** (scaffold + archive + US/non-US scanner migration in file terms).
**Phase 2–3 in progress** (unified pipeline components, reporting layer, scanner registry).
**Phase 4+ pending** (task registration, new scanner builds, litigation scanners).

---

## Active Candidates (migrated from legacy systems)

### US (from Tool 1, Session 68 state)

| Ticker | Profile | Score | Status | Next catalyst |
|--------|---------|-------|--------|---------------|
| RPAY | activist_governance | 30 | active | Forager $4.80 offer went public 2026-04-17 |
| AXSM | binary_catalyst | 30 | active | PDUFA 2026-04-30 (AXS-05 Alzheimer's agitation) |
| RGR | activist_governance | 30 | active | AGM 2026-05-27; Beretta $44.80 partial tender proposal |
| VERA | binary_catalyst | 30 | active | PDUFA 2026-07-07 (atacicept / IgAN) |
| VRDN | binary_catalyst | 30 | active | PDUFA 2026-06-30 (veligrotug / TED) + REVEAL-2 Q2 |
| AVNS | merger_arb | — | archived post-edge | AIP tender signed 2026-04-14 (+72% premium captured) |
| GSAT | merger_arb | — | archived post-edge | AMZN cash deal signed |
| SEM | merger_arb | — | archived post-edge | WCAS PREM14A filed 2026-04-15 |
| TVTX | binary_catalyst | — | delivered / archived | FSGS FILSPARI approved 2026-04-13 |

### Non-US (from Tool 2)

| Ticker | Exchange | Profile | Status | Notes |
|--------|----------|---------|--------|-------|
| ITRK | XLON | merger_arb | active | EQT Rule 2.4 possible offer, PUSU ~2026-05-14 |
| PTSB | XLON | merger_arb | active | BAWAG recommended cash offer |
| 1878, 2540, 2692, 2972, 3391, 4206, 6135, 6197, 8001, 8267, 8934 | XTKS | merger_arb | active | various tender offers |
| 4343, 6058, 7512, 9601 | XTKS | activist_governance | active | impairments / special losses |
| 6027 | XTKS | activist_governance | active | bengo4 impairment |
| 1882, 6367 | XTKS | litigation | active | litigation/regulatory |
| 7085 | XTKS | activist_governance | active | profit upgrade |
| PDI, WBC, CCL | XASX | various | active | ASX candidates |
| (10 watchlist JSONs in candidates/watchlist/) | XTKS | — | watch | lse_rns + tdnet watchlist |

---

## What's Done

- ✅ Folder structure created at `unified_system/`.
- ✅ All 3 legacy tool folders archived under `_ARCHIVED_*_2026-04-16`.
- ✅ Scanner scripts copied: edgar, esma, fda, congressional (US); lse_rns, tdnet, asx, sedar_plus (non-US); party_resolver, build_exhibit21_map (litigation).
- ✅ OpenFIGI resolvers from both T1 and T2 copied (pending merge into unified resolver).
- ✅ Legacy convergence engine preserved (pending redesign as multi-profile).
- ✅ 33 candidate markdowns + 1 delivered + 10 watchlist JSONs migrated.
- ✅ signal_log.json (166 entries) migrated.
- ✅ OpenFIGI cache (133 entries), JPX mcap cache, ASX universe preserved in `working/`.
- ✅ 5 scoring profiles written in `framework/`.
- ✅ candidate_template.md written.
- ✅ INSTRUCTIONS.md, OBJECTIVES.md, CONTEXT.md written.
- ✅ All 10 legacy scheduled tasks already disabled (verified).

## What's In Progress

- 🔄 Writing DECISIONS, OPEN_QUESTIONS, PROGRESS_LOG, INDEX.
- 🔄 scanner_registry.json.
- 🔄 Unified shared utilities (http_client, merged openfigi_resolver).
- 🔄 Unified pipeline components (pipeline_runner, run_post_scan, convergence_engine).
- 🔄 TDnet FIGI defect fix.
- 🔄 report_generator.py with reportlab.
- 🔄 Registering `unified-operational`, `unified-maintenance`, `unified-reporting` scheduled tasks.

## Next Priorities (S2)

1. Re-score all migrated candidates against the new 5 profile rubrics. Expected: no demotions from active — the plan specifies this should not happen with accurate profiles. If a demotion is triggered, investigate the rubric calibration before acting.
2. Build `working/ca_universe.json` to unblock SEDAR+ scanner.
3. Validate `party_resolver.py` against live EDGAR (never live-tested in Tool 3).
4. First operational + reporting cycles — verify PDFs produce correctly, no lock collisions.

---

## Active Warnings

- **W-001**: Scoring profile re-scoring is PENDING for all migrated candidates. Profile rubrics differ from the legacy 7-dimension system; scores are not directly comparable. Until re-scored, treat legacy scores as stale.
- **W-002**: SEDAR+ scanner is non-operational — blocked on `working/ca_universe.json`.
- **W-003**: TDnet FIGI-resolve defect on 5-char alphanumeric tickers. Fix pending — see OPEN_QUESTIONS Q-003.
- **W-004**: 7 planned scanners (HKEx, KIND, BSE/NSE, CVM, BMV, CourtListener, SEC enforcement) are stubs only. Full builds deferred to Phases 5–6.
- **W-005**: ClinicalTrials.gov returns 403 in this sandbox; FDA PDUFA pipeline uses EDGAR fallback.
- **W-006**: CNMV (Spain) short disclosure access is blocked — Pedro's home market, high priority to unblock (Q-002).
- **W-007**: Entity resolution correction from S68 (legacy): **SEM CIK = 0001320414** (NOT 0001320350, which is LENSAR/LNSR). This is a cautionary lesson — always re-verify CIK when a filing's ticker doesn't match the candidate's ticker.
