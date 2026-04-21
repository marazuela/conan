# Scoring Profile 2 — Activist / Governance Events

**Applies to**: SC 13D filings, PREN14A/DEFC14A (contested proxy), poison-pill adoptions (rights plans), board disputes, cooperation agreements, standstill breaches, major shareholder-change filings (non-merger), Rule 2.4 "possible offer" announcements (LSE), Article 324 filings (TDnet), SEDAR+ major shareholder reports.

**Philosophy**: Governance disputes resolve through catalysts (board settlement, proxy vote, cooperation agreement, tender offer). The edge is recognizing a credible campaign before the market prices in the terminal outcome.

---

## Triage Gate

Same as Profile 1 (market cap ≥ $215M USD, public exchange, novelty, freshness, translation confidence ≥ 0.70).

**Additional gate**: Filer must be identifiable. "Unknown fund" = signal dropped.

---

## Scoring Dimensions

| # | Dimension | Weight | What it measures |
|---|-----------|--------|------------------|
| 1 | Signal Strength | ×2 | Strength of causal link to price movement |
| 2 | Information Asymmetry | ×2 | How few market participants are aware |
| 3 | Activist Track Record | ×1.5 | History of successful campaigns by this filer |
| 4 | Risk/Reward | ×1.5 | Asymmetry of upside vs. downside |
| 5 | Catalyst Clarity | ×1 | How bounded the timeline is |
| 6 | Edge Decay | ×1 | How long the informational advantage persists |
| 7 | Liquidity | ×1 | Average daily dollar volume |

**Max**: 10 + 10 + 7.5 + 7.5 + 5 + 5 + 5 = **50**

---

## Rubric Detail

### Dimension 1 — Signal Strength (×2)

| Score | Criteria | Example |
|-------|----------|---------|
| 5 | Direct, mechanistic — signal IS the event | 13D filed with stated intent to replace board; poison pill adopted in direct response to known bidder |
| 4 | Strong causal link, reliable precedent | Board refresh under cooperation agreement with seat granted |
| 3 | Moderate link, multiple plausible interpretations | 13D filed with "discussions with management" language, no stated plan |
| 2 | Weak link, mostly defensive boilerplate | Standard 13G conversion to 13D without stated intent |
| 1 | Speculative — requires layered assumptions | Passive-to-active filing by a non-campaigner |

### Dimension 2 — Information Asymmetry (×2)

| Score | Distribution | Example |
|-------|--------------|---------|
| 5 | Filing just hit; no news coverage yet; not on Bloomberg/Reuters tickers | Today's RNS/EDGAR filing within the hour |
| 4 | Filed today; specialist press might cover in hours | Same-day filing, some scrolling ticker mention |
| 3 | Filed 1–3 days ago, limited coverage | Small-cap ignored by mainstream press |
| 2 | Filed within a week; broad coverage | Already on every news service |
| 1 | Stale; fully priced; market-wide awareness | Campaign in progress for months |

### Dimension 3 — Activist Track Record (×1.5)

| Score | Filer Profile | Examples |
|-------|---------------|----------|
| 5 | Tier-1 activist with >10 proven campaigns | Elliott, Icahn, Starboard, ValueAct, Trian |
| 4 | Established activist with 3+ successful campaigns | Jana, Engaged Capital, Blue Harbour |
| 3 | Newer or niche, 1–2 known campaigns | Sector-specialist hedge fund |
| 2 | First-time 13D filer, credible background | Experienced PM at a new fund |
| 1 | Unknown filer, no campaign history | First-ever 13D, opaque LP structure |

### Dimension 4 — Risk/Reward (×1.5)

| Score | Asymmetry |
|-------|-----------|
| 5 | 3:1 or better — clear upside path, limited downside (e.g., activist has cost basis near floor) |
| 4 | 2:1 — plausible upside with known support |
| 3 | 1.5:1 — evenly matched |
| 2 | 1:1 — equal upside and downside risk |
| 1 | Downside heavier than upside |

### Dimension 5 — Catalyst Clarity (×1)

| Score | Timeline Boundedness |
|-------|----------------------|
| 5 | Specific date (annual meeting, poison pill expiry, deadline to nominate) |
| 4 | Narrow window (next 30–90 days) |
| 3 | Known process, fuzzy timing (Q2 2026) |
| 2 | Undefined timeline |
| 1 | No catalyst — only structural thesis |

### Dimension 6 — Edge Decay (×1)

| Score | How Fast Advantage Erodes |
|-------|---------------------------|
| 5 | Advantage lasts weeks+ (complex campaign, ongoing disclosure) |
| 4 | Advantage lasts ~5 days |
| 3 | Advantage lasts 1–3 days |
| 2 | Advantage lasts < 1 day |
| 1 | Advantage lasts hours or less |

### Dimension 7 — Liquidity (×1)

Same scale as Profile 1.

---

## Thresholds

| Band | Score | Action |
|------|-------|--------|
| Immediate | 35+ | Full deep-dive within 24h |
| Watchlist | 25–34 | Re-score on next material filing |
| Archive | 15–24 | Log with reason |
| Discard | < 15 | Drop |

---

## Key Judgment Notes

- Ownership percentage is NOT a scoring dimension directly — it informs Signal Strength (5% = standard 13D trigger; 10%+ = material stake; 15%+ = near-controlling).
- "Cooperation agreement" signals — look at whether settlement terms include board seats (higher signal) vs. only strategic review (moderate) vs. only standstill (low).
- Multi-holder simultaneous filings (e.g., two 13Ds same week from independent activists) → boost Signal Strength by +1 and flag for convergence with Profile 4 (Short Positioning) for crowding analysis.
