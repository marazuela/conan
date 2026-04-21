# Strategy 1 — Federal Civil (PACER / RECAP)

## Thesis

Federal civil dockets are the highest-volume, highest-signal docket in the US legal system. Service-of-process can precede a defendant's 8-K by 3–10 days. Motion-to-dismiss rulings, Markman claim-construction orders, preliminary-injunction decisions, and summary-judgment rulings are binary market-movers that equity analysts routinely miss because the parsing cost is high.

## Primary sources

- **CourtListener RECAP API v4** — `www.courtlistener.com/api/rest/v4/` (search + dockets). Free API token, registered. Documented 5000 req/hr for free tier.
- **RECAP Archive** — `www.courtlistener.com/recap/`. Free document body retrieval where RECAP has the PDF.
- **PACER index (via CourtListener)** — index metadata is free through CourtListener even when the document body is behind PACER. Document body itself is PACER paywalled: flagged for manual pull per D-008, never autonomous.

## Cadence

Every 6 hours (per D-005).

## Target universe filter

- Nature-of-suit codes: 830 (patent), 820 (copyright), 840 (trademark), 850 (securities), 410 (antitrust), 190 (other contract), 195 (franchise), 710–790 (labor/employment class actions), 422–423 (bankruptcy appeals), 442 (civil rights employment). Excludes: prisoner petitions, social security, immigration, most criminal.
- Courts: all 94 US District Courts + 13 Circuit Courts (appeals).
- Date filter: filings in the last 72 hours per scan (with 24h overlap to catch delayed RECAP ingest).

## Triage rules (Stage 1)

Drop if:
1. Neither party normalizes to a `corporate_entity` type.
2. Corporate parties but none appear in-universe after party resolution at confidence ≥ 0.85.
3. Docket entry is purely procedural (extension motions, clerk-admin entries, judge-reassignment).
4. Party is a subsidiary whose parent is already covered by a same-court signal in the last 14 days (dedup rule).

## Signal types to emit

- `complaint_filed` — new case. Signal Strength typically 3–4.
- `motion_to_dismiss_granted` / `motion_to_dismiss_denied` — 4–5 on Signal Strength.
- `preliminary_injunction_granted` / `preliminary_injunction_denied` — 5.
- `markman_order_issued` — 4–5 (patent-case specific).
- `summary_judgment_granted` / `summary_judgment_denied` — 5.
- `class_certified` / `class_decertified` — 4.
- `settlement_agreement_filed` — 3–4.
- `case_transferred` — 1–2.
- `consolidated_with` / `mdl_transferred` — 3 (re-link signals).

## Dedup key

`(court, case_number, docket_entry_id)` — never caption-based. Re-emission of the same docket entry from RECAP retry is dropped.

## Known failure modes

- F-03: RECAP lag (some courts update on 6–24h delay). Mitigation: overlap window, re-scan.
- F-05: Over-reads on nature-of-suit filter; mitigation: Phase 3 tuning pass on false-positive rate.
- F-08: Multi-district litigation consolidates cases; dedup on the MDL master docket.
- F-14: Party-name variant (subsidiary vs parent in caption); handled by Exhibit 21 resolution path.
- F-16: Rate-limit breach on CourtListener; mitigation: 500 req/pass budget, exponential backoff.

## Output

Standard signal JSON per `CONTEXT.md` schema. `raw_data.court`, `raw_data.case_number`, `raw_data.case_caption`, `raw_data.party_role`, `raw_data.document_status` populated. If document is PACER-only, `raw_data.document_status = "in_pacer_only"` and `raw_data.pacer_cost_estimate_cents` set.
