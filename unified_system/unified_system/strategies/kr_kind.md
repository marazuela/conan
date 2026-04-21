# Strategy — Korea KIND (Korea Investor's Network for Disclosure)

**Exchange:** Korea Exchange — KOSPI (main board), KOSDAQ (venture board)
**MIC:** XKRX (KOSPI), XKOS (KOSDAQ)
**Language:** Korean (native). Some large-cap issuers publish English summaries voluntarily.
**Translation required:** Yes — in-session Claude translation with confidence scoring per D-002.
**Build phase:** 6
**Status:** STUB — to flesh out in Phase 6 after Hong Kong is stable.

---

## 1. Data source

KIND is the official disclosure portal operated by KRX. All KOSPI and KOSDAQ issuers file material disclosures here.

**Planned primary endpoint (UNVERIFIED — probe at Phase 6):**

- `https://kind.krx.co.kr/disclosure/todaydisclosure.do` — today's disclosures.
- `https://kind.krx.co.kr/` — portal landing.
- DART (금융감독원 전자공시시스템) — Financial Supervisory Service filing system, complementary to KIND for statutory filings. `https://dart.fss.or.kr/`.

Use KIND for market disclosures (earnings, M&A, governance) and DART for statutory filings (annual/quarterly reports, major-shareholder reports, prospectuses).

## 2. Filing categories of interest

| Korean disclosure type | Signal type |
|------------------------|-------------|
| 주요사항보고서 (Major matter report) | `major_matter_report` |
| 공시번복 / 공시정정 (disclosure correction/reversal) | `disclosure_correction` |
| 영업정지 / 영업양도 (business suspension/transfer) | `business_transfer` |
| 합병/분할 (merger/spin-off) | `merger_spinoff` |
| 타법인 주식 취득 (acquisition of shares in other company) | `stake_acquisition` |
| 주식 등의 대량보유상황보고서 (5%+ shareholder report) | `major_shareholder_change` |
| 공개매수 (tender offer) | `tender_offer` |
| 자기주식 취득/처분 (treasury share buy/sell) | `buyback_announcement` |
| 실적 공시 (earnings disclosure) | `earnings_disclosure` |
| 실적 전망 수정 (earnings guidance revision) | `guidance_revision` |
| 상장폐지 / 관리종목 지정 (delisting / administrative issue) | `delisting_warning` |

## 3. Signal filters (Stage 1 triage)

- KOSPI preferred; KOSDAQ accepted when strength_estimate ≥ 4.
- Ticker (6-digit numeric code) + `.KS` (KOSPI) or `.KQ` (KOSDAQ) resolves via yfinance.
- Market cap ≥ USD $300M.
- Chaebol-affiliate awareness: Samsung, SK, LG, Hyundai, Lotte, Hanwha groups have many cross-held entities — capture group-level structural signals.

## 4. Entity resolution (D-003)

OpenFIGI: `{"idType": "TICKER", "idValue": "<6-digit code>", "micCode": "XKRX"}` or `"XKOS"`.

Cross-listing awareness: some Korean issuers have US ADRs (KB Financial, POSCO, LG Display). Flag `cross_listed_on`.

## 5. Translation integrity

Per D-002. Korean critical flip-error phrases:
- 증가 / 감소 (increase / decrease)
- 상향 / 하향 (upward / downward revision)
- 초과 / 미달 (exceed / fall short)
- 예정 / 전망 (scheduled / forecast)
- 않다 / 없다 (not / none — multiple negation forms)
- ~지 않 (auxiliary negation)

Double-check honorific and formal-register verb endings, which can shift meaning in guidance language.

## 6. Signal output

Standard schema. `company_name_local` = Korean (Hangul). `raw_data.kind_category` captures the Korean disclosure category.

## 7. Deep dive checklist

- For major shareholder reports: filer identity (chaebol holding company, foreign activist like Elliott/Dalton, domestic pension — NPS is a frequent filer), direction.
- For merger/spin-off: appraisal rights, minority shareholder protection mechanisms, court approval timeline.
- For delisting warnings: KRX administrative issue process, remediation timeline.
- For chaebol restructurings: parent-subsidiary swap ratios, succession planning implications.
- Web research layer: Korea Economic Daily (한국경제), Maeil Business (매일경제), Chosun Biz, Reuters Seoul.
- Corporate governance overlay: Korea's "Corporate Value-up" program (2024+) changes disclosure incentives — factor into thesis.

## 8. Known risks

- **Chaebol opacity.** Related-party transactions are frequent and nuanced. Deep-dive must check cross-holding flow.
- **Translation load is heavy.** Korean Tanshin-equivalents (earnings disclosures) can be 30+ pages. Triage before full translation.
- **Short-selling restrictions.** Korean short-sale rules have changed multiple times 2023–2026. Affects Risk/Reward scoring for short theses — apply current rules at scan time.
- **NPS (National Pension Service) as shareholder.** NPS holds large stakes in many KOSPI names; its vote/divestment actions are slow but consequential.

## 9. Tool file

`tools/kind_scanner.py` — Phase 6.
