# OBJECTIVES — Silence Scanner (Tool 4)

## Primary goal

Identify and surface **issuer-level behavioral silences** — periods where an issuer's observable activity across multiple dimensions drops meaningfully below its own historical baseline — as probabilistic early signals of undisclosed material developments (restatements, investigations, pre-announcement dark periods, M&A confidentiality windows, board consolidation, going-concern risk). The tool produces candidate write-ups for silences that a defensible hypothesis connects to a plausible near-term catalyst.

## Mandate

- **Universe**: US-listed equities in the Russell 1000 (approximately 1,500 issuers). Market-cap floor $2B at inclusion; $1.5B for continued coverage (preventing forced expulsion during transient drawdowns).
- **Position-size model**: 1–3% satellite positions sized to avoid post-disclosure liquidity crunch.
- **Free-sources-only**: EDGAR, exchange press-release pages, public conference agendas, Reddit/StockTwits public APIs, GDELT, Wikipedia edit streams, FINRA 13F public filings. No paid feeds in v1. If a dimension requires a paid source, it waits for Phase 8+.
- **Probabilistic signals**: every signal carries `z_score`, `p_value`, `baseline_n_observations`, and an explicit alternative-hypothesis field. A silence is never treated as binary.
- **Human-in-the-loop**: the tool produces ranked candidates; the user makes every position decision. The tool never executes, never recommends buy/sell, never estimates price targets.

## Universe rationale

Why Russell 1000 and not a broader universe:

1. **Baseline quality depends on observable volume per issuer.** A micro-cap issuer may have 4 press releases per year; its baseline is a distribution with n=4 per year — any silence is statistically indistinguishable from normal variance. Russell 1000 issuers average 40–100 press releases per year, producing a tight enough baseline that a 60% drop over 30 days is detectable at p<0.05.
2. **Liquidity floor**: $2B market cap at inclusion implies typical ADV of $15M+, sufficient for 1–3% satellite positions without market impact.
3. **Analyst coverage floor**: Russell 1000 issuers have ≥ 3 sell-side analysts on average. This is a precondition for one of the six dimensions (analyst-note cadence) producing meaningful data.
4. **Signal coverage — where silence actually matters**: restatements, SEC investigations, and going-concern events cluster heavily in mid-to-large caps where the *act* of going quiet is itself noticed by the market. Below ~$2B, going quiet is the baseline state; the signal is drowned.

