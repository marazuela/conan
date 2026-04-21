# Scoring Profile 3 — Binary Catalyst (FDA, Clinical, Regulatory Decisions)

**Applies to**: FDA PDUFA dates, AdCom votes, Phase 3 pivotal readouts, PMA/510(k) decisions, EMA CHMP opinions, MHRA decisions, and non-pharma binary regulatory outcomes with a defined decision date.

**Philosophy**: The edge is in the gap between a rigorous probability estimate and the market-implied probability. Expected value, not conviction, drives action.

---

## Triage Gate

- Market cap ≥ $215M USD.
- Specific decision date or narrow window (≤ 60 days).
- Signal is novel (first occurrence or material clinical/regulatory update).
- Drug/device has published trial data or prior FDA interaction on record (no pure-speculation catalysts).
- Publicly traded on a major exchange.

---

## Scoring Dimensions

| # | Dimension | Weight | What it measures |
|---|-----------|--------|------------------|
| 1 | Approval Probability | ×2.5 | Your estimated P(approval) from clinical + regulatory signals |
| 2 | Market Mispricing | ×2.5 | Gap between your estimate and market-implied probability |
| 3 | Magnitude of Move | ×1.5 | Expected % price move on positive outcome |
| 4 | Competitive Landscape | ×1.5 | Competing approvals / pipeline |
| 5 | Catalyst Timeline | ×1 | Urgency — days to decision |
| 6 | Liquidity | ×1 | Average daily dollar volume |

**Max**: 12.5 + 12.5 + 7.5 + 7.5 + 5 + 5 = **50**

---

## Rubric Detail

### Dimension 1 — Approval Probability (×2.5)

Base rates from FDA historical data, adjusted for deal-specific evidence:

| Score | Your P(approval) Estimate |
|-------|---------------------------|
| 5 | > 80% — Priority Review + Breakthrough + strong Phase 3 + supportive AdCom OR prior FDA green-light signaling |
| 4 | 60–80% — Strong pivotal data, standard review, no AdCom concerns flagged |
| 3 | 40–60% — Mixed data or novel mechanism; outcome genuinely uncertain |
| 2 | 20–40% — Weak data, prior CRL, safety concerns |
| 1 | < 20% — Hail Mary, prior rejection, narrow-path |

Input evidence — clinical trial primary endpoint hit/miss, safety profile, AdCom vote if held, precedent label from same class, RTOR designation status, CMC issues flagged.

### Dimension 2 — Market Mispricing (×2.5)

Compute market-implied P(approval) from options IV skew around decision date OR from stock price vs. consensus upside/downside estimates. Then compare to your Dimension 1 estimate.

| Score | Gap (Your P − Market P) |
|-------|------------------------|
| 5 | > +20 percentage points (market too pessimistic) or < −20 (market too optimistic, short setup) |
| 4 | ±10–20 pp |
| 3 | ±5–10 pp |
| 2 | ±2–5 pp |
| 1 | < ±2 pp (fairly priced) |

Direction of gap determines long/short thesis. Absolute gap size drives score.

### Dimension 3 — Magnitude of Move (×1.5)

| Score | Expected % Move on Positive Outcome |
|-------|-------------------------------------|
| 5 | > 50% |
| 4 | 30–50% |
| 3 | 15–30% |
| 2 | 5–15% |
| 1 | < 5% |

Anchor via: (a) implied move from straddle pricing, (b) sell-side base-bull-bear targets, (c) comparable historical moves in the same therapeutic area.

### Dimension 4 — Competitive Landscape (×1.5)

| Score | Competitive Profile |
|-------|---------------------|
| 5 | First-in-class, no direct competition, strong IP |
| 4 | Best-in-class candidate, 1 prior approval in class |
| 3 | 2–3 approved competitors, differentiated profile |
| 2 | Crowded class, me-too profile |
| 1 | Heavily crowded, weak differentiation, pricing pressure likely |

### Dimension 5 — Catalyst Timeline (×1)

| Score | Days to Decision |
|-------|------------------|
| 5 | ≤ 14 days |
| 4 | 15–30 days |
| 3 | 31–60 days |
| 2 | 61–90 days |
| 1 | > 90 days |

### Dimension 6 — Liquidity (×1)

Same scale as Profile 1.

---

## Thresholds

| Band | Score | Action |
|------|-------|--------|
| Immediate | 35+ | Full dossier, daily kill-sweep monitoring from T-14 |
| Watchlist | 25–34 | Weekly review |
| Archive | 15–24 | Log only |
| Discard | < 15 | Drop |

**Auto-cap rule — Expected Value**:
Compute EV = (P_approval × upside_pct) − (P_rejection × downside_pct). If EV < 5%, auto-cap at **Watchlist** regardless of other scores. Positive EV is necessary but not sufficient — it's the floor below which no position makes sense.

Example (AXSM S68 snapshot):
- P_approval = 65% (Priority Review + positive Phase 3)
- Upside = +40% (consensus target)
- P_rejection = 35%
- Downside = −25%
- EV = 0.65 × 40 − 0.35 × 25 = 26 − 8.75 = **+17.25%** → passes EV gate

---

## Key Judgment Notes

- Accurately scoring this profile requires reading the primary clinical publication or FDA briefing docs, NOT a press release summary. Press releases cherry-pick.
- AdCom votes are heavy Bayesian evidence but not deterministic. A 6-6 tie historically converts to approval ~40%; a 10-2 positive converts ~90%.
- 10b5-1 plan affirmations within the catalyst window are routine, NOT a signal — confirm via `<aff10b5One>` XML tag on Form 4 filings, as per operational protocol.
- Insider sales near decision date without 10b5-1 affirmation → red flag, subtract 1 from Approval Probability.
