# SCORING — 7-Dimension Rubric for Litigation Signals (Tool 3)

This rubric adapts the 7-dimension scoring framework from Tools 1 and 2 to litigation signals. Two dimensions change their meaning; one is replaced. The numerical envelope (max 42.5, same thresholds) is preserved so scores are comparable across all three tools for cross-tool convergence.

When the new session creates `framework/scoring_system.md`, it should copy this file verbatim and append a worked example using a real (or representative) signal from each channel.

---

## The Seven Dimensions

| # | Dimension | Weight | Score 1–5 Scale |
|---|-----------|--------|-----------------|
| 1 | Signal Strength | ×2 | How material is this docket event to the issuer's equity value? |
| 2 | Catalyst Clarity | ×1.5 | How deterministic is the upcoming resolution (date certain, outcome-bound)? |
| 3 | Info Asymmetry | ×1.5 | How under-followed is this case by equity research? |
| 4 | Risk/Reward | ×1 | Implied move magnitude vs. downside of a wrong thesis |
| 5 | Edge Decay | ×1 | How many days until the edge is priced in |
| 6 | Liquidity | ×1 | Issuer ADV / position-size feasibility |
| 7 | **Party-Resolution Confidence** | ×1 | **NEW** — how confident are we this case actually involves the public company we think it does? |

**Max raw score**: (5×2) + (5×1.5) + (5×1.5) + (5×1) + (5×1) + (5×1) + (5×1) = 10 + 7.5 + 7.5 + 5 + 5 + 5 + 5 = **42.5**

