---
ticker_local: "6058"
mic: XTKS
ticker_plus_mic: 6058.XTKS
isin: null
figi: BBG002PJLR30
issuer_figi: BBG002PJLR21
company_name_local: ベクトル
company_name_en: "Vector Inc."
market_cap_usd_mm: 375
exchange: TDnet
country: JP
score: 31.0
convergence_bonus: 0
score_total: 31.0
convergence_strategy_count: 1
status: pending_deep_dive
thesis_direction: short
translation_confidence: 0.88
first_signal_date: 2026-04-14
last_updated: 2026-04-14
primary_catalyst_date: 2026-04-14
cross_listed_on: []
related_signal_ids: []
signal_type: impairment_loss
signal_category: results
scanner: tdnet
---

# Vector Inc. (6058.XTKS) — Special Gains & Losses Booking

> **STATUS: pending_deep_dive.** Automated-scanner stub. Do not act on this alone.

## TL;DR

On 2026-04-14 Vector Inc. (6058) announced the booking of both special gains and special losses (特別利益及び特別損失). Scanner matched on the impairment_loss pattern (特別損失.計上). thesis_direction=short, translation_confidence=0.88, score 31. The concurrent special-gain booking introduces ambiguity — net impact could be positive, neutral, or negative. Deep-dive must size both sides.

## Source signal

- https://www.release.tdnet.info/inbs/140120260414503199.pdf — 2026-04-14 15:30 JST — TDnet — Japanese-language notice; title-pattern match only; direction thesis provisional.

## Translation notes

Japanese source. The title "特別利益及び特別損失の計上に関するお知らせ" translates literally as "Notice regarding the booking of special gains and special losses." The co-presence of special gains weakens the short thesis relative to a pure impairment. Deep-dive must:
- Translate the PDF body to extract 特別利益 (gain) and 特別損失 (loss) amounts
- Determine net impact on the current-year forecast
- Identify the cause of each (asset sale? restructuring? subsidiary disposal?)

## Company context

- Market cap: $375M USD (≈¥60B at 158.8 JPY/USD)
- Sector: PR / advertising / influencer marketing — Vector runs Japan's largest PR firm, plus advertising and content subsidiaries.
- Trading status: N.
- Recent price action: TODO.
- Portfolio: Vector is acquisitive — special items may relate to subsidiary revaluation or disposal.

## Thesis statement (to be completed by deep-dive)

**Pending.** Direction provisional-short. Critical questions:
- Net impact of gains vs. losses — is this neutral or materially negative?
- Is the loss related to an impaired subsidiary that Vector was known to be carrying at stretched book value?
- Does this change FY26 guidance, or is it already embedded?
- If this is a "kitchen sink" quarter (book everything bad now), the post-news setup may be long, not short.

## Steelman of the opposite view

- Gains > losses, net boost to earnings
- Special loss is on a subsidiary being divested; cleaning up balance sheet pre-strategic refocus
- Vector's core PR business (high-margin, structural growth) untouched
- Market may misread the "loss" headline and oversell on day one

## Web research layer (mandatory — pending)

- Nikkei, Bloomberg Japan coverage
- Vector investor relations page for the full release
- Recent M&A history: has Vector bought anything that looks like it might now be impaired?
- Peer comparison: Sunny Side Up, ADK — any similar charges?

## Kill conditions

- **Kill 1:** Net impact is positive (gains > losses). Flip thesis.
- **Kill 2:** Loss is < 5% of market cap AND already in guidance. Downgrade to watchlist or discard.
- **Kill 3:** Management commentary signals "kitchen sink" — treat as long.

## Catalyst map (skeleton)

| Event | Date/Window | Entry trigger | Exit trigger |
|-------|-------------|---------------|--------------|
| Tanshin release for relevant quarter | TBD | N/A | - |
| Guidance revision (if any) | T+0 to T+14 | - | - |
| Post-close commentary | Next IR event | - | - |

## Position sizing

Small satellite (1-2%) until thesis is sharpened. Ambiguous signal — the gain+loss combo requires deep-dive before conviction.

## Source traceability

- https://www.release.tdnet.info/inbs/140120260414503199.pdf — retrieved 2026-04-14 via tdnet scanner
- OpenFIGI: ticker=6058 mic=XTKS → figi=BBG002PJLR30, issuer_figi=BBG002PJLR21 (cached 2026-04-14)
- Market cap: yfinance 6058.T, ≈¥60B × 0.006298 → $375M USD (2026-04-14)
