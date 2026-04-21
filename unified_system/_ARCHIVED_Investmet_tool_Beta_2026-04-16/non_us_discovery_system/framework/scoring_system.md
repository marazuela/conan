# Scoring System — 7-Dimension Rubric

This rubric evaluates a signal that has passed Stage 1 triage. It is a **filter**, not a probability estimate. The rubric's job is to rank signals so deep-dive attention goes to the most promising ones first.

---

## The 7 dimensions

Each dimension scored 1–5 integer. Weighted sum = final score. Max = 42.5 before convergence bonus.

| # | Dimension | Weight | What to score on |
|---|-----------|--------|------------------|
| 1 | Signal Strength | ×2.0 | How clearly does the source filing imply a material, non-trivial change to the company's prospects? 1 = routine/boilerplate. 5 = unambiguous, high-magnitude event (e.g., Rule 2.7 firm offer). |
| 2 | Catalyst Clarity | ×1.0 | Is there a well-defined event that will resolve the thesis, with a roughly known date? 1 = no event, indefinite. 5 = specific event, specific date (e.g., shareholder vote on X date). |
| 3 | Info Asymmetry | ×1.5 | How invisible is this to standard English-language research? 1 = already on Bloomberg headline. 5 = Japanese-only Tanshin on small-cap with zero English coverage. |
| 4 | Risk/Reward | ×1.0 | Does the structure of the situation offer asymmetric upside? 1 = symmetric or worse. 5 = clear downside bound with meaningful upside optionality. |
| 5 | Edge Decay | ×1.0 | How long before the market prices this in? 1 = hours. 5 = weeks-to-months (matches mandate horizon). |
| 6 | Liquidity | ×1.0 | Can a satellite position be entered/exited without moving the price? 1 = untradeable. 5 = daily volume > 20× intended position size. |
| 7 | Catalyst Timeline | ×1.0 | Is the catalyst within the weeks-to-months mandate window? 1 = > 1 year or unknown. 5 = 2–12 weeks. |

Weighted maximum: 2.0×5 + 1.0×5 + 1.5×5 + 1.0×5 + 1.0×5 + 1.0×5 + 1.0×5 = **42.5**.

## Convergence bonus

After per-signal scoring, apply convergence bonus based on `issuer_figi`:

- 2 independent strategies within 14-day window (post cross-listing dedup per D-001/D-004): **+4**.
- 3 or more independent strategies: **+8**.

Max possible score with full convergence: **50.5**.

## Thresholds

| Band | Range | Action |
|------|-------|--------|
| Immediate | 28+ | Create full candidate writeup via `framework/candidate_template.md`; run deep dive; add to active work units in `SESSION_STATE.md`. |
| Watch | 22–27 | Condensed analysis in `candidates/watchlist/`. Re-score on every scheduled session; promote on material change. |
| Archive | 14–21 | Log only. Check periodically during maintenance audit. Move to `archive/` after 30 days. |
| Discard | <14 | Log to signals but do not carry forward. |

## Scoring rules specific to Tool 2

### Translation-direction honesty (D-002)

When `thesis_direction = unknown` (because translation confidence on direction-relevant passages was below 0.85):

- **Signal Strength is capped at 2.** Rationale: signal strength requires interpretable direction; `unknown` means the system cannot confidently assert materiality direction.
- **Info Asymmetry is unaffected.** The signal is still structurally invisible regardless of direction certainty.
- **Risk/Reward is capped at 3.** Rationale: asymmetric risk/reward requires knowing which side of the trade is asymmetric.

Effect: an `unknown`-direction signal maxes out around 27 pre-convergence — it reaches Immediate only when it converges with another strategy. This is correct — translation ambiguity alone should not produce an Immediate candidate.

### Cross-listing dedup (D-001, D-004)

Before scoring, the convergence engine has already deduplicated cross-listing echoes. If a signal survived dedup, it is genuinely independent from any other signal it is being compared against.

### Stage 1 triage prerequisites (must all pass before any scoring)

- Issuer is publicly traded on a major exchange.
- Market cap ≥ USD $300M (via yfinance with appropriate exchange suffix).
- Signal is novel — not a duplicate of one in `signals/signal_log.json` within 30 days.
- Source date within last 7 days.
- For non-English sources: translation confidence on critical passages ≥ 0.70.

---

## Worked example — UK LSE RNS Rule 2.7 firm offer

Filing: ACME plc, Rule 2.7 announcement of recommended cash offer from BidCo at £4.50 per share. Current price £3.80. Offer subject to regulatory clearance, expected completion in 10 weeks.

Scoring:

| Dimension | Score | Reasoning |
|-----------|-------|-----------|
| Signal Strength | 5 | Rule 2.7 is a firm offer — highest-strength UK takeover filing. |
| Catalyst Clarity | 5 | Specific price, specific buyer, specific completion window. |
| Info Asymmetry | 2 | UK Rule 2.7 is tracked by professional merger-arb desks. Not invisible. |
| Risk/Reward | 4 | Downside = price if deal breaks (~£3.80). Upside = offer price (£4.50). Spread ~18%. |
| Edge Decay | 3 | Spread will close over 10 weeks as regulatory clearance progresses. |
| Liquidity | 4 | LSE main-market; market cap > $300M filter ensures adequate volume. |
| Catalyst Timeline | 5 | 10-week window = squarely in mandate. |

Weighted: 5×2 + 5 + 2×1.5 + 4 + 3 + 4 + 5 = 10 + 5 + 3 + 4 + 3 + 4 + 5 = **34**.

Above 28 → Immediate candidate. Full deep dive, explicit kill conditions (regulatory rejection, competing offer pushing spread negative, BidCo financing collapse).

---

## Worked example — Japan TDnet Tanshin with ambiguous translation

Filing: Small-cap Japanese industrial, Tanshin with a downward revision to full-year guidance. Translation confidence on the critical "revision direction" passage: 0.72 — below 0.85 threshold, above 0.70 triage gate.

Direction: `unknown` per D-002.

| Dimension | Score | Reasoning |
|-----------|-------|-----------|
| Signal Strength | 2 | Capped at 2 per D-002 (direction unknown). |
| Catalyst Clarity | 3 | Next quarterly filing will clarify; no specific event. |
| Info Asymmetry | 5 | Japanese-only filing, small-cap, no English research. |
| Risk/Reward | 3 | Capped at 3 per D-002. |
| Edge Decay | 4 | Days-to-weeks — Japanese analysts will cover this. |
| Liquidity | 3 | Small-cap Japanese liquidity — marginal. |
| Catalyst Timeline | 3 | 4–8 weeks to next material filing. |

Weighted: 2×2 + 3 + 5×1.5 + 3 + 4 + 3 + 3 = 4 + 3 + 7.5 + 3 + 4 + 3 + 3 = **27.5**.

Below 28 pre-convergence → Watch. Only promoted to Immediate if it converges with another strategy (e.g., a cross-listed ADR on LSE or an HKEx connected-transaction disclosure implicating the same issuer).

This is the intended behavior of D-002: ambiguous direction prevents Immediate promotion in isolation, but still allows convergence to surface the name when a second independent source corroborates.
