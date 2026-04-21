# Strategy — Hong Kong HKEx

**Exchange:** Hong Kong Stock Exchange (Main Board + GEM)
**MIC:** XHKG
**Language:** English and Traditional Chinese (filings are bilingual by rule — both versions are authoritative)
**Translation required:** Generally no (English version is typically available) — but translation confidence check applied when only Chinese is available or when English appears to be machine-translated summary.
**Build phase:** 5
**Status:** STUB — to flesh out in Phase 5 after Canada is stable.

---

## 1. Data source

HKEXnews is the official disclosure portal. Listed issuers must publish announcements simultaneously in English and Chinese during trading hours.

**Planned primary endpoint (UNVERIFIED — probe at Phase 5):**

- `https://www1.hkexnews.hk/listedco/listconews/advancedsearch/search_active_main.aspx` — filing search.
- `https://www.hkexnews.hk/listedco/listconews/sehk/today.htm` — today's announcements.
- RSS feeds by issuer.

## 2. Filing categories of interest

| HKEx announcement type | Signal type |
|------------------------|-------------|
| Inside information announcement (Rule 13.09) | `inside_information` |
| Profit warning / profit alert | `profit_warning` |
| Positive profit alert | `positive_profit_alert` |
| Connected transaction (Chapter 14A) | `connected_transaction` |
| Very substantial acquisition/disposal (Chapter 14) | `very_substantial_transaction` |
| Discloseable transaction | `discloseable_transaction` |
| Voluntary general offer / mandatory general offer (Code on Takeovers) | `general_offer` |
| Disclosure of Interests (DI) forms (SFO Part XV) | `disclosure_of_interests` |
| Results announcement (interim / annual) | `results_announcement` |
| Placing / rights issue / open offer | `capital_raising` |
| Suspension of trading | `trading_suspension` |
| Resumption announcement | `trading_resumption` |

## 3. Signal filters (Stage 1 triage)

- Main Board preferred; GEM accepted only when strength_estimate ≥ 4.
- Stock code (4-digit) + `.HK` resolves via yfinance.
- Market cap ≥ USD $300M.
- H-share and red-chip issuers (PRC-domiciled, HK-listed) included — these are often uncovered by Western research.
- Exclude property-REIT and synthetic-fund structures unless explicitly carrying a corporate catalyst.

## 4. Entity resolution (D-003)

OpenFIGI: `{"idType": "TICKER", "idValue": "<4-digit code>", "micCode": "XHKG"}`.

Cross-listing awareness:
- A+H dual listings: same PRC issuer with A-share on Shanghai/Shenzhen and H-share on HKEx. `issuer_figi` will link them, but A-shares are out of scope (no Tool 2 scanner for Shanghai/Shenzhen).
- ADR echoes: Alibaba, Tencent variants, etc. Flag `cross_listed_on`.
- UK dual listings: HSBC, Standard Chartered — LSE scanner will also catch these.

## 5. Translation integrity

Bilingual filings are the norm. Scanner prefers the English version. If only Chinese is available (rare, typically for GEM micro-caps), apply D-002:

Critical flip-error phrases (Traditional Chinese):
- 增加 / 減少 (increase / decrease)
- 高於 / 低於 (above / below)
- 預期 / 預計 (expected / projected)
- 不 (negation prefix)

## 6. Signal output

Standard schema. `company_name_local` = Traditional Chinese when available. `raw_data.hkex_announcement_type` captures the specific chapter/rule reference.

## 7. Deep dive checklist

- For connected transactions: counterparty identity (parent, controlling shareholder, related party), pricing vs. independent valuation, independent shareholder approval status.
- For very substantial transactions: shareholder vote timing, conditions precedent, break fee.
- For DI forms: filer identity (strategic, passive fund, activist), threshold (5% / 10% / rounded percentage-point triggers).
- For profit warnings: magnitude, stated reason, currency effects (many HK issuers have PRC operations with RMB exposure).
- Web research layer: SCMP, HKEJ, Caixin, Bloomberg Asia, Reuters.
- PRC regulatory overlay: CSRC, PBOC, SAMR approvals can block HK deals.

## 8. Known risks

- **PRC policy risk is a recurring kill condition.** Many HK-listed issuers are subject to Beijing regulatory shifts that don't surface in HKEx filings until late.
- **Opacity of H-share parent chains.** Connected transactions can hide parent-company value extraction. Skeptical review required.
- **Trading suspensions are common.** Resumption conditions often opaque. Scanner should flag but not auto-generate candidates from suspensions alone.

## 9. Tool file

`tools/hkex_scanner.py` — Phase 5.
