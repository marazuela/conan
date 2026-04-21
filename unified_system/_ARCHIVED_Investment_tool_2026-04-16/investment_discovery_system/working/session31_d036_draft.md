# D-036 Draft: Pre-PDUFA Price-Action Weighting Rule (Session 31)

**Status:** DRAFT — one confirmed observation (REPL CRL), one confirmed inverse (AXSM pre-breakout), one partial inverse (VERA spike). Needs 2-3 more pattern confirmations before full formalization.

## Decision (proposed)

When a biotech candidate has a hard catalyst (PDUFA, AdCom, topline readout, FDA meeting outcome) within ≤5 trading days, the scanner system and human scorer shall treat **pre-catalyst single-day price moves ≥15% on volume ≥1.5× 20-day average, without a discrete adverse-news trigger**, as a **high-conviction directional leading signal** about the eventual outcome.

**Long-thesis implications:**
- Pre-catalyst **-15%+ single-day down move** on elevated volume and no news → **immediate downgrade of any existing long thesis** by at least 5 points; consider archiving; do not treat as a dip-buying opportunity.
- Pre-catalyst **+15%+ single-day up move** on elevated volume → confirmation of long thesis direction; allowed as an upgrade contributor (but capped, see below).

**Short-thesis implications (inverse):**
- Pre-catalyst **+15%+ up move** → downgrade short thesis or close
- Pre-catalyst **-15%+ down move** → confirmation of short direction

**Boundary conditions:**
- Must be within ≤5 trading days of catalyst
- Must be single-day move (not cumulative 5-day)
- Must be on elevated volume (≥1.5× 20-day average)
- Must NOT have a discrete adverse/favorable news trigger (e.g., if an AdCom vote is announced mid-day, the subsequent price move is news-driven, not information-asymmetry-driven, and is not D-036)
- The **information source is structurally asymmetric** — large holders with insider knowledge, hedge funds with preferential analyst access, option-market makers seeing dealer hedging, etc. The market's aggregated pre-positioning is treated as a proxy for Bayesian updating we cannot otherwise observe.

## Evidence Base (as of S31)

### Confirming: REPL CRL (2026-04-10)
- Apr 8 2026: -24.6% single day close-to-close on 2.0× volume (6.3M vs ~2.8M avg)
- No discrete news; BLA response file quiet, no FDA press release, no management statement
- **2 trading days** before Apr 10 PDUFA
- Apr 10 outcome: **CRL** (second CRL on re-submission)
- Candidate was being monitored but not scored (no active thesis to protect); lesson is: this is the **reference case** for when pre-PDUFA weakness should trigger immediate downgrade.

### Confirming inverse (long): AXSM Pre-PDUFA Breakout (2026-04-09, T-14)
- Apr 9 2026: +3.3% close above $175 resistance on 1.85× volume
- T-14 trading days from Apr 30 PDUFA (outside the ≤5-day window, so this is a **weaker form** of D-036)
- Interpretation: Pre-PDUFA rally reflects the market's bullish positioning. Consistent with long thesis (score 30.75 → provisional upgrade to 31.0 pending day-2 hold).
- Apr 10 held at $178.11 intraday high $181.99 — day 2 of consolidation, not yet day-2 confirmation (we wanted close >$175 on non-trivial volume — we got close $178.11 on 0.85× volume, so neither confirming nor killing)
- **Not a clean D-036 case** because it's T-14, not T-≤5. Rule should probably generalize: "Pre-catalyst moves ≥15% in the final week" or "pre-catalyst moves ≥5% on 1.5x+ vol in the final 2 weeks" — needs calibration.

### Partial confirming inverse (long): VERA Pre-PDUFA Rally (2026-04-10, T-62 trading days)
- Apr 10 2026: +10.04% single day on 2.23× volume
- Well **outside the ≤5-day window** (T-62 to July 7 PDUFA)
- Catalysts attributed: Wolfe Research upgrade, $200M institutional investment, pre-PDUFA positioning
- **Not a clean D-036 case** — this is "well ahead of catalyst" regime, where D-036 doesn't directly apply. It's a **different signal**: confirming positioning with 2+ months of room for thesis development.

