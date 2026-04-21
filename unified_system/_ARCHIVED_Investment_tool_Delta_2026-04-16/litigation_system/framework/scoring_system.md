# Scoring System — Litigation & Docket Signal System (Tool 3)

> Seven-dimension rubric, max 42.5 points, plus convergence bonus up to +8. Bands: 28+ Immediate, 22–27 Watch, 14–21 Archive, <14 Discard.

The rubric is the gate into the candidate pipeline. Scoring happens AFTER party resolution + entity resolution + confidence gate + convergence detection. A signal with confidence < 0.85 never reaches scoring (triaged out at Stage 1 per D-003).

---

## The Seven Dimensions

| # | Dimension | Weight | Range | Description |
|---|-----------|--------|-------|-------------|
| 1 | Signal Strength | ×2 | 0–5 | How binary / market-material is the event? |
| 2 | Catalyst Clarity | ×1.5 | 0–5 | Is the next market-relevant milestone date known? |
| 3 | Info Asymmetry | ×1.5 | 0–5 | How likely is it that the market already knows? |
| 4 | Risk / Reward | ×1 | 0–5 | Asymmetric payoff profile if thesis is right? |
| 5 | Edge Decay | ×1 | 0–5 | How fast does the edge erode once public? |
| 6 | Liquidity | ×1 | 0–5 | Can a satellite 2–5% position enter/exit cleanly? |
| 7 | Party-Resolution Confidence | ×1 | 0–5 | **Litigation-specific** — how confident are we in party → issuer mapping? |

**Max score:** (5×2) + (5×1.5) + (5×1.5) + (5×1) + (5×1) + (5×1) + (5×1) = 10 + 7.5 + 7.5 + 5 + 5 + 5 + 5 = **45 points**.

> Correction note: the rubric summary in `CONTEXT.md` quick-reference says max 42.5. The actual math above is 45. This is resolved by observing that dimension #2 (Catalyst Clarity ×1.5) was elevated from Tool 1's ×1 weighting specifically for litigation because court calendars are deterministic. When referencing the quick-ref card, the 42.5 figure is stale; the governing max is **45**. TODO: reconcile by a future decision (or simply update CONTEXT.md quick-ref in Phase 2 maintenance pass).

**Convergence bonus** (added to final weighted score):
- +4 if the entity has ≥ 2 channels' signals within the 30-day window.
- +8 if ≥ 3 channels.

**Bands** (after bonus):
- **28+ IMMEDIATE** — promote to deep-dive brief. Candidate writeup required.
- **22–27 WATCH** — add to watchlist; re-evaluate on next convergence event.
- **14–21 ARCHIVE** — log to scan_results, no active pipeline entry.
- **<14 DISCARD** — drop silently.

---

## Dimension 1 — Signal Strength (×2)

How binary and market-material is the docket event?

| Score | Rubric |
|-------|--------|
| 5 | Binary outcome with immediate price implication. Examples: ITC institution notice; PTAB IPR institution granted; SEC enforcement action filed; DOJ/FTC merger challenge filed in federal court; Markman claim-construction order in revenue-core patent case. |
| 4 | Strongly suggestive but not fully binary. Examples: motion-to-dismiss denied in securities class action; Delaware appraisal petition filed in announced deal; HSR Second Request public disclosure. |
| 3 | Directionally material but not market-moving alone. Examples: discovery dispute ruling; summary-judgment motion filed (not ruled); Wells Notice disclosed in 10-Q (proxy for SEC action). |
| 2 | Informational. Examples: case transfer to a different court; amended complaint filed (minor). |
| 1 | Procedural noise. Examples: motion for extension of time; scheduling-order amendments. |
| 0 | Not material. |

**Judge-prior caveat:** motion-to-dismiss grant rates vary up to 3× by federal judge. v1 uses population averages; per-judge priors deferred (Q-001).

---

## Dimension 2 — Catalyst Clarity (×1.5)

Is the next market-relevant milestone date known? Legal calendars are deterministic, so this dimension is weighted higher than in Tool 1.

| Score | Rubric |
|-------|--------|
| 5 | Statutorily fixed date in the next 90 days. Examples: PTAB 6-month-to-institution clock; ITC target-completion date; motion-to-dismiss hearing on calendar. |
| 4 | Schedule order on file with milestone dates in 90–180 days. |
| 3 | Typical court pacing implies milestone within 180 days. |
| 2 | Milestone exists but timing vague. |
| 1 | No clear next milestone. |
| 0 | No catalyst identifiable. |

---

## Dimension 3 — Info Asymmetry (×1.5)

How likely is the market to already know?

