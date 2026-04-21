# Session 25 — REPL Outcome Correction (MATERIAL)

**Date**: 2026-04-10 ~11:30 UTC
**Session**: Scheduled Session 25
**Status**: Third adversarial self-correction in 3 sessions. Process working — maintaining discipline.

---

## The Correction

Session 24 (and earlier sessions) treated REPL as a resolved-outcome reference point, citing "REPL July 2025 CRL" from web snippet headlines and interpreting the Apr 8 2026 -24.5% crash as potentially signaling a SECOND CRL. **This framing was wrong.**

**The actual REPL timeline:**

- **Jun-Jul 2025**: First FDA CRL on original BLA (IGNYTE trial "not adequate and well-controlled")
- **Aug 2025**: Stock crashed ~77% on the CRL news; Hagens Berman filed a securities class action (dated Aug 25, 2025)
- **Oct 2025**: FDA accepted the resubmission (Class II, Priority Review maintained); new PDUFA set
- **Apr 10, 2026**: **NEW PDUFA date — TODAY**
- **Apr 8 2026 -24.5% on 6.3M (2x vol)**: Pre-PDUFA risk-off selling, not a CRL announcement
- **Apr 9 2026 $5.91 on 8.3M (2.6x vol)**: Continued pre-PDUFA de-risking

**There is NO 8-K from REPL in April 2026.** The last 8-K was Feb 3, 2026 (fiscal Q3 earnings). Any CRL would trigger an 8-K within 4 business days by law. The absence of an 8-K confirms no new CRL has been filed yet — the decision is literally pending today.

**The 14 Form 4s filed April 7 are NOT a trading signal.** They are routine annual equity grants (transaction code A, disposition code A, price $0) to Board of Directors members and executives:
- Weinand Dieter (Director): 44,500 shares granted
- Peeples-Dyer Veleka (Director): 44,500 shares granted
- Baker Bros. Advisors (Director): 44,500 + 44,500 shares granted
- Emily Luisa Hill (CFO): 50,000 + 75,000 shares granted
- Several others (12 more Form 4s in this cluster, all 2026-04-01 grant date)

The only real market transaction was CCO Christopher Sarchi selling 6,500 shares at $8.01 on Apr 2 (pre-crash, ~$52K — likely a pre-scheduled 10b5-1 or small-scale liquidity sale, not a material bearish signal).

---

## Why the Error Happened

Three contributing causes:

1. **Web snippet ambiguity**: Headlines like "FDA Issues CRL for Vusolimogene Oderparepvec" from AJMC/Targeted Oncology/Pharmacy Times often refer to the ORIGINAL July 2025 CRL, but snippets don't always clearly date-stamp the event. Session 24 correctly flagged this as "inconclusive" — the error was failing to push through the ambiguity via direct EDGAR 8-K pull as Session 24 itself recommended.

2. **Tape-reading bias**: A -24.5% crash + 2x volume immediately before a PDUFA looks like a CRL has already been announced. The more parsimonious explanation — pre-PDUFA risk-off before a high-uncertainty decision — was underweighted.

3. **Context echo**: Once "REPL CRL" entered SESSION_STATE.md as a working hypothesis, each subsequent session inherited the framing. The cold-start relay mechanism is a strength for continuity but a weakness for error propagation. **This is a process issue worth flagging for future hardening.**

---

## Revised Read-Across to TVTX

TVTX's Monday April 13 PDUFA is **still resolved independently** of REPL — the drugs are in different therapeutic classes (TVTX sparsentan is an FSGS endothelin/angiotensin dual antagonist; REPL RP1 is an oncolytic HSV-1 immunotherapy). But REPL's Friday outcome provides **same-day macro sentiment** read-across:

**Scenario A — REPL Approval (today)**: FDA continues to approve novel modalities with unusual pivotal data packages. Positive macro biotech bid, TVTX Monday looks structurally better by 1-2 pp on sentiment alone (not fundamentals). Implied probability of TVTX approval rises slightly (contextual, not scientific).

