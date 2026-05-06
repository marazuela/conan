"""Phase 0 eval harness.

Held-out resolved historical FDA signals + replay against orchestrator versions.
Brier-gated CI on prompt changes. Builds on modal_workers/shared/fda_calibration_math
for Brier + bounded_drift; adds calibration curve + ranking AUC for orchestrator-
specific evaluation.

Modules:
  metrics.py       — Brier, calibration curve, ranking AUC computations
  gold_standard.py — load eval_harness rows + outcome labels (Phase 0 curation pending)
  replay.py        — replay a held-out asset×date through the orchestrator (Phase 2 stub)
  cli.py           — `python -m orchestrator_runtime.eval_harness.cli ...` (Phase 0 sketch)
"""
