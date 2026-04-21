# DIMENSIONS — Six Observable Silence Dimensions (Tool 4)

Each dimension below is designed to be extracted into its own file in `dimensions/<name>.md` in the working project. Structure is uniform so scanner implementations follow a common scaffold.

For each dimension, during Phase 1 the session must decide:
- Exact endpoint schema (live-probe before coding).
- Baseline window (default below; adjust only with a D-0XX decision).
- Seasonal adjustment rules (default below; re-fit during warm-up validation).
- Wall-clock budget per scan pass (default 60s per dimension; adjust with per-dim decision).

---

## Dimension 1 — EDGAR Filing Cadence

### What is measured

Count of SEC filings by filing type and 30-day rolling window. Separate sub-counters for:
- Form 4 (insider transactions) — tracked as its own dimension (D3) but also counted here as a coarse activity proxy.
- Form 8-K (material events) — primary silence indicator; 8-K suppression is directly caused by Reg FD compliance during review periods.
- Form 10-Q / 10-K (periodic reports) — expected on fixed schedules; silence here means delayed filing or NT-10 flagging.
- Form S-8 / S-3 / S-4 (registration statements).
- Form DEF 14A (proxy).
- Form SC 13D/G (beneficial ownership).

### Why silence here is informative

8-K filings are the closest thing to a corporate "heartbeat." Issuers going multiple weeks without any 8-K when their baseline is 2+/month are either experiencing nothing material (possible) or actively suppressing disclosure (informative). NT-10 filings (notification of late filing) are themselves direct silence signals with their own semantics.

### Baseline model

- Window: 365-day rolling.
- Aggregation unit: counts per 30-day rolling window.
- Seasonal adjustments:
  - Quarter-end multiplier: +80% (Apr/May, Jul/Aug, Oct/Nov, Jan/Feb reflect 10-Q/K surges).
  - Summer multiplier: -15% (late Jul–early Sep).
  - December multiplier: -20% (holiday period).
- Minimum observations for eligibility: ≥ 12 months of coverage, ≥ 20 total filings in baseline window.

### Anomaly detection

- Compute observed 30-day count; subtract expected (seasonally adjusted mean); divide by stddev → z-score.
- One-sided p-value: P(X ≤ observed | baseline).
- Silence signal if z ≤ -2 (approximately p ≤ 0.025).
- Strong silence signal if z ≤ -3 (p ≤ 0.0013).

### Edge cases

- Issuer recently completed 10-K filing → expected 30-day count is elevated; silence threshold is much higher (a 0-filings 30-day window post-10-K is *not* anomalous).
- Issuer going through restatement → NT-10 / 10-K/A filings may appear — these are themselves anti-silence signals, correctly handled by counting them as filings.

### Scanner responsibilities

- Pull CIK filing list from EDGAR `data.sec.gov/submissions/CIK##########.json` (same endpoint Tool 1 uses, reused).
- Parse timestamps, filing types; compute 30-day rolling count.
- Store observation in per-issuer baseline JSON under `baselines.edgar_filing_cadence.observations`.
- Compute z-score and p-value; emit signal if anomalous.

---

## Dimension 2 — Corporate Press-Release Cadence

### What is measured

Count of corporate press releases (self-issued, not third-party news) per 30-day rolling window. Sources:
- Business Wire per-company archive pages.
- GlobeNewswire per-company archive pages.
- PR Newswire per-company archive pages.
- Issuer IR page RSS feeds (where published).

Dedup on title+date across sources (a single release often cross-posts).

### Why silence here is informative

Corporate communications teams have steady cadences. A PR team that normally issues 6–10 releases per month going silent for 45 days is almost always caused by internal review: M&A talks, restatement investigation, executive transition, legal/regulatory action under review. Reg FD doesn't directly compel silence, but practical caution does.

### Baseline model

- Window: 365-day rolling.
- Aggregation: 30-day rolling count.
- Seasonal adjustments:
  - Summer multiplier: -25%.
  - December multiplier: -35% (last two weeks near-zero across the universe).
  - Quarter-end multiplier: +20% (earnings releases clustered).
- Minimum observations: ≥ 24 releases over 12 months.

### Anomaly detection

