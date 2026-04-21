# RETIRED FOLDER — silence_reports/

**Retired:** 2026-04-15
**Reason:** Reporting Hub restructure — two top-level purposes (`Candidates/` + `Performance/`).
**Replaced by:**
- deep-dive artifacts → `Candidates/deep_dives/{docx,pdf}/`
- candidate registry (all tools) → `Candidates/candidates_index.json` (schema 3.0)
- master candidate summary → `Candidates/ALL_CANDIDATES.pdf`
- per-tool candidate summary → `Candidates/per_tool/<tool>_candidates.pdf`
- per-tool performance PDF → `Performance/per_tool/<tool>/`
- cross-tool performance dashboard → `Performance/ALL_TOOLS_PERFORMANCE.pdf`

**Audit snapshot:** `archive/2026-04-15_pre_restructure/silence_reports/` — full contents preserved there pre-restructure.

**Do not write here.** Any scheduled task or script that references this path is a regression. The hub self-audit greps for the bare name (`silence_reports/`) in run logs and aborts on hit.
