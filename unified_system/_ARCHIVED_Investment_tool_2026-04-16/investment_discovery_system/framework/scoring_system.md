# Signal Scoring System

## Overview

Every signal passes through a three-stage pipeline: Triage → Scoring → Deep Dive. Only signals that survive all three stages become actionable candidates.

---

## Stage 1: Signal Triage (Automated Gate)

Signals must pass ALL triage checks before scoring. Failures are logged but discarded.

| Check | Threshold | Rationale |
|-------|-----------|-----------|
| Publicly traded | Must be listed on a major exchange (NYSE, NASDAQ, LSE, Euronext, XETRA, BME, etc.) | OTC pinks and unlisted companies are untradeable at size |
| Market cap | ≥ $215M (€200M) | Below this, liquidity risk dominates signal quality |
| Signal novelty | First occurrence in dedup window, or material escalation of prior signal | Recurring boilerplate language (e.g., routine "going concern" in annual filings of known-distressed companies) is noise |
| Data freshness | Signal source date within strategy scan window | Stale data that slipped through date filters |

---

## Stage 2: Opportunity Scoring (7 Dimensions)

Signals that pass triage are scored on seven dimensions, each rated 1–5.

### Dimension 1: Signal Strength (Weight: ×2)

How strong is the causal link between this signal and future price movement?

| Score | Criteria | Example |
|-------|----------|---------|
| 5 | Direct, mechanistic link — signal IS the event | Company files for Chapter 11; FDA approval letter published |
| 4 | Strong causal link — signal reliably precedes the event | Director forms acquisition vehicle + shelf registration filed |
| 3 | Moderate link — consistent with thesis but could have other explanations | Unusual hiring surge in new geography |
| 2 | Weak link — suggestive but ambiguous | Single insider purchase at modest size |
| 1 | Speculative — requires multiple assumptions to connect to price | Change in website copy |

### Dimension 2: Catalyst Clarity (Weight: ×1)

How clear and bounded is the timeline for price realization?

| Score | Criteria | Example |
|-------|----------|---------|
| 5 | Specific date known | FDA PDUFA date, trial verdict date, shareholder vote |
| 4 | Window known — catalyst within defined period | Activist 13D filed, must make offer within 10 days |
| 3 | Approximate timeline — likely within quarter | Regulatory comment period closing, likely final rule Q3 |
| 2 | Vague timing — sometime this year | New legislation introduced, uncertain committee schedule |
| 1 | No visible catalyst — depends on "market will eventually notice" | Undervaluation based on hidden asset |

### Dimension 3: Information Asymmetry (Weight: ×1.5)

How few market participants are likely aware of this signal?

| Score | Criteria | Example |
|-------|----------|---------|
| 5 | Obscure, foreign-language, or format-hostile source | Short position disclosure buried on national regulator site in local language |
| 4 | Public but requires significant effort to find/parse | Buried in 200-page proxy filing appendix |
| 3 | Accessible but not widely monitored | SAM.gov contract award for mid-cap IT contractor |
| 2 | Monitored but by limited audience | SEC 8-K filed after hours on Friday |
| 1 | Widely known — terminal alert, headline news | Major earnings miss covered by all wire services |

### Dimension 4: Risk/Reward Profile (Weight: ×1)

How asymmetric is the potential payoff?

| Score | Criteria | Example |
|-------|----------|---------|
| 5 | 5:1+ reward/risk — limited downside, convex upside | Distressed company near litigation resolution, equity near zero but option value high |
| 4 | 3:1–5:1 — clear asymmetry | Activist accumulation in undervalued mid-cap with identifiable catalysts |
| 3 | 2:1–3:1 — favorable but not extreme | Regulatory approval likely but partially priced |
| 2 | 1:1–2:1 — roughly symmetric | Event-driven with balanced outcome probabilities |
| 1 | <1:1 — risk exceeds reward or downside is unbounded | Short thesis in momentum stock with no catalyst |

### Dimension 5: Edge Decay Rate (Weight: ×1)

How long does the informational advantage persist?

| Score | Criteria | Example |
|-------|----------|---------|
| 5 | Weeks to months — slow information diffusion | Obscure foreign-language registry filing |
| 4 | Days to weeks — edge persists through a news cycle | Court filing that won't be covered by media for days |
| 3 | Days — edge decays within a few trading sessions | Friday after-hours SEC filing |
| 2 | Hours — edge decays intraday | Bloomberg headline, but with nuance missed |
| 1 | Minutes or already priced — no actionable edge | Wire service alert already disseminated |

### Dimension 6: Liquidity & Tradeability (Weight: ×1)

Can you realistically enter and exit a position without the trade itself distorting the price?

| Score | Criteria | Example |
|-------|----------|---------|
| 5 | Highly liquid — >$20M avg daily volume, tight spreads, deep order book | Large-cap with active options market |
| 4 | Good liquidity — $5-20M avg daily volume | Mid-cap with institutional following |
| 3 | Adequate — $1-5M avg daily volume, manageable spreads | Smaller mid-cap, position sizing constrained |
| 2 | Thin — $250K-1M daily volume, wide spreads | Small-cap, can only trade in small lots over multiple days |
| 1 | Illiquid — <$250K daily volume or no options | Micro-cap, position entry/exit is itself a risk |

