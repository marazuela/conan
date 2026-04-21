# Strategy: Pre-Edge Phase 3 Readout Scanner

## Purpose
Identify biotech / pharma companies with a **Phase 3 primary endpoint readout expected within 90 days** where positive readout would near-certainly trigger an NDA/BLA filing. Captures the edge window *before* NDA is filed — complementary to the existing `us_fda_pdufa.md` scanner which catches the post-NDA PDUFA window.

**Why we need this lane**: by the time a PDUFA date is set, the Phase 3 data is already public and most of the binary has priced. The edge is often richest in the 60–120 day window leading up to the Phase 3 primary endpoint printing — when positive data is not yet confirmed but the trial design, enrollment, and competitive context give an informed reader a probability edge.

## Data Sources
- **ClinicalTrials.gov API v2** (already operational in unified_system): `/studies` endpoint with filters for Phase 3, status = "Active, not recruiting" (primary endpoint is imminent when enrollment is closed and follow-up is in progress).
- **Primary completion date field**: `PrimaryCompletionDate` / `ResultsFirstPostedDate` — drives the 90-day window filter.
- **openFDA**: precedent drug approvals in the same therapeutic area for base-rate approval probability.
- **EDGAR 8-K filings**: company guidance on readout timing.
- **Company IR pages / press releases**: guidance updates ("readout expected H2 2026").
- **Cost**: free, all within existing scanner stack.

## Signal Logic
Pre-edge biotech setup has the following structure:

1. **Trial design**: Phase 3 with a single-asset primary endpoint, non-inferiority or superiority against a comparator. Multi-endpoint trials require more nuanced scoring.
2. **Enrollment complete**: status = "Active, not recruiting" OR "Completed" with PrimaryCompletionDate in the next 90 days.
3. **Precedent base rate**: FDA approval probability in the indication, conditional on hitting primary endpoint, must be ≥ 70% (high-base-rate indications like rare-disease, oncology, cardiology).
4. **Market mispricing**: small-cap / mid-cap biotech ($215M–$10B) trading below 90-day average price, low analyst coverage (< 8 analysts), insider buying or institutional accumulation.
5. **Strategic optionality**: the company has an existing approved drug or a credible second-asset pipeline — so a positive readout does not require existential approval to re-rate.

Minimum bar: a candidate must hit **at least 3 of the 5 patterns** AND the readout window must be within 90 days.

## Triage Filters (Stage 1)
- Market cap ≥ $215M USD.
- Phase 3 primary completion within 90 days (ClinicalTrials.gov).
- Not currently under a definitive merger agreement.
- Not already at PDUFA stage (post-filing, that's the FDA PDUFA scanner's job).
- Company has a credible balance sheet (cash runway > 12 months — a failed Phase 3 shouldn't zero the stock).

## Scoring (uses existing `binary_catalyst` rubric OR new `pre_pdufa_readout` subtype)
Fits within the `binary_catalyst` rubric (0–50) with pre-readout scoring tweaks:
- **Approval Probability** (0–15): base rate + trial design quality + precedent drugs.
- **Market Mispricing** (0–10): options-implied vol vs. historical Phase 3 move; stock below 90-day moving average.
- **Magnitude** (0–10): unmet-need size, pricing power if approved.
- **Competitive Landscape** (0–10): first-in-class vs. me-too, IP runway.
- **Timeline Clarity** (0–5): how tight is the readout window? Trials with firm primary completion dates score higher.

Auto-caps:
- Phase 3 primary completion date has moved backward in the last 6 months → cap at archive (delay risk).
- Trial has known enrollment issues or SAE concerns → cap at watchlist.
- Company has a prior failed Phase 3 in the same indication → no cap, but adjust approval probability.

## Execution
- **Frequency**: weekly.
- **Output**: signals to `signals/` with `scoring_profile: binary_catalyst`, `signal_type: pre_phase3_readout`, `thesis_direction: long` (or short when probability of failure is mispriced).
- **Candidate promotion threshold**: score ≥ 30, same as existing PDUFA scanner.
- **Expected pipeline volume**: 2–5 pre-Phase 3 candidates in active pool at any time.

## Expected Outcome / Kill Watch
- **Hit**: Phase 3 hits primary endpoint → stock re-rates 30–80% within a week of readout. Move to `us_fda_pdufa` scanner coverage as it transitions to NDA-filing window.
- **Miss (edge disappears)**: Phase 3 fails primary → archive with `outcome: MISS`, stock has already bottomed, no residual edge.
- **Decay**: company pushes primary completion date back > 90 days → archive.

## Operational Integration
- Lives in `unified_system/tools/scanners/pre_phase3_readout_scanner.py` (new).
- Re-uses existing `openfigi_resolver.py`, ClinicalTrials.gov API client from the FDA PDUFA scanner.
- Feeds `_collect_all_candidates()` with `.md` dossier + `_curated_rationales.json` entry.
- On readout date (catalyst fires), automatically moves to `_archived` with outcome noted — no human step required to apply the post-edge gate.

## Dependencies
- ClinicalTrials.gov API v2 client (already operational in `us_fda_pdufa.md` infrastructure — reuse).
- `openfigi_resolver.py` (operational).
- Base-rate tables for approval probability by indication / phase at `config/phase3_approval_base_rates.json` (new — seed from published meta-analyses).

## Status
**Planned.** Not yet implemented. Implementation estimate: 1–2 scheduled sessions given the ClinicalTrials.gov client is already operational. Lower effort than takeover-candidate scanner.
