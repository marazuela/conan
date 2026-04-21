# Scoring Profile 6 — Takeover Candidate (pre-edge)

**Applies to**: signals from `takeover_candidate_scanner` identifying small/mid-cap public companies that show the setup pattern of a likely M&A target 3–12 months *before* any deal is announced.

**Philosophy**: Pre-edge pattern recognition. The edge is identifying the setup *before* the market prices it. A candidate surfaced under this profile is a *hypothesis*, not a confirmed deal — scoring measures strength of the setup, not spread to a price.

**Distinction from `merger_arb`**: merger_arb scores *announced* deals (spread × certainty × time). This profile scores *un-announced* candidates (setup × freshness × fit).

---

## Triage Gate (before scoring)

A signal must pass ALL of these to be scored under this profile:

- Market cap ≥ $215M USD (system-wide floor).
- Publicly traded on a major exchange (NYSE, NASDAQ, or equivalent).
- No definitive merger agreement currently in effect (post-edge disqualifier per D-013).
- Company has not rejected a prior bid within the trailing 6 months (would cap to archive).
- At least 2 of the 5 setup patterns hit (see Signal Logic in `strategies/pre_edge_takeover_candidate.md`).

---

## Scoring Dimensions

| # | Dimension | Weight | What it measures |
|---|-----------|--------|------------------|
| 1 | Setup Strength | ×3 | How many of the 5 setup patterns are hit; extra weight for explicit strategic-review language. |
| 2 | Edge Freshness | ×2 | How recently the key triggering signal appeared. Brand-new disclosures score higher; known-for-months patterns score lower. |
| 3 | Valuation Cushion | ×2 | % discount to historical median EV/EBITDA or EV/Revenue vs. comparables. Larger cushion = more premium headroom for a buyer. |
| 4 | Strategic Buyer Clarity | ×2 | Can a likely acquirer be named? Named strategic with prior M&A history in the sector = max. Unknown "some PE fund" = low. |
| 5 | Liquidity | ×1 | 30-day ADV, spread, borrow availability. |

**Max score**: 15 + 10 + 10 + 10 + 5 = **50**

---

## Rubric Detail

### Dimension 1 — Setup Strength (×3)

| Score | Pattern Count / Quality |
|-------|--------------------------|
| 5 | 4–5 of 5 patterns hit, including an explicit "strategic alternatives" / "financial advisor engaged" disclosure. |
| 4 | 3 patterns hit, including either strategic-review language OR a banker mandate named in press/8-K. |
| 3 | 3 patterns hit, no strategic-review language. |
| 2 | 2 patterns hit. |
| 1 | Edge case — only 1 pattern hit but unusually strong signal (e.g., activist 13D with M&A-demand language). |

The 5 patterns: (1) PE take-private setup, (2) streamlined-for-sale pattern, (3) strategic-review disclosure, (4) insider + institutional accumulation, (5) strategic buyer fit.

### Dimension 2 — Edge Freshness (×2)

| Score | Signal Age |
|-------|-----------|
| 5 | Key signal within last 30 days; setup just "completed" (e.g., divestiture closed or CFO hired in last 30 days). |
| 4 | Key signal within 30–90 days. |
| 3 | Key signal within 3–6 months. |
| 2 | Setup has been stable for 6–12 months with no deal. |
| 1 | Setup > 12 months old — stale; consider archive. |

### Dimension 3 — Valuation Cushion (×2)

| Score | Discount to 5-yr median EV/EBITDA or EV/Revenue |
|-------|--------------------------------------------------|
| 5 | > 35% discount (compressed; rich cushion for premium). |
| 4 | 20–35% discount. |
| 3 | 5–20% discount. |
| 2 | Near median. |
| 1 | Trading above median — buyer would need synergies thesis to justify. |

If comparables unavailable, fall back to absolute EV/EBITDA vs. sector typical PE take-private multiples (9–12x EV/EBITDA).

### Dimension 4 — Strategic Buyer Clarity (×2)

| Score | Buyer Path |
|-------|-----------|
| 5 | Named strategic competitor with prior M&A track record in this sub-sector (e.g., "Fortune 500 X has bought 3 peers in last 24 months"). |
| 4 | Named PE firm with sector history + clear fund-cycle motivation. |
| 3 | Generic PE take-private — sector is popular with PE but no specific firm named. |
| 2 | Unclear path; could go either strategic or PE but no obvious fit. |
| 1 | No credible buyer identifiable; company may be too complex or too small for clean take-out. |

### Dimension 5 — Liquidity (×1)

| Score | 30-day ADV |
|-------|-----------|
| 5 | > $50M/day |
| 4 | $15–50M/day |
| 3 | $5–15M/day |
| 2 | $1–5M/day |
| 1 | < $1M/day |

---

## Band Thresholds

Standard system-wide thresholds apply:
- **≥ 35** = immediate (auto-promote to active candidate with rationale required)
- **25–34** = watchlist
- **15–24** = archive
- **< 15** = discard

---

## Auto-Caps

- **Definitive merger agreement already announced** → disqualify (post-edge per D-013).
- **Company rejected a prior offer in trailing 6 months** → cap at archive (low management receptiveness).
- **Going-concern warning in last 10-Q** → cap at watchlist (distressed; sale may happen at steep discount or not at all).
- **Sector in active consolidation wave but target already acquired peer recently** → cap at watchlist (less likely to be a target than acquirer).

---

## Dependencies

- `config/pe_filer_allowlist.json` — PE CIK/name list used by the scanner to flag 13G filings.
- `strategies/pre_edge_takeover_candidate.md` — operational spec and signal logic.
