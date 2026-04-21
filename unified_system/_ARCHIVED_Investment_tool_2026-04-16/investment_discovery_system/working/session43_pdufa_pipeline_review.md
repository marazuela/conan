# Session 43 — PDUFA Pipeline Review & Data Quality

## Purpose
Review all PDUFA entries for data quality, mcap eligibility, and candidate potential.

## Market Cap Screening (Apr 14 yfinance)

| Ticker | Mcap | Passes $215M? | Notes |
|--------|------|---------------|-------|
| TVTX | $2.7B | ✅ | Active candidate |
| AXSM | $9.1B | ✅ | Active candidate |
| VERA | $3.2B | ✅ | Active candidate |
| MNKD | $794M | ✅ | Watchlist |
| VRDN | — (not re-pulled) | ✅ (prev $1.4B) | Watchlist |
| ARVN | — (not re-pulled) | ✅ (prev ~$1B) | Watchlist |
| HROW | — (not re-pulled) | ✅ (prev ~$1.1B) | Watchlist |
| ZLAB | $2.3B | ✅ | Low priority — multi-indication approved |
| ACHV | $186M | ❌ Below floor | Disqualified. BTD + first new smoking cessation drug in 20yr but too small. |
| UNCY | $167M | ❌ Below floor | Disqualified |
| CING | $67M | ❌ Below floor | Disqualified |
| ARQT | $3.0B | ✅ | Pediatric label expansion (low binary) |
| LNTH | $5.2B | ✅ | Diagnostic imaging (low binary) |
| IONS | $12.4B | ✅ | Large cap, less binary risk |
| AZN | Mega-cap | ✅ | Far too large for binary play |
| PFE | Mega-cap | ✅ | Far too large for binary play |
| SVRA | $1.2B | ✅ | molbreevi for aPAP. PDUFA Aug 22. Worth future look. |
| CAPR | $1.8B | ✅ | deramiocel for DMD. PDUFA Aug 22. |
| PRAX | $8.8B | ✅ | relutrigine for epilepsy. PDUFA Sep 27. Large cap. |
| SRRK | $5.7B | ✅ | **SI 19.4%** — highest in pipeline. apitegromab for SMA. PDUFA Sep 30. Squeeze potential. Deep dive at T-30. |
| BEREN | N/A | ❌ Ticker not found | May be pre-IPO or different ticker |
| ORCA | N/A (private) | ❌ Not publicly traded | **DATA QUALITY ISSUE**: Orca Bio is private. Remove from watchlist or tag as non-tradeable. |

## Data Quality Issues Found

1. **ORCA/Orca Bio**: Not publicly traded. PDUFA Jul 6 for Orca-T (allogeneic cell therapy for heme malignancies). The entry should be tagged as non-tradeable in the watchlist JSON. The PDUFA pipeline auto-discovery picked this up from ClinicalTrials.gov/EDGAR but there's no public equity to trade.

2. **BEREN**: Ticker not found in yfinance. Adrabetadex for Niemann-Pick C. May be listed under different ticker or still private/pre-IPO. Needs verification.

3. **ACHV**: Just below $215M floor ($186M). If mcap recovers above $215M, could be a strong candidate — Breakthrough Therapy, first new class in 20yr, PDUFA Jun 20. **Flag for re-check at T-30 (~May 20).**

## Future Deep Dive Schedule

| Ticker | Trigger | Target Date | Reason |
|--------|---------|-------------|--------|
| ARVN | T-20 | ~May 15 | First PROTAC in oncology. Pfizer partner. |
| ACHV | T-30 if mcap ≥$215M | ~May 20 | Smoking cessation BTD. Currently below floor. |
| SRRK | T-30 | ~Aug 31 | SI 19.4% + SMA PDUFA Sep 30. Squeeze potential. |
| SVRA | T-30 | ~Jul 23 | molbreevi for aPAP. Rare disease BLA. Aug 22 PDUFA. |
| CAPR | T-30 | ~Jul 23 | deramiocel for DMD. Aug 22 PDUFA. |
