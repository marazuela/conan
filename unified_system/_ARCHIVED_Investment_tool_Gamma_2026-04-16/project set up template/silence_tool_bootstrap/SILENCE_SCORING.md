# SCORING — 7-Dimension Rubric for Silence Signals (Tool 4)

This rubric adapts the 7-dimension framework from Tools 1/2/3 to silence signals. Two dimensions change their anchors; one is replaced. The numerical envelope (max 42.5, same thresholds 28+/22–27/14–21/<14) is preserved for cross-tool convergence comparability.

When the new session creates `framework/scoring_system.md`, it should copy this file verbatim and append a worked example from the first production silence signal.

---

## The Seven Dimensions

| # | Dimension | Weight | Score 1–5 Scale |
|---|-----------|--------|-----------------|
| 1 | Signal Strength | ×2 | How strong is the combined anomaly across triggered dimensions? |
| 2 | Catalyst Clarity | ×1 | How clear is the upcoming catalyst that silence likely precedes? (lowered from Tool 1/2's ×1.5 because silence's catalyst is inferred, not scheduled) |
| 3 | Info Asymmetry | ×1.5 | How under-watched is this issuer's behavioral profile by equity research? |
| 4 | Risk/Reward | ×1 | Implied move magnitude if the silence resolves into the inferred catalyst |
| 5 | Edge Decay | ×1 | How many days until the silence either resolves or becomes widely noticed |
| 6 | Liquidity | ×1 | Issuer ADV / position-size feasibility |
| 7 | **Baseline Validity** | ×1.5 | **NEW** — how confident are we the silence is real (not a baseline artifact)? (elevated from ×1 because baseline integrity is the tool's load-bearing question) |

**Max raw score**: (5×2) + (5×1) + (5×1.5) + (5×1) + (5×1) + (5×1) + (5×1.5) = 10 + 5 + 7.5 + 5 + 5 + 5 + 7.5 = **45.0**

Thresholds (scaled up proportionally from the 42.5 envelope to the 45.0 envelope):
- **≥ 30**: Immediate candidate → full deep dive.
- **23–29**: Watchlist → condensed analysis, re-check every scan.
- **15–22**: Archive → log only.
- **< 15**: Discard.

**Convergence bonus** (intra-tool, across the six observable dimensions within a single issuer-scan): built into Signal Strength anchors below; no separate bonus. *Cross-issuer convergence* (multiple issuers in the same sector going silent together) is a sector-signal, scored separately in `SILENCE_DIMENSIONS.md` Phase 8+.

---

## Dimension Details

### 1. Signal Strength (×2) — anchors reflect joint anomaly magnitude

How strong is the anomaly?

| Score | Anchor |
|-------|--------|
| 5 | 3+ dimensions triggered AND combined anomaly score ≥ 9 (near-certain joint improbability). Example: 45-day silence on press releases (z=-3.2), 90-day silence on insider transactions (z=-2.8), absence from a regularly-attended investor conference. |
| 4 | 2 dimensions triggered AND combined anomaly score 6–9, OR single dimension z ≤ -3.5. |
| 3 | 2 dimensions triggered AND combined score 4–6, OR single dimension z ≤ -3.0. |
| 2 | 1 dimension triggered at z ≤ -2.5 to -3.0; no convergence. |
| 1 | 1 dimension at z ≤ -2.0 to -2.5; marginal single-dimension anomaly. |

### 2. Catalyst Clarity (×1)

Silence precedes *something*, but the something is inferred rather than observed. This dimension captures how well the inferred catalyst can be anchored.

| Score | Anchor |
|-------|--------|
| 5 | Silence maps to a near-term specific event type with documentary evidence. Example: silence pattern + NT-10 filing → very likely restatement; silence pattern + recent sector M&A activity → likely confidential M&A discussion; silence pattern + known SEC inquiry disclosure → pending enforcement action. |
| 4 | Silence maps to a plausible catalyst type with indirect evidence. Example: silence pattern + 10-K filing overdue → likely accounting issue; silence pattern + CEO recently departed → likely leadership transition. |
| 3 | Silence is unusual but no specific catalyst hypothesis is well-supported. Deep-dive produces 3–5 equally plausible hypotheses. |
| 2 | Silence could be attributable to benign causes (executive vacation, product-cycle gap) that are hard to rule out. |
| 1 | Silence is most plausibly a seasonal or known-pattern artifact; baseline adjustment imperfect. |

### 3. Info Asymmetry (×1.5)

How under-watched is this issuer's *behavioral profile*? (Distinct from how under-watched the issuer itself is; a well-covered mega-cap can still have an under-watched silence profile because no research desk holds behavioral baselines.)

| Score | Anchor |
|-------|--------|
| 5 | Small-cap issuer (in-universe minimum, $2–5B), 0–3 analysts, no specialty research coverage. Silence will not be picked up by anyone else for weeks. |
| 4 | Mid-cap ($5–20B), 3–8 analysts, minimal alternative-data coverage. |
| 3 | Large-cap ($20–100B), 8–20 analysts, some alternative-data coverage but not focused on behavioral profile. |
| 2 | Mega-cap ($100B–$500B), heavily covered, some dedicated behavioral-monitoring by institutional quant shops. |
| 1 | Top-20-by-market-cap, exhaustively covered; silence here is unlikely to offer meaningful alpha. |

### 4. Risk/Reward (×1)

If the silence's inferred catalyst is the actual cause, what is the implied move magnitude and downside asymmetry?

| Score | Anchor |
|-------|--------|
| 5 | Implied move ≥ 15%, downside asymmetric (stock trades rich relative to baseline fundamentals; negative catalyst causes meaningful correction). |
| 4 | Implied move 8–15%, reasonable downside containment or asymmetric payoff. |
| 3 | Implied move 4–8%. |
| 2 | Implied move 2–4%. |
| 1 | < 2% or unclear payoff structure. |

### 5. Edge Decay (×1)

How many days until the silence either resolves (catalyst announced) or is noticed by the broader market?

| Score | Anchor |
|-------|--------|
| 5 | Silence first emerges this scan cycle; 0–3 days since detection; market has not begun repricing. |
| 4 | 3–14 days since detection; early; price not yet reflecting the inferred catalyst. |
| 3 | 14–30 days; mid-window; some drift observed but not systematic. |
| 2 | 30–45 days; some market participants may be noticing; edge narrowing. |
| 1 | > 45 days; silence is mature, and by now either the catalyst is imminent (low remaining edge) or the silence is a false positive. |

### 6. Liquidity (×1)

Can a 1–3% satellite position be established without market impact?

| Score | Anchor |
|-------|--------|
| 5 | ADV > $100M; position trivially scalable. |
| 4 | ADV $25–100M. |
| 3 | ADV $10–25M. |
| 2 | ADV $5–10M; position requires patience. |
| 1 | ADV < $5M; liquidity-constrained. |

### 7. Baseline Validity (×1.5) — NEW, replaces Catalyst Timeline

How confident are we this silence is real vs. an artifact of baseline corruption, seasonality, or inadequate warm-up?

| Score | Anchor |
|-------|--------|
| 5 | Baseline has ≥ 24 months of observations, seasonal adjustment validated on out-of-sample data, no recent corporate restructuring that could have reshaped baseline, no recent universe-entry (issuer has been in universe ≥ 12 months). Highest confidence silence is real. |
| 4 | Baseline has 18–24 months, seasonal adjustment standard, no major corporate events in last 180 days. |
| 3 | Baseline has 12–18 months, seasonal adjustment applied but not out-of-sample validated, or minor corporate events in the history window. |
| 2 | Baseline has only the minimum window (12 months), OR the issuer has had a recent corporate event (merger, spin-off, major product launch) that reshapes expected cadence. Signal is admitted but with reduced confidence. |
| 1 | Baseline is at the edge of warm-up eligibility; should have been filtered out at triage. If it reaches scoring, score this 1 and surface as an OPEN_QUESTION about warm-up threshold calibration. |

**Why elevated weight (×1.5 vs. Tool 3's ×1 for Party-Resolution Confidence)**: In Tool 3, party-resolution failure produces a wrong-entity signal that is loud enough to be caught manually. In Tool 4, baseline-validity failure produces a *false silence signal that looks real* — subtle, hard to catch, and poisonous to the candidate pipeline. Weighting Baseline Validity higher forces the scoring model to heavily penalize signals from thin or compromised baselines, pushing them below the 30-point candidate threshold even when other dimensions would push the signal through.

---

## Convergence across dimensions

Intra-issuer convergence (multiple dimensions silent on the same issuer in the same window) is built into Signal Strength (Dimension 1). A separate intra-tool convergence bonus is NOT added — it would double-count.

Cross-tool convergence (a Tool 4 silence signal + a Tool 1/2/3 event signal on the same issuer within 60 days) is handled by a future cross-tool analyzer project per PROJECT_TEMPLATE non-negotiable #8. Tool 4 does not score it.

---

## Worked Example (representative)

**Signal:** Mid-cap semiconductor issuer (ticker MSEM, market cap $8B, ADV $35M). Scan on 2026-04-14 detects:

- Press-release silence: observed 1 release in last 30 days, expected 7.5 (z = -3.1, p = 0.0010). Seasonal adjustment applied (summer -25%). Baseline n = 84 observations over 12 months.
- Insider-transaction silence: observed 0 in last 90 days, expected 3.2 (z = -2.4, p = 0.0082). Excluding earnings-blackout windows. Baseline n = 38 over 24 months.
- Conference absence: MSEM has presented at the Needham Growth Conference in each of the last 3 years. Agenda for 2026 has been published and MSEM is not listed.
- Combined anomaly score: (3.1 × 1.0) + (2.4 × 1.0) + (conference binary → 2.0 equivalent) = 7.5.

**Scoring:**

| Dim | Score | Weighted |
|-----|-------|----------|
| Signal Strength | 4 (3 dimensions, combined 7.5) | 8.0 |
| Catalyst Clarity | 3 (semiconductor sector M&A activity recent; plausible confidential M&A; also plausible earnings warning; 3 hypotheses) | 3.0 |
| Info Asymmetry | 4 (mid-cap, modest analyst coverage, no behavioral monitoring) | 6.0 |
| Risk/Reward | 4 (sector M&A precedent: 15–25% moves; downside contained by recent low) | 4.0 |
| Edge Decay | 5 (first-detect this cycle) | 5.0 |
| Liquidity | 4 (ADV $35M) | 4.0 |
| Baseline Validity | 4 (18-month baselines across dimensions; no recent corporate event; seasonal adjustment in-sample only) | 6.0 |
| **Raw** | | **36.0** |
| **Final** | | **36.0** |

Result: 36.0 ≥ 30 threshold → immediate candidate → full deep dive. Deep-dive brief enumerates the three alternative hypotheses (confidential M&A, pre-earnings warning, restatement prep) and surfaces the M&A hypothesis as leading based on sector precedent.

---

## Using the rubric — session discipline

- Every silence signal that survives triage gets scored in the same session. Deep dive (hypothesis ranking + catalyst research) is post-score.
- Rubric weights are not adjusted per-session. Weight changes are D-0XX decisions.
- If Baseline Validity would score 1, the signal is killed pre-scoring; the session does not score-then-discard — it terminates the signal at triage and logs the root cause.
- If the candidate list persistently clusters in a narrow score band (every signal 30–33), one or more dimensions is correlated; raise in `OPEN_QUESTIONS.md`.
- A silence signal that triggers all 7 dimensions at ≥ 4 and scores ≥ 40 must pass an additional self-review: what specific disclosed alternative explanation have we ruled out? High-scoring signals with no killable alternatives are the highest-value candidates.
