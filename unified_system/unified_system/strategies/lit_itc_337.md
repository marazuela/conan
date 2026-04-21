# Strategy 2 — ITC Section 337 Investigations

## Thesis

ITC 337 investigations are structurally underweight in equity research relative to impact. Institution of a 337 investigation moves respondent stocks 5–15%. The ITC publishes institution notices 3–7 days before most respondents file an 8-K. Complainant stock moves on complaint filing (~2 weeks before institution). Component makers, chip designers, and pharmaceutical generics are the most exposed.

## Primary sources

- **USITC EDIS** — `edis.usitc.gov` (electronic document information system). Free. Search API documented; HTML fallback for niche queries.
- **USITC Federal Register notices** — `usitc.gov/press_room/news_release.htm`. Institution notices post here the day of Commission vote.
- **USITC Section 337 investigations index** — `usitc.gov/intellectual_property/investigations.htm`.

## Cadence

Every 12 hours (per D-005).

## Target universe filter

- All 337 investigations. Parties: both complainants and respondents; respondents typically more market-relevant (defensive equity risk).
- Market-cap filter applied at party-resolution stage, not scan stage (ITC investigations have small party lists; filter late).

## Triage rules (Stage 1)

Drop if:
1. No party resolves to an in-universe issuer.
2. Institution notice re-published (dedup by investigation number).
3. Party is a named individual (rare in ITC; usually corporate).

## Signal types to emit

- `itc_complaint_filed` — complainant side signal. Signal Strength 3–4.
- `itc_investigation_instituted` — respondent side; binary. Signal Strength 5.
- `itc_investigation_terminated_by_settlement` — 3–4.
- `itc_initial_determination_issued` — ALJ's initial decision. 4–5.
- `itc_commission_final_determination` — 5.
- `itc_exclusion_order_issued` — 5 (imports blocked).
- `itc_cease_and_desist_issued` — 4.
- `itc_temporary_exclusion_order` — 4.

## Dedup key

`investigation_number` (e.g., "337-TA-1234"). The same investigation can generate multiple signals at different procedural milestones — NOT deduped by investigation number alone, deduped by `(investigation_number, signal_type, source_date)`.

## Known failure modes

- F-07: ITC redacts confidential information; public version of institution notice may be 2–7 days behind the non-public version (analysts with Commission access have the edge). Mitigation: signal on the public version only; note redaction lag in brief.
- F-11: EDIS downtime (observed in prior periods). Mitigation: retry with exponential backoff; log to Tool Health.
- F-14: Party names in ITC captions often differ from SEC company names (Korean/Taiwanese parents named by English legal name, subsidiaries named by brand). Exhibit 21 parser is the primary fallback.
- F-20: Institution notices sometimes amend respondent lists (adding or removing respondents after vote). Re-scan and re-emit on amendment.

## Output

Signal JSON with `signal_category = "itc_337"`. `raw_data.investigation_number`, `raw_data.commission_action`, `raw_data.parties_complainants`, `raw_data.parties_respondents` populated.