Excluded from v1 universe:
- Below-Russell-1000 US equities (thin baseline, low liquidity).
- Non-US equities (silence is harder to distinguish from timezone/holiday/regulatory-calendar effects; Tool 2's coverage scope could be revisited for silence in Phase 8+).
- ETFs, closed-end funds, SPACs (fundamentally different disclosure patterns; baseline model does not apply).
- Recent IPOs (< 18 months public) — insufficient history to establish a defensible baseline.

## Six observable-activity dimensions

Silence is measured across six observable dimensions. Each has its own scanner, its own baseline model, its own z-score. An issuer silence signal is raised when the *combined* anomaly score across dimensions exceeds threshold, OR when a single dimension's z-score is extreme (|z| ≥ 3) on its own.

| # | Dimension | Baseline window | Observable unit |
|---|-----------|-----------------|-----------------|
| 1 | EDGAR filing cadence | 365-day rolling | # of Forms 4, 8-K, 10-Q, 10-K, S-8, proxy filings per 30-day window |
| 2 | Corporate press-release cadence | 365-day rolling | # of press releases per 30-day window (from issuer IR page RSS where available, otherwise Business Wire / GlobeNewswire / PR Newswire cross-index) |
| 3 | Insider transaction cadence | 730-day rolling | # of Form 4 transactions per 90-day window; separate baseline for open-market purchases vs. sells vs. plan-based |
| 4 | News/social mention volume | 180-day rolling | # of GDELT news mentions + public Reddit/StockTwits posts per 7-day window (daily scrape, weekly aggregate) |
| 5 | Conference/investor-event presence | 365-day rolling | # of scheduled investor conference appearances, earnings calls with Q&A, sell-side-hosted events per rolling quarter |
| 6 | Analyst-note cadence (proxy) | 365-day rolling | # of published analyst ratings changes per rolling 60-day window (via Finviz / Yahoo! Finance public analyst-activity pages) |

Full operational definitions in `SILENCE_DIMENSIONS.md`.

## Sub-goals (v1)

1. **Baseline infrastructure** — build and populate per-issuer baselines across all six dimensions for the Russell 1000 universe. (Phase 1)
2. **Anomaly detection engine** — implement z-score and one-sided p-value computation with seasonal adjustment (quarterly-close effect, holiday effect, earnings-season effect) per dimension. (Phase 2)
3. **Single-dimension signal emission** — first live signals emitted against a single dimension, validated against 90 days of historical data before enabling operational cadence. (Phase 3)
4. **Multi-dimension convergence engine** — identify issuers simultaneously silent across ≥ 2 dimensions; emit higher-confidence signals. (Phase 4)
5. **Scoring and candidate pipeline** — adapt 7-dim rubric for probabilistic signals; produce first candidates. (Phase 5)
6. **Reporting layer** — daily performance report + deep-dive briefs for 28+ candidates. (Phase 6)
7. **Autonomous operation validation** — 14 consecutive days unattended operation (longer than litigation tool's 7 days because silence signals have longer feedback cycles). (Phase 7)

## Success criteria

- [ ] Russell 1000 universe defined, cached, refreshed quarterly.
- [ ] All six dimensions have live baseline data populated for every in-universe issuer with ≥ 18 months of history.
- [ ] False-positive rate on historical backtest < 25% (defined: a silence signal that, over the following 90 days, has no observable material development explaining the quiet).
- [ ] True-positive examples validated: at least 5 known historical restatement/investigation cases where the tool's backtest fires a silence signal ≥ 14 days before the public disclosure.
- [ ] Continuous operational run: 14 consecutive days with zero manual intervention, no lock failures, no session over 4 hours.
- [ ] At least 3 validated candidates produced in production (any score); at least 1 at 28+.
- [ ] Write-scope isolation maintained between `silence_system/` and `reporting_layer/` throughout.

## Definition of Done (v1)

Tool 4 v1 is **done** when:
1. Phase 7 succeeds (14-day autonomous run with the above criteria).
2. Backtest against at least 20 known pre-disclosure windows from 2022–2025 shows the tool firing at least 60% of the time with ≥ 14 day lead, and false-positive rate on the same period < 25%.
3. All decisions D-000 through D-0XX are in `DECISIONS.md`; no silent overrides.
4. `OPEN_QUESTIONS.md` is either empty or contains only items explicitly deferred to Phase 8+ with a written rationale.

Anything beyond v1 (additional dimensions, non-US universe, real-time streaming, cross-tool convergence analyzer) is Phase 8+ and scope-gated by Phase 7 outcomes.

## Out of scope (v1)

- **Real-time streaming**. Operational cadence is 12-hourly; silence is a slow signal and sub-hourly granularity adds cost without information.
- **Paid data sources**. Bloomberg, Refinitiv, FactSet, S&P Global — all excluded from v1 per free-sources mandate.
- **Non-US equities**. Defer to Phase 8+ after US baseline model validates.
- **Sentiment analysis on mentions**. Volume only in v1; sentiment adds model-risk without clear benefit for silence detection.
- **Cross-tool convergence writing**. Tool 4 never writes into Tool 1/2/3 folders. A future analyzer project reads all four and produces cross-tool reports.
- **Prediction of the specific undisclosed event**. The tool identifies silences; it does not predict "this silence means restatement vs. M&A vs. investigation." Deep-dive briefs enumerate alternative hypotheses but do not commit.
