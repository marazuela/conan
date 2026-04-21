# STRATEGIES — Per-Channel Specifications (Tool 3)

Each channel below is designed to be extracted into its own file in `strategies/<name>.md` in the working project folder. The sections follow the same structure for every channel so scanner implementations can follow a common scaffold.

For each channel the new session should decide, during the manual first-pass build:
- Exact endpoint schemas (live-probe before coding against them).
- Rate-limit budgets (log in SESSION_STATE Tool Health).
- Wall-clock budgets per scan pass (default 45s; adjust per channel).

---

## Strategy 1 — Federal Civil (PACER / RECAP)

### What data, why

Every federal civil case — patent, antitrust, securities class action, commercial contract disputes, employment, intellectual property — is filed in one of 94 district courts. Every filing is docketed with a timestamp, document ID, party list, and free-text description. The docket is public; most document bodies cost $0.10/page via PACER.

The Free Law Project's RECAP archive mirrors a meaningful subset of already-fetched documents *for free*, and CourtListener provides a clean API over it plus its own docket-metadata index. For v1, Tool 3 reads:
- All docket **metadata** (case captions, party lists, docket entries, dates, judge, nature-of-suit code) via CourtListener API — free, hourly updates.
- Document bodies only when already-in-RECAP (free) or when flagged for user-directed PACER pull.

### Why this is an asymmetry

A complaint filed at 3pm on Tuesday appears in the docket immediately. The 8-K typically goes out 2–10 days later (the company needs service perfection, internal review, securities counsel sign-off, Reg FD clearance). Every one of those days is edge.

Motion-to-dismiss orders, preliminary-injunction rulings, and Markman claim-construction orders are typically 8-K'd within 1–4 days — shorter window but still nonzero.

### Endpoint access method

- **Primary**: `GET www.courtlistener.com/api/rest/v4/search/` with `type=r` (RECAP docket search) and filters for nature-of-suit codes, filing-date windows, party names.
- **Secondary**: `GET www.courtlistener.com/api/rest/v4/dockets/` for detailed docket-level queries once a case is identified as potentially material.
- **Auth**: Free API token (register at courtlistener.com/help/api).
- **Rate limit**: Documented at 5000 requests/hour; default scanner budget 500/pass.

### Entity-resolution notes

Complainant and defendant both resolved via the two-stage protocol in `LITIGATION_CONTEXT.md`. Both sides are candidates for signals (a defendant that just got sued is different from a complainant whose IP claim was upheld — both are material, with opposite directionality).

### Signal-type taxonomy

- `complaint_filed` — initial complaint against or by a public company.
- `motion_to_dismiss_denied` — defendant's MTD denied; case proceeding.
- `motion_to_dismiss_granted` — defendant's MTD granted; claim dismissed.
- `markman_order` — claim construction in patent cases; often binary for outcome.
- `preliminary_injunction_granted` / `preliminary_injunction_denied`.
- `summary_judgment_ruling` (either direction).
- `jury_verdict`.
- `settlement_filed` — stipulation of dismissal indicating settlement.
- `class_certification_ruling`.
- `appeal_filed`.
- `consolidation_order`.

### Triage filters (Stage 1)

- Party resolved with confidence ≥ 0.85.
- Issuer market cap ≥ USD $300M.
- Signal type in the taxonomy above (not procedural ministerial entries like notice of appearance, pro hac vice motion, clerk-entered housekeeping).
- Docket entry ≤ 14 days old at scan time.
- Docket entry not already signaled in prior pass (dedup on `court + case_number + docket_entry_id`).

### Deep-dive checklist

- Read the actual docket entry and, if available in RECAP, the referenced document.
- Classify the case theory (patent, antitrust, securities, contract, employment).
- Assess materiality: is the amount at stake / product at risk / patent claim central to the issuer's revenue?
- Cross-reference issuer's recent 10-Q risk factors for acknowledgment of the case.
- Web-search for prior coverage; if Law360 or Bloomberg Law already covered it, edge has decayed — flag in `edge_decay` scoring dimension.
- Kill conditions: case dismissed with prejudice, settled, or consolidated into a multidistrict litigation (MDL) that blunts the signal.

