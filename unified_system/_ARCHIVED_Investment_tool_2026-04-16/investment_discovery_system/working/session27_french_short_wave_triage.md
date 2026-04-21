# Session 27 — ESMA French Short Wave Triage

**Date**: 2026-04-10
**Scanner**: esma_short (11 raw signals, compressed to 6 unique entities)
**Trigger**: Large number of new BaFin/AMF disclosures April 8 2026, heavy French consumer/services concentration

---

## Raw Signals

| Ticker | Name | Cap €M | Holders | Aggregate % | New Position |
|--------|------|--------|---------|-------------|--------------|
| UBI.PA | Ubisoft Entertainment | 554 | 9 | 14.67% | BlackRock 0.94, DE Shaw 1.96, Marshall Wace 2.30, Millennium 0.51 (all Apr 8) |
| EDEN.PA | Edenred | ~4600 est | 10 | 10.63% | Marshall Wace 1.42, DE Shaw 0.98, AQR 0.91, Squarepoint 0.51 (all Apr 8) |
| SW.PA | Sodexo | 5746 | 5 | 3.69% | Capital Fund Mgmt 0.5 (Apr 8) |
| VIRI.PA | Viridien (ex-CGG) | 885 | 4 | 3.31% | Marshall Wace 0.53 (Apr 8) |
| ELIOR.PA | Elior Group | 629 | 3 | 2.10% | Millennium Intl 0.52 (Apr 8) |
| ACCOR (ACC.PA) | Accor | N/A | 3 | 1.94% | Ilex Capital 0.54 (Apr 8) |

---

## Price Action (10-day) + Entry Signal Validation

| Ticker | Price | 10d Chg | Off 52w High | Entry Signal | Verdict |
|--------|-------|---------|--------------|--------------|---------|
| UBI.PA | €4.11 | **+7.5%** | -64.8% | **DISCONFIRMATION** — short build into rally | **SKIP** |
| EDEN.PA | €18.69 | **+21.5%** | -39.1% | **DISCONFIRMATION** — massive short squeeze underway | **SKIP** |
| SW.PA | €39.46 | -7.5% | -31.5% | **CONFIRMING** — price down with shorts | Watchlist |
| VIRI.PA | €123.50 | **-10.1%** | -10.1% (at high, just crashed) | **CONFIRMING** — just broke high on new short | Watchlist |
| ELIOR.PA | €2.48 | +2.4% | -19.1% | Disconfirmation | Skip |
| ACCOR | N/A | — | — | No price resolve | Deferred |

**Pattern recognition**: This is a **macro-systematic French consumer/services short wave on April 8**. Same day. Multiple names. Multiple funds (Marshall Wace, DE Shaw, Millennium, BlackRock, AQR, Capital Fund Mgmt, Squarepoint). This is either (a) pair trade / basket short against a country ETF or sector ETF going long, (b) sector-wide thesis (e.g., French consumer cyclical deceleration, French political risk around a specific event on/around Apr 8), or (c) rolling of existing positions that happened to hit disclosure thresholds simultaneously.

**Key adversarial question**: Does the macro systematic nature REDUCE or ELIMINATE the single-name edge? **Answer: Reduces significantly.** When every L/S fund adds the same name on the same day, there's no information asymmetry — it's consensus crowding. The *surprise* in the signal is the basket composition, not any individual name.

---

## Scoring — Top Two Candidates

### VIRI.PA — Viridien (ex-CGG)
- Signal strength: 3.0 (moderate crowding, 4 holders 3.31%)
- Catalyst clarity: 2.5 (no scheduled catalyst, Q1 2026 results TBD, new CEO June 3)
- Info asymmetry: 3.5 (mid-cap oil services, thinly covered)
- R/R: 3.5 (-49% YoY, volatile)
- Edge decay: 3.5 (shorts still building Apr 8)
- Convergence: 1.0 (none)
- **Fundamental alignment: 2.5** — this is the killer. Viridien posted strong 2025 ($1.17B rev +4%, $551M EBITDA +21%, $71M net income +40%, $107M FCF above guidance). Bearish shorts must be betting on Q1 2026 miss or 2027 cyclical downturn. Hard to have edge vs specialist oil services shorts.

**Unweighted sum: 19.5/35 → estimated weighted ~23-24. Below candidate floor (28). Watchlist only.**

### SW.PA — Sodexo
- Signal strength: 3.0 (5 holders 3.69%)
- Catalyst clarity: 2.0 (no scheduled event)
- Info asymmetry: 2.0 (€5.75B mega-cap, 15+ analyst coverage)
- R/R: 3.0 (-31.5% off high, established business)
- Edge decay: 3.5 (fresh entry Apr 8)
- Convergence: 1.0
- Fundamental alignment: 3.0 (food services / contract caterer, cycle headwinds credible)

**Unweighted sum: 17.5/35 → estimated ~21-22. Below candidate floor. Not even watchlist priority.**

---

## Final Verdict

**Zero new candidates from French short wave.** All either (a) fail entry signal validation (UBI, EDEN, ELIOR, ACCOR rallying during short build), (b) lack info asymmetry (SW.PA mega-cap), or (c) have strong fundamentals that conflict with the bearish thesis (VIRI.PA).

**Watchlist additions**:
- **VIRI.PA — watchlist 23-24**, monitor for Q1 2026 earnings date announcement. If Q1 miss + short pile continues + price breaks <€110, re-score. Potential trigger event.
- **SW.PA — informational only**, not watchlist.

**Pattern note for methodology**: "Multiple new BaFin/AMF shorts on same day across a sector basket" is a high-false-positive pattern. These are typically L/S fund macro pair trades using a whole basket to hedge against long exposures. Going forward, ESMA signals where ≥3 new positions disclosed on same day across unrelated names should trigger BASKET ANALYSIS first, not individual scoring.

**New DECISIONS.md entry candidate**: D-035 — ESMA same-day basket short signals deprioritized vs isolated name signals.

---

## Sources

- [Viridien IR news](https://www.viridiengroup.com/newsroom)
- [Ubisoft Entertainment Yahoo](https://finance.yahoo.com/quote/UBI.PA/)
- [AMF short position disclosures](https://www.amf-france.org)
- Signals file: `signals/esma_short_20260410_110224.json`