### Counter-example to pre-check: AXSM March Rally
- Mar 31 2026: AXSM +5.4% to $169.02 on 1.2× volume (T-21, not in window)
- Apr 1 2026: Balipodect acquisition announcement — a **discrete favorable news** event
- The subsequent rally is news-driven, not pre-positioning driven, so D-036 does NOT apply.

## Alternatives Considered

1. **Apply D-036 only to short theses**: Too narrow. The REPL case shows the rule is most valuable for long theses (it prevents scoring a dip as a buying opportunity). Rejected.
2. **Extend the window to ≤10 trading days**: Would catch the AXSM breakout as a D-036 case. But the signal quality drops meaningfully beyond 5 days because other catalysts (earnings, conference presentations, macro) can explain the price move. The tighter window is more conservative and reduces false confirmation. **Partial acceptance**: consider tiered window — strict D-036 at ≤5 days, weaker "Pre-catalyst positioning watch" at 6-10 days.
3. **Set threshold at 10% instead of 15%**: Catches more signals but increases false-positive risk (normal biotech daily volatility can be 10%+ on news-free days). 15% is the robust threshold for "unusual" in this sector. Keep 15%.
4. **Require 2 consecutive days of direction, not a single day**: More conservative but may miss the initial move. The REPL case was a single day. Keep single-day threshold.
5. **Weight by market cap**: A 15% move in a $100M biotech is noise; in a $10B biotech is huge. Add a size cap: require mcap ≥$500M for the rule to apply. **Accept**: add mcap threshold.

## Open Calibration Questions

1. **How much weight to apply?** REPL calibration suggests "immediate −5 points on 7-dimension score" — but if the starting score was 30 and this takes it to 25, that may be too aggressive (still above watchlist floor of 22). Alternative: "demote to watchlist immediately regardless of starting score." Need more data points to decide.
2. **Does a same-day reversal cancel the signal?** E.g., if a stock is down 20% intraday but closes down only 5%, does that count as a D-036 down signal? Probably NO — the close is the integrated market view, not the intraday panic.
3. **News-trigger disambiguation**: When is a "discrete adverse news" trigger declared? Official FDA press release = clear. Reuters/Bloomberg rumor = ambiguous. Management conference call commentary = ambiguous. Need a practical test: "Is there a named source of material information published within 2 hours of the price move?"
4. **Volume threshold calibration**: 1.5× 20-day average is conservative. REPL's -24.6% was on 2.0× volume. Most D-036-qualifying moves will be on 2×+. Consider raising floor to 2.0× or using absolute dollar-volume threshold.
5. **Short-thesis inverse application**: We haven't yet observed a short thesis where a pre-catalyst rally invalidated the signal. Reserve this application until evidence base exists.

## Proposed Deferral

**Do NOT formalize D-036 yet.** The framework refinement candidate already has ONE strong confirming case (REPL) and needs 1-2 more before enshrining in DECISIONS.md. Track in OPEN_QUESTIONS.md as a monitored candidate.

**Action items for next 2-3 sessions:**
1. **Every kill sweep must include 10-day price history with volume ratios.** This is already part of the framework but D-036 elevates its importance.
2. **Track all candidates for pre-catalyst single-day moves ≥10%** and compare eventual outcome to signal direction. Build an empirical table in `working/d036_evidence_log.md`.
3. **S32 action**: Before Monday TVTX open, stress-test the rule in both directions. If TVTX gaps down 5%+ at open with no news, apply D-036 inverse (downgrade); if flat or up, D-036 not triggered.
4. **On TVTX Monday outcome**: Regardless of direction, record in evidence log. The Apr 10 TVTX -7.8% sympathy drop is already a **failed D-036 signal** because we verified the attributive cause (REPL CRL sympathy) — the rule requires "no discrete adverse news trigger" and the REPL CRL counts as a trigger for the broader biotech cohort.
5. **Draft D-036 final version** in DECISIONS.md after 2 more observations.

## Sources
- See `working/session31_repl_resolution.md` for REPL CRL analysis
- See `working/session31_tvtx_final_sweep.md` for TVTX sympathy context
