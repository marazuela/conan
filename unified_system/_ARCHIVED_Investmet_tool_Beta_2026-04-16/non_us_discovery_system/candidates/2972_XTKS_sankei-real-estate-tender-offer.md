---
ticker_local: "2972"
mic: XTKS
ticker_plus_mic: 2972.XTKS
isin: null
figi: BBG00N8WDMQ9
issuer_figi: BBG00N8WCM62
company_name_local: Ｒ－サンケイＲＥ
company_name_en: "SANKEI REAL ESTATE Inc."
market_cap_usd_mm: 368
exchange: TDnet
country: JP
score: 35.0
convergence_bonus: 0
score_total: 35.0
convergence_strategy_count: 1
status: pending_deep_dive
thesis_direction: long
translation_confidence: 0.92
first_signal_date: 2026-04-14
last_updated: 2026-04-14
primary_catalyst_date: indefinite
cross_listed_on: []
related_signal_ids: ["82e86a8ac81b2ae7afc1a1256de5495d", "f17cc614aaad0cf0eaedb3e981a8ba49"]
signal_type: tender_offer
signal_category: takeover
scanner: tdnet
---

# SANKEI REAL ESTATE Inc. (2972.XTKS) — Tiger LPS / Lion LPS Tender Offer Terms Update

> **STATUS: pending_deep_dive.** Automated-scanner stub. Do not act on this alone.

## TL;DR

On 2026-04-14 a three-filing cluster on TDnet for Sankei Real Estate Investment Corporation (J-REIT, ticker 2972): Tiger LPS and Lion LPS have updated the terms of their public tender offer (公開買付け, TOB) for Sankei's investment units. Scanner classifies this as tender_offer, strength_estimate=5, thesis_direction=long, translation_confidence 0.92 (TOB pattern is unambiguous). Score 35 is immediate-route. Two sibling filings (progress report + correction notice) were merged by the convergence engine's hard rule (same issuer + same signal_type + same source_date).

## Source signals

- **Primary** — https://www.release.tdnet.info/inbs/140120260414503539.pdf — 2026-04-14 17:00 JST — TDnet — headline: "Tiger LPS及びLion LPSによるサンケイリアルエステート投資法人（証券コード：2972）投資口に対する公開買付けの買付条件等の変更に関するお知らせ" (Notice regarding change of TOB conditions)
- **Sibling (merged)** — 進捗報告 (progress report) — filed earlier same day
- **Sibling (merged)** — 訂正 (correction) — filed earlier same day

The cluster suggests this is an **updated / revised** TOB — most interesting outcome if the update is a price increase (offer bump).

## Translation notes

Japanese source. TOB (公開買付, kōkai-kaitsuke) is the Japanese tender-offer disclosure. Deep-dive must read the PDF to determine:
- Old vs. new offer price per investment unit
- Whether the acceptance period has been extended
- Acceptance rate to date (if disclosed)
- Whether the Sankei board's position has changed

## Company context

- Market cap: $368M USD (≈¥58B at 158.8 JPY/USD)
- Structure: J-REIT (Japanese Real Estate Investment Trust). Investment units trade on TSE REIT market (code 2972).
- Sector: Office-focused J-REIT associated with Sankei Shimbun / Fuji Media Holdings group.
- Trading status: N (normal) per scanner.
- Recent price action: TODO — deep-dive fetch 30/90-day.
- Unit-holder structure: J-REITs historically have high retail ownership; acceptance dynamics differ from corporate TOBs.

## Thesis statement (to be completed by deep-dive)

**Pending.** Direction pre-tagged long — target REIT units generally rally to offer price on TOB announcements, and if this filing is an offer bump, there is further spread to close. Deep-dive must verify:
- What changed in the revised terms (price? acceptance threshold? period?)
- Who Tiger LPS / Lion LPS are (investment SPVs — look for ultimate beneficial owner; likely a private real-estate fund)
- Regulatory path: J-REIT TOBs involve FSA review but no separate competition clearance in most cases
- Sankei board position (recommended / neutral / opposed)
- Comparable J-REIT TOBs (Daiwa Office Investment 2016, Japan Hotel REIT 2020) for precedent timeline

## Steelman of the opposite view

**Pending deep-dive.** Possible angles:
- Terms revision is negative (price cut or threshold raise) — offer struggling
- Sankei board opposition; management-side defensive plan (white-knight, scheme to prevent change of control)
- Underlying NAV of the portfolio drops below offer price on re-appraisal
- Tiger/Lion LPS acceptance conditions are not met; offer lapses and units fall back

## Web research layer (mandatory — pending)

- Nikkei / Reuters coverage of Sankei Real Estate TOB
- Tiger LPS / Lion LPS identity (search J-REIT custodian filings, SPV filings)
- J-REIT analyst coverage (SMBC Nikko, Mitsubishi UFJ Morgan Stanley, Mizuho)
- Sankei board rationale and independent advisor opinion
- JPX REIT Index weighting / ETF inclusion impact if delisted

## Kill conditions

- **Kill 1:** Terms revision is a price cut. Flip thesis or cut position to 0.
- **Kill 2:** Sankei board recommends rejection. Elevated break risk.
- **Kill 3:** Acceptance rate below threshold at period expiry. Observable in final acceptance filing.
- **Kill 4:** Counter-bid from a white-knight J-REIT at a lower price but with structural preference. Observable in new Rule 27 TDnet filing.
- **(refine with specific JPY thresholds during deep-dive)**

## Catalyst map (skeleton)

| Event | Date/Window | Entry trigger | Exit trigger |
|-------|-------------|---------------|--------------|
| TOB amendment effective date | T+0 | N/A | - |
| Sankei board response | T+5 to T+10 | - | - |
| Acceptance period close | Per amended terms | - | Settle to cash |
| Payment / settlement | T+30 typical | - | - |

## Position sizing

Satellite-arbitrage (2-5%). J-REIT liquidity is generally good on TSE REIT market. Arbitrage spread tight if offer is well-received; wider if board pushback creates break risk. Size to acceptable annualized IRR given days-to-close.

## Source traceability

- https://www.release.tdnet.info/inbs/140120260414503539.pdf — retrieved 2026-04-14 via tdnet scanner
- OpenFIGI: ticker=2972 mic=XTKS → figi=BBG00N8WDMQ9, issuer_figi=BBG00N8WCM62 (cached 2026-04-14)
- Market cap: yfinance 2972.T, ≈¥58B × 0.006298 USD/JPY → $368M USD (2026-04-14)
- Related signal_ids: 82e86a8ac81b2ae7afc1a1256de5495d (progress report), f17cc614aaad0cf0eaedb3e981a8ba49 (correction)
