# Candidate: SEM — Select Medical Holdings WCAS+Ortenzio Take-Private ($16.50 cash)

> **Score**: 22.5 / 42.5 — **WATCHLIST** (below 28 active threshold)
> **Status**: Watchlist — spread too tight for active candidate; monitor for blow-out triggers
> **Source Strategy**: EDGAR keyword scanning (mna category — via S64 strength-5 rotation backlog triaged in S66)
> **Date Identified**: 2026-04-16 (S66 triage); formalized as candidate 2026-04-16 (S67)
> **Last Updated**: 2026-04-16 (S67)

---

## Company Overview

- **Name**: Select Medical Holdings Corporation
- **Ticker / CIK**: SEM / 0001320414
- **Exchange**: NYSE
- **Sector**: Health Care — Medical Care Facilities (critical illness recovery hospitals, inpatient rehabilitation, outpatient rehabilitation, occupational health)
- **Market Cap**: ~$2.03B (2026-04-16)
- **Current Price**: $16.38 (2026-04-16 yfinance fast_info)
- **Unaffected Price**: $14.01 (pre-announcement reference per PREM14A)

---

## Thesis

On **2026-04-15**, SEM filed a **PREM14A** (accession 0001104659-26-043505) and **SC 13E-3** (accession 0001104659-26-043531) disclosing a take-private transaction at **$16.50 per share all-cash**. The buyer consortium is **Welsh, Carson, Anderson & Stowe XIV, L.P. (WCAS)** in partnership with the **Ortenzio family** (the founding family, via rollover equity). Merger Agreement was signed **March 2, 2026**. Corporate structure: Stallion Group Parent → Stallion Intermediate → Stallion MergerSub → merges with and into Select Medical Holdings, with Select surviving as a wholly-owned sub of the new private holdco.

At current price $16.38, the gross spread is only **$0.12 = 0.73%**. Outside Date is **December 1, 2026** (~7.5 months from today), implying an annualized IRR of ~1.1% if closes at the Outside Date — **below risk-free** (current 3M T-bill ~4%+). Deal certainty is decent (experienced PE sponsor + family rollover eliminates management alignment risk; no go-shop mentioned), but the spread is too tight to be compelling as an active merger-arb candidate.

**Why watchlist not dead**: The thesis flips meaningful on **spread blow-out** events. Specifically, (a) HSR second request / DOJ antitrust pushback, (b) shareholder litigation (Special Committee sued 658 times in proxy — need to verify litigation status), (c) WCAS funding disruption, (d) material-adverse-change event. Any of these widens the spread from 0.73% → 4–8% and re-prices the risk/reward.

---

## Signal Evidence

| Signal | Source | Date | Detail |
|--------|--------|------|--------|
| PREM14A preliminary proxy | SEC EDGAR | 2026-04-15 | Accession 0001104659-26-043505; 1,354,627 chars plain-text extracted to `working/s66/SEM_PREM14A_text.txt`; triggered EDGAR mna strength-5 signal |
| SC 13E-3 going-private statement | SEC EDGAR | 2026-04-15 | Accession 0001104659-26-043531; required for affiliated going-private transactions (Rule 13e-3) |
| Merger Agreement signature | PREM14A disclosure | 2026-03-02 | WCAS + Ortenzio family rollover holders |
| Ortenzio references count | PREM14A parse | 2026-04-15 | 308 mentions — strong confirmation of family rollover structure |

---

## Deal Terms (verified from PREM14A + SC 13E-3)

- **Consideration**: $16.50 per Company Share, all cash, without interest, subject to tax withholding
- **Premium**: ~18% over $14.01 unaffected price; ~25% over 90-day VWAP (per S66 extraction)
- **Buyer Consortium**:
  - Welsh, Carson, Anderson & Stowe XIV, L.P. ("WCAS", Parent)
  - Ortenzio family (Rollover Holders — retain equity interest in private SuccessorCo)
- **Merger Structure**: Reverse triangular — Stallion MergerSub merges into Select Medical Holdings; Select survives as wholly-owned sub of new private holdco (Stallion Intermediate)
- **Merger Agreement Date**: March 2, 2026
- **Termination Fee (Company → Parent)**: $66.504 million (~3.3% of deal value)
- **Reverse Termination Fee (Parent → Company)**: $133.010 million (~6.5% of deal value)
- **No go-shop**: confirmed (0 instances of "go-shop" in PREM14A)
- **Outside Date**: **December 1, 2026** — if Merger not consummated by this date, either party may terminate (subject to carve-outs for antitrust extension)
- **Special Committee**: Yes — SEM formed a Special Committee (658 references in PREM14A), signaling proper governance process given affiliated Rollover Holders
- **Appraisal Rights**: DGCL §262 available for dissenting shareholders
- **Regulatory**: HSR filing required (24 "HSR" references); standard antitrust clearance; no foreign investment approvals appear material

