# Strategy 4: Government Contract Award Monitoring

## Summary
Monitor US federal contract awards for large awards to publicly listed companies. Government contracts are material revenue events often published before the market reacts.

## Data Sources
- **Primary**: USAspending.gov API (`https://api.usaspending.gov/api/v2/`)
  - Free, no API key required, no auth
  - POST-based REST API with filter/sort/pagination
  - Returns: recipient name, award amount, awarding agency, description, dates
  - Verified accessible from Cowork sandbox (April 2026)
- **Cost**: Free, zero setup

## Previous Plan Change
Originally planned SAM.gov API (required free API key, 1-4 week approval, 10 req/day limit). Replaced because: (1) USAspending.gov provides the same contract award data, (2) no API key needed — zero setup delay, (3) no rate limit issues at our usage levels, (4) verified accessible from sandbox. SAM.gov remains a supplementary source if needed but is no longer the primary.

## Key API Endpoints
```
POST /api/v2/search/spending_by_award/
  - Filters: time_period, award_type_codes, recipient_search_text, agencies
  - Fields: Award ID, Recipient Name, Award Amount, Awarding Agency, Description, Start Date, Award Type
  - Sort by Award Amount descending to catch largest awards first
  - Pagination: limit + page parameters

POST /api/v2/recipient/
  - Lookup recipient details by name
```

## Signal Logic
- **Award size**: Focus on contracts >$50M (material to companies ≥$300M market cap)
- **Contract type codes**: A (BPA), B (Purchase Order), C (Delivery Order), D (Definitive Contract)
- **Sector focus**: Defense, IT/cybersecurity, healthcare IT, infrastructure
- **Materiality threshold**: Award > 10% of company's trailing 12-month revenue

## Triage Filters (Stage 1)
- Company is publicly listed and ≥ $300M market cap
- Award ≥ $50M (or ≥ 10% of revenue, whichever is lower)
- New award or modification >$25M (not routine option exercises)
- Recipient name matches a public company in the contractor-to-ticker mapping table

## Contractor-to-Ticker Mapping
Build and maintain incrementally. Start with top 50 defense/IT contractors:
- Defense primes: LMT, RTX, GD, NOC, BA, LHX, HII, etc.
- IT/Cyber: PLTR, PANW, CRWD, NET, LDOS, SAIC, BAH, CACI, etc.
- Healthcare IT: VEEV, etc.
- Infrastructure: PWR, EME, FLR, J, etc.
- Expand as new contractors appear in awards

## Execution
- **Frequency**: Daily (query for awards from prior 48 hours, sorted by size descending)
- **Tool**: To be built — USAspending API client, contractor name matching, materiality calculation
- **Output**: JSON signal objects to `signals/`
- **No blockers**: API is free, accessible, and requires no registration

## Deep Dive Analysis Checklist (Stage 3)
When a signal scores 30+ or is part of a convergence:
1. **Materiality calculation** — award size / trailing 12-month revenue. >20% is highly material
2. **Contract type analysis** — new work vs. renewal vs. modification. New work is the strongest signal
3. **Backlog context** — how does this award compare to the company's existing backlog?
4. **Revenue guidance impact** — does this contract change the company's ability to meet/beat analyst estimates?
5. **Analyst coverage** — fewer analysts covering = higher information asymmetry
6. **Earnings timing** — next earnings report within 30-60 days? Contract before earnings = potential beat
7. **Press release check** — has the company already announced this? If not, USAspending IS the first public notice
8. **Competitive context** — did the company win this from a competitor? Is the competitor publicly traded?

9. **Web research layer** — mandatory. Search for company press releases about the contract, competitor reactions, analyst commentary, and any related government program news. Assess whether findings strengthen or weaken the thesis. Follow the full checklist in `framework/candidate_template.md`.

## Edge
Contract awards appear on USAspending.gov often 1-3 days before company press releases. For mid-cap contractors, a single large contract can be 10-20% of annual revenue. The market typically reacts to the press release, not the government publication.
