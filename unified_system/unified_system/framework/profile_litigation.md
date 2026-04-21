# Scoring Profile 5 — Litigation / Legal Events

**Applies to**: CourtListener federal civil cases (class actions, securities suits, major contract disputes), SEC enforcement actions (litigation releases, settled administrative actions), DOJ/FTC antitrust, ITC 337 investigations, Delaware Chancery fiduciary-duty cases, PTAB IPR decisions.

**Philosophy**: Legal outcomes move stocks when (a) financial exposure is material to enterprise value, (b) the resolution path has a clear timeline, and (c) the market hasn't already priced in the outcome. Party resolution is the hardest part — most false positives come from matching to the wrong entity.

---

## Triage Gate

- Market cap ≥ $215M USD at signal date.
- Party resolution confidence ≥ 0.85 (exact CIK match or fuzzy-match with corroborating fields).
- Signal is novel (first filing in 30-day dedup window OR material stage change — motion to dismiss ruling, settlement, summary judgment).
- Publicly traded party (or direct subsidiary of a publicly traded parent, validated via Exhibit 21).

---

## Scoring Dimensions

| # | Dimension | Weight | What it measures |
|---|-----------|--------|------------------|
| 1 | Financial Materiality | ×3 | Damages/exposure as % of enterprise value |
| 2 | Legal Outcome Probability | ×2 | P(adverse outcome) based on stage + precedent |
| 3 | Market Pricing | ×2 | Has the stock already moved on this litigation? |
| 4 | Resolution Timeline | ×1.5 | How fast the case resolves |
| 5 | Liquidity | ×1 | Average daily dollar volume |
| 6 | Party Resolution Confidence | ×0.5 | Confidence in the entity match |

**Max**: 15 + 10 + 10 + 7.5 + 5 + 2.5 = **50**

---

## Rubric Detail

### Dimension 1 — Financial Materiality (×3)

| Score | Damages / Exposure vs. Enterprise Value |
|-------|-----------------------------------------|
| 5 | > 20% of EV |
| 4 | 10–20% |
| 3 | 5–10% |
| 2 | 2–5% |
| 1 | < 2% |

Use the claimed damages as UPPER bound. For class actions, use class-action reserve analogs in same industry. For SEC enforcement, look at disgorgement + civil penalty scales (recent median ~$50M for issuer-side cases).

### Dimension 2 — Legal Outcome Probability (×2)

| Score | Case Profile | Example |
|-------|--------------|---------|
| 5 | Near-certain adverse outcome | Consent decree signed; settled administrative action with admission |
| 4 | Strong adverse indicator | Motion to dismiss denied + discovery produced damning internal docs |
| 3 | Uncertain | Case past MTD but pre-summary-judgment; competing precedent |
| 2 | Weak case | Early stage, thin complaint, favorable jurisdiction for defendant |
| 1 | Speculative | Complaint just filed, routine pleading, no underlying event |

### Dimension 3 — Market Pricing (×2)

| Score | Prior Price Response |
|-------|----------------------|
| 5 | No material stock move on this litigation; market unaware |
| 4 | Mild move (< 5% over case history); likely underpriced |
| 3 | Moderate move (5–15%); partially priced |
| 2 | Major move (15–30%); largely priced |
| 1 | Fully priced or over-priced (> 30% move) |

Measure by comparing price vs. 30-day pre-filing VWAP, adjusted for sector moves.

### Dimension 4 — Resolution Timeline (×1.5)

| Score | Expected Resolution |
|-------|---------------------|
| 5 | ≤ 1 month (settled administrative action, TRO hearing) |
| 4 | 1–3 months (motion to dismiss pending, settlement talks) |
| 3 | 3–6 months (summary judgment, bench trial) |
| 2 | 6–12 months (jury trial scheduled) |
| 1 | > 12 months (discovery ongoing, appeal possible) |

### Dimension 5 — Liquidity (×1)

Same scale as Profile 1.

### Dimension 6 — Party Resolution Confidence (×0.5)

| Score | Match Quality |
|-------|---------------|
| 5 | Exact CIK match via EDGAR, confirmed via address / EIN / officer names |
| 4 | Exact name match + same state of incorporation |
| 3 | Fuzzy match ≥ 0.92 |
| 2 | Fuzzy match 0.85–0.92 |
| 1 | Fuzzy match < 0.85 — **signal dropped** |

---

## Thresholds

| Band | Score | Action |
|------|-------|--------|
| Immediate | 35+ | Full dossier, read primary court docs |
| Watchlist | 25–34 | Track docket, re-score on next filing |
| Archive | 15–24 | Log only |
| Discard | < 15 | Drop |

**Auto-cap rule — Party confidence**:
If Party Resolution Confidence < 3 (i.e., fuzzy match < 0.92), auto-cap at **Archive**. Never promote a candidate when the entity match is uncertain. A wrong-party candidate is worse than no candidate — it contaminates the signal log.

---

## Key Judgment Notes

- Subsidiary liability: If the litigation names a subsidiary, check Exhibit 21 map (`baselines/exhibit21_subsidiary_table.json`) to link to public parent. Materiality is computed against PARENT EV.
- Class actions: The filing itself is NOT a signal — most securities class actions settle for a fraction of alleged damages. The signal is either (a) motion-to-dismiss denied + discovery producing damning documents, or (b) lead plaintiff appointment with a tier-1 firm (Robbins Geller, Labaton, Bernstein).
- SEC enforcement releases name the respondent clearly. Always cross-check the respondent CIK via EDGAR before scoring.
- Delaware Chancery fiduciary-duty cases are especially value-relevant when challenging a controlling-shareholder take-private — "entire fairness" review often leads to price bumps.
