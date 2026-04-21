# Session 28 — REPL PDUFA Resolution Check

**Date:** 2026-04-10 (PDUFA day)
**Scan time:** ~07:16 ET (11:16 UTC), pre-market, FDA not yet open for business
**Prior context:** S27 scan ran pre-market, zero April 8-Ks on CIK 1737953, Apr 8 crash -24.6%, Apr 9 flat/continued weakness

## Primary Source Check

### EDGAR CIK 0001737953 (latest 20 filings)
Latest 20 filings pulled via `data.sec.gov/submissions/CIK0001737953.json`:

- **Latest non-Form 4 filing:** 2026-02-03 (8-K `tm264822d1_8k.htm` + 10-Q `repl-20251231.htm`)
- **No April 8-K exists** (session time 07:16 ET)
- All April filings = Form 4 equity grants (Apr 6-7 cluster, decoded in S27 as routine Code A awards)
- No Form 8-K, no Form 25-NSE, no DEFA14A, no special meeting calls

### News Search
- Google/web search: no announcement of approval OR CRL as of scan time
- Last material news: Oct 2025 BLA acceptance press release
- Biospace/Merlintrader articles from early 2026 confirm Apr 10 PDUFA as outstanding

## Price Action Update (through Apr 9 close)

| Date | Open | High | Low | Close | Volume |
|------|------|------|-----|-------|--------|
| 2026-03-26 | 7.45 | 7.59 | 7.21 | 7.54 | 1.10M |
| 2026-03-31 | 7.35 | 7.88 | 7.25 | 7.65 | 2.13M |
| 2026-04-01 | 7.72 | 7.93 | 7.59 | 7.61 | 1.10M |
| 2026-04-02 | 7.70 | 8.43 | 7.68 | 8.41 | 3.19M |
| 2026-04-06 | 8.20 | 8.85 | 8.20 | 8.54 | 3.22M |
| 2026-04-07 | 8.62 | 8.80 | 7.76 | 7.80 | 3.62M |
| **2026-04-08** | **8.05** | **8.19** | **5.76** | **5.885** | **6.35M** |
| **2026-04-09** | **5.88** | **6.52** | **5.22** | **5.910** | **8.34M** |

**Apr 9 session:** Opened near crash-day close, tested $5.22 intraday (below S27 low of $5.76), recovered to close $5.91 on heaviest volume of the entire month (8.34M). The late-day 15:55 bar shows 716K shares in one 5-minute window — suggestive of forced liquidation or stop-runs rather than fresh informed flow.

**Interpretation:** Market is pricing continued uncertainty — not a full CRL scenario (which would likely have taken price below $5), not an approval scenario (which would have shown gap-up). Implied: the decision has not leaked and the market is bracing for bad news but still holding a lottery-ticket premium (~$5.90 ≈ 40-50% of pre-crash reference of $8.20, suggesting roughly 30-40% weighted approval probability).

## Scan-Time Status: **STILL PENDING**

FDA press releases for PDUFA decisions typically land between ~16:00 ET (market close) and ~20:00 ET on the assigned date, or occasionally early the next morning. At 07:16 ET on PDUFA day, the decision has not been publicly issued. Primary-source confirmation is not yet obtainable.

## Decision Tree for This Session

Since REPL resolution cannot be completed in this pre-market scheduled session, the rational path is:

1. **Do NOT block on REPL** — document the pending status clearly, execute the rest of the daily pipeline, and hand off to the next session with an explicit "check REPL first" flag.
2. **TVTX read-across remains SENTIMENT ONLY** — no substance action, per S27 discipline. REPL and TVTX are different companies, different drugs, different mechanisms, different FDA divisions. A REPL CRL does not mechanically predict TVTX.
3. **Next session critical path:** Pull EDGAR CIK 1737953 submissions API first. Scan news for "Replimune RP1 FDA". If 8-K filed, extract outcome. If approval: note surprise, update methodology on trust of market pre-positioning signals, consider whether TVTX sympathy discount creates asymmetric opportunity. If CRL: archive REPL thesis with lessons-learned memo documenting that the Apr 8 crash was the signal and market priced it correctly.

## Sympathy Risk to TVTX (Monitor-Only)

Price of TVTX last observed at $31.44 (S27 close). TVTX PDUFA is Monday Apr 13. If REPL CRL hits today, TVTX may gap down on Monday via sector sentiment / "FDA is being stricter" narrative. This is SENTIMENT noise, not substance: FSGS is a different indication, different division (Cardio-Renal vs Oncology), different mechanism (ETA antagonist vs oncolytic virus). Plan: **do not chase gap in either direction**. S27 TVTX kill sweep was clean; next session's Monday T-0 sweep should re-verify using direct EDGAR pull before any action consideration.

## Conclusion

REPL outcome **unresolved at session start, pre-market on PDUFA day is too early**. Session will proceed with remainder of pipeline (TVTX/AXSM kill sweeps, 5 scanners, triage) and flag REPL as the top priority for the next session.
