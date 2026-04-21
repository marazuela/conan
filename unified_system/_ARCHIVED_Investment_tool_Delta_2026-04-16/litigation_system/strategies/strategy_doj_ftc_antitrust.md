# Strategy 6 — DOJ / FTC Antitrust

## Thesis

Second Requests on announced M&A and merger challenges filed in federal district court are public via agency press release and PACER days before deal parties 8-K the extension of the HSR waiting period. Conduct investigations (non-merger) are often foreshadowed by agency press releases or by PACER filings where the agency is a plaintiff.

## Primary sources

- **DOJ Antitrust Division** — `www.justice.gov/atr/public-documents`. Announcement feed for merger challenges, consent decrees, and conduct cases.
- **FTC Press Releases (competition)** — `www.ftc.gov/news-events/press-releases?field_press_release_classification_target_id=1282`. Competition-specific classification filter.
- **FTC Cases and Proceedings** — `www.ftc.gov/legal-library/browse/cases-proceedings`. Canonical case index.
- **PACER (via CourtListener RECAP)** — for merger challenges filed in federal court, the complaint and filings appear on PACER. Cross-reference against Federal Civil scanner for dedup.

## Cadence

Every 12 hours (per D-005).

## Target universe filter

- All DOJ/FTC competition actions involving in-universe corporate parties.
- Cross-reference PACER complaints where DOJ or FTC is a plaintiff (nature-of-suit 410 antitrust).

## Triage rules (Stage 1)

Drop if:
1. Action is against a privately-held company with no public parent.
2. Press release announces the conclusion of a previously-public action (closing conditional consent decree; already priced in).
3. Action is an industry-wide guidance document (not enforcement).

## Signal types to emit

- `hsr_second_request_announced` — 5 (binary; extends waiting period, signals deal-break risk). Both parties to the deal get signals.
- `doj_merger_challenge_filed` — 5 (civil complaint to block merger). PACER cross-reference links to Federal Civil signal.
- `ftc_merger_challenge_filed` — 5.
- `doj_consent_decree_filed` — 3 (typically pre-announced).
- `ftc_consent_order_accepted` — 3.
- `doj_conduct_investigation_disclosed` — 4 (agency confirms investigation, often follows public reporting).
- `ftc_6b_study_launched` — 2 (not enforcement but signals future risk).
- `agency_closed_investigation` — 2 (clears overhang).

## Dedup key

`(agency, matter_identifier, signal_type)` where `matter_identifier` is the agency's docket/case number. For the same merger challenge that produces both an FTC press release and a PACER complaint, both signals are emitted but linked via a `related_matter_id` field.

## Known failure modes

- F-02: Agency press releases sometimes embargoed; first-public-trace can be a morning-markets release vs an after-hours release. Scan at 12h cadence risks missing intra-morning asymmetry. Mitigation: accept that F-02 lag caps the edge at ~12h on the most perishable signals; the deep-asymmetry names are the conduct investigations and 6(b) studies where the 12h lag is immaterial.
- F-08: Cross-agency duplication (DOJ and FTC occasionally share jurisdiction, especially on vertical mergers). Dedup by matter semantics, not ID.
- F-14: Parent-subsidiary: agencies name the operating subsidiary in press releases ("Alphabet Inc."'s subsidiary X). Exhibit 21 parser handles.
- F-18: Consent decrees can unwind (rare; requires Tunney Act proceeding). Maintenance task watches for ongoing consent-decree matters.

## Output

Signal JSON with `signal_category = "doj_ftc_antitrust"`. `raw_data.agency` (DOJ | FTC), `raw_data.matter_identifier`, `raw_data.deal_context` (if M&A-related: ticker of both sides), `raw_data.related_matter_id` (if cross-referenced to a PACER case) populated.