Same z-score/p-value approach. Threshold z ≤ -2.

### Edge cases

- Issuers with very low PR cadence (<= 12/year) cannot produce reliable silence signals; exclude from this dimension.
- Spin-offs and corporate events reshape baselines — issuers with a major corporate event in the last 180 days are flagged "baseline rebuilding" and their signals from this dimension are downweighted.

---

## Dimension 3 — Insider Transaction Cadence

### What is measured

Count of Form 4 filings per 90-day rolling window. Separate baselines for:
- Any Form 4 (total insider activity).
- Non-plan open-market purchases (CEO/CFO directly buying).
- Non-plan open-market sales.
- Plan-based (Rule 10b5-1) transactions.

The 90-day window (longer than other dimensions) reflects that insider transactions are sparser than corporate releases.

### Why silence here is informative

Insiders going silent — particularly an abrupt stop of previously-regular plan-based or open-market transactions — is a classical signal of a blackout period triggered by undisclosed material information. Rule 10b5-1 plan suspensions are particularly telling; a plan was designed to trade automatically, and it stopped.

### Baseline model

- Window: 730-day rolling (2 years) — needed for sparse signal.
- Aggregation: 90-day rolling count.
- Seasonal adjustments: none (insider activity does not cluster seasonally beyond earnings-window blackouts, which are themselves excluded from the "silent" attribution via the earnings-season exclusion rule below).
- Minimum observations: ≥ 8 transactions over 24 months.

### Anomaly detection

Same z-score approach. Separate signal for total count and for "plan suspension suspected" (when plan-based transactions stop abruptly).

### Edge cases