### Dimension 7: Catalyst Timeline (Weight: ×1)

When is the price-moving event expected? This determines urgency, not quality.

| Score | Criteria | Example |
|-------|----------|---------|
| 5 | Within 1-2 weeks — immediate action required | PDUFA date next week; AdCom vote in 3 days |
| 4 | Within 1 month — high urgency | Shareholder vote scheduled; contract award period closing |
| 3 | Within 1 quarter — moderate urgency | Regulatory review period; legislative session timeline |
| 2 | Within 6 months — low urgency, monitor | Early-stage activist campaign; pipeline drug in late trials |
| 1 | 6+ months or unknown — watchlist only | Structural thesis with no near-term trigger |

---

## Composite Scoring

### Formula
```
Composite = (Signal Strength × 2) + Catalyst Clarity + (Info Asymmetry × 1.5) + Risk/Reward + Edge Decay + Liquidity + Catalyst Timeline
```

Maximum possible: 10 + 5 + 7.5 + 5 + 5 + 5 + 5 = **42.5**

### Multi-Strategy Convergence Bonus
When signals from multiple strategies point to the same entity (resolved via OpenFIGI):
- 2 strategies converging: **+4 bonus**
- 3+ strategies converging: **+8 bonus**

### Score Thresholds

| Score | Action |
|-------|--------|
| **28+** | **Immediate candidate** — full deep dive, candidate writeup, position structuring |
| **22–27** | **Watchlist** — monitor for confirmation, begin preliminary research |
| **14–21** | **Archive** — log signal, check periodically for developments |
| **<14** | **Discard** — noise, or edge already decayed |

---

## Stage 3: Deep Dive Analysis (Strategy-Specific + Web Research)

Candidates scoring 28+ get a full writeup. Watchlist candidates (22-27) get a condensed version. Deep dive layers vary by source strategy — see individual strategy specs in `/strategies/` for the specific analysis checklist per strategy.

Common elements across ALL deep dives:
- Filing/source text review (read the actual document, not just the keyword match)
- Company context (market cap, sector, analyst coverage count, recent price action)
- Thesis statement (2-3 paragraphs: what the signal means, what the market is missing, what would change the stock price)
- **Web research layer** (mandatory — see below)
- Kill conditions (explicit, measurable conditions that invalidate the thesis)
- Catalyst map (what event, what date or window, what triggers position entry/exit)
- Source links (every claim traceable to a URL)

### Web Research Layer (Mandatory for All Candidates)

After the strategy-specific analysis, conduct a structured web research sweep using WebSearch and WebFetch. This layer validates and enriches the thesis with information our structured data sources cannot capture. Full template in `framework/candidate_template.md`.

**Research checklist:**
1. **Recent news** (last 30 days) — earnings, press releases, M&A rumors, management changes, product launches, lawsuits
2. **Market narrative** — what does consensus believe, and how does our thesis differ? If there's no gap, there's no edge.
3. **Analyst activity** — upgrades, downgrades, price target changes, coverage initiations. Low/no coverage = higher info asymmetry.
4. **Litigation & regulatory** — pending lawsuits, investigations, warning letters, sanctions. These can be kill conditions.
5. **Social & alternative sentiment** — unusual retail attention, short squeeze dynamics, viral narratives that change the risk profile.
6. **Web research verdict** — does the research strengthen, weaken, or leave the thesis neutral? If it reveals a kill condition, flag immediately.

**Rules:**
- Every finding must include a source URL and date.
- Distinguish verified reporting from speculation or opinion. Label each.
- Never reproduce copyrighted article content — summarize in your own words.
- If web research reveals information that contradicts the thesis, this may trigger a kill condition or score downgrade. Do not ignore inconvenient findings.

---

## Scoring Worked Example

**Signal**: CNMV filing shows a major hedge fund has increased its net short position in a Spanish mid-cap utility from 0.6% to 1.2% over three weeks. Simultaneously, an EDGAR 8-K from the same company's US-listed parent mentions "strategic alternatives."

| Dimension | Score | Rationale |
|-----------|-------|-----------|
| Signal Strength | 4 | Short position doubling + "strategic alternatives" is a strong convergent pattern |
| Catalyst Clarity | 3 | Strategic review announced but no timeline for outcome |
| Info Asymmetry | 4 | Short position data from CNMV; few international investors check Spanish regulator |
| Risk/Reward | 4 | Stock trades at 30% discount to peers; M&A would re-rate significantly |
| Edge Decay | 4 | Few analysts cover this name; CNMV filing in Spanish only |
| Liquidity | 3 | €3M avg daily volume on BME; adequate for satellite position |
| Catalyst Timeline | 3 | Strategic review typically resolves within 1-2 quarters |

**Base composite**: (4×2) + 3 + (4×1.5) + 4 + 4 + 3 + 3 = 8 + 3 + 6 + 4 + 4 + 3 + 3 = **31**
**Convergence bonus**: +4 (2 strategies: ESMA + EDGAR)
**Final score**: **35**

**Action**: Immediate candidate — full deep dive with cross-strategy analysis.
