# AXS-05 (Auvelity) — eval-harness fixture

**Provenance.** Copied 2026-05-06 from Investment_engine_v2 export bundle's `analyze-fda-approval-prospects` (P1, Tier-1) outputs. Imported under D-107 as the first worked-output fixture for v3 sub-agent A/B testing. The predecessor system ran two verified passes (2026-04-29 + 2026-05-05 WI-8-A-1) — both versions retained.

**Asset.** AXS-05 (dextromethorphan-bupropion, Auvelity) — Axsome Therapeutics (AXSM, CIK 1579428). Indication: Major Depressive Disorder. PDUFA: 2022-08-19 (already resolved — approved). Class: NMDA receptor antagonist + CYP2D6 inhibitor (combination).

**Files.**
- `AXS-05_approval_analysis.md` — first-pass narrative analysis.
- `AXS-05_approval_analysis_verified_2026-04-29.md` — second-pass verified version (post-primary-source confirmation).
- `AXS-05_probability_estimate.json` — first-pass `(p_low, p_mid, p_high)` + assumption ledger.
- `AXS-05_probability_estimate_verified_2026-04-29.json` — verified version.

**How v3 uses this fixture.**
1. **A/B regression check for sub-agent skills (Phase 5).** When a sub-agent skill version is bumped (e.g., `sub_agent_regulatory_history.md` v0 → v1), replay the fixture with both versions and compare structured outputs against the verified version's claims. Any regression in `class_precedents`, `base_rates.class_approval_rate`, or `sponsor_track_record` flagged.
2. **Eval-harness held-out probe.** Once R1/D-109 lands and `eval_harness` is seeded, AXS-05 row gets `is_holdout=true`; orchestrator runs end-to-end against the fixture's document_set, predicted conviction_pct compared against the realized `approved` outcome. Brier contribution recorded in `eval_runs`.
3. **Constitutional-check fixture.** The verified version's claims include explicit primary-source URLs — useful as ground truth for testing Stage 7 constitutional check (claim verification against cited docs).

**What's intentionally NOT here.**
- Helper-code outputs (e.g., `helpers/analyze.py` invocation traces). v3 sub-agents have a different runtime; helper traces don't replay.
- The original `outputs/` raw materials beyond the four files above.

**Reference.** Full SKILL.md at `/Users/Pico/Downloads/_EXPORT_skills_scoring_methodology/skills/v2_skills/skills/analyze-fda-approval-prospects/SKILL.md`.