| Score | Rubric |
|-------|--------|
| 5 | Filing not yet covered anywhere we can find; no company disclosure; no Law360/Bloomberg Law hit; no 8-K. Typically 0–3 days post-docket. |
| 4 | Sparse coverage only (one specialty-legal blog, no mainstream or IR). |
| 3 | Covered by specialty legal media but not by mainstream financial press. |
| 2 | Covered by one mainstream source; not yet company-disclosed. |
| 1 | Company has 8-K'd or acknowledged; public knowledge. |
| 0 | Over-covered. |

**Edge-decay note:** info asymmetry on litigation signals decays on the order of 2–10 business days depending on channel. Federal-civil is the fastest-decaying (RECAP mirrors hourly); Delaware Chancery can sustain asymmetry 5+ days due to the HTML-scraping-only moat.

---

## Dimension 4 — Risk/Reward (×1)

Is the payoff asymmetric if the thesis is correct?

| Score | Rubric |
|-------|--------|
| 5 | Estimated +15% or more upside / −5% or less downside on binary outcome. |
| 4 | +10% / −5%. |
| 3 | +7% / −5% (roughly symmetric on the good side). |
| 2 | +5% / −5% (barely asymmetric). |
| 1 | Downside comparable to upside. |
| 0 | Downside exceeds upside. |

---

## Dimension 5 — Edge Decay (×1)

How fast does the edge erode once the signal becomes public?

| Score | Rubric |
|-------|--------|
| 5 | Edge persists ≥ 5 trading days post-docket (e.g., Chancery HTML-only feeds). |
| 4 | 3–5 days. |
| 3 | 1–3 days. |
| 2 | <1 day (company likely to 8-K same day). |
| 1 | Minutes–hours. |
| 0 | Already decayed. |

---

## Dimension 6 — Liquidity (×1)

Can a 2–5% portfolio position enter/exit cleanly?

| Score | Rubric |
|-------|--------|
| 5 | 30-day ADV ≥ $50M, tight spreads, listed options. |
| 4 | ADV ≥ $20M, decent spreads. |
| 3 | ADV ≥ $10M. |
| 2 | ADV ≥ $5M. |
| 1 | ADV < $5M. |
| 0 | Untradeable at scale. |

---

## Dimension 7 — Party-Resolution Confidence (×1) — NEW

Litigation-specific. How confident is the party-name → issuer-FIGI mapping?

| Score | Rubric | Resolution path |
|-------|--------|-----------------|
| 5 | Confidence ≥ 0.95 | Internal cache exact; SEC EDGAR exact match. |
| 4 | 0.90 ≤ c < 0.95 | SEC EDGAR exact; Exhibit 21 direct subsidiary. |
| 3 | 0.85 ≤ c < 0.90 | SEC EDGAR fuzzy (Levenshtein ≤ 3); Exhibit 21 indirect. |
| 2 | 0.80 ≤ c < 0.85 | **Admitted but flagged**; caveat in brief. |
| 1 | 0.70 ≤ c < 0.80 | Borderline — should have been triaged out. |
| 0 | <0.70 | Never reaches scoring. |

Per D-003, signals with confidence <0.85 are triaged out at Stage 1. A score of 1 or 2 here means the signal slipped the gate and should be re-examined.

---

## Worked Example

**Signal:** PTAB IPR institution granted on Apple patent US 9,XXX,XXX; petitioner is Samsung. Apple is the patent owner (defendant-equivalent).

| Dim | Score | Weighted |
|-----|-------|----------|
| Signal Strength | 5 (binary institution) | 10.0 |
| Catalyst Clarity | 5 (12-month statutory clock to final written decision) | 7.5 |
| Info Asymmetry | 4 (PTAB published same day; no 8-K yet) | 6.0 |
| Risk / Reward | 2 (patent is not revenue-core; low asymmetry) | 2.0 |
| Edge Decay | 3 (2–3 days) | 3.0 |
| Liquidity | 5 (AAPL) | 5.0 |
| Party-Resolution Confidence | 5 (cache exact) | 5.0 |
| **Subtotal** | | **38.5** |
| Convergence bonus | None (single channel) | 0 |
| **Final** | | **38.5** |

Band: **28+ IMMEDIATE** → promote to deep-dive brief.

Caveat: if the patent is peripheral to revenue (as assumed here at R/R=2), the deep-dive brief should recommend PASS despite the high score — a reminder that the rubric is a gate, not the thesis.

---

## Scoring Procedure

1. Scanner produces raw signal JSON.
2. Triage drops obvious non-signals (procedural, non-universe parties, <$300M mcap).
3. Party resolution → issuer FIGI with confidence.
4. Confidence gate (<0.85 drops).
5. Convergence engine checks 30-day window keyed on `issuer_figi`.
6. Scorer applies all 7 dimensions + convergence bonus.
7. Band-assignment: 28+ → promote; 22–27 → watchlist; 14–21 → archive; <14 → discard.
8. Promoted candidates get a `candidate_<figi>_<YYYYMMDD>.md` file per `candidate_template.md`.