**Scenario B — REPL Second CRL (today)**: FDA is tightening standards on single-arm/post-hoc evidence. Negative macro biotech sentiment into Monday. TVTX thesis is still scientifically strong (positive pivotal DUPLEX + PROTECT combo) but headlines will be brutal and weekend positioning will lean defensive. **Entry discipline becomes even more critical — do NOT chase a red open Monday pre-decision.**

**Scenario C — Delay / No Action (today)**: Rare but possible. Ambiguous signal. REPL would trade chaotically; TVTX read-through near zero. Keep TVTX plan intact.

**Scenario D (base case before market open, pre-PDUFA)**: Market-implied probability at $5.91 (down from ~$8-9 pre-Apr-8) is consistent with ~25-35% approval odds (rough calc: if approval = +70% to $10+, CRL = -50% to $3, then E[price] ≈ $5.91 implies p_approval ≈ 0.28). The market is skeptical. **We should not reflexively match the market's skepticism** — REPL's IGNYTE data (32.9% ORR, 15% CR in anti-PD-1-refractory melanoma) is clinically meaningful, and the Class II resubmission is a full response to the CRL. **But market positioning matters: if REPL is approved, the short squeeze alone is material.**

---

## TVTX-Specific Updates from This Correction

1. **The "REPL diverged from TVTX" narrative is INCORRECT**. They are not diverging — they are on different PDUFA clocks. TVTX is simply NOT yet facing its binary.

2. **TVTX score 29.75 provisional is NOT impacted** by this correction. The kill-condition sweep from Session 24 remains intact (no 8-K since Apr 7, proxy cluster unrelated, no AdCom, no safety signal, no FDA advance announcement).

3. **Monday pre-PDUFA weekend positioning matters**. If REPL gets a CRL today, expect TVTX to gap down Monday pre-market on sentiment regardless of fundamentals. Hold-existing discipline remains: DO NOT initiate a fresh long at any price. DO NOT add.

4. **Post-REPL decision (later today)**: Pull REPL 8-K directly from EDGAR; if filed, read the FDA letter text for any language about oncolytic virus / novel modalities / AdCom requirements. Write a 3-5 sentence TVTX read-across memo. Then update SESSION_STATE warnings accordingly.

---

## Process Hardening (Going Forward)

Add to session protocol: **When a referenced outcome is "inconclusive from web snippets," the next session MUST resolve it via direct primary source (EDGAR 8-K, FDA website, press release)** before any downstream analysis depends on it. An unresolved ambiguity cannot linger across sessions.

---

## Sources

- [Replimune FDA Acceptance of BLA Resubmission (Oct 2025)](https://ir.replimune.com/news-releases/news-release-details/replimune-announces-fda-acceptance-bla-resubmission-rp1-0/)
- [Replimune CRL (July 2025)](https://ir.replimune.com/news-releases/news-release-details/replimune-receives-complete-response-letter-fda-rp1-biologics)
- [BioSpace: Biotech Looks to Replimune RP1 Decision as 'First Indicator' of FDA Direction](https://www.biospace.com/fda/biotech-looks-to-replimune-rp1-decision-as-first-indicator-of-fda-direction)
- [Stocktwits: REPL Worst Drop in 7 Months Ahead of FDA Decision](https://stocktwits.com/news-articles/markets/equity/repl-stock-on-track-for-worst-drop-in-nearly-7-months-ahead-of-fda-decision-on-skin-cancer-drug/cZJf4sTRIAp)
- [RTTNews: Replimune Awaits FDA Verdict](https://www.rttnews.com/3637913/replimune-awaits-fda-verdict-on-oncolytic-therapy-rp1.aspx)
- EDGAR submissions API: CIK 0001737953 — 0 8-K filings in April 2026
- Form 4s 2026-04-07 cluster: all transaction code A (grants) at price $0, not market transactions
