# Strategy 5 — SEC Enforcement Docket

## Thesis

SEC files litigation releases the day of enforcement action, often before the respondent company can 8-K it. Wells Notices leak through 10-Q risk factors occasionally, but the enforcement release is the first public signal. Enforcement against executives (not the entity) is especially under-covered by equity analysts despite material reputational and governance signal value.

## Primary sources

- **SEC Litigation Releases** — `www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=litigation`. User-Agent header required. Free.
- **SEC Enforcement press releases** — `www.sec.gov/news/pressrelease`. Announces notable actions (parallel to Litigation Releases, with more narrative).
- **SEC EDGAR administrative proceedings** — `efts.sec.gov/LATEST/search-index` with form-type filter for administrative proceedings, consent orders, orders instituting proceedings.

## Cadence

Every 6 hours (per D-005). SEC publishes intraday.

## Target universe filter

- All enforcement actions where at least one respondent is (a) an in-universe corporate entity or (b) an executive of an in-universe entity (cross-reference via `executive_lookup.json` per D-010).
- Dollar-amount filter: no hard floor; even small civil penalties can indicate larger problems.

## Triage rules (Stage 1)

Drop if:
1. Respondent is an individual not in `executive_lookup.json`.
2. Respondent is a registered investment advisor with AUM < $500M and not associated with a public parent.
3. Action is a settled administrative proceeding that was pre-disclosed in the entity's 10-Q risk factors more than 90 days prior.

## Signal types to emit

- `sec_litigation_release_filed` — generic; Signal Strength varies.
- `sec_enforcement_action_against_entity` — 4–5 (direct corporate action).
- `sec_enforcement_action_against_executive` — 3–4 (governance signal; use `executive_lookup.json` to map to issuer).
- `sec_cease_and_desist_order_issued` — 3–4.
- `sec_wells_notice_implied` (derived from 10-Q risk factors, not from direct SEC disclosure) — 2–3.
- `sec_parallel_criminal_referral` — 5 (DOJ co-plaintiff noted in release).
- `sec_consent_order_entered` — 2–3 (typically pre-disclosed; low info asymmetry).

## Dedup key

`(release_number, respondent_cik_or_exec_name, signal_type)`.

## Known failure modes

- F-06: SEC litigation-release page format changes occasionally; scanner must be resilient to minor HTML drift. Mitigation: structured HTML selectors with fallback to raw-text extraction.
- F-10: Executive-to-issuer mapping drift — executives leave between proxy-statement snapshot dates. Maintenance task refreshes `executive_lookup.json` quarterly (D-010).
- F-14: SEC captions can list many respondents; Stage 1 must evaluate all respondents for in-universe match, not just the first.
- F-15: SEC redacts some information in administrative-proceeding orders; public version lags 30–60 days in rare cases.

## Output

Signal JSON with `signal_category = "sec_enforcement"`. `raw_data.sec_release_number`, `raw_data.respondents` (list of normalized names + roles), `raw_data.penalty_amount_usd` if stated, `raw_data.parallel_criminal_flag` populated.
