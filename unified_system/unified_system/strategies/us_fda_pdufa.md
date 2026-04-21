# Strategy 5: FDA PDUFA Calendar Analysis

## Summary
Track FDA Prescription Drug User Fee Act (PDUFA) target action dates — binary approval/rejection events with known dates months in advance. Identify mispriced setups through evidence synthesis.

## Data Sources
- **PDUFA calendar**: ⚠️ No clean API exists. FDA.gov 2026 pages return 404, biopharmawatch is JS-rendered (no data in HTML). **Approach**: Use WebSearch to find/verify PDUFA dates, maintain a manually-curated watchlist JSON. Semi-manual but functional.
- **Drug approval history**: openFDA API (`api.fda.gov/drug/drugsfda.json`) — free, no auth, verified accessible April 2026. Returns application numbers, approval dates, submission types, active ingredients. Use for precedent drug analysis and CRL history.
- **Clinical trial data**: ClinicalTrials.gov API v2 (`clinicaltrials.gov/api/v2/studies`) — free, no auth, verified accessible April 2026. Use `query.term` for searches (not `filter.phase` which returns 400). Returns: trial status, phases, enrollment, endpoints, results, statistical significance.
- **SEC filings**: Company 8-K disclosures about NDA/BLA submissions, AdCom dates, CRL responses (via EDGAR EFTS)
- **AdCom transcripts**: FDA.gov (published publicly after advisory committee meetings)
- **Cost**: All free

### Feasibility Assessment (April 2026)
- **ClinicalTrials.gov**: ✅ Fully working. Parameterized search by condition, intervention, sponsor. Phase filtering via `query.term` (e.g., `query.term=phase 3`).
- **openFDA**: ✅ Fully working. Drug approval history searchable by brand name, application number, sponsor.
- **PDUFA Calendar**: ⚠️ No automated source available. FDA.gov pages 404, biopharmawatch.com is JS-rendered. **Mitigation**: WebSearch-based PDUFA date discovery + manual curation of upcoming dates in a JSON watchlist. This is the only component requiring semi-manual input across all 5 strategies.

## Signal Logic
PDUFA dates are binary events (approve / reject / CRL). The edge is not predicting the outcome but identifying mispriced setups:
1. **Neglected small/mid-cap biotechs**: PDUFA date approaching with <5 analysts covering — market hasn't done the work
2. **Strong Phase 3 data in overlooked indication**: Positive trial results that haven't moved the stock because the indication isn't "sexy"
3. **AdCom vote signals**: Advisory committee votes (public, days before PDUFA) that strongly predict FDA decision. A 12-1 favorable vote is near-certain approval
4. **CRL recovery plays**: Companies that received Complete Response Letter, addressed issues, resubmitted — often mispriced on second attempt because market anchors on the first rejection
5. **Implied volatility mispricing**: Options market pricing too low vol for a binary event (requires options data access)

## Triage Filters (Stage 1)
- Company market cap ≥ $300M
- PDUFA date within next 90 days (pipeline tracking) or within 14 days (active monitoring)
- Listed on major exchange (NASDAQ, NYSE)
- Drug is in NDA/BLA review stage (not still in clinical trials)

## Execution
- **Frequency**: Daily calendar review. Intensity increases within 30 days of a PDUFA date
- **Tool**: To be built — FDA calendar scraper, ClinicalTrials.gov API client, PDUFA watchlist manager
- **Output**: JSON signal objects to `signals/` for approaching PDUFA dates; full analysis docs for imminent events
- **Monitoring windows**:
  - 90 days out: add to watchlist, begin background research
  - 30 days out: deep dive analysis, score the setup
  - 7 days out: final analysis update, check for AdCom results
  - PDUFA day: monitor for decision publication

## Deep Dive Analysis Checklist (Stage 3)
When a PDUFA date is within 30 days and the setup scores 30+:
1. **Phase 3 trial results** — pull from ClinicalTrials.gov API. Did the primary endpoint hit statistical significance (p < 0.05)? What was the effect size?
2. **Safety profile** — any black box warnings, serious adverse events, or safety signals that could derail approval?
3. **AdCom review** (if applicable) — read the transcript. What specific concerns did panelists raise? What was the vote count? A split vote (8-5) is much less predictive than a strong one (12-1)
4. **FDA staff review** — if published (sometimes released days before AdCom), what was the FDA reviewer's assessment?
5. **Precedent drug analysis** — has the FDA approved other drugs in the same class with similar efficacy data? If yes, approval probability is higher. If this is first-in-class, more uncertainty
6. **Complete Response Letter history** — if this is a resubmission after CRL, what did the FDA ask for and did the company address it? Resubmissions with clear resolution have >80% approval rates
7. **Market pricing assessment** — has the stock already run up in anticipation? Compare current price to pre-NDA filing price to estimate how much approval is "priced in"
8. **Analyst coverage and consensus** — how many analysts cover this name? What's consensus price target? Low coverage + approaching PDUFA = highest asymmetry
9. **Pipeline dependency** — is this the company's only drug? Single-asset biotechs have maximum binary exposure (50%+ moves on approval/rejection)

10. **Web research layer** — mandatory. Search for recent clinical data presentations, conference abstracts, KOL commentary, competing drug approvals, and patent litigation. Biotech news sites (FiercePharma, STAT News, Endpoints) are particularly valuable. Assess whether findings strengthen or weaken the thesis. Follow the full checklist in `framework/candidate_template.md`.

## Edge
PDUFA dates are publicly known months in advance. The edge comes from synthesizing AdCom transcripts, trial data, precedent decisions, and CRL patterns into a probability estimate that the market hasn't priced correctly. Small biotechs with strong data and low analyst coverage are the richest hunting ground.