---

## Strategy 2 — ITC Section 337 Investigations

### What data, why

The USITC adjudicates unfair-import disputes under Section 337 of the Tariff Act. A 337 action is a tariff/import remedy; the remedy is an exclusion order banning import of allegedly-infringing products. For public-company respondents, a 337 investigation is a potentially-existential threat to a product line's US market.

The USITC publishes:
- Institution of Investigation notices (Federal Register).
- Scheduling orders (target date for Initial Determination).
- Initial Determinations (the ALJ's ruling).
- Commission review and Final Determinations.

### Why this is an asymmetry

Institution of a 337 investigation moves respondent stock 5–15%, often same-day. Complainant stock moves 2–8% on the *filing* of the Complaint (weeks before institution). Most equity analysts are not watching EDIS daily.

### Endpoint access method

- **Primary**: `edis.usitc.gov` search — query by filing date, investigation number, party name.
- **Secondary**: USITC press releases page for institution announcements.
- **Auth**: None.
- **Rate limit**: Be polite — USITC is a government site with no published rate limit; default to 1 req/sec.

### Entity-resolution notes

Both complainants and respondents resolved. Respondents are typically the interesting party (the one with downside). Complainants are often non-public or foreign; when public, upside signal.

### Signal-type taxonomy

- `itc_complaint_filed` — new 337 Complaint.
- `itc_investigation_instituted` — USITC votes to institute (35-day window from complaint filing).
- `itc_initial_determination` — ALJ's ruling (typically 12–16 months post-institution).
- `itc_commission_review_decision`.
- `itc_final_determination` — exclusion order or no violation.
- `itc_consent_order` / `itc_settlement`.

### Triage filters

- Respondent (or complainant, for upside) resolved with confidence ≥ 0.85.
- Issuer market cap ≥ USD $300M.
- Investigation relates to a product line material to the issuer (requires lightweight deep-dive at triage — or flag for scoring stage and triage there).

### Deep-dive checklist

- Identify the specific patent(s) and the accused products.
- Estimate the accused products' revenue share of issuer's total.
- Timeline: institution date, scheduled ID date, scheduled Final Determination date.
- Prior art / claim-construction issues if already briefed.
- Settlement likelihood (sector norms; respondent's history).

---

## Strategy 3 — PTAB IPR (Inter Partes Review)

### What data, why

Inter Partes Review is a USPTO administrative proceeding that can invalidate issued patents. Filed at the PTAB (Patent Trial and Appeal Board). Key milestones are deterministic:
- Filing: petition submitted.
- Institution decision: 6 months from filing.
- Final Written Decision (FWD): 12 months from institution.

An IPR institution is 25–40% of the time-to-outcome behind; an FWD is the outcome itself. Both are public, time-stamped, and cleanly indexed.

### Why this is an asymmetry

Patent-heavy companies (biotech, medical devices, semiconductors, software) have individual patents that drive double-digit percentages of revenue. An IPR that invalidates one of those patents is a 5–20% stock move. Coverage by equity analysts is shallow because patent law is specialized. Coverage by patent lawyers is deep but disconnected from equity research.

### Endpoint access method

- **Primary**: USPTO PTAB End-to-End API — `developer.uspto.gov/api-catalog/ptab-api-v2` — returns petition metadata, filings, decisions.
- **Secondary**: `ptab.uspto.gov` search portal for HTML-scraping when API gaps.
- **Auth**: Free API key.
- **Rate limit**: Documented; default 60 req/min.

### Entity-resolution notes

Petitioners and patent owners both resolved. Patent owner is the party with downside (their patent may be invalidated). Petitioner is often a competitor or a troll-defendant; directional signal depends on case context.

### Signal-type taxonomy

- `ipr_petition_filed`.
- `ipr_institution_decision_instituted` / `ipr_institution_decision_denied`.
- `ipr_final_written_decision` (claim-by-claim outcome — some claims can survive while others are cancelled).
- `ipr_appeal_to_federal_circuit`.
- `ipr_settlement`.

### Triage filters

- Patent owner (or petitioner) resolved with confidence ≥ 0.85.
- Issuer market cap ≥ USD $300M.
- Signal is a decision (institution or FWD) rather than a pure procedural filing — petition filings are lower-signal but still admitted with reduced strength.

### Deep-dive checklist

- Identify the specific patent(s) challenged and look up their revenue materiality via 10-K "patents material to our business" sections.
- Claim-level outcomes — distinguish total cancellation from partial cancellation.
- Appeal prospects at Federal Circuit.
- Parallel district-court litigation involving the same patents (check via PACER/RECAP).

---

## Strategy 4 — Delaware Chancery Court

### What data, why

The Court of Chancery is Delaware's specialty court for corporate disputes. Most US public companies are Delaware-incorporated, so most corporate-governance and M&A disputes end up here. Particularly valuable signals:
- **Appraisal actions** — shareholders demanding statutory appraisal in announced deals.
- **DGCL 220 books-and-records demands** — a leading indicator of activist or plaintiff-side litigation.
- **Revlon / breach-of-fiduciary-duty actions** against boards in announced M&A.
- **TRO / preliminary injunction motions** against merger closings.

### Why this is an asymmetry

Chancery's public docket system is HTML-only, slow, and requires CAPTCHA-adjacent session management to search fully. Law firms pay for Courthouse News or Bloomberg Law to monitor; retail and most buy-side analysts do not. Appraisal filings in announced deals directly signal deal-break risk before the parties issue revised disclosures.

### Endpoint access method

- **Primary**: Delaware Courts File & Serve system (requires registration for full docket), or the free public docket search at `courts.delaware.gov/help/onlineservices/docketsearch.aspx`.
- **Secondary**: Chancery opinions RSS (posts final opinions — trailing, not leading).
- **Auth**: None for public search; registration required for e-filing access (not needed for v1).
- **Rate limit**: None published; scrape politely, ≤ 1 req per 2s; maintain session cookies.

### Entity-resolution notes

This is the hardest channel for entity resolution because Chancery cases are often captioned with holding-company names or individual plaintiff names ("In re [Company] Shareholder Litigation" or "John Smith v. [Company] Board of Directors"). Heavy reliance on in-case-body parsing.

### Signal-type taxonomy

- `chancery_appraisal_petition`.
- `chancery_220_demand_refused` (company refused books-and-records demand; often precedes derivative suit).
- `chancery_derivative_complaint`.
- `chancery_m_and_a_challenge`.
- `chancery_tro_filed` / `chancery_preliminary_injunction_filed`.
- `chancery_final_opinion` (for each above).

### Triage filters

- Target entity resolved with confidence ≥ 0.85.
- Target market cap ≥ USD $300M.
- Case category in the taxonomy above (ignore garden-variety contract cases unrelated to corporate governance).

### Deep-dive checklist

- Case theory and relief sought.
- Announced deal details if applicable (target, acquirer, price, closing date, closing conditions).
- Appraisal class size estimate.
- Prior Chancery history for the same board/advisors (serial-appraisal patterns).

---

## Strategy 5 — SEC Enforcement Docket

### What data, why

The SEC announces enforcement actions via litigation releases (for federal court filings) and administrative proceedings (for in-house adjudication). Releases are same-day; they typically land before the respondent's own 8-K.

### Why this is an asymmetry

SEC litigation releases are technically well-covered — but coverage is by *legal* press (Law360, Cornerstone Research), not *equity* research, and the equity impact of enforcement-against-executives (not just the entity) is underappreciated. Wells Notices occasionally surface in 10-Q risk factors but the enforcement release itself is often the first public signal.

### Endpoint access method

- **Primary**: SEC Litigation Releases — `www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=litigation` or `www.sec.gov/litigation/litreleases.htm`.
- **Secondary**: SEC Administrative Proceedings via EDGAR full-text search.
- **Tertiary**: SEC press releases for high-profile actions.
- **Auth**: User-Agent header with valid email (same convention as Tool 1 EDGAR scanner).
- **Rate limit**: 10 req/sec documented by SEC; default to 5 req/sec.

### Entity-resolution notes

Both entity defendants and individual defendants resolved. Individual-defendant signals require the executive-lookup table (built up over sessions; names of C-suite and directors of in-universe companies).

### Signal-type taxonomy

- `sec_litigation_release_entity` — enforcement action against a public company.
- `sec_litigation_release_executive` — enforcement action against a named executive of a public company.
- `sec_administrative_proceeding_instituted`.
- `sec_administrative_proceeding_settled` (consent order).
- `sec_administrative_proceeding_contested_outcome`.
- `sec_asset_freeze_order`.
- `sec_trading_suspension` (rare but decisive).

### Triage filters

- Respondent resolved with confidence ≥ 0.85.
- For entity respondents: market cap ≥ USD $300M.
- For executive respondents: the employer (resolved via executive lookup) has market cap ≥ USD $300M.
- Release date within scan window.

### Deep-dive checklist

- Nature of the alleged violation (disclosure fraud, accounting fraud, insider trading, Reg FD).
- Scope: entity-only, executive-only, or both.
- Relief sought: monetary, injunctive, officer-and-director bar, disgorgement.
- Prior SEC history for the same entity / executive.
- Parallel criminal referral (DOJ press release same day) — materially different severity.

---

## Strategy 6 — DOJ/FTC Antitrust (Merger Challenges and HSR Second Requests)

### What data, why

The DOJ Antitrust Division and FTC share merger-review authority under the Hart-Scott-Rodino Act. Two signals:
- **Second Request** issuance — the agency wants more information; extends the waiting period significantly. Signals elevated deal-break risk.
- **Merger challenge** filed in federal court — the agency is suing to block the deal.

### Why this is an asymmetry

Second Requests are not themselves public documents, but press-release acknowledgments (by the parties) and public filings are. Merger challenges are filed in federal district court (appear in RECAP). Companies 8-K these within 1–4 business days of filing, leaving a short but exploitable window.

### Endpoint access method

- **Primary**: DOJ ATR public documents page — `www.justice.gov/atr/public-documents` — press releases filterable by date.
- **Primary**: FTC press releases filtered by competition topic.
- **Secondary**: FTC Cases and Proceedings — `www.ftc.gov/legal-library/browse/cases-proceedings`.
- **Cross-reference**: PACER/RECAP search for DOJ or FTC as plaintiff against a public-company defendant.
- **Auth**: None.
- **Rate limit**: None published; polite.

### Entity-resolution notes

Both announcing parties (acquirer and target) are typically public and resolved via the standard protocol. Target often moves more than acquirer on challenge.

### Signal-type taxonomy

- `hsr_second_request_issued` (from press-release acknowledgment).
- `doj_merger_challenge_filed`.
- `ftc_merger_challenge_filed`.
- `doj_ftc_consent_decree_proposed` (agency allows deal with conditions).
- `doj_ftc_merger_cleared` (no action — positive for both parties).

### Triage filters

- At least one party (acquirer or target) resolved with confidence ≥ 0.85 and market cap ≥ USD $300M.
- Announced deal is active (not already closed, not already abandoned pre-signal).

### Deep-dive checklist

- Deal details (announcement date, price, break fee, outside date).
- Agency's theory of harm (horizontal overlap, vertical foreclosure, innovation harm).
- Precedent for similar deals (approval rate, divestiture requirements).
- Market reaction already reflected (spread to announced price).
- Remedy feasibility — can divestitures save the deal.
