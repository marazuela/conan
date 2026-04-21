# Objectives

## Primary Goal

Build and operate an autonomous investment signal discovery system that identifies non-traditional equity investment opportunities from legally public but practically invisible data sources — sources that traditional research workflows miss because the information is scattered, format-hostile, foreign-language, or buried in government databases.

## Mandate

- **Universe**: Publicly listed equities, any geography, minimum $300M market cap
- **Edge type**: Structurally obscure public information (not temporarily unnoticed)
- **Holding horizon**: Weeks to months (not multi-year thematic, not intraday)
- **Position sizing**: Satellite positions (2–5% of portfolio) — asymmetric risk/reward
- **Constraint**: Legal public data only. No proprietary data feeds, no consensus-dependent theses, no macro bets
- **Reporting**: Daily signal reports + full candidate writeups as they emerge

## The 5 Strategies

| # | Strategy | Edge Source |
|---|----------|------------|
| 1 | EDGAR Keyword Scanning | Full-text scan of entire SEC filing universe, not just a watchlist |
| 2 | ESMA Short Position Aggregation | No public aggregated EU short position database exists — we build one |
| 3 | Congressional Trading Replication | Committee-aligned trades show 4–8% annual alpha (academic evidence) |
| 4 | Government Contract Award Monitoring | Awards published 1–3 days before company press releases |
| 5 | FDA PDUFA Calendar Analysis | Binary events with known dates; edge in neglected small-cap biotechs |

## Sub-Goals

1. **Signal infrastructure**: Common JSON signal format, entity resolution (OpenFIGI), convergence detection across strategies
2. **Quality over quantity**: Every candidate must survive a 3-stage pipeline (triage → scoring → deep dive) — we want 2–5 high-conviction candidates per week, not 50 noisy alerts
3. **Compounding data advantage**: Historical signal accumulation (especially ESMA short position time series) creates a dataset that grows more valuable over time
4. **Full autonomy**: System runs via daily scheduled Cowork sessions without requiring Pedro's intervention for routine operation

## Success Criteria

- [ ] All 5 Python scanner tools built, tested, and producing valid JSON signals
- [ ] OpenFIGI entity resolution module operational across all strategies
- [ ] Convergence engine detecting cross-strategy signal overlap within 14-day rolling window
- [ ] Daily scheduled session running the full pipeline: scan → triage → score → deep dive → report
- [ ] First batch of validated candidates produced with full deep dive analysis
- [ ] Kill condition monitoring active on all existing candidates each session
- [ ] System operates autonomously for 7 consecutive days without manual intervention

## Definition of Done

The system is "done" when a single daily Cowork session can: run all 5 scanners, triage and score the results, detect convergences, produce or update candidate writeups for any signal scoring 30+, monitor existing candidates against kill conditions, and output a daily report — all without human input. Pedro reviews the daily report and candidates at his discretion, but the system does not depend on him being present.
