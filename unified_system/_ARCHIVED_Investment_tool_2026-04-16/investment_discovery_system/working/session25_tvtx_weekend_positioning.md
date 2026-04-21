# TVTX Weekend Positioning — Session 25

**Date**: 2026-04-10 ~11:30 UTC (Friday pre-market)
**Status**: PDUFA Monday April 13 — **T-1 business day**
**Score**: Provisional 29.75 (Session 24 T-1 kill sweep all clear)
**Price**: $31.44 (Apr 9 close, $2.90B mcap)

---

## The REPL Collision

After the Session 25 REPL correction, we now know REPL's PDUFA is **TODAY** — the same week as TVTX. This creates a FDA-sentiment collision that was not previously modeled in TVTX thesis.

REPL is being characterized in biotech press as the "first indicator" of FDA direction in Q2 2026 — the first major high-profile decision since recent agency discussions about single-arm / post-hoc evidence standards. **Whatever the FDA decides today on REPL will color the narrative going into Monday's TVTX decision, regardless of scientific independence.**

## Possible Today Paths (REPL)

### Path A — REPL Approved (40-50% probability, above market-implied)
- **REPL tape**: likely +40-70% intraday to $8-10 range on massive short cover
- **TVTX read-across**: Mildly positive. Biotech risk-on bid into weekend. TVTX could drift $31.50-32.50 on Friday post-REPL.
- **Monday entry conditions**: Our discipline holds — **do NOT add**. Hold-existing only.
- **Why not add even here**: TVTX is ALREADY PRICED for a positive PDUFA outcome (it's trading at $31.44, near analyst PT clusters). Upside from approval is already compressed. Creating a fresh position at $31.44 with the event 3 days away offers poor reward / time decay.

### Path B — REPL Second CRL (30-40% probability)
- **REPL tape**: likely -45-65% to $2-3.50 range. Second CRL + lawsuit history = brutal.
- **TVTX read-across**: Negative macro sentiment. **Expect TVTX to gap DOWN 3-6% Monday open** even though science is independent.
- **Monday entry conditions**: STRONGER discipline needed. **Do NOT interpret a TVTX gap-down as an entry**. The market is repricing biotech risk-off broadly, not repricing TVTX's science.
- **Opportunity risk**: A gap to $29-30 on REPL sympathy pre-decision is possible. Tempting but NOT a value entry — the binary is in 3 days and the downside tail is asymmetric vs the compressed upside.
- **Risk asymmetry**: Existing holders face a sentiment drag into the decision. If fundamental kill conditions remain clear, the position is intact.

### Path C — REPL Delay / PDUFA Extension (5-10% probability)
- **REPL tape**: chaotic, likely -15 to -30% on uncertainty (delayed binary = option extension = theta)
- **TVTX read-across**: Minimal direct. Creates a modest headline "FDA is slowing" narrative, but TVTX's full review is in motion and unlikely affected.
- **Monday entry conditions**: Discipline holds. No change.

### Path D — REPL AdCom Called (1-3% probability, very late)
- Extreme tail. Would imply ~6-month+ extension for REPL. Unlikely this close to PDUFA.
- **Read-across**: Minor negative sentiment.

## TVTX Kill Conditions Re-Check (repeat of Session 24 sweep, confirmed)

1. ✅ **No new TVTX 8-K** since Apr 7 Baynes Form 4 (known Rule 10b5-1). Verified via EDGAR submissions API this session.
2. ✅ **No FDA advance announcement** (FDA has not publicly discussed sparsentan labeling or AdCom).
3. ✅ **No new safety signal** in openFDA (no checks triggered recent flags).
4. ✅ **Proxy cluster Apr 6 unrelated** — annual proxy, DEFA14A is normal.
5. ✅ **REPL sympathy risk acknowledged but not a kill condition** — TVTX's science is independent.

**ALL 4 + 1 kill conditions CLEAR as of Session 25 11:30 UTC Friday.**

## Friday Session Plan

1. **Post-REPL 4:00pm ET (20:00 UTC)**: Check REPL's actual FDA decision via:
   - REPL 8-K filing at EDGAR (legally required within 4 business days; typically same-day for major outcomes)
   - FDA press release on FDA.gov
   - Direct news search
2. **Document REPL outcome** in `working/session25_repl_outcome.md`
3. **Write TVTX read-across memo** (3-5 sentences) based on actual REPL outcome
4. **Re-check TVTX Apr 10 close** via yfinance — if unusual move (>3% in either direction), trigger kill-condition re-sweep
5. **Update SESSION_STATE.md** with REPL outcome and revised TVTX weekend warnings
6. **Prep Monday morning protocol**:
   - 09:30 UTC Monday: final kill-condition sweep
   - 13:30 UTC: US market open, watch for TVTX gap direction
   - Every 30 min: FDA press release check
   - On decision: instant assessment, update score, update candidates file

## Hard Rules for Monday

- **DO NOT chase a gap-up** pre-decision (sentiment bid on REPL approval). The binary is unresolved.
- **DO NOT buy a gap-down** pre-decision (REPL CRL sympathy). The TVTX downside tail on a CRL is worse than any sympathy discount.
- **Hold-existing only** until FDA decision posted.
- **On approval**: reassess score upward, consider trim for risk management (50% rule — never hold >50% on binary post-resolution if price spiked).
- **On CRL**: instant archive, score → 0, log kill-condition trigger, mark lesson for review.
- **On delay**: reassess holding period, likely continue to hold through next catalyst.

## Score

**TVTX score unchanged at provisional 29.75.** The REPL collision does not impact the fundamental thesis. It adds a **sentiment risk overlay for the 48-72 hour window** without changing the underlying probability estimate for the TVTX outcome.

---

## Sources (for this memo)

- Session 25 REPL correction memo (`session25_repl_correction.md`)
- TVTX EDGAR submissions API — CIK 0001438533 last 8-K check
- yfinance TVTX 10-day history
- [BioSpace: Biotech Looks to Replimune RP1 Decision as 'First Indicator'](https://www.biospace.com/fda/biotech-looks-to-replimune-rp1-decision-as-first-indicator-of-fda-direction)
