# CONTEXT — Silence Scanner (Tool 4)

## Why silence has asymmetry

Five structural reasons silence is an exploitable-but-underexploited signal:

1. **Absence is not surfaced.** Every tool, every dashboard, every news aggregator is built to show things that happened. None display "this entity stopped doing the things it usually does." The observation requires holding a per-entity baseline in memory and comparing against it — machinery that institutional research desks *could* build but, empirically, mostly do not. Anti-surveillance-by-default.
2. **Silence is ambiguous, therefore underweighted by markets.** An event has a clear directional reading; silence could mean many things. Ambiguity discourages analyst action ("what would I write in the note?"), leaving the signal to accumulate information content as it persists. The ambiguity is the alpha.
3. **Silence precedes disclosure by structural necessity.** Almost every material corporate event has a pre-announcement dark period: Reg FD constrains, internal review freezes comment, counsel prohibits speculation, accounting teams close books, M&A confidentiality holds. Quiet periods are not incidental — they are *caused* by the undisclosed event.
4. **Baselines decay slowly; silences manifest quickly.** A well-calibrated baseline from 12+ months of observation does not shift meaningfully over 30 days of real activity. This means a 30-day silence is statistically visible even as the baseline remains stable.
5. **Multi-dimensional silence is much rarer than single-dimension silence, and much more informative.** An issuer going quiet on press releases alone is ambiguous (summer, CEO vacation, between-product-cycle). An issuer simultaneously going quiet on press releases AND insider transactions AND conference appearances AND news mentions is rare enough that the joint probability under a random-variation null is vanishingly small. The tool's real power is multi-dimensional convergence.

## What makes this domain different from Tools 1/2/3

Tools 1/2/3 scan firehoses. Their scanners are thin translators from source to signal. Tool 4 has no firehose; it *constructs* the signal from absence-of-firehose. Consequences:

- **Baseline IS the tool's memory.** The baseline persistence layer is not a cache; it is the primary dataset. Its integrity is load-bearing in a way that Tool 1/2/3 caches are not.
- **Scanners run on a schedule whether or not anything happened.** A scanner that finds nothing in a scan window does not return zero signals — it returns a stream of "observed zero; baseline expected X; compute z" results.
- **Warm-up is mandatory.** No issuer can produce signals until its baseline is populated from ≥ 18 months of history. Tools 1/2/3 can emit signals from day one; Tool 4 cannot.
- **Signals are probabilistic, not declarative.** A silence signal is a distribution claim, not an event claim.

## Data sources

### Primary endpoints — baseline inputs and ongoing observation

| Dimension | Endpoint | Auth | Rate limit | Status |
|-----------|----------|------|------------|--------|
| EDGAR filing cadence | `data.sec.gov/submissions/CIK##########.json` + EDGAR full-text search for Forms 4/8-K/10-Q/10-K/DEF 14A/S-8 | User-Agent header | 10 req/sec | ⚠️ UNVERIFIED — live-probe Phase 1 |
| Corporate press-release cadence | Business Wire per-company pages; GlobeNewswire per-company pages; PR Newswire per-company pages; issuer IR-page RSS feeds where published | None | Per-site polite (≤ 1 req/2s) | ⚠️ UNVERIFIED |
| Insider transaction cadence | Derived from EDGAR Form 4 stream (same source as dim 1, separate parser) | User-Agent header | 10 req/sec | ⚠️ UNVERIFIED |
| News/social mention volume | GDELT DOC 2.0 API (news); Reddit public JSON API (subreddit search); StockTwits public API | None for GDELT/StockTwits; Reddit requires User-Agent | GDELT 10 req/sec; Reddit 60/min; StockTwits 200/hr | ⚠️ UNVERIFIED |
| Conference/investor-event presence | Wall Street Horizon public event feeds (free tier if available); sell-side conference public agendas; issuer IR event pages | Varies | Varies | ⚠️ UNVERIFIED — Phase 1 decides feasibility; may need fallback |
| Analyst-note cadence | Finviz issuer pages (analyst activity table); Yahoo! Finance analyst-rating pages; Benzinga public rating-changes RSS | None | Polite scrape | ⚠️ UNVERIFIED |

### Support endpoints

| Purpose | Endpoint | Status |
|---------|----------|--------|
| Universe definition (Russell 1000) | iShares IWB holdings CSV (monthly refresh) | ✅ VERIFIED (pattern proven in Tools 1/2) |
| Market cap and ADV | `query1.finance.yahoo.com/v7/finance/quote` | ✅ VERIFIED from Tool 1 |
| CIK → ticker mapping | SEC `company_tickers.json` | ✅ VERIFIED from Tool 1 |
| CIK/ticker → FIGI | OpenFIGI API | ✅ VERIFIED from Tool 1 |

Every ⚠️ UNVERIFIED endpoint is live-probed during Phase 1 and upgraded to ✅ VERIFIED before any code is written against its schema. Per PROJECT_TEMPLATE Part 13.

## Baseline data model

Each in-universe issuer has a per-issuer JSON at `baselines/issuer_<cik>.json` with the following structure:

```json
{
  "cik": "0000320193",
  "ticker": "AAPL",
  "issuer_figi": "BBG000B9XRY4",
  "name": "Apple Inc.",
  "market_cap_usd": 3000000000000,
  "avg_daily_volume_usd": 8000000000,
  "universe_membership": {
    "russell_1000": true,
    "in_universe_since": "2024-01-01",
    "last_refresh_date": "2026-04-14"
  },
  "baselines": {
    "edgar_filing_cadence": {
      "window_days": 365,
      "observations": [...],
      "n_observations": 87,
      "mean_per_30d": 7.2,
      "stddev_per_30d": 2.1,
      "seasonal_adjustment": {
        "quarter_end_multiplier": 1.8,
        "summer_multiplier": 0.85
      },
      "last_updated": "2026-04-14T10:00:00Z"
    },
    "press_release_cadence": {...},
    "insider_transaction_cadence": {...},
    "news_social_mention_volume": {...},
    "conference_presence": {...},
    "analyst_note_cadence": {...}
  },
  "eligibility": {
    "warm_up_complete": true,
    "warm_up_completed_at": "2025-07-01",
    "excluded_reasons": []
  }
}
```

Plus a SQLite index at `baselines/_index.sqlite` with schema:

```sql
CREATE TABLE issuers (
  cik TEXT PRIMARY KEY,
  ticker TEXT,
  issuer_figi TEXT,
  market_cap_usd INTEGER,
  warm_up_complete INTEGER,
  last_baseline_update TIMESTAMP,
  last_scan_timestamp TIMESTAMP
);
CREATE INDEX idx_issuer_figi ON issuers(issuer_figi);
CREATE INDEX idx_warm_up ON issuers(warm_up_complete);
```

The SQLite index is for fast cross-issuer queries ("which issuers are in universe and warm-up-complete"); the per-issuer JSON is the authoritative record.

## Signal JSON schema

Tool 4's outer schema matches Tools 1/2/3 for cross-tool convergence compatibility (per D-004). Silence-specific fields live inside `raw_data`.

```json
{
  "entity_id": "0000320193",
  "entity_aux_id": "AAPL",
  "entity_name": "Apple Inc.",
  "entity_size_metric": 3000000000000,
  "signal_type": "silence_multi_dimension",
  "signal_category": "behavioral_anomaly",
  "strength_estimate": 3.8,
  "source_url": "silence_system://scan/2026-04-14T10:00:00Z/AAPL",
  "source_date": "2026-04-14",
  "scan_date": "2026-04-14T10:00:00Z",
  "raw_data": {
    "dimensions_triggered": ["press_release_cadence", "insider_transaction_cadence"],
    "per_dimension_scores": {
      "press_release_cadence": {
        "observed_30d": 1,
        "expected_30d": 8.2,
        "stddev": 2.1,
        "z_score": -3.43,
        "p_value_one_sided": 0.0003,
        "seasonal_adjusted": true,
        "n_observations_in_baseline": 87
      },
      "insider_transaction_cadence": {
        "observed_90d": 0,
        "expected_90d": 4.1,
        "stddev": 1.6,
        "z_score": -2.56,
        "p_value_one_sided": 0.0052,
        "seasonal_adjusted": true,
        "n_observations_in_baseline": 46
      }
    },
    "combined_anomaly_score": 4.8,
    "alternative_hypotheses": [
      "summer_seasonality_uncaptured",
      "executive_transition_previously_disclosed",
      "pre_earnings_quiet_period_expected"
    ],
    "baseline_validity": {
      "warm_up_complete": true,
      "baseline_last_refreshed": "2026-04-01",
      "suspected_baseline_corruption": false
    }
  }
}
```

## Entity resolution

Identical to Tool 1's protocol: ticker + MIC → OpenFIGI. Silence tool does not need Tool 3's two-stage party-name resolver because its inputs are already issuer-keyed (EDGAR filings carry CIK natively; press-release sources are issuer-pages). See D-003.

## Scoring quick reference

7-dimension rubric from Tool 1/2/3, with **one** semantic change (see `SILENCE_SCORING.md`):
- Dimension 1 (Signal Strength) interpretation adapted: strength is a function of the joint z-score across triggered dimensions, not of a single event's materiality.
- Dimension 7 (Catalyst Timeline in Tool 1/2, Party-Resolution Confidence in Tool 3) → **Baseline Validity** in Tool 4: how confident are we this silence is real vs. an artifact of baseline corruption, seasonality, or inadequate warm-up?

All other dimensions (Catalyst Clarity, Info Asymmetry, Risk/Reward, Edge Decay, Liquidity) retain their Tool 1/2/3 semantics.

## Convergence window: 60 days

Silence signals precede material disclosures on longer lead times than event-driven signals. A restatement announcement is typically preceded by 30–60 days of unusual quiet; an SEC investigation by 60–90 days; a major M&A by 21–45 days. The convergence window must accommodate the longest typical lead, hence 60 days (vs. Tool 1/2's 14, Tool 3's 30). Per D-005.

## Cadence rationale

Operational scan runs every 12 hours. Silence is a slow signal; sub-12-hour scans add cost without information. Maintenance task runs 50 minutes after operational (same offset pattern as Tool 3). Performance report runs daily at 1:30am. Deep-dive generation runs every 12 hours (aligned with operational because candidate generation is the bottleneck, not candidate depth).

Baseline refresh runs weekly (full universe) with daily incremental updates for active issuers. Weekly full refresh prevents baseline drift from dominating; daily incremental keeps warm-up monotonic.

## Execution environment

Python 3.11+ with:
```
pip install requests beautifulsoup4 lxml yfinance openpyxl pandas numpy scipy rapidfuzz python-dateutil --break-system-packages
```

`scipy` is required for statistical computations (z-scores with seasonal adjustment, p-value computation); this is the first tool in the family to require it. `numpy` and `pandas` are required for baseline manipulation.

No ML libraries in v1. Seasonal adjustment uses explicit calendar rules (quarter-end multiplier, summer multiplier, holiday multiplier) with parameters fitted once during Phase 1 and locked until Phase 8+. Time-series ML (Prophet, state-space models) is deferred.