- **Earnings-window blackout** — every issuer has a pre-earnings blackout window (typically 14 days before earnings through 2 days after). Insider silence during blackout is expected and must be excluded. Scanner consults earnings-calendar cache and masks blackout windows before computing z-scores.
- **Plan termination vs. suspension** — plan termination (Form 4 footnote) is distinct from plan suspension (no filing — inferred from absence of expected Form 4s on the plan's schedule). Termination is an event signal; suspension is a silence signal. Plan schedules are parsed from prior Form 4 footnotes where disclosed.

---

## Dimension 4 — News/Social Mention Volume

### What is measured

Combined daily count of:
- GDELT news article mentions (GDELT DOC 2.0 API, ticker/name query, de-duplicated).
- Public Reddit posts mentioning the ticker or issuer name (subreddit union: wallstreetbets, stocks, investing, security-specific subs where active).
- Public StockTwits posts tagged with the ticker.

Aggregated to 7-day rolling window.

### Why silence here is informative

Public attention is a noisy signal but a high-volume one. Attention silence (sharp drops in mention volume) is sometimes an artifact of information absorption — market has priced what it knows, waiting for next catalyst — and sometimes a signal that journalists and message-board participants have reduced input because the issuer stopped producing news.

### Baseline model

- Window: 180-day rolling (mention volume is more volatile; shorter baseline captures current regime).
- Aggregation: 7-day rolling count.
- Seasonal adjustments:
  - Weekend multiplier: ~-40% (Sat/Sun are naturally lower).
  - Holiday multiplier: -50% for US federal holidays.
  - Earnings-week multiplier: +200% (earnings-week mentions spike).
- Minimum observations: ≥ 90 days of active mentions (at least 10 mentions/week average).

### Anomaly detection

Same approach but with more conservative threshold (z ≤ -2.5) because mention-volume baselines are intrinsically noisier.

### Edge cases

- **Mega-cap issuers** (top 20 by market cap) have mention floors that rarely allow silence detection; the baseline stddev is too wide. Downweight signals from this dimension for mega-caps.
- **Retail-darling issuers** (heavy wallstreetbets mention volume) have baselines dominated by non-fundamental attention; silence is less informative. Flag these in the baseline metadata.

---

## Dimension 5 — Conference/Investor-Event Presence

### What is measured

Count of scheduled investor events per rolling quarter:
- Sell-side conference appearances (JPMorgan Healthcare, Goldman Sachs Communacopia, Morgan Stanley TMT, etc.).
- Issuer-hosted events (investor days, capital markets days).
- Earnings calls with Q&A.

Sources: public sell-side conference agendas (scraped during conference seasons); issuer IR event calendars; Wall Street Horizon free-tier feed if available.

### Why silence here is informative

An issuer *declining* to present at a conference they historically attend (absence from a published agenda they normally appear on) is a strong signal — conference presence is a binary opt-in decision typically made 2–4 weeks pre-conference; declining reflects management's judgment that exposure is risky.

### Baseline model

- Window: 365-day rolling, comparing same-quarter-prior-year for conference participation.
- Aggregation: per-quarter count, with conference-specific tracking (did they present at JPM Healthcare 2026 if they did in 2024 and 2025?).
- Seasonal adjustments: None; baseline is year-over-year same-quarter.
- Minimum observations: ≥ 4 conferences over 24 months.

### Anomaly detection

Conference-specific absence: if issuer presented at a given conference 2+ of the last 3 years and is absent from the current year's agenda, flag as silence signal. This is a near-binary signal (present / absent) rather than a z-score.

### Edge cases

- Sector-specific conferences (biotech, semiconductor) — Scanner must maintain per-sector conference calendars.
- Conference agendas are published at varying lead times; absence signal can only be emitted once the agenda is published and a reasonable window has passed (typically 14 days before the conference).

### Data availability caveat

This is the most sparsely-sourced dimension. If Phase 1 finds reliable data coverage is below 50% of universe, this dimension is demoted to a lower weight in the convergence scoring (still emitted, but at reduced confidence). See D-013.

---

## Dimension 6 — Analyst-Note Cadence (proxy)

### What is measured

Count of publicly-observable analyst rating changes and target-price revisions per rolling 60-day window. Sources:
- Finviz analyst-activity table.
- Yahoo! Finance analyst-recommendations page.
- Benzinga public rating-changes RSS.

The count captures *analyst engagement intensity*, not rating direction.

### Why silence here is informative

Analysts going quiet — stopping rating updates and target-price revisions — reflects either (a) no new information from the issuer to update on, or (b) analysts consciously not publishing because the issuer has gone dark. In both cases, the silence is an indirect read on issuer communicativeness.

### Baseline model

- Window: 365-day rolling.
- Aggregation: 60-day rolling count.
- Seasonal adjustments:
  - Earnings-window multiplier: +150% (analysts update post-earnings).
  - Summer multiplier: -20%.
- Minimum observations: issuer must have ≥ 3 tracked analysts and ≥ 12 rating changes in baseline window.

### Anomaly detection

Standard z-score; threshold z ≤ -2.

### Edge cases

- Small issuers with 1–2 analyst coverage produce unreliable signals; exclude from this dimension.
- Analyst rotation (coverage handoff) produces short-term silences that are procedural, not material. Flag issuers with recently-changed coverage team (detectable via first-time appearance of new analyst name).

---

## Convergence across dimensions

Single-dimension anomalies are admitted as signals but scored conservatively. The *power* of the tool is multi-dimensional convergence. The convergence engine (Phase 4) computes:

- **Combined anomaly score** = sum of |z| values across triggered dimensions, weighted by each dimension's reliability (conference dim weighted 0.7, mention-volume dim weighted 0.8, others 1.0).
- **Joint probability estimate** under independence null (a rough lower bound; dimensions are correlated, so real joint p-values are higher than the product — reported with caveat).
- **Dimensions-triggered count** (how many of the six are simultaneously anomalous).

A signal with 3+ dimensions triggered and combined anomaly score ≥ 7 is a strong candidate regardless of which specific dimensions are firing.

## Dimension prioritization for Phase 2 (first scanner)

Phase 2 builds the **EDGAR Filing Cadence** scanner first. Rationale:
1. Endpoint is highest-reliability (SEC EDGAR; already proven in Tool 1).
2. Schema is cleanest (filing types are enumerated; no NLP needed).
3. Baseline data is most reliably backfillable (full history available from EDGAR).
4. Signal semantics are best-understood (NT-10, 8-K suppression are well-documented).
5. Validates the end-to-end baseline → scanner → z-score → signal pipeline before adding more complex dimensions.

Other dimensions follow in Phase 3 (press-release, insider transactions) and Phase 5 (news/social, conference, analyst-notes). Conference dimension is last because source coverage is uncertain.
