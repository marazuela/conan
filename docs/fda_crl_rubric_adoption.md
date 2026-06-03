# FDA CRL rubric — adoption runbook

Make the calibrated **M14 NDA CRL rubric** the source of truth for
`fair_probability` on first-cycle original NDA/BLA catalysts, replacing the
static base-rate table. sNDA is a **rank-only** triage flag. Branch:
`feat/fda-crl-rubric-adoption`.

## Architecture (what's built)

```
fda_signal_bridge.scan()                          # Seam 1
  └─ score_catalyst_crl(client, asset, event)     # once per event, never blocks
       ├─ feature_assembly.assemble_nda/snda_features()   # DB -> 13 features
       └─ fda_crl.score.score_crl()                       # router -> NDA|sNDA|refused
  └─ build_features(..., crl=…)
       └─ fda_event_features.compose_features()    # Seam 2
            └─ if FDA_CRL_OVERRIDE_ENABLED and scope==original and conf>=0.5:
                   fair_probability = 1 - crl_risk         # else base-rate
            └─ always stamps raw_inputs.crl.shadow_fair_probability
```

- **Originals** → calibrated `crl_risk` drives `fair_probability`.
- **Efficacy supplements** → `crl_percentile` (rank-only); never moves fair_probability.
- **Refused** (biosimilar / resubmission / CMC supplement / non-NDA-BLA, incl.
  Phase-3 readouts & AdCom) → untouched base-rate pipeline. **No hard decline** —
  the engine never goes dark on out-of-scope catalysts.

## Why forward-validation, not a backtest

The rubric routes on the FDA `application_number`, which the PDUFA watchlist
source doesn't carry — so ~98% of pending PDUFA assets have it empty and there is
**no app-numbered resolved history to backtest against**. Instead the override
ships **OFF by default** (shadow mode): the rubric records `shadow_fair_probability`
on every in-scope event, and `fda_crl_shadow_report.py` compares it to the
base-rate against realized outcomes once events resolve. Flip the override on only
when that report says `go`.

## Runbook (ordered)

1. **Apply migrations** (additive; `fda_warning_letters` already applied):
   ```
   supabase db push        # applies fda_application_submissions + fda_drug_inspections
   ```
2. **Link application numbers** (lifts coverage ~1% → ~30% on the current book):
   ```
   python -m modal_workers.fetchers.universe.fda_application_linker --dry-run   # review matches
   python -m modal_workers.fetchers.universe.fda_application_linker --commit
   ```
   High-confidence matches only; dev-coded pre-approval drugs (e.g. AXS-05) are
   left empty by design.
3. **Backfill feature tables** — run the openFDA ingest + fetchers so the rubric
   has features to read:
   ```
   python -m modal_workers.ingestion.openfda_ingest          # submissions
   # + the fda_inspections / fda_warning_letters universe fetchers
   ```
4. **Deploy the branch in shadow** — leave `FDA_CRL_OVERRIDE_ENABLED` unset. The
   rubric runs live, stamps `raw_inputs.crl` + `shadow_fair_probability`, changes
   nothing.
5. **Accumulate + read the verdict** as events resolve:
   ```
   python -m modal_workers.scripts.fda_crl_shadow_report --lookback-days 365
   ```
   Verdict `go` = rubric beats the base-rate Brier by ≥2% over ≥20 resolved
   in-scope events.
6. **Cut over — logged + reversible** (requires migration `20260615000030`):
   ```
   python -m modal_workers.scripts.fda_crl_override_admin --status
   python -m modal_workers.scripts.fda_crl_override_admin --enable v1 --notes "shadow verdict=go"
   ```
   This records + activates an `fda_model_versions` row (scope `fda_crl_override`);
   the bridge reads it once per run. **Rollback** = `--disable` (supersedes the
   active row → base-rate resumes instantly). `FDA_CRL_OVERRIDE_ENABLED=true|false`
   remains an explicit force-on / emergency kill-switch that overrides the row.

## Durable follow-up — implemented

The PDUFA watchlist scanner now backfills `application_number` at the source: the
D-046 openFDA cross-check (`_run_approval_crosscheck`) sets `application_number` +
`nda_type` on each active entry from its already-warm openFDA cache (single-match
only, zero extra calls), so coverage **self-heals over runs** as drugs get
filed/indexed. The one-shot linker (`fda_application_linker --commit`) clears the
existing backlog; the scanner keeps it current. The ~30% openFDA ceiling applies
per run but rises naturally as pre-approval drugs reach openFDA.

## Coverage / weight notes

- Three NDA features have no production source yet (`ctgov_failed_primary`,
  `ctgov_any_randomized`, `sponsor_has_orphan_history`) = **4.7%** of model |coef|,
  left absent; the shadow verdict is the backstop on whether they're worth building.
- sNDA stays rank-only (uncalibrated, AUC 0.52–0.72) — never feeds `fair_probability`.

## File map

| Area | Path |
|---|---|
| Scorer (NDA/sNDA/router/percentile) | `modal_workers/shared/fda_crl/` |
| Feature assembly | `modal_workers/shared/fda_crl/feature_assembly.py` |
| Seam 2 (fair_probability override + flag) | `modal_workers/scanners/fda_event_features.py` |
| Seam 1 (CRL threaded + observability) | `modal_workers/scanners/fda_signal_bridge.py` |
| Application-number linker | `modal_workers/fetchers/universe/fda_application_linker.py` |
| Forward shadow report | `modal_workers/scripts/fda_crl_shadow_report.py` |
| Override activation (record + activate) | `modal_workers/scripts/fda_crl_override_admin.py` |
| Migrations | `supabase/migrations/20260615*.sql` |
