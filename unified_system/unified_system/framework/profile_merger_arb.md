# Scoring Profile 1 — Merger Arbitrage / Announced Deals

**Applies to**: EDGAR M&A filings (DEFM14A, PREM14A, SC 13E3, S-4), TDnet tender offers, LSE firm offers (Rule 2.7), ASX schemes of arrangement, SEDAR+ plans of arrangement, HKEx Takeovers Code offers, any announced transaction with a fixed or exchange ratio price.

**Philosophy**: Spread × certainty × time. The edge is quantifiable. The risk is binary (deal closes or breaks).

---

## Triage Gate (before scoring)

A signal must pass ALL of these to be scored under this profile:

- Market cap ≥ $215M USD (≈€200M) at announcement.
- Publicly traded on a major exchange (NYSE, NASDAQ, LSE, Euronext, XETRA, TSE, ASX, TSX, HKEX, KRX, BSE/NSE, B3, BMV).
- Signal is novel (first occurrence in 14-day dedup window OR material escalation of prior signal).
- Source date within scan window (strategy-specific).
- Translation confidence ≥ 0.70 for non-English sources.

---

## Scoring Dimensions

| # | Dimension | Weight | What it measures |
|---|-----------|--------|------------------|
| 1 | Spread Size | ×3 | Gross spread = (deal price − current price) ÷ current price |
| 2 | Deal Certainty | ×2.5 | Regulatory, financing, shareholder-vote, MAC risk |
| 3 | Annualized Return | ×2 | Spread ÷ days-to-close, annualized (×365) |
| 4 | Break Risk | ×1.5 | Downside to unaffected pre-announcement price if deal fails |
| 5 | Liquidity | ×1 | Average daily dollar volume, bid-ask spread, tradability at position size |

**Max score**: 15 + 12.5 + 10 + 7.5 + 5 = **50**

---

## Rubric Detail

### Dimension 1 — Spread Size (×3)

| Score | Gross Spread |
|-------|--------------|
| 5 | > 10% |
| 4 | 5–10% |
| 3 | 3–5% |
| 2 | 1–3% |
| 1 | < 1% |

Use current price (prior close or intraday mid) vs. headline deal price. For stock-for-stock deals, compute spread using the buyer's current price × exchange ratio.

### Dimension 2 — Deal Certainty (×2.5)

| Score | Condition Profile |
|-------|-------------------|
| 5 | Unconditional or only confirmatory closing conditions; cash-funded; no regulatory hurdle beyond HSR/EC early termination |
| 4 | Minor conditions — shareholder vote (>50% support indicated), routine antitrust, debt commitments in hand |
| 3 | Moderate — meaningful antitrust review (Phase II possible), financing condition, minority shareholder approval required |
| 2 | Material risk — multiple conditions, hostile board, no-shop waived, competing bidder possible |
| 1 | Serious risk — CFIUS/national-security review, conditional financing, MAC dispute active, going-private with significant-shareholder holdout |

### Dimension 3 — Annualized Return (×2)

| Score | Annualized Return |
|-------|-------------------|
| 5 | > 20% |
| 4 | 12–20% |
| 3 | 8–12% |
| 2 | 4–8% |
| 1 | < 4% |

Formula: `spread_pct × (365 / estimated_days_to_close)`. If expected close is "H2 2026" and today is April, estimate 180 days.

### Dimension 4 — Break Risk (×1.5)

| Score | Downside to Unaffected Price |
|-------|------------------------------|
| 5 | < 10% |
| 4 | 10–20% |
| 3 | 20–30% |
| 2 | 30–40% |
| 1 | > 40% |

"Unaffected price" = VWAP over the 30 trading days prior to first leak/rumor/announcement.

### Dimension 5 — Liquidity (×1)

| Score | Average Daily Dollar Volume (USD) |
|-------|-----------------------------------|
| 5 | > $50M |
| 4 | $20–50M |
| 3 | $10–20M |
| 2 | $3–10M |
| 1 | < $3M |

---

## Thresholds & Auto-Caps

| Band | Score | Action |
|------|-------|--------|
| Immediate | 35+ | Full deep-dive, candidate dossier, monitor daily |
| Watchlist | 25–34 | Track, re-score on material update |
| Archive | 15–24 | Log, no active monitoring |
| Discard | < 15 | Drop, note reason |

**Auto-cap rule A — Sub-scale return**:
If Annualized Return < (risk-free rate + 3%), auto-cap at **Watchlist** regardless of other scores. Current risk-free anchor: 10Y UST (check fresh at scoring time; as of 2026-04-16 ≈ 4.3%, so cap triggers below ~7.3% annualized). This rule would have correctly flagged SEM's 1.1% annualized as not actionable.

**Auto-cap rule B — Break risk dominance**:
If Break Risk scores 1 AND Deal Certainty ≤ 2, auto-cap at **Watchlist**. Asymmetric downside on a shaky deal is not a merger-arb setup.

---

## Key Judgment Notes

- For stock deals, the "spread" is only real if the buyer's stock is tradable for a hedge. Lower Liquidity score if the buyer is illiquid or foreign-listed in a market you can't short.
- For cross-border deals, add CFIUS/FDI/antitrust-by-jurisdiction risk to Deal Certainty.
- For going-private with founder/controlling-shareholder, examine the record of the controller's prior take-privates. A controller with a history of price-bumps is a +0.5 to Certainty; a history of price-cuts is −0.5.
