# S33 EDGAR + FDA Signal Triage

**Date**: 2026-04-12 (S33)

## EDGAR — 28 signals, all archived

All 28 signals from the S33 EDGAR scan fail triage. Breakdown below.

### Category 1: mcap = $0 / null / unresolved (22 signals)

Failed triage because the ticker couldn't be resolved to a publicly traded entity above the $215M floor. These are pre-IPO S-1s, private trusts, SPAC SPACs, ABS-15G securitization trusts, 497 mutual-fund prospectuses, and similar:

- OXO, Inc (S-1/A) — pre-IPO
- America Great Health (10-K) — OTC micro
- GATC HEALTH CORP (1-SA) — Reg A exempt offering
- Alpex Acquisition Corp (S-1, S-1/A) — SPAC
- Akston Biosciences Corp (S-1/A) — pre-IPO biotech
- C2 Capital Group, Inc. (S-1) — pre-IPO
- Conexeu Sciences Inc. (S-1/A) — pre-IPO
- Aeon Acquisition I Corp (S-1) — SPAC
- Air Water Ventures Ltd (F-4) — foreign M&A registration
- TPG Twin Brook Capital Income Fund (ARS) — private BDC
- PennyMac Corp (ABS-15G) — securitization
- Morgan Stanley Capital I Inc. (ABS-15G) — securitization
- Apogee Acquisition Corp (AACP) (8-K) — SPAC
- Encore Inc. (S-1) — pre-IPO
- VMC Asset Depositor III (ABS-15G) — securitization
- Hemab Therapeutics Holdings (S-1) — pre-IPO biotech
- Seaport Therapeutics, Inc. (S-1) — pre-IPO biotech
- FedEx Freight Holding Company (FDXF) (10-12B/A EX-4.1) — spin-off registration, mcap null
- Exchange Place Advisors Trust (497) — mutual fund prospectus
- HawkEye 360, Inc. (S-1) — pre-IPO (TYTO reorg)
- NEW YORK LIFE INS & ANNUITY VAR UNIV LIFE (N-6/A) — variable annuity prospectus
- Titan Holdings Corp (KMCM) (S-4/A) — pre-merger

None are investable candidates under our $215M floor.

### Category 2: Resolved ticker, but archive on content

#### EVOH — EvoAir Holdings Inc. — $625M — "going concern" 10-Q — **ARCHIVE**

Verified the 10-Q body. EvoAir is a micro-float shell: in 2024 the former majority shareholder sold his entire 67.34% stake to "WKL Global Limited" for **$100 total consideration**. The company has done a 1-for-4 reverse split in September 2024. 10-Q net loss $2M on near-zero revenue, auditor note "has not yet established a sustainable ongoing source of revenue." Share count approximately 2.97M → $625M market cap is a low-float pump artifact, not a real market cap. Not shortable, not borrowable, not investable under our strategy. This is a classic shell game, not a distressed-equity signal. **Not a candidate. Noise, not signal.**

Source: https://www.sec.gov/Archives/edgar/data/1700844/000149315226016152/form10-q.htm

#### GIG — GigCapital7 Corp — $355M — "going concern" S-4/A EX-23.1 — **ARCHIVE**

