# Candidate Dossier Template

Use this template for EVERY candidate at Immediate threshold (35+). Every section must be completed. "N/A" is acceptable when truly not applicable; blank is not.

---

## Header

- **Ticker**: `TICKER[.MIC]`
- **Company**: Full legal name
- **Exchange / MIC**: `XNYS` / `XLON` / `XTKS` / etc.
- **Market cap**: $XXX M (as of YYYY-MM-DD)
- **Scoring profile**: `merger_arb` | `activist_governance` | `binary_catalyst` | `short_positioning` | `litigation`
- **Thesis direction**: `long` | `short` | `neutral`
- **Source tool**: `edgar_filing_monitor` (or whichever)
- **Signal date**: YYYY-MM-DD
- **Candidate created**: YYYY-MM-DD
- **Last updated**: YYYY-MM-DD
- **Status**: `active` | `watch` | `killed` | `delivered`
- **Conviction**: `high` | `medium` | `low`

---

## 1. Headline Thesis (1–2 sentences)

The single-sentence pitch. What is happening, why it matters, what the expected outcome is.

---

## 2. Signal Origin

- **Scanner**: which tool produced the signal
- **Source URL**: primary source link (SEC, EDGAR, LSE RNS, TDnet, court filing)
- **Signal type**: `merger_announced` / `activist_13d` / `pdufa_approaching` / `short_buildup` / `litigation_filed` / etc.
- **Source content hash**: SHA256 of primary source body
- **Corroborating signals**: list any convergence signals from other scanners on this issuer in the 14/30-day window

---

## 3. Scoring Breakdown

Table with all profile-specific dimensions, raw scores, weighted scores, and total.

| Dimension | Raw | Weight | Weighted |
|-----------|-----|--------|----------|
| ...       | ... | ×...   | ...      |
| **TOTAL** | — | — | **XX.X / 50** |

**Band**: Immediate / Watchlist / Archive / Discard
**Auto-caps triggered**: none / rule_A / rule_B / etc.
**Convergence bonus**: +0 / +5 / +10 (and justification)
**Final score**: XX.X

---

## 4. Verification Tier — Evidence Labels

Every claim below must be labeled:
- **[VERIFIED]** — traceable to source code, data, or primary document
- **[INFERRED]** — reasonable conclusion from verified facts
- **[SPECULATED]** — forward-looking or hypothetical

---

## 5. Catalysts & Timeline

- **Primary catalyst**: what triggers price realization
- **Primary catalyst date**: YYYY-MM-DD (or range)
- **Secondary catalysts**: list
- **Kill conditions**: explicit triggers that invalidate the thesis. REQUIRED. A candidate without named kill conditions must not graduate from Watchlist.

---

## 6. Risk/Reward

- **Entry reference price**: $XX.XX at YYYY-MM-DD HH:MMZ
- **Upside target**: $XX.XX (+X%)
- **Downside (kill-level)**: $XX.XX (−X%)
- **Asymmetry**: X:1

---

## 7. Liquidity & Sizing

- **Avg daily $ volume (30d)**: $XX M
- **Borrow availability** (for short theses): easy / medium / hard / special
- **Suggested position size** (as % of portfolio): guidance only
- **Hedge pairing** (if applicable): stock or ETF to offset beta/sector risk

---

## 8. Deep Dive Research

Free-form primary-source research. Must cite source URLs for every factual claim. Minimum sections:

- **Transaction / Event Detail**: the specific fact pattern
- **Counterparty / Filer Background**: who is the activist, bidder, plaintiff, regulator
- **Historical Precedent**: similar fact patterns and their outcomes
- **Competitive / Regulatory Context**: what else is happening in the sector

---

## 9. Kill-Sweep Log

Running log of verification checks:

```
YYYY-MM-DD HH:MMZ — Check type — Result — Notes
```

---

## 10. Convergence Log

If this candidate has convergence bonuses, list the contributing signals:

```
- signal_id=... scanner=... date=... thesis_direction=... score=...
- signal_id=... scanner=... date=... thesis_direction=... score=...
```

---

## 11. Notes / Open Questions

Free-form. Things to watch, unresolved questions, future-session hand-off notes.
