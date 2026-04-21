# Congressional Trading Scanner — Diagnostic
**Tool**: `tools/congressional_trading.py` v2.0
**Grade**: **B** — working reliably but signal-to-noise is too low; known FP patterns unfiled

---

## What it does (verified)
Scrapes Capitol Trades HTML (free, no auth) for recent congressional stock disclosures. Filters by trade size (≥$15k), market cap (≥$215M), stock-type (excludes mutual funds/ETFs where possible). Classifies signals into four types: committee-aligned, unusual size (≥3× member's median), timing cluster (≥2 members same ticker within 2 weeks), generic trade. Dedup via MD5.

## Current health (verified)
- **Today's live-run (S56, 2026-04-14)**: 11 signals, all strength 2–3, all mega-caps (JNJ, NVDA, MSFT, CB, etc.) — no candidates.
- **Wall-clock**: 58.6 s — the slowest scanner; close to the 60 s mental budget.
- **Compilation**: clean.
- **API**: Capitol Trades 200.
- **Historical**: Past 2 sessions (S30, S31) flagged the same Ro Khanna spouse/mega-cap pattern (Q-014).

## What's working (verified)
- **Capitol Trades scraping stable** — D-013 migration off Quiver (which now requires auth) has held. No scraping-break in ~40 sessions.
- **COMMITTEE_SECTOR_MAP + SECTOR_TICKER_MAP** — cross-references legislator committee assignments against traded tickers. 7 committees mapped.
- **MEMBER_COMMITTEES_BY_NAME** — static lookup ~70 members. Approach is pragmatic (congress.gov XML API was slow/unreliable per strategy spec).
- **Q-014 partial — spouse/child flagging already in place** — but only as a note, not a strength downgrade.

## Known issues (verified)

### Q-014: Ro Khanna spouse/child mega-cap FP (pattern confirmed, rule not yet applied)
- **Pattern**: Owner ∈ {Spouse, Child} + trade $1k–$50k + mega-cap ($100B+) consumer/cloud tech (AMZN, CRM, GOOGL, AAPL, META, NVDA, AVGO) + committee = Commerce → strength 4 false positive.
- **Confirmed across**: S30, S31, and implicitly every subsequent session where strength-3 AMZN/MSFT/NVDA signals show up with no actionable thesis (including today).
- **Rule (proposed)**: IF owner ∈ {Spouse, Child} AND trade ≤$50k AND mcap ≥$100B AND committee = "Commerce" → downgrade 4→2.
- **Effort**: ~30 minutes. Just needs to land in `_score_signal()`.

### Ticker-matching false positives
- Today's signals include "FBBEU" (JPMorgan Beta Builder Europe ETF), "TFVEA" (Vanguard FTSE Developed Markets ETF), "TPYP" (Tortoise North American Pipeline Fund), "FSPYM" (TRADR 2× SPY), "DFTGC" (FT Global Tactical). These are **ETFs/managed funds** where the congressperson's trade carries no info asymmetry about individual holdings.
- **Root cause**: TickerType filter (`TickerType == "Stock"`) isn't catching these — likely because Capitol Trades labels them as "Stock" by asset class.
- **Fix needed**: Maintain a reject-list of ETF/fund tickers OR use yfinance `quoteType` lookup. ~1 hour.

### Non-US ticker noise
- "CAMCR" (Amcor PLC, London-listed) — non-US exchange ticker should not be flagged under US-committee-alignment logic.
- **Fix needed**: Reject tickers with exchange suffixes or <3-char tickers that look non-US. ~30 min.

### Committee alignment scope
- Commerce committee gets flagged as "aligned with everything" because it oversees tech, transportation, telecom, consumer products, etc. Too broad.
- **Fix needed**: Narrow Commerce alignment to specific subcommittee jurisdictions. This is hard (no clean data source). INFERRED lower priority.

## Data-structure observations (verified from source)
- `raw_data` includes: politician name, party, chamber, committee assignments (resolved), owner (Self/Spouse/Child), trade_date, filing_date, trade_type, size_range, filed_delay_days.
- Dedup hash: `(politician, ticker, trade_date, trade_type)`.
- Scoring boost for "unusual_size" uses median trade size per member — clever; may be noisy for low-activity members (small sample).

## What to build next (ranked)

**P1** (next session):
1. **Apply Q-014 downgrade rule** (30 min).
2. **Add ETF/fund reject list** (`{FBBEU, TFVEA, TPYP, FSPYM, DFTGC, ...}` — seed from today's FP list) (30 min).
3. **Reject non-US tickers** with length check + exchange suffix detection (30 min).

**P2** (1–2 weeks):
4. **ExcessReturn proxy**: rank members by historical trade P&L (computed over last 12 mo of disclosures vs. SPY). Boost top-quartile, demote bottom. Quiver had this metric built-in; we lost it when we migrated. Approximation effort: ~1 day.
5. **Options-trade flagging** — rare but high-intent. Capitol Trades exposes the asset type; check scanner handles it.
6. **Cluster-strength weighting**: 2 committee-aligned members on same ticker in 3 days = higher signal than 2 members across parties on same ticker in 14 days.

**P3** (speculative):
7. **Amendment detection** — if a trade is re-filed with a different size/date, that's itself a signal (late-disclosure penalty, audit target).
8. **Sector rotation detection** — if total congressional dollar flow into Energy rises 30% MoM, that's a macro tell independent of individual trades.

## Signal quality context
Today's 11 signals = 0 actionable. The scanner is producing a lot of noise per candidate. Session notes show this pattern for many weeks — congressional has **not produced a candidate-grade signal alone** in the recent window. Its value has been as a convergence input, not a standalone source. That's fine *if* we stop scoring mega-cap non-aligned ETF trades as strength 3.

## Synergy hooks
- **Congressional + Contract + EDGAR defense** — the highest-value convergence use case. Armed Services member trades defense prime in same 14 days as a USAspending award or an 8-K earnings surprise = stackable signal.
- **Congressional + FDA PDUFA** — less clean (HELP committee members rarely move on PDUFA-specific info), but worth logging.

## Verification notes
- Source code read in full; all structural claims verified.
- Today's run data verified against `reports/2026-04-14_daily_report.md`.
- Q-014 pattern confirmation verified against OPEN_QUESTIONS.md + SESSION_STATE history references.
- Non-US/ETF FP claims INFERRED from today's report rows; ticker identities verified by name-column reading.