GigCapital7 is a SPAC. The keyword match is in **EX-23.1 (auditor's consent)** of an S-4/A filing. S-4/A is the proxy/prospectus for a planned business combination. Auditor consent exhibits on SPAC S-4/As routinely carry the target company's historical auditor report, which for any pre-merger company commonly includes a going-concern qualification. This is textbook D-029-A — routine M&A disclosure boilerplate — **not a distress signal for the SPAC or the target**. Archive.

Source: https://www.sec.gov/Archives/edgar/data/2023730/000119312526151242

#### EXPE — Expedia Group — $27.9B — "waiver" 8-K EXHIBIT 4.2 — **ARCHIVE (D-029-A)**

Mega-cap ($27.9B). 8-K Exhibit 4.2 is almost universally an indenture supplement (note issuance or amendment). "Waiver" language in such exhibits is standard indenture boilerplate referencing trustee waiver mechanics, event-of-default waivers, covenant-waiver procedures. This is D-029-A. A $27.9B mega-cap with a routine indenture filing is not a distress signal. Archive.

#### ASPI — ASP Isotopes — $560M — "waiver" 10-K EX-10.44 — **ARCHIVE (D-029-A)**

Verified the exhibit. EX-10.44 is **"Amendment No. 1 to Finance Agreement between TETRA4 Proprietary Limited and U.S. International Development Finance Corporation, dated as of March 30, 2020."** ASPI (ASP Isotopes) files this retroactively as an exhibit to its 10-K; it relates to ASPI's South African subsidiary TETRA4's long-standing DFC facility. The "waiver" keyword match is in the standard **"No Waiver"** boilerplate clause ("The execution, delivery, and effectiveness of this Amendment shall be limited precisely as written and... shall not be deemed to be a consent to any waiver..."). Textbook D-029-A. No new covenant stress — the amendment is 5 years old being filed as a backward-looking exhibit. Archive.

Source: https://www.sec.gov/Archives/edgar/data/1921865/000119312526151294/aspi-ex10_44.htm

---

## FDA PDUFA Pipeline Pulse — 14 signals

One **imminent** (TVTX Mon Apr 13), 1 approaching (AXSM Apr 30), 1 near (ZLAB May 10), rest watchlist.

| Ticker | PDUFA | Status | Notes |
|--------|-------|--------|-------|
| **TVTX** | 2026-04-13 | **T-0 DECISION DAY** | FSGS sNDA — operative candidate, score 29.75 |
| **AXSM** | 2026-04-30 | T-12 | ADA sNDA — score 30.75, running kill sweeps |
| **ZLAB** | 2026-05-10 | watchlist | Zai Lab — NEW ENTRY this scan, PDUFA ~18 trading days out |
| MNKD | 2026-05-29 | watchlist | S31 archived, held below threshold |
| ARVN | 2026-06-05 | watchlist | existing watchlist |
| PFE | 2026-06-15 | watchlist | mega-cap, noise-dominated |
| VRDN | 2026-06-30 *(was 06-12)* | hold, score 30.00 | **Date shifted** — was 2026-06-12 last session |
| ARQT | 2026-06-29 | watchlist | existing |
| LNTH | 2026-06-29 | watchlist | existing |
| IONS | 2026-06-30 | watchlist | existing |
| AZN | 2026-06-30 | watchlist | mega-cap |
| ORCA | 2026-07-06 | watchlist | **NEW — private? flag** |
| VERA | 2026-07-07 | watchlist, deep-dive in progress | score pending |
| CORT | 2026-07-11 | watchlist | **NEW ENTRY this scan** |

### Items needing verification

1. **VRDN date shift** — FDA signal shows 2026-06-30, SESSION_STATE says 2026-06-12. This could be the tool auto-discovering a new date from EDGAR or ClinicalTrials.gov, or it could be a conflict. **Action**: Verify with WebSearch / TVT Section 7 style re-check. Candidate file should be updated.
2. **ZLAB 2026-05-10** — new PDUFA signal, first time appearing. Zai Lab is a $2-3B Chinese biotech. Worth a watchlist entry confirmation.
3. **ORCA** — flagged as "private" in the signal. If not publicly tradeable, this is a data artifact and should be filtered from the tool output. OrcaBio **is** a private company (stealth cell therapy). Wait — it did IPO in late 2024 as ORKA? Let me verify next session.
4. **CORT** — Corcept Therapeutics new PDUFA 2026-07-11. Need to identify which drug/indication triggered the date discovery.

These are all **watchlist-level** items — none rise to the 28+ candidate threshold on first-touch. Logged for next-session investigation.

---

## Summary

- **0 new candidates** generated from S33 scan.
- **1 convergence alert** (AMT) — archived as false positive, see `working/session33_amt_convergence_triage.md`.
- **4 EDGAR items** inspected and archived (EVOH micro-float shell; GIG SPAC consent; EXPE mega-cap indenture; ASPI retrospective exhibit).
- **22 EDGAR items** filtered by mcap floor (SPACs, S-1s, ABS-15G, 497, N-6, private trusts).
- **4 FDA items** flagged for next-session verification (VRDN date shift, ZLAB new, ORCA private-flag, CORT new).

The 6-session clean sweep on TVTX holds into Monday. No new signals threaten existing candidate scores.
