# Strategy 3 — PTAB Inter Partes Review (IPR)

## Thesis

PTAB IPR institution decisions invalidate patents core to revenue streams. The schedule is deterministic: 6 months from petition filing to institution decision; 12 months from institution to final written decision. Small- and mid-cap biotech and tech stocks move decisively on IPR outcomes. Coverage by equity analysts is unusually thin despite the binary outcomes, because the PTAB calendar is not integrated into most equity research workflows.

## Primary sources

- **USPTO PTAB End-to-End (E2E) API** — `developer.uspto.gov/api-catalog/ptab-api-v2`. Free API key registration. Structured data on every petition, decision, and filing.
- **PTAB search portal** — `ptab.uspto.gov`. HTML fallback for edge cases.

## Cadence

Daily (per D-005). PTAB posts updates mid-day ET; once-daily post-update is sufficient.

## Target universe filter

- All IPR petitions and post-grant reviews (PGR, CBM legacy).
- Petitioner and patent owner both evaluated for in-universe mapping.
- Pharmaceutical and biotech IPRs (PGR more common than IPR in biotech) get a flag for Orange Book cross-reference — a challenged patent listed in the Orange Book on a revenue-core drug is a high-stakes event.

## Triage rules (Stage 1)

Drop if:
1. Neither petitioner nor patent owner is in-universe at confidence ≥ 0.85.
2. Filing is purely procedural (scheduling order amendment, extension of time).
3. The patent under review has no identifiable revenue connection (generic challenge on expired or peripheral patent).

## Signal types to emit

- `ipr_petition_filed` — petitioner signal (aggressive posture against patent owner). Signal Strength 3.
- `ipr_institution_granted` — binary; patent owner loses presumption of validity. Signal Strength 5.
- `ipr_institution_denied` — patent owner wins first round. Signal Strength 4.
- `ipr_final_written_decision_all_claims_unpatentable` — 5 (full invalidation).
- `ipr_final_written_decision_mixed` — 4.
- `ipr_final_written_decision_all_claims_patentable` — 4.
- `ipr_settled_prior_to_institution` — 3.
- `ipr_appealed_to_federal_circuit` — 3 (catalyst extended).

## Dedup key

`(ptab_proceeding_number, signal_type)`. Same proceeding generates at most one institution signal, one final-written-decision signal, etc.

## Known failure modes

- F-09: PTAB E2E API rate-limits (documented 120 req/min). Mitigation: spaced polling, query by date range.
- F-13: Patent-to-revenue mapping is often not available from the patent number alone. Mitigation: Orange Book cross-reference for pharma; 10-K risk-factor scan for tech (Phase 4+).
- F-14: Biotech petitioners are often generic manufacturers with no US listing; petition-side signal lacks an investable name. Patent-owner-side signal still actionable.
- F-17: IPR final-written-decision dates slip occasionally (extensions). Track current scheduling order.

## Output

Signal JSON with `signal_category = "ptab_ipr"`. `raw_data.ptab_proceeding_number`, `raw_data.patent_number`, `raw_data.petitioner_name`, `raw_data.patent_owner_name`, `raw_data.orange_book_flagged` populated.
