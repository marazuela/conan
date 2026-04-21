---
ticker_local: "6197"
mic: XTKS
ticker_plus_mic: 6197.XTKS
isin: null
figi: BBG00CXKN2W7
issuer_figi: BBG00BGK7648
company_name_local: ソラスト
company_name_en: "Solasto Corporation"
market_cap_usd_mm: 637
exchange: TDnet
country: JP
score: 35.0
convergence_bonus: 0
score_total: 35.0
convergence_strategy_count: 1
status: pending_deep_dive
thesis_direction: long
translation_confidence: 0.92
first_signal_date: 2026-04-09
last_updated: 2026-04-15
primary_catalyst_date: indefinite
cross_listed_on: []
related_signal_ids: ["550feb1b10d6e0ac41a404efddbf697f", "f71138cdc30df65ce72c74fbc2aea6b6", "0e74a7a04cc4e5f4a1b64bf91809fd50"]
signal_type: mbo_announcement
signal_category: takeover
scanner: tdnet
---

# Solasto Corporation (6197.XTKS) — MBO / Tender Offer

> **STATUS: pending_deep_dive.** Automated-scanner stub. Do not act on this alone.

## TL;DR

On 2026-04-09 two paired TDnet filings for Solasto Corp (JP medical-clerical outsourcer): an MBO announcement (management buyout) plus a concurrent tender-offer filing. Scanner classified both as immediate-route (score 35, tc 0.92, direction=long). MBO + TOB pattern typically means management-led take-private at a premium to unaffected share price.

## Source signals

- **Primary (MBO)** — https://www.release.tdnet.info/inbs/140120260409500870.pdf — 2026-04-09 — signal_id 550feb1b10d6e0ac41a404efddbf697f
- **Primary (TOB)** — https://www.release.tdnet.info/inbs/140120260409500876.pdf — 2026-04-09 — signal_id f71138cdc30df65ce72c74fbc2aea6b6
- **Related** — 0e74a7a04cc4e5f4a1b64bf91809fd50 (merged by convergence engine)

## Translation notes

Japanese source. MBO = マネジメント・バイアウト; TOB = 公開買付. Pattern is unambiguous at tc=0.92. Deep-dive must read the PDFs to extract:
- Offer price per share
- Unaffected price (pre-announcement) and implied premium
- Sponsor PE firm (if any)
- Management rollover terms
- Squeeze-out / minority buyout path

## Company context

- Market cap: $637M USD (≈¥100B at 157 JPY/USD)
- Sector: Medical and nursing-care administrative outsourcing (back-office services to hospitals and care facilities)
- Trading status: N (normal)
- Founding-family / management ownership levels are relevant for MBO feasibility — fetch during deep-dive

## Thesis statement (to be completed by deep-dive)

**Pending.** Direction pre-tagged long — MBO/TOB announcements typically converge to offer price; merger-arb spread available. Deep-dive must verify:
- Offer price vs. unaffected and implied premium (typically 20–40%)
- PE sponsor identity (Bain, KKR, CVC, Japan Industrial Partners, MBKP all active in JP MBOs)
- Board recommendation & independent committee opinion
- Anti-monopoly review (JFTC) — generally formal for MBOs of this size
- Squeeze-out cash merger timeline post-TOB close (typically 60–90 days)

## Steelman of the opposite view

- Offer price is "lowball" vs. intrinsic value; shareholder opposition emerges (Oasis, Murakami Group, Strategic Capital).
- Competing bid materializes at higher price → current spread compresses or inverts.
- Management rollover is excessive / conflicted → independent committee opposes.
- Regulatory stall — JFTC approval delayed beyond normal window.

## Web research layer (mandatory — pending)

- Nikkei / Reuters / Bloomberg Solasto MBO coverage
- PE sponsor track record on JP MBOs
- Activist involvement (CommonWealth, 3D, Effissimo, Strategic Capital)
- Peer MBO multiples (Benesse 2023, Outsourcing 2024)

## Kill conditions

- **Kill 1:** Activist announces stake and opposition → spread widens on break risk. Trim/hedge.
- **Kill 2:** Superior proposal surfaces → re-evaluate on new terms.
- **Kill 3:** JFTC requests additional information; timeline slips >90 days → IRR falls below threshold.
- **Kill 4:** Scanner flags subsequent TDnet filing indicating delay, price revision, or withdrawal.

## Catalyst map

| Event | Date/Window | Entry trigger | Exit trigger |
|-------|-------------|---------------|--------------|
| MBO/TOB launch | T+0 (2026-04-09) | In progress | — |
| Board position fixed | T+5 to T+10 | Confirm support | Reject if opposition |
| Tender period | typically 20–30 business days | Ride spread | Close near offer |
| Squeeze-out cash merger | T+60 to T+90 | — | Settle to cash |

## Position sizing

Satellite-arbitrage (2–5%). $637M mcap is mid-liquidity for TSE; monitor average daily volume before sizing. Standard JP MBO spread is 2–4% annualized at tight levels, 5–10% with break-risk overhang.

## Source traceability

- https://www.release.tdnet.info/inbs/140120260409500870.pdf (MBO) — retrieved via tdnet scanner 2026-04-15
- https://www.release.tdnet.info/inbs/140120260409500876.pdf (TOB) — retrieved via tdnet scanner 2026-04-15
- OpenFIGI: ticker=6197 mic=XTKS → figi=BBG00CXKN2W7, issuer=BBG00BGK7648 (cached)
- Market cap: yfinance 6197.T → $637M USD
- Related signal_ids per convergence engine D-004 hard-merge.
