# Operational Status — 2026-04-17 18:38 UTC

## What ran today

**Full scanner sweep** (`pipeline_runner.py --force-all`) — all 15 operational scanners forced. Result:

| Scanner | Signals | Status |
| --- | ---: | --- |
| edgar_filing_monitor | 0 | ok |
| esma_short_scanner | 0 | ok |
| fda_pdufa_pipeline | 0 | ok |
| congressional_trading | 0 | ok |
| lse_rns_scanner | 0 | ok |
| tdnet_scanner | 0 | ok |
| asx_scanner | 0 | **timeout** (120s hard kill) |
| sedar_plus_scanner | 0 | ok |
| hkex_scanner | 13 | ok |
| kind_scanner | 0 | ok |
| bse_nse_scanner | 84 | ok |
| cvm_scanner | 21 | ok |
| bmv_scanner | 8 | ok |
| courtlistener_scanner | 0 | ok |
| sec_enforcement_scanner | 30 | ok |

**156 new signals total.** Convergence ran → 44 groups. Scored. Post-scan log updated. Signal log now carries 402 entries.

**1 signal crossed the 35-point promotion threshold**: HKEX 02427 GUANZE MEDICAL (tender_offer, 30 + 5 convergence bonus = 35). **Disqualified under D-013 pre-edge mandate**: the HKEX filing is a "Composite Document under the Takeovers Code" — the tender offer is already live with public terms, so the premium is already priced. Routed to archive, not promotion.

## Active candidates (5) — kill-sweep passed

All five remain in their pre-edge window today:

1. **RPAY** (T-3, HIGH) — Forager's $4.80/share offer was made *public* today 2026-04-17. RPAY stock closed $3.18 → 51% spread. Board has rights plan in place since April 13. Pre-edge because no deal signed.
2. **AXSM** (T+13, HIGH) — PDUFA April 30, 2026 (AXS-05 Alzheimer's agitation).
3. **RGR** (T+40, HIGH) — May 27 AGM + Beretta $44.80 partial tender proposal.
4. **VERA** (T+81, HIGH) — PDUFA July 7, 2026 (atacicept / IgAN).
5. **VRDN** (HIGH) — PDUFA June 30, 2026 (veligrotug / TED); REVEAL-2 Q2 readout.

## Reports published

- `reporting/summary/executive_summary.pdf` (6 pages, 5 candidates, index with full one-liners, no truncation)
- `reporting/dossiers/RPAY.pdf` (8pp)
- `reporting/dossiers/AXSM.pdf` (9pp)
- `reporting/dossiers/RGR.pdf` (9pp)
- `reporting/dossiers/VERA.pdf` (10pp)
- `reporting/dossiers/VRDN.pdf` (9pp)

**Pre-delivery verification**: pypdf extraction confirms no archived tickers (TVTX, AVNS, GSAT, SEM) appear as active candidates. TVTX appears inside the VERA dossier as explicit *comparator* analysis in the thesis body — intended precedent reference, not a candidate leak.

## Environment / code health

- All 38 tools in `tools/` compile cleanly after fixing a corruption in `convergence_engine.py` (duplicated tail removed; file now 214 lines).
- Pipeline ran end-to-end: scan → resolve → score → converge → post-scan report → publish.
- ASX scanner timed out at 120s (persistent issue, not new today).

## Pending to be fully operational

| Item | Status | Effort |
| --- | --- | --- |
| **Takeover-candidate scanner** (new pre-edge lane) | Spec'd in `strategies/pre_edge_takeover_candidate.md`; not yet coded | 2-3 sessions |
| **Pre-Phase-3 readout scanner** (new pre-edge lane) | Spec'd in `strategies/pre_edge_phase3_readout.md`; not yet coded | 1-2 sessions |
| ASX scanner timeout | Hits 120s hard kill; needs batching or endpoint change | 1 session |
| `candidates/rejected_pending_thesis/` backlog | ~60 scanned non-US candidates (JP, UK, AU, Brazil, Mexico) sitting without thesis write-ups; they can't promote to active without one | 1 session per ~10 theses; ongoing |
| `scoring_profile: takeover_candidate` rubric file | Required by the takeover-candidate scanner; not yet written | Part of takeover scanner build |
| `config/pe_filer_allowlist.json` | PE filer allowlist for 13G flagging in takeover scanner | Part of takeover scanner build |
| `config/phase3_approval_base_rates.json` | Base-rate table by indication for Phase-3 scanner scoring | Part of Phase-3 scanner build |
| `SESSION_STATE.md` | Still lists AVNS/GSAT/SEM/TVTX as active — stale | 5 min cleanup |
| `candidates/delivered/TVTX_FSGS_PDUFA_APPROVED_2026-04-13.md` | Duplicated — TVTX is now in `_archived_post_edge/` too; one of the two locations should be canonical | 5 min cleanup |
| Verify RPAY board's formal response to Forager's public $4.80 offer | Tomorrow — this is the next kill-watch trigger for RPAY | Passive monitor |

## What I'm tracking as the next priority

**The two new pre-edge scanners are the missing capability** — they're the reason you missed AVNS pre-announcement. Without them, the system only catches post-edge events (after M&A / PDUFA / proxy is public). Spec is complete; implementation is the next build.

Until those are live, the pre-edge mandate (D-013) works as a gate — it filters out post-edge signals like GUANZE so nothing stale gets promoted, but it can't *manufacture* pre-edge signals the scanners don't collect.
