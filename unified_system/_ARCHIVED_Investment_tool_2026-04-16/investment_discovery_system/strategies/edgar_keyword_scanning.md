# Strategy 1: EDGAR Keyword Scanning

## Summary
Scan SEC EDGAR full-text search (EFTS) API for filings containing activist, distress, M&A, and governance keywords. Detect signals before market reacts.

## Data Sources
- **Primary**: `https://efts.sec.gov/LATEST/search-index` — full-text search across all filings since 2001
- **Secondary**: `https://data.sec.gov/submissions/CIK{CIK}.json` — structured filing metadata per company
- **Cost**: Free, no auth required
- **Rate limit**: 10 req/sec with User-Agent header (must include valid email)
- **Latency**: Filings indexed within minutes of submission

## Key Filing Types
- SC 13D/G (activist stakes >5%)
- 8-K (material events)
- Form 4 (insider transactions)
- DEF 14A / DEFA14A (proxy, governance)
- 10-K/10-Q (risk factors, distress language)
- NT 10-K/Q (late filing notifications — often a distress signal itself)

## Keyword Dictionaries
- **Activist**: "strategic alternatives", "board representation", "maximize shareholder value", "undervalued", "change in control", "proxy contest", "consent solicitation"
- **Distress**: "going concern", "covenant breach", "waiver", "forbearance agreement", "material weakness", "restatement", "liquidity shortfall", "substantial doubt"
- **M&A**: "merger agreement", "tender offer", "fairness opinion", "change of control", "break-up fee", "definitive agreement", "received indication of interest"
- **Governance**: "poison pill", "rights plan", "bylaw amendment", "declassify board", "auditor resignation", "whistleblower", "internal investigation"

## Triage Filters (Stage 1)
- Market cap ≥ $300M (use data.sec.gov for company metadata, cross-ref with OpenFIGI)
- Listed on major exchange (NYSE, NASDAQ)
- Signal novelty: skip if same keyword appeared in same company's filings in last 90 days without material change
- Skip routine boilerplate: many 10-K filings include standard "going concern" language that hasn't changed year-over-year

## Execution
- **Frequency**: Daily (morning scan of prior 24-48h filings)
- **Tool**: `tools/edgar_filing_monitor.py` (exists, needs significant refactoring — see notes below)
- **Output**: JSON signal objects to `signals/`
- **Escalation**: SC 13D filing or 3+ keyword hits on same ticker → automatic score boost

### Existing Tool Issues (identified in feasibility testing)
The current `edgar_filing_monitor.py` has incorrect assumptions about the EFTS API response:
- ❌ Uses `entity_id` field — **does not exist** in API response
- ✅ Correct field for CIK: `ciks` (array, use `ciks[0]`)
- ✅ Correct field for accession number: `adsh` (use for filing URL construction)
- ❌ `display_names` contains company+ticker+CIK in a single string, not separate fields
- ❌ Output is print-to-stdout only — needs JSON signal file output
- Refactoring required before tool can be used in the pipeline

## Deep Dive Analysis Checklist (Stage 3)
When a signal scores 30+ or is part of a convergence:
1. **Read the actual filing text** — not just the keyword match. What is the context? Is the language new or boilerplate?
2. **Filing history comparison** — did this company use this language before? Is this an escalation?
3. **Insider transaction cross-ref** — pull Form 4 data for same company in last 30 days. Are insiders buying or selling around this signal?
4. **Short interest check** — if available, check current short interest for contrarian positioning signals
5. **Analyst coverage count** — fewer analysts = higher information asymmetry
6. **Recent price action** — has the stock already moved? If it's down 30% in a month, the distress signal might be priced in
7. **Peer comparison** — is this company's situation unique or sector-wide?

8. **Web research layer** — mandatory. Search for recent news, analyst activity, litigation, management changes, and market narrative. Assess whether findings strengthen or weaken the thesis. Follow the full checklist in `framework/candidate_template.md`.

## Edge
Most investors see EDGAR filings through Bloomberg/Reuters alerts filtered to their watchlist. We scan the entire filing universe for keywords, catching signals on companies we don't yet follow.