---

## 7-Dimension Scoring

| Dimension | Score (1-5) | Weight | Weighted | Rationale |
|-----------|-------------|--------|----------|-----------|
| Signal Strength | 5 | ×2 | 10 | Signal IS the event — signed definitive agreement + PREM14A + SC 13E-3 |
| Catalyst Clarity | 4 | ×1 | 4 | Outside Date Dec 1, 2026; clear close path; small uncertainty around shareholder-vote timing |
| Info Asymmetry | 1 | ×1.5 | 1.5 | Public deal, low asymmetry. WCAS is a major PE firm with broad news coverage |
| Risk/Reward | 1 | ×1 | 1 | 0.73% gross spread vs 7.5-month timeline ≈ 1.1% annualized — below risk-free. Unattractive without spread widening |
| Edge Decay | 1 | ×1 | 1 | Fully diffused post-announcement |
| Liquidity | 4 | ×1 | 4 | $2B mcap, reasonable ADV |
| Catalyst Timeline | 1 | ×1 | 1 | Long horizon (~7.5 months) with unfavorable spread |
| **Base Total** | | | **22.5** | **Below 28 threshold — WATCHLIST** |

---

## Kill / Exit Conditions

- **K-SEM-1 (hard)**: Deal close → spread resolves at $16.50; no upside from current
- **K-SEM-2 (hard)**: Deal breaks (MAC / HSR block / WCAS funding failure) → stock likely reverts to $14.01 unaffected (or lower) = **-14% downside**
- **K-SEM-3 (hard)**: Shareholder litigation settles with bump in consideration → upside scenario, typical 2-5%
- **K-SEM-4 (soft — WATCHLIST TRIGGER)**: HSR second request issued → spread widens to 3-5% → re-evaluate as active candidate
- **K-SEM-5 (soft — WATCHLIST TRIGGER)**: Shareholder vote delayed beyond August 2026 → spread widens → re-evaluate

---

## Watchlist Monitoring Cadence

**Weekly checks**:
1. Spread: SEM price vs $16.50 — alert if spread >1.5%
2. HSR status (FTC/DOJ filings and any second requests)
3. Shareholder litigation dockets (Delaware Chancery + NY state court)
4. DEF 14A (definitive proxy) filing — typically 20-45 days after PREM14A
5. WCAS / Ortenzio public statements or news

**Event triggers to upgrade to active**:
- Spread >3% (would imply $16.00 or below at $16.50 deal price)
- Any deal-break signal (MAC invoked, HSR block, funding failure)
- Topping bid from strategic acquirer (unlikely but possible in hospital sector)

**Event triggers to archive**:
- Deal closes
- Definitive vote passes with >95% of shares — close-certainty maximal; spread likely collapses to 0.2%

---

## Sources

- PREM14A 2026-04-15: https://www.sec.gov/Archives/edgar/data/1320414/000110465926043505/tm268269-1_prem14a.htm
- SC 13E-3 2026-04-15: https://www.sec.gov/Archives/edgar/data/1320414/000110465926043531/tm2611660-1_sc13e3.htm
- SEC submissions JSON: https://data.sec.gov/submissions/CIK0001320414.json
- PREM14A text extract: `working/s66/SEM_PREM14A_text.txt` (1,354,627 chars)
- Primary-source triage analysis: S66 SESSION_STATE (triaged as single genuine MNA finding among 8 EDGAR strength-5 signals)
- WCAS firm profile (public): https://www.wcas.com/

---

## Update Log

| Date | Update |
|------|--------|
| 2026-04-16 (S66) | **Triaged** as 1 genuine M&A among 8 EDGAR strength-5 mna false-positives (see SESSION_STATE warning #20). Primary-source parse of PREM14A + SC 13E-3 confirmed Welsh Carson + Ortenzio $16.50 cash deal. Score 22.5 — watchlist tier. |
| 2026-04-16 (S67) | Candidate file formalized. Outside Date Dec 1, 2026. Termination fees $66.5M / $133.0M. No go-shop. Spread 0.73%. Current price $16.38. Monitoring cadence set: weekly spread + HSR + litigation checks. No new filings Apr 16. |

---

*Created 2026-04-16 by scheduled Session 67. Watchlist status; monitor weekly unless event trigger.*
