# Strategy 3: Congressional Trading Replication

## Summary
Monitor US congressional stock trades (STOCK Act disclosures) for unusual activity, particularly committee-aligned trades that may reflect non-public legislative intelligence.

## Data Sources
- **Primary**: Capitol Trades HTML scraping (free, no auth, accessible from sandbox)
  - URL: `https://www.capitoltrades.com/trades`
  - Returns: Recent congressional trades with fields: politician name, party, chamber, trade date, ticker, asset type, trade type (buy/sell), size range, filing date
  - Verified accessible and working from Cowork sandbox (April 2026)
  - Tool: `tools/congressional_trading.py` v1.0 — 37/37 tests passed
- **Committee data**: Static JSON lookup table mapping BioGuideID → committee assignments (see D-009)
- **Cost**: Free, no auth required

## Data Source History
- Originally planned Lambda Finance API → DNS blocked from sandbox
- Switched to Quiver Quantitative API → worked during feasibility testing (April 2026) but now requires auth (see D-013)
- Final: Capitol Trades HTML scraping — accessible, stable, sufficient data for signal generation

## Signal Logic
Not all congressional trades are informative. Filter for:
1. **Committee alignment** (highest signal): Senator on Health committee buying pharma, Armed Services member buying defense contractor
2. **Unusual size**: Trades >$50K where member typically trades <$15K
3. **Timing clusters**: Multiple members trading same stock within 2-week window
4. **Contrarian trades**: Buying during market selloff or sector downturn
5. **Options activity**: Any options trades by members (rare and highly intentional)
6. **High ExcessReturn history**: Members whose past trades show consistently positive ExcessReturn (use Quiver's built-in metric)

## Triage Filters (Stage 1)
- Market cap ≥ $300M (verify via yfinance or OpenFIGI)
- Trade size ≥ $15K (filter out trivial transactions)
- TickerType == "Stock" (exclude mutual funds, ETFs)
- Signal novelty: deduplicate amended filings (check last_modified vs ReportDate)

## Committee-Sector Mapping
Build and maintain a lookup table:
- Armed Services → Defense (LMT, RTX, GD, NOC, etc.)
- Health/HELP → Pharma/Biotech
- Banking → Financials
- Energy → Oil & Gas, Utilities, Renewables
- Commerce/Science → Tech, Telecom
- Agriculture → Ag companies, Food producers
- Judiciary → Litigation-exposed companies

## Execution
- **Frequency**: Daily scan (API returns 1,000 most recent; new disclosures published in batches ~2x/week)
- **Tool**: To be built — Quiver Quant API client + committee cross-reference + signal scoring
- **Output**: JSON signal objects to `signals/`
- **Escalation**: Committee-aligned trade + unusual size → automatic score boost

### Committee Cross-Reference Implementation Note
The congress.gov XML API (with DEMO_KEY) works but is slow and unreliable for bulk lookups. **Approach**: Build a static committee-member lookup table (JSON) covering current Congress, updated manually when new sessions begin. This is faster and more reliable than real-time API calls. The lookup table maps BioGuideID → list of committee assignments for instant committee-alignment checks.

## Deep Dive Analysis Checklist (Stage 3)
When a signal scores 30+ or is part of a convergence:
1. **Committee assignment verification** — confirm the legislator sits on a committee relevant to the traded stock's sector
2. **Pending legislation check** — search congress.gov for bills in committee that affect the company or sector
3. **Upcoming hearings** — are there scheduled committee hearings related to the industry?
4. **Legislator track record** — use Quiver's ExcessReturn data to assess historical performance. Members with consistently positive excess returns are higher-signal
5. **Cluster analysis** — did other members of the same committee make similar trades in the same window?
6. **Trade timing vs. news** — was the trade made before or after publicly available information?
7. **Company catalyst check** — what upcoming events (earnings, FDA decisions, contract awards) could explain the trade?

8. **Web research layer** — mandatory. Search for recent news on the traded company, pending legislation, lobbying activity, and any media coverage of the legislator's trading activity. Assess whether findings strengthen or weaken the thesis. Follow the full checklist in `framework/candidate_template.md`.

## Edge
Academic research shows committee-aligned congressional trades outperform by 4-8% annually. The 45-day disclosure delay means the edge is partially decayed but still actionable for multi-week holding periods, especially when combined with catalyst analysis.
