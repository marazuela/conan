"""modal_workers.bc_outcome_labeler — Phase 3 outcome logging (vendored from bcfda).

Vendored (P4) from the standalone ``bc-fda`` repo's ``bcfda/outcomes/`` package into
conan's Modal workers. The ONLY edits vs the source are import rewires (and the
``_build_market_data`` construction) — logic, thresholds, SQL, and the LOGGING-ONLY
behavior are byte-for-byte identical.

Modules (W3):
  - ``resolve.py``        pure regulatory-outcome resolution: CRL Transparency
                          (gated on availability) | Drugs@FDA AP/WD | PDUFA push ->
                          {crl, approved, withdrawn, extended} (all LOWERCASE per the
                          CHECK); + ``hypothesis_outcome`` band-vs-reality pairing.
  - ``price_returns.py``  pure t+1/7/30 trading-day returns vs the pre-PDUFA close
                          (``fetch_returns`` is the only network fn — Polygon).
  - ``run_labeler.py``    daily worker: resolve -> price -> pair scored_p_crl from
                          the PRE-PDUFA bc_rubric_scores row -> 3-row null-omitting
                          merge-upsert into ``bc_prediction_outcomes``. LOGGING ONLY
                          — NEVER reads/writes bc_refit_log / l7.* (no refit, no gate).

Fail-loud: ``run_labeler`` opens/closes a ``bc_pipeline_runs`` row in a ``finally``.
INVARIANT: ``p_crl`` is read only here (paired into ``scored_p_crl``), never surfaced.
"""
