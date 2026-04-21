# Non-US Primary-Source Discovery System (Tool 2)

Autonomous investment signal discovery system that scans nine non-US exchange disclosure portals — LSE RNS, TDnet, ASX, SEDAR+, HKEx, KIND, BSE/NSE, CVM, BMV — for primary-source filings that English-language research misses. Structurally complementary to Tool 1 (US catalyst discovery): every candidate this system produces is one Tool 1 cannot see by construction.

---

## Cold-start read order (in this order, no exceptions)

1. `SESSION_STATE.md` — current phase, active work units, warnings, next queue.
2. `INSTRUCTIONS.md` — architecture, pipeline, session rules.
3. `OPEN_QUESTIONS.md` — only if `SESSION_STATE` flags blockers.
4. One task-specific file — the strategy spec you're building/running, or the scoring rubric, or the candidate template.

Do NOT read `PROGRESS_LOG.md` on every session. It's history, not working state.

---

## Quick-reference table

| Concept | File |
|---------|------|
| Project charter and reasoning standard | `PROJECT_INSTRUCTIONS.md` |
| Goals and definition of done | `OBJECTIVES.md` |
| Architecture and daily flow | `INSTRUCTIONS.md` |
| API endpoints and validation status | `CONTEXT.md` |
| Why was X decided? | `DECISIONS.md` |
| Current blockers | `OPEN_QUESTIONS.md` |
| Current state + next actions | `SESSION_STATE.md` |
| Scoring rubric | `framework/scoring_system.md` |
| Candidate writeup template | `framework/candidate_template.md` |
| Per-exchange specs | `strategies/strategy_*.md` |

---

## Common session commands

```bash
# Install deps
pip install requests beautifulsoup4 lxml yfinance openpyxl pandas python-dateutil feedparser --break-system-packages

# Tool validation
python -c "import py_compile, pathlib; [py_compile.compile(str(p), doraise=True) for p in pathlib.Path('tools').glob('*.py')]"

# Run one scanner manually
python tools/lse_rns_scanner.py --since 2026-04-13

# Run full pipeline
python tools/pipeline_runner.py
```

---

## Status

Phase 0 — Scaffold: IN PROGRESS.
First scanner (UK LSE RNS) build begins after scaffold is complete.
