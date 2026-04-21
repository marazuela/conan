# Strategy 2: ESMA Short Position Aggregation

## Summary
Aggregate net short position disclosures from EU/UK national regulators into a single cross-border dataset. Detect short-seller conviction buildups and crowded shorts across Europe.

## Data Sources
- **Regulation**: EU Short Selling Regulation — net short positions >0.5% of issued shares must be published
- **Sources**: National regulator websites (no centralized ESMA API exists)
- **Cost**: Free (public disclosures)

## Implementation Status (v2.0 — Updated 2026-04-10)

### Implemented (4 regulators — all verified accessible)

1. **FCA (UK)** — Direct XLSX download at `https://www.fca.org.uk/publication/data/short-positions-daily-update.xlsx`. Returns structured Excel with columns: Position Holder, Name of Share Issuer, ISIN, Net Short Position (%), Position Date. ~580 positions per snapshot. No auth needed. **Note**: FCA site occasionally goes down for maintenance; tool uses cached snapshot as fallback.

2. **AMF (France)** — CSV via data.gouv.fr API. Dataset ID `62738e33f1be79935d3e5553`. The CSV URL changes daily (contains timestamps). Tool uses `_discover_amf_csv_url()` to query data.gouv.fr API for the current resource URL, with a date-pattern fallback. The CSV contains ALL historical disclosures (~38K rows); tool filters to active-only positions (where "Date de fin de publication position" is empty). ~185 active positions. Columns: Emetteur, ISIN, Détenteur, Position (%), Date.

3. **AFM (Netherlands)** — CSV export at `https://www.afm.nl/export.aspx?type=8a46a4ef-f196-4467-a7ab-1ae1cb58f0e7&format=csv`. **Requires full browser User-Agent string** (returns 403 with truncated UA). ~1,000 positions. Columns: Positie houder, Naam van de emittent, ISIN, Netto Shortpositie, Positiedatum.

4. **BaFin/Bundesanzeiger (Germany)** — CSV download with session cookies. Two-step process: (1) GET `https://www.bundesanzeiger.de/pub/en/nlp?1` to establish session cookies, (2) GET `https://www.bundesanzeiger.de/pub/en/nlp?0--top~csv~form~panel-form-csv~resource~link` with the session cookies. Uses `requests.Session()` for cookie persistence. ~475 positions.

### Not Yet Implemented (blocked from sandbox)

5. **CONSOB (Italy)** — Public register exists but protected by Radware bot detection. Cannot be accessed from Python or WebFetch. Needs browser automation (Claude in Chrome).

6. **CNMV (Spain)** — Returns 403 from all automated access methods. Pedro's home market — has strategic value. Needs browser automation.

## Normalized Schema
All regulators normalize to common format:
```json
{
  "regulator": "FCA",
  "holder_name": "AQR Capital Management, LLC",
  "target_company": "Aberdeen Group plc",
  "isin": "GB00BF8Q6K64",
  "position_pct": 0.50,
  "position_date": "2026-04-01",
  "previous_position_pct": null,
  "change_pct": null,
  "disclosure_date": "2026-04-09"
}
```
Use OpenFIGI to resolve ISIN → ticker for cross-strategy matching. ISIN country suffix mapping: GB→.L, DE→.DE, FR→.PA, NL→.AS, ES→.MC, IT→.MI, CH→.SW, BE→.BR, AT→.VI, IE→.IR, PT→.LS, SE→.ST, NO→.OL, DK→.CO, FI→.HE, AU→.AX, LU→.PA.

## Triage Filters (Stage 1)
- Market cap ≥ $215M / €200M (resolve via OpenFIGI + yfinance)
- Position ≥ 0.5% (regulatory minimum)
- Signal novelty: flag only NEW positions or changes ≥ 0.2% from prior snapshot
- Dedup window: 7 days

## Signal Types
- **New position**: Entity appears for the first time at ≥ 0.5%
- **Position increase**: ≥ 0.2% increase vs. prior snapshot
- **Position decrease**: ≥ 0.2% decrease — potential short covering (contrarian buy signal)
- **Crowded short**: 3+ entities shorting same company
- **Large position**: Any single position ≥ 2.0%

## Execution
- **Frequency**: Daily download from all 4 regulators
- **Tool**: `tools/esma_short_scanner.py` v2.0
- **CLI**: `python esma_short_scanner.py` (all regulators), `--regulators fca amf` (specific), `--dry-run`
- **Output**: JSON signal objects to `signals/`
- **Snapshots**: Each scan saves a multi-regulator snapshot to `signals/esma_snapshots/` for historical comparison
- **Ticker cache**: `signals/esma_ticker_cache.json` (203 entries) for fast ISIN→ticker resolution

## Deep Dive Analysis Checklist (Stage 3)
When a signal scores 28+ or is part of a convergence:
1. **Short seller track record** — is this fund known for successful short campaigns?
2. **Position trajectory** — map the last 4-8 snapshots. Steady build or sudden spike?
3. **Sell-side consensus check** — a short against unanimous Buy ratings is the most interesting signal
4. **Upcoming catalysts** — earnings, regulatory decisions, litigation milestones
5. **Cross-border check** — same entity shorting related companies in other jurisdictions?
6. **Peer short comparison** — sector-wide short (macro bet) or company-specific?
7. **Company response** — buyback announcements, insider purchases, public rebuttals?
8. **Web research layer** — mandatory. Search for recent news, earnings surprises, analyst downgrades, regulatory actions, and activist activity. Follow full checklist in `framework/candidate_template.md`.

## Edge
Information is public but scattered across multiple websites in different languages and formats. Aggregation creates a dataset that doesn't exist freely elsewhere. Cross-regulator coverage (UK + France + Netherlands + Germany) captures ~2,200 active short positions. Time series compounds in value — historical snapshot accumulation enables position velocity and pattern analysis.
