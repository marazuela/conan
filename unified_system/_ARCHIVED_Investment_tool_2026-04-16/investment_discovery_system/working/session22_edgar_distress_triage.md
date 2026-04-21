# Session 22 EDGAR Distress Rotation Triage

**Category**: distress (rotation index 2/4)
**Date**: 2026-04-10 06:11 UTC
**Total signals**: 30 (all `distress_keyword`, all str≥2)
**Budget**: wall-clock stopped at 35s after 50 unique hits

## High-strength (str=4) "going concern" hits — passed $215M floor

| Ticker | Mcap ($M) | Filing date | URL | Assessment |
|--------|-----------|-------------|-----|------------|
| **APAD** | 280.0 | 2026-04-09 | edgar/data/1956439 | NEW — above floor, needs review |
| **NUCL** | 265.7 | 2026-04-09 | edgar/data/2089283 | NEW — above floor, needs review |
| MUFG | 204,869.9 | 2026-04-08 | edgar/data/67088 | **NOISE** — Japanese mega-bank; "going concern" in risk-factor boilerplate of a 6-K/F-1 is standard. Ignore. |
| CYDY | 393.3 | 2026-04-08 | edgar/data/1175680 | **BELOW floor** (passes but micro-cap biotech, CytoDyn has been in perpetual going-concern state since 2020; non-actionable per S12 review) |
| GSMT | None | 2026-04-09 | edgar/data/1940243 | Mcap unresolved (likely SPAC shell per CIK 1940243) |
| GCGJ | None | 2026-04-08 | edgar/data/1765048 | Mcap unresolved (likely ADR/SPAC) |

## Lower-strength (str=2) hits above floor

| Ticker | Mcap ($M) | Keyword | Assessment |
|--------|-----------|---------|------------|
| MS | 282,893.2 | covenant breach | Morgan Stanley — noise, normal bank disclosure language |
| CRBG | 11,941.0 | waiver | Corebridge Financial — routine waiver/credit facility language |
| EQH | 10,827.9 | waiver | Equitable Holdings — routine |
| AMCX | 327.3 | waiver | AMC Networks — already known distress, not new |

## Ticker-less hits (CIK only)

21 signals resolved only to CIK without OpenFIGI ticker resolution. These are almost all shell-company / micro-cap / post-IPO pre-trading entities filing standard going-concern disclosures. Pre-filter working as designed — they'll be dropped at post-scan by the strength/mcap gate.

## Initial thesis assessment

- **APAD and NUCL are the only actionable signals.** Both in the $215-300M band where a going-concern disclosure is material but not already fully priced in. Need deep dive in candidate review step.
- **Proxy-season rotation problem resolved:** Distress category produced 2 candidates-worth of signals where activist produced 0 last session. Confirms D-030 rotation fix is correct.

## Deep-dive results (post web-research + SEC submissions API)

**APAD — REJECTED as noise.** `AParadise Acquisition Corp.` is a SPAC (triple-ticker APAD/APADR/APADU, SIC 7990). Last 8 filings are dominated by S-4/A and Rule 425 communications for a business combination with Enhanced Ltd. Going-concern language in an S-4/A is boilerplate for a blank-check company approaching its deadline — not a distress signal. The April 9 filing at `edgar/data/1956439/000162828026024523` is an S-4/A proxy supplement, not a distress disclosure.

**NUCL — REJECTED as noise.** `Eagle Nuclear Energy Corp.` (CIK 2089283, SIC 1090 Misc Metal Ores, tickers NUCL + NUCLW warrants). Last 10 filings include an S-1 + S-1/A stream (March 19 → April 9), a Schedule 13G, and multiple Form 3 insider initial ownership filings. This is a **pre-revenue post-de-SPAC nuclear/uranium company** in its early S-1 registration flow. Going-concern language in an S-1 is standard pre-revenue risk-factor disclosure, not a failing-business signal. The $265.7M market cap reflects the initial trust/listing value.

## Pattern and proposed fix (candidate for D-031)

**Pattern identified:** The distress rotation in April is contaminated by SPAC/de-SPAC/pre-IPO filings. These trigger "going concern" via standard S-1/S-4 boilerplate, not actual operating distress. The form signature is predictable:

- Tickers with warrant pairs (e.g., NUCL/NUCLW, APAD/APADR/APADU)
- Form types: S-1, S-1/A, S-4, S-4/A, 425, DRS
- CIKs in the 1,950,000–2,150,000 range (recent SEC registrations)

**Proposed fix (log as tool improvement Q-XXX):** Add a SPAC/pre-IPO filter to the EDGAR distress scanner:
1. Hard exclude forms: S-1, S-1/A, S-4, S-4/A, 425, DRS, DRS/A, N-CSR
2. Hard exclude SPAC ticker patterns via OpenFIGI name match (`Acquisition Corp`, `Blank Check`, `SPAC`)
3. Only accept distress keywords in 10-K, 10-K/A, 10-Q, 10-Q/A, 8-K (Item 2.04, 3.01, 4.02, 5.02)

This mirrors the D-030 fix for activist rotation (form whitelist) but for distress.

## Net result this rotation

- **Surviving signals: 0** (of 30 raw, 28 pre-filtered by mcap/strength, 2 killed in deep dive)
- **New candidates created: 0**
- **Watchlist additions: 0**
