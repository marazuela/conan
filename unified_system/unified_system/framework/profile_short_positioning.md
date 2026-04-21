# Scoring Profile 4 — Short Positioning / Flow Signals

**Applies to**: ESMA short disclosures (FCA, AMF, AFM, BaFin, CNMV, CONSOB), SEC Form 4 insider transactions (once scanner is built), institutional short-interest data, crowded-short registrations.

**Philosophy**: Positioning data is metadata about conviction — who is putting capital behind a view. The signal is strongest when multiple independent holders converge on the same name, and strongest when that convergence is CHANGING (recently built up, not steady-state).

---

## Triage Gate

- Market cap ≥ $215M USD.
- Publicly traded on a major exchange.
- Novelty: new disclosure OR material change (new holder added, crossing a 0.5% threshold, threshold crossing a 5% trigger in some jurisdictions).
- Translation confidence ≥ 0.70 for non-English filings.

---

## Scoring Dimensions

| # | Dimension | Weight | What it measures |
|---|-----------|--------|------------------|
| 1 | Crowding Intensity | ×2.5 | Number of independent holders / insiders |
| 2 | Trend Direction | ×2 | Building vs. unwinding |
| 3 | Catalyst Proximity | ×2 | Upcoming event that could force covering or validate thesis |
| 4 | Position Size vs. Float | ×1.5 | Aggregate position as % of float |
| 5 | Historical Analog | ×1 | Precedent-match with similar profiles |
| 6 | Liquidity | ×1 | Average daily dollar volume |

**Max**: 12.5 + 10 + 10 + 7.5 + 5 + 5 = **50**

---

## Rubric Detail

### Dimension 1 — Crowding Intensity (×2.5)

For ESMA short disclosures:

| Score | Independent Short-Sellers |
|-------|---------------------------|
| 5 | ≥ 6 unique holders OR 4+ holders in same 30-day window |
| 4 | 4–5 unique holders |
| 3 | 3 unique holders |
| 2 | 2 unique holders |
| 1 | 1 unique holder |

For insider Form 4 clusters:

| Score | Insider Cluster |
|-------|-----------------|
| 5 | 3+ C-suite sellers in same 30-day window |
| 4 | 2 C-suite sellers + 1+ VP |
| 3 | Cluster of VPs/directors, no C-suite |
| 2 | 1–2 minor insiders |
| 1 | Single minor insider |

**Important**: Affiliated funds (e.g., Citadel Capital / Citadel Advisors / Citadel Americas) count as ONE holder, not three.

### Dimension 2 — Trend Direction (×2)

| Score | Trajectory | Example |
|-------|------------|---------|
| 5 | Rapid new buildup — ≥ 3 new positions opened in last 7 days; aggregate position increased > 50% in 30 days |
| 4 | Steady buildup — new positions outpacing closures |
| 3 | Stable — roughly flat aggregate position |
| 2 | Slow unwinding — closures outpacing new positions |
| 1 | Rapid unwinding — ≥ 3 positions closed in last 7 days; aggregate fell > 50% in 30 days |

This is the dimension that requires **historical tracking** — scanner must persist daily snapshots in `esma_snapshots/` to compute trend.

### Dimension 3 — Catalyst Proximity (×2)

| Score | Upcoming Event | Example |
|-------|----------------|---------|
| 5 | Catalyst ≤ 14 days | Earnings next week, trial readout, regulatory decision |
| 4 | Catalyst 15–30 days | Investor day, court hearing |
| 3 | Catalyst 31–90 days | Quarterly earnings, product launch |
| 2 | Visible catalyst > 90 days | Major debt maturity, planned refinancing |
| 1 | No visible catalyst | Steady-state short, structural thesis only |

This dimension is what separates "crowded shorts approaching a catalyst" (potentially explosive) from "steady-state shorts in a declining business" (not actionable).

### Dimension 4 — Position Size vs. Float (×1.5)

| Score | Aggregate Position as % of Float |
|-------|----------------------------------|
| 5 | > 10% |
| 4 | 5–10% |
| 3 | 2–5% |
| 2 | 1–2% |
| 1 | < 1% |

### Dimension 5 — Historical Analog (×1)

Is there a strong precedent for this pattern?

| Score | Precedent Strength |
|-------|-------------------|
| 5 | Strong historical pattern — similar crowding into similar catalyst repeatedly produced 20%+ moves in same direction |
| 4 | Clear analogs exist |
| 3 | Some precedent, mixed outcomes |
| 2 | Weak or noisy precedent |
| 1 | No relevant precedent — first-of-kind setup |

### Dimension 6 — Liquidity (×1)

Same scale as Profile 1.

---

## Thresholds

| Band | Score | Action |
|------|-------|--------|
| Immediate | 35+ | Full deep-dive within 24h |
| Watchlist | 25–34 | Re-score weekly or on disclosure updates |
| Archive | 15–24 | Log only |
| Discard | < 15 | Drop |

**Multi-regulator boost**: If the same name has independent short disclosures across 2+ national regulators (e.g., FCA + AMF on a cross-listed name), add +1 to Crowding Intensity.

---

## Key Judgment Notes

- Disclosed short positions lag actual positions (EU thresholds trigger at 0.5%, 0.6%, 0.7%... so a holder disclosed at 0.5% could actually be at 0.59%).
- For dual-listed names, aggregate short positions from ALL listing regulators into one convergence entity (use `issuer_figi`).
- A **declining** aggregate short position on a name with approaching positive catalyst is a LONG signal (shorts covering into good news). Invert thesis direction in this case.
- For insider Form 4 clusters, distinguish routine 10b5-1 plan sales from discretionary sales. Route planned sales to noise, discretionary to signal.
