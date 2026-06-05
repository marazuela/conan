# BC-FDA Light v4 — Build Handoff & Cross-Cutting Insights

> **For the session implementing the 7 reconciled plans.** Read this FIRST, then the per-phase docs.
> The plans own the detail; this doc is the compass (don't drift back to overkill), the consolidated
> landmine map (gotchas are scattered across 7 files), and the build sequence. Distilled 2026-06-04 from
> the strategy + verification work that produced the plans. Where it says "verify," verify live —
> **the deployed DB/migrations are authoritative over any spec or plan prose.**

## 0. The canonical plan set (7 files)
`a0_cohort_confidence` · `phase0_universe_spike` (**THE GATE**) · `phase1_live_score` · `phase2_monitor_streams` · `phase2_synthesis_contract` · `phase3_digest` · `phase4_dashboard`.
Dedup is done; the older `*_live_scorer`/`*_fetchers`/`*_digest_outcomes` drafts were merged in + deleted. Don't resurrect them.

---

## 1. Strategic guardrails — the soul of the reframe (easy to lose mid-build)

1. **Monitor-first, score-as-input.** The product is the **daily monitor + synthesis framed vs the market-implied move**, on ~20 in-window tradeable names. The CRL score is a **ranking input (risk-band)**, NOT the moat and NOT a gate. Never display a calibrated `p_crl`. Don't over-build the scorer or a feedback loop.
2. **NO LLM in the control flow — the single most important rule.** Deterministic Python decides *whether* to fire, *which* events to classify, and the *max action allowed*. The LLM only (a) classifies one event and (b) writes one synthesis JSON — it can never widen its own gate (`phase2_synthesis_contract §2.3`). This one rule is what deletes the entire v4 reactor→orchestrator→sub-agent→tier→circuit-breaker apparatus that caused ~every burn in memory. If you find yourself adding an LLM "decider," stop.
3. **Fail-loud, never fail-silent.** Every cron opens+closes a `bc_pipeline_runs` row (even on crash, via outer try/finally). Liveness = "did today's run write its row?" — **not** a watchdog meta-system (v4's watchdog went dark and blinded itself; see memory `cowork_session_halt`). No honest-empty stubs that look like success.
4. **Zero Cowork on the daily path.** Cowork = single-host Mac = the top reliability liability (memory `cowork_sharing`, `cowork_session_halt`). Use deterministic fetch + metered Haiku/Sonnet only.
5. **Strangle, don't migrate.** Build bc_* beside v4. Touch NOTHING in v4's machinery. Disable v4's FDA path only after the monitor is proven on resolved outcomes.
6. **Minimize sources.** Score spine ≈ Drugs@FDA + EDGAR 8-K count. CT.gov features are dropped for v1 (the scorer doesn't require them). Every new external source is a liability that ages — make it earn its place.

---

## 2. Build sequence & gates

- **Phase 0 is the GATE — build it first and stop there until it passes.** Exit criteria (`phase0 §4`): ≥15 (target 15–20) in-window pending NDA/BLA names with trustworthy PDUFA dates, ≥12 passing the tradeability gate, date-exact ≥0.80, reproducible-daily, ~$0. If no source clears this cheaply, **escalate — do not build the monitor on a universe that doesn't exist.** Approach 1 (lift `fda_pdufa_pipeline.py::_parse_filing_for_pdufa`) is the favorite.
- **A0 runs in parallel, offline, now** — zero dependency on Phase 0; it's the most immediately executable track and sets band display-prominence (expect "show band with a caveat," CI floor < 0.70).
- **Critical path: 0 → 1 → 2 → 3.** Phase 4 (dashboard) can lag — the digest is the product.
- **Options/IV ships dormant** (see landmine #6) — do NOT block Phase 2 on it.

---

## 3. Landmine checklist — verify/honor BEFORE writing code

These are the traps that already bit one or both planning tracks. Each names the owning plan §.

1. **The two live CHECK constraints are strict — conform, don't invent tokens.** `bc_application_features.feature_quality ∈ {standard, low, built_at_install}`; `bc_pipeline_runs.status ∈ {running, succeeded, failed, partial}`. Both bit the drafts. Put nuance (`killed_budget`, `skipped_no_entitlement`, phase-provenance) in `reason`/`log`, never in the enum column. Any *new* bc_ write: check the live CHECK first.
2. **Migration 005 (`operator_flags` bc_ sources) is NOT applied.** `operator_flags.source` is a hardcoded allowlist with **zero** bc/l1/l4 entries — any bc_ flag write throws (23514) until 005 lands. Apply it disk-first (re-introspect the live CHECK first), OR route flag intents to `bc_pipeline_runs.log` until then (`phase2_synthesis_contract §4.3`). Don't crash the worker over a flag-sink gap.
3. **Feature substrate isn't live-populated.** Don't call `assemble_nda_features` expecting populated tables — `fda_application_submissions`/`fda_drug_inspections` aren't live and the inspections path was found unbuilt. Use the **offline point-in-time builder, shared with A0, byte-aligned to `feature_assembly` + parity-tested** (`phase1 §0.3/§1.2`, `a0 §3.2`). No look-ahead: every feature query bounded `≤ ref_date`.
4. **Scorer location.** It must run inside the repo (Modal can't reach `~/Downloads`). `phase1 §0.1` says it's already vendored — **confirm** the script + model JSON are in-repo and `MODEL_PATH` is fixed before relying on it. Pin a golden test against the shipped example fixtures.
5. **`bc_scanner` is capture-only** (zero table grants). It can call `bc_news_event_upsert` and nothing else. The classify UPDATE + insider/options/pipeline-run writes need **new SECURITY DEFINER RPCs** — do NOT fall back to service-role (keep the metered/internet-reading worker least-priv; honor the L4.2 trust boundary). `phase2_synthesis_contract §3.4`, `phase2_monitor_streams §6`.
6. **Polygon options = 403, entitlement-blocked (confirmed live).** The code (`PolygonOptionsData`) is fine; the *subscription* lacks options snapshots. Ship the options fetcher **dormant behind `l4.options_enabled=false`** → `streams_available.options=false` → synthesis caps at `monitor`, digest renders "implied move unavailable." **Don't waste an hour debugging "no options data" — it's billing, not code.** Flipping it on later (Polygon ~$29–199/mo) is a one-flag change. `phase2_monitor_streams §2`.
7. **Resend: call it DIRECT from the digest sender; do NOT reuse `fanout`** (that couples to the v4 `convergence_assessments` trigger — the opposite of strangle-don't-entangle). Needs `RESEND_API_KEY` as its own secret (the edge secret is separate). `phase3 §3`.
8. **Modal's 5-cron cap is binding.** New crons (universe-build, weekly score, daily monitor, digest, outcome-labeler) already meet/exceed it — fit them via the `public.scanners` DB-row registry + `dispatch_release_times`/`dispatch_weekly` pattern, **not** new top-level `@app.function` schedules. Retiming is a DB UPDATE (`scanners` row is authoritative, not the JSON — memory `scanner_registry_vs_db`). `phase2_monitor_streams §7`.
9. **Migration discipline:** code-tracked DDL (the new RPCs + config seeds) is **disk-first then `supabase db push`**, NOT MCP `apply_migration` (that's for one-shots only — memory `feedback_mcp_apply_migration_discipline`). Re-introspect grants after apply.
10. **Idempotency everywhere.** Snapshot-versioned upserts on the composite UNIQUEs; a same-day re-run must be a clean no-op. Non-idempotent writes are where v4's DLQ/stale-stamping pain came from.

---

## 4. Don't re-derive (verified, so you don't repeat the work)

- **eval_harness CRL cohort is junk** (8-K-mining exhaust; 45 rows → ~12 usable, 26 = one Axsome event). It is NOT the validation gate. That's *why* the M14 backtest was demoted from a gate to A0's display-prominence note.
- **M14's headline AUC 0.810 rests on 9 CRLs; reproduced CI floor 0.637 < 0.70.** → ranking input only; "show band with caveat." (`a0` is the honest cohort: FDA CRL Transparency dump `api.fda.gov/download.json → results.transparency.crl`, 426 CRLs, 100% FDA-keyed; 33 ready-built negatives in the M14 `prospective_2026` CSV.)
- **The universe largely exists already.** `edgar_8k_pdufa.py` writes pdufa rows but leaves `event_date=NULL` by design; the working date extractor is `fda_pdufa_pipeline.py::_parse_filing_for_pdufa` → lift-and-harden (Phase 0 approach 1). FDA has no forward-PDUFA API — dates come from corporate disclosure (8-K/PR).

---

## 5. Process & coordination (meta-lessons from how these plans got made)

- **Run the build from ONE session.** Two concurrent sessions on the same files produced the duplicate plans and a near-collision. The cleanup worked out by luck-adjacent caution. Don't fan out file-writing agents onto the same paths concurrently.
- **Verify live before asserting schema.** Every CHECK-constraint bug here was caught only by querying the live DB — the project drifts (memory `supabase_migrations_drift`). Migrations > spec prose > plan prose.
- **Quiescence-check + `git status` before any destructive op** (delete/merge). "Audit against a clean tree" — a churning tree turns a cleanup into corruption.
- **Open `operator_flags` context:** there are ~10 open warn/critical flags right now — they are **v3/v4 operational exhaust** (skill_watchdog, v3 bridge, asset_linker backlog, a `live_patch_regression` critical) and are **unrelated to bc_**. Don't let them confuse bc_ liveness debugging; bc_ liveness = the bc_pipeline_runs rows only.

---

## 6. The one open product decision (Pedro's call, not a blocker)
**Polygon options tier** — pay ~$29–199/mo to light up the implied-move moat now, or ship **band-only** (the encoded default) and flip it on later. The build proceeds either way.
