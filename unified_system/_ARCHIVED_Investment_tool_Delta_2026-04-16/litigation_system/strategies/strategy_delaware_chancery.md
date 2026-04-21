# Strategy 4 — Delaware Chancery Court

## Thesis

Chancery is THE court for M&A disputes, appraisal actions, Revlon claims, and DGCL 220 books-and-records demands. Appraisal filings in announced deals are strong signals of deal-break risk. The free index is HTML-scraping-only and CAPTCHA-adjacent, which is the moat — equity analysts rarely scrape it, giving 3–7 days of asymmetry.

## Primary sources

- **Delaware Courts public docket search** — `courts.delaware.gov/help/onlineservices/docketsearch.aspx`. HTML-only, slow, session-cookie managed. Feasibility from Cowork sandbox is Q-002; scanner must degrade gracefully to RSS-only if CAPTCHA blocks access.
- **Delaware Chancery RSS / recent opinions** — `courts.delaware.gov/opinions/`. Always reachable; authoritative for opinion publication.
- **Delaware Division of Corporations UCC search** — tangential; used for entity-normalization cross-check.

## Cadence

Every 12 hours (per D-005). Chancery opinions post irregularly; 12h captures all within one business day.

## Target universe filter

- All Chancery matters involving in-universe corporate parties (defendants or plaintiffs).
- Appraisal actions receive a flag: when an appraisal petition is filed during an announced-deal window, it is a deal-break-risk signal.
- DGCL 220 books-and-records demands: precursor to derivative suits; flag.
- Section 205 validation proceedings: rare but high-signal when filed.

## Triage rules (Stage 1)

Drop if:
1. Chancery matter is a personal-trust or real-estate dispute (common Chancery subject matters that are not securities-related).
2. Corporate party is not in-universe.
3. Case is a routine books-and-records demand without pending M&A context (unless a same-entity Federal Civil signal converges in the 30-day window).

## Signal types to emit

- `chancery_appraisal_filed` — high-signal; deal-break risk. Signal Strength 4–5.
- `chancery_books_and_records_demand_denied` — 3 (precursor filter).
- `chancery_revlon_claim_filed` — 4 (board-breach theory in announced deal).
- `chancery_motion_to_expedite_granted` — 3 (schedule compression signals merit).
- `chancery_injunction_granted_blocking_deal` — 5 (rare but binary).
- `chancery_opinion_released` — variable, Signal Strength determined by content summary (Phase 4 parsing pass).

## Dedup key

`(case_number, signal_type, source_date)`. Delaware case numbers are uniquely formatted (e.g., "2026-0123-AGB").

## Known failure modes

- F-11 / Q-002: CAPTCHA or session-cookie management blocks docket search. Mitigation: scanner designed with RSS as primary, docket-search as enrichment. Graceful degradation documented.
- F-04: Chancery opinion PDFs are typography-heavy and not always OCR-clean for signal extraction. Mitigation: rely on captioning-and-header parse; full-text summary deferred to Phase 4+.
- F-12: Delaware caption conventions are idiosyncratic (e.g., "In re X Stockholders Litigation"). Party-resolution confidence will be lower; expect more flags.
- F-19: Chancery opinion-release timing: some opinions are released on a Friday-afternoon pattern; 12h cadence may lag the market by a few hours.

## Output

Signal JSON with `signal_category = "delaware_chancery"`. `raw_data.chancery_case_number`, `raw_data.matter_type` (appraisal | books_and_records | revlon | 205 | other), `raw_data.m_and_a_context` (ticker of the announced deal if any) populated.
