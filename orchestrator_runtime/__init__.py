"""Conan v3 orchestrator runtime.

Top-level package for the FDA + EDGAR signal orchestrator. Layout (Phase 0+1
scaffolding; subsequent phases populate):

  eval_harness/      — replay + Brier scoring (Phase 0)
  stages/            — 10-stage pipeline (Phase 2)
  sub_agent_dispatcher.py — Agent SDK subagent firing (Phase 5)
  tools/             — orchestrator-direct tools (Phase 2)
  memory/            — hierarchical memory manager (Phase 2)
  ensemble.py        — Batch + streaming ensemble (Phase 2)
  constitutional.py  — Sonnet validator (Phase 2)
  calibration.py     — isotonic regression + nightly refit (Phase 3)
  client.py          — Anthropic SDK wrapper (Phase 2)
  runtime.py         — main orchestration loop (Phase 2)
  cost_tracker.py    — per-stage metrics (Phase 2)

See /Users/Pico/.claude/plans/confirm-orchestrator-cuddly-bubble.md for the
full architectural spec.
"""