**Convergence bonus**: +4 for 2 channels, +8 for 3+ channels, within a 30-day rolling window (wider than Tool 1/2's 14-day — see D-005).

**Thresholds**:
- **28+**: Immediate candidate → full deep dive and candidate writeup.
- **22–27**: Watchlist → condensed analysis, re-check on each scan cycle.
- **14–21**: Archive → log only, periodic audit.
- **<14**: Discard.

---

## Dimension Details

### 1. Signal Strength (×2) — unchanged semantics, litigation-specific anchors

How material is this docket event to the issuer's equity value?

| Score | Anchor |
|-------|--------|
| 5 | Outcome-determining event on a matter affecting ≥ 20% of revenue or a bet-the-company product line. Examples: 337 Final Determination / exclusion order against respondent's primary product; PTAB FWD invalidating all challenged claims on a patent central to a drug franchise; merger challenge filed against a $10B+ deal. |
| 4 | Milestone event on a matter affecting 5–20% of revenue or a core product. Examples: Markman order construing patent claims in a respondent's favor/disfavor; 337 institution of investigation on a mid-tier product; Second Request issuance on a sizable merger. |
| 3 | Procedural event on a matter affecting 1–5% of revenue, OR outcome event on a peripheral matter. Examples: motion-to-dismiss denied in a patent case; initial complaint filed in a material dispute. |
| 2 | Procedural event on a peripheral matter; complaint filed without clear materiality; settlement of a minor case. |
| 1 | Ministerial docket entry; procedural minutiae; signal barely distinguishable from noise. |

### 2. Catalyst Clarity (×1.5 — elevated from ×1 in Tool 1/2)

How deterministic is the upcoming resolution? Litigation has **unusually clear schedules** compared to most investment catalysts (PDUFA dates excepted), so this dimension is weighted higher.

| Score | Anchor |
|-------|--------|
| 5 | Deterministic date certain with deterministic outcome classes. PTAB FWD date is set; ITC Final Determination has a target date; Markman hearing scheduled. |
| 4 | Deterministic date, probabilistic outcome. Summary judgment motion briefed and submitted. |
| 3 | Calendar item exists but may slip (typical district-court motion practice, Chancery preliminary-injunction hearing). |
| 2 | Event will happen but date is vague (settlement talks in progress; case awaiting trial assignment). |
| 1 | Highly contingent on future developments (appeal possibilities, remand risk, MDL consolidation). |

### 3. Info Asymmetry (×1.5)

How under-followed is this case by equity research?

| Score | Anchor |
|-------|--------|
| 5 | Zero equity-research coverage; not in Law360, Bloomberg Law, or sell-side notes. Small-cap issuer (< $2B market cap). Party buried in a caption with a non-obvious name. |
| 4 | Minimal coverage; mentioned in passing in one 10-Q risk factor; small-/mid-cap issuer. |
| 3 | Covered by legal press but not equity analysts; mid-cap issuer. |
| 2 | Covered by some equity research; large-cap issuer. |
| 1 | Well-covered; mega-cap issuer; on the front page of Law360 and the WSJ. |

### 4. Risk/Reward (×1)

Implied move magnitude vs. cost of being wrong.

| Score | Anchor |
|-------|--------|
| 5 | Binary outcome, implied move ≥ 15%, downside asymmetric (stock already priced-in the bad outcome partially). |
| 4 | Implied move 8–15%, reasonable downside containment. |
| 3 | Implied move 4–8%. |
| 2 | Implied move 2–4%. |
| 1 | Implied move < 2% or downside > upside. |

### 5. Edge Decay (×1)

How many days until the market fully prices the signal?

| Score | Anchor |
|-------|--------|
| 5 | ≤ 24 hours since docket entry; no press coverage yet. |
| 4 | 1–3 days; some legal-press coverage; no equity-research note yet. |
| 3 | 3–7 days; Law360 has covered it; no sell-side note. |
| 2 | 1–2 weeks; at least one sell-side note. |
| 1 | > 2 weeks; widely disseminated; Edge likely fully priced. |

### 6. Liquidity (×1)

Can a 2–5% satellite position be built without moving the stock?

| Score | Anchor |
|-------|--------|
| 5 | ADV > $100M; position easily scaled. |
| 4 | ADV $25–100M. |
| 3 | ADV $5–25M. |
| 2 | ADV $1–5M; position requires patience. |
| 1 | ADV < $1M; effectively untradeable at size. |

### 7. Party-Resolution Confidence (×1) — NEW, replaces Catalyst Timeline

How confident are we that this case actually involves the public company we think it does? This dimension exists because litigation signals have a fundamentally new failure mode not present in Tools 1 and 2: **the docket party could be a subsidiary, acquired entity, unrelated namesake, or incorrectly resolved via a low-confidence method**. If this dimension is low, no amount of signal strength rescues the thesis.

| Score | Anchor |
|-------|--------|
| 5 | Direct-hit resolution: CIK match via SEC EDGAR exact match, OR internal party→issuer cache hit with prior confirmed attribution. Confidence ≥ 0.95. |
| 4 | Strong resolution: SEC EDGAR fuzzy match (Levenshtein ≤ 3) or 10-K Exhibit 21 direct-subsidiary match. Confidence 0.85–0.95. |
| 3 | Adequate resolution: Exhibit 21 indirect-subsidiary match or authoritative public mapping. Confidence 0.80–0.85. |
| 2 | Borderline: OpenFIGI NAME-type match only, or ambiguous caption with multiple possible parent mappings. Confidence 0.70–0.80. |
| 1 | Unresolved or low-confidence: below 0.70 — should have been triaged out at Stage 1; if it reaches scoring, score this 1 and surface as an OPEN_QUESTION about triage-filter calibration. |

Note that **Catalyst Timeline (Tool 1's 7th dimension) was dropped** because catalyst timing in litigation is already captured by Catalyst Clarity (deterministic schedule) and Edge Decay (how far into the window we are). Adding a separate Catalyst Timeline dimension would double-count.

---

## Convergence Bonus

Within a 30-day rolling window, identical `issuer_figi` appearing in signals from multiple channels earns a bonus:

- 2 distinct channels → +4
- 3+ distinct channels → +8

Examples that trigger +4:
- PACER federal civil complaint + SEC enforcement release on the same issuer within 30 days.
- ITC 337 institution + PTAB IPR filing by the same petitioner against the same patent owner.
- DOJ merger challenge + Delaware Chancery TRO on the same announced deal.

Cross-tool convergence (Tool 3 + Tool 1 or Tool 3 + Tool 2) is NOT scored here — it happens in a separate analyzer project per PROJECT_TEMPLATE D-004 discipline.

---

## Worked Example (representative, not actual)

**Signal:** A 337 Complaint is filed against Respondent Corp (mid-cap, $4B market cap, ADV $15M) alleging infringement of three patents that map to Respondent's "Widget X" product line, which per its 10-K accounts for ~18% of total revenue. USITC publishes the Complaint notice today; institution decision expected in 35 days.

**Scoring:**

| Dim | Score | Weighted |
|-----|-------|----------|
| Signal Strength | 4 (milestone event, 18% revenue at stake) | 8.0 |
| Catalyst Clarity | 5 (35-day window to institution, deterministic) | 7.5 |
| Info Asymmetry | 4 (mid-cap, minimal analyst coverage of IP risk) | 6.0 |
| Risk/Reward | 3 (implied move ~6%) | 3.0 |
| Edge Decay | 5 (same-day) | 5.0 |
| Liquidity | 3 (ADV $15M) | 3.0 |
| Party-Resolution Confidence | 5 (direct EDGAR match) | 5.0 |
| **Raw** | | **37.5** |
| Convergence bonus | 0 (single channel) | 0 |
| **Final** | | **37.5** |

Result: 37.5 >> 28 threshold → immediate candidate → full deep dive.

---

## Using the Rubric — Session Discipline

- Every surviving-triage signal is scored in the same session. Scoring is deterministic; it does not require web research. Deep dive (post-score) is the research phase.
- Rubric weights are not adjusted per-session. Changes to weights are D-0XX decisions in the working project.
- If a signal scores 28+ and deep-dive research reveals a kill condition, the candidate is written and immediately marked killed. Both the creation and the kill are logged.
- If the top of the candidate list persistently clusters in a narrow score band (e.g., every candidate scores 28–31), one or more dimensions is correlated; raise the issue in `OPEN_QUESTIONS.md` per PROJECT_TEMPLATE Part 10 adversarial-discipline rule.
