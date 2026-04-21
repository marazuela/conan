# Execution Instructions — Unified Investment Discovery System

**This is the backbone.** Every session — interactive or scheduled — reads this file after SESSION_STATE.md and before doing any work.

---

## Cold Start Protocol

Every session starts with zero memory. This protocol bridges that gap.

**Step 1** — Read `SESSION_STATE.md`. The relay baton. Current phase, what was last completed, what comes next, active warnings or blockers.

**Step 2** — Read this file (`INSTRUCTIONS.md`) top to bottom. Architecture, pipeline, session flow, execution environment, rules.

**Step 3** — If SESSION_STATE references blockers, read `OPEN_QUESTIONS.md`.

**Step 4** — Read ONLY the specific file needed for your task:
- Project overview → `README.md` / `OBJECTIVES.md`
- Why was X decided? → `DECISIONS.md`
- History of past sessions? → `PROGRESS_LOG.md`
- Running a scanner? → its strategy spec in `strategies/`
- Scoring a signal? → the matching `framework/profile_*.md`
- API endpoints? → `CONTEXT.md`
- Writing a candidate? → `framework/candidate_template.md`

**Do NOT read all files.** SESSION_STATE + INSTRUCTIONS is full working context. PROGRESS_LOG is history — read on demand.

---

## System Architecture (4 Layers)

### Layer 1 — Scanners
Each scanner queries one primary-source data feed and emits standardized signal objects. Scanners run as isolated subprocesses (120s hard-kill). Currently 6 operational (edgar, esma_short, fda_pdufa, congressional, lse_rns, tdnet, asx, sedar_plus once unblocked) + 7 planned (hkex, kind, bse_nse, cvm, bmv, courtlistener, sec_enforcement).

### Layer 2 — Entity Resolution
All signals pass through `openfigi_resolver.py` to map company names, tickers, ISINs, CUSIPs to canonical `issuer_figi`. This is the convergence key — the same issuer on multiple exchanges resolves to the same FIGI. Cache at `config/entity_cache.json` + `working/openfigi_cache/`.

### Layer 3 — Scoring + Convergence
- **Scoring**: each signal matched to one of 5 profile rubrics based on `scoring_profile` field (set by the scanner from `signal_type`). See `framework/profile_*.md`.
- **Convergence**: `convergence_engine.py` reads 14-day (30-day for litigation) rolling signal log grouped by `issuer_figi`, detects same-direction / orthogonal / contradiction patterns, applies +5/+10 bonuses.

### Layer 4 — Reporting (read-only)
The reporting task reads operational data and writes ONLY to `reports/`. Daily digest PDF, per-candidate dossier PDFs, weekly strategic PDF, machine-readable `candidates_index.json`.

---

## The 16 Scanners

| # | Scanner | Source | Profile | Frequency | Status |
|---|---------|--------|---------|-----------|--------|
| 1 | edgar_filing_monitor | SEC EFTS + data.sec.gov | merger_arb / activist_governance | 3h | **operational** |
| 2 | esma_short_scanner | FCA + AMF + AFM + BaFin + CNMV + CONSOB | short_positioning | daily | **operational** |
| 3 | fda_pdufa_pipeline | ClinicalTrials.gov + openFDA + EDGAR | binary_catalyst | 3h | **operational** |
| 4 | congressional_trading | Capitol Trades | activist_governance | daily | **operational** |
| 5 | lse_rns_scanner | LSE RNS | merger_arb / activist_governance | 3h | **operational** |
| 6 | tdnet_scanner | TDnet (JP) | merger_arb / activist_governance | 3h | **operational** |
| 7 | asx_scanner | ASX announcements | merger_arb / activist_governance | 3h | **operational** |
| 8 | sedar_plus_scanner | SEDAR+ | merger_arb / activist_governance | daily | **blocked** — needs `working/ca_universe.json` |
| 9 | hkex_scanner | HKEx | merger_arb / activist_governance | daily | **planned** |
| 10 | kind_scanner | KIND (KR) | merger_arb / activist_governance | daily | **planned** |
| 11 | bse_nse_scanner | BSE + NSE | merger_arb / activist_governance | daily | **planned** |
| 12 | cvm_scanner | CVM (BR) | merger_arb / activist_governance | daily | **planned** |
| 13 | bmv_scanner | BMV (MX) | merger_arb / activist_governance | daily | **planned** |
| 14 | courtlistener_scanner | CourtListener RECAP | litigation | daily | **planned** |
| 15 | sec_enforcement_scanner | sec.gov litigation releases | litigation | daily | **planned** |

---

## Three-Stage Signal Pipeline

### Stage 1 — Triage (inside each scanner)
Hard filters, all must pass:
- Publicly traded on a major exchange
- Market cap ≥ $215M USD (≈ €200M)
- Signal is novel (14-day dedup window or material escalation)
- Data is fresh (within scan window)
- Translation confidence ≥ 0.70 for non-English sources

### Stage 2 — Entity Resolution + Scoring
- OpenFIGI resolves → `issuer_figi`, `figi`, `isin`, etc.
- `run_post_scan.py` reads `scoring_profile` field → applies matching rubric → emits `{score, band, auto_caps_triggered}`.
- Any signal scored ≥ 35 is marked `immediate`; 25–34 `watchlist`; 15–24 `archive`; <15 `discard`.

### Stage 3 — Convergence + Candidate Promotion
- `convergence_engine.py` groups by `issuer_figi`, applies bonus.
- Candidates at `immediate` (possibly boosted by convergence) get a full dossier written to `candidates/TICKER_description_YYYY-MM-DD.md`.
- Deep dive is MANDATORY — no candidate leaves the system without explicit kill conditions and primary-source citations.

---

## Common Signal JSON Schema

All scanners emit this format. This is the contract between scanners and the scoring/convergence layer.

```json
{
  "signal_id": "<stable unique hash>",
  "upstream_scanner": "edgar_filing_monitor",
  "scoring_profile": "activist_governance",

  "ticker": "RPAY",
  "ticker_local": null,
  "mic": "XNAS",
  "isin": null,
  "figi": "BBG00BN7PVD8",
  "issuer_figi": "BBG00BN7PVD8",
  "company_name": "Repay Holdings Corporation",
  "company_name_local": null,
  "market_cap_usd_mm": 450,
  "country": "US",

  "signal_type": "activist_13d",
  "signal_category": "edgar",
  "thesis_direction": "long",
  "strength_estimate": 4,

  "source_url": "https://...",
  "source_date": "2026-04-10",
  "scan_date": "2026-04-10T15:30:00Z",
  "source_content_hash": "<sha256>",
  "translation_confidence": null,

  "raw_data": {
    "filing_type": "SC 13D",
    "filer": "Forager Fund",
    "ownership_pct": 12.9
  }
}
```

Critical fields:
- `issuer_figi` — convergence key. Resolves cross-listings.
- `scoring_profile` — which rubric applies.
- `source_content_hash` — SHA256 of filing body. Used for cross-listing dedup.
- `translation_confidence` — only for non-English sources. < 0.70 = drop.
- `thesis_direction` — long/short/neutral/unknown. Required for convergence classification.

---

## Scheduled Tasks (3 total)

### `unified-operational` — every 3 hours at :00
Acquires `SESSION_LOCK.md`. Writes to everything EXCEPT `reports/`.

Flow: cold-start reads → install deps → lock → tool validation (py_compile + endpoint probes + terminal-marker check) → read scanner_registry.json → dispatch due scanners → resolve entities → score → convergence → promote/demote candidates → monitor existing candidates against kill conditions → update SESSION_STATE + PROGRESS_LOG + INDEX → release lock.

### `unified-maintenance` — every 3 hours at :50
Acquires `SESSION_LOCK.md` (same lock — mutual exclusion with operational). Health fixes only, never touches candidates/scoring/signals.

Flow: cold-start → deps → lock → py_compile all tools + terminal-marker check → endpoint probes → signal log integrity check → entity cache audit → scanner cadence audit → fix compile errors or truncated files (atomic write per D-052) → update SESSION_STATE warnings → release lock.

### `unified-reporting` — every 4 hours at :30
Acquires `reports/REPORTING_LOCK.md` (independent from SESSION_LOCK). **READ-ONLY** to operational data; writes ONLY to `reports/`.

Flow: lock → deps (reportlab) → read SESSION_STATE + signal_log + candidates/*.md → generate daily digest PDF → detect new/updated candidates → generate dossier PDFs → update candidates_index.json atomically → if Sunday: generate weekly strategic PDF → release lock.

**CRITICAL**: Reporting task NEVER modifies candidates, signals, SESSION_STATE, or any operational file. If it detects a bad state, logs to `reports/working/issues_YYYY-MM-DD.log` — does NOT fix. Fixing is the maintenance task's job.

---

## Session Rules

### Prime Directive
Every claim in every deliverable must be labeled:
- **VERIFIED** — traceable to source code, data, or primary document
- **INFERRED** — reasonable conclusion from verified facts
- **SPECULATED** — forward-looking or hypothetical

### Data Discipline
- Every signal traces to a source URL.
- Market cap floor: $215M USD (≈ €200M).
- Entity resolution via OpenFIGI is mandatory before scoring.
- Translation confidence < 0.70 → signal dropped.
- Party resolution confidence < 0.85 → litigation signal capped at Archive.

### Session Discipline
- Cold-start: SESSION_STATE → INSTRUCTIONS → task-specific file.
- **Work until usage limit. No early exits.** Running scanners is step 1 of many. There is ALWAYS more work: re-validation, deep dives, kill-sweeps, convergence review, documentation updates.
- Save after every discrete unit of work (atomic writes per D-052).
- Never delete files — archive with dated suffix.
- One concept per file.
- Shutdown protocol: flush state → SESSION_STATE → PROGRESS_LOG → INDEX → release lock.

### Pre-Delivery Verification (MANDATORY)
**Always inspect your own output before reporting a task complete.** No exceptions.

For PDFs / reports / anything rendered:
- Extract the text (pypdf, pdftotext, or equivalent) and read it back.
- Grep for telltale truncation artefacts: `...`, mid-sentence cut-offs, column overflow, missing sections, broken links, stray placeholder tokens like `TODO`, `—` where real content should be.
- Open the final file path(s) and confirm they match what you claim in the response (right page count, right ticker list, right date in the header).
- For tables: confirm no cell is truncated. If a Paragraph is used, confirm text wraps instead of being cut.

For code changes:
- Run `python3 -c "import ast; ast.parse(open(path).read())"` (or equivalent) after every material edit.
- Run the actual command you told the user will work (e.g., `--publish`) and confirm exit code 0 plus expected artefacts on disk.

For content edits (JSON, markdown, config):
- Re-parse the file to confirm it's valid (json.load, yaml.safe_load, markdown lint).

**The rule**: if the deliverable will be handed back to Pedro with "here's your file", the file must have been opened and verified in the same turn. Basic content/layout defects ("every sentence ends with `...`", truncated columns, malformed JSON, broken links) are never acceptable and must be caught before delivery — not by the user.

If verification reveals an issue, fix it and re-verify before responding. Do not ship and apologize.

### Pre-Edge Mandate (MANDATORY — see D-013)
**Only pre-edge candidates get surfaced.** A candidate has pre-edge value if the market has not yet priced the catalyst. Candidates with post-edge status are archived and never appear in reporting output.

Post-edge disqualifiers (auto-archive):
1. FDA has issued an approval or CRL for the relevant drug/indication.
2. A definitive merger agreement has been signed and publicly announced (stock has spiked on the announcement — the edge is gone).
3. An activist has gone fully public with a priced take-out offer AND the stock has absorbed the expected response window.
4. A proxy fight date is set AND the stock has already repriced for the expected outcome.
5. Merger-arb spreads below ~5% with standard timeline.

Every rationale must answer: **"what is the earliest moment this edge disappears?"** That answer becomes the implicit kill-watch. If the answer is "already happened," the name moves to `_curated_rationales.json` `_archived` block and gets out of the active pool.

Mechanically:
- Archived tickers live in `candidates/_curated_rationales.json` under `_archived`.
- `report_generator._load_post_edge_archive()` is the gate — any ticker in `_archived` is filtered out of `_collect_all_candidates()` and cannot appear in the summary or dossiers.
- Archived `.md` files move to `candidates/_archived_post_edge/`.

**Why this matters:** reporting what everyone already knows wastes a session. Pedro missed AVNS because the system only surfaced it *after* the $25 deal was announced. Identifying post-announcement names is information-value zero. The system's job is to surface names *before* the paperwork publishes — either via the merger-arb / activist / FDA scanners acting early, or via the new takeover-candidate and pre-Phase-3 scanners.

### Quality Over Quantity
- Target: 2–5 high-conviction candidates per week across all scanners.
- Every candidate survives the full pipeline: triage → score → deep dive → primary-source research → kill conditions.
- No candidate leaves without explicit kill conditions.
- Every candidate must pass the pre-edge test — see above.

---

## Execution Environment

Sandbox (Linux, Ubuntu 22) — resets between sessions. Reinstall dependencies at the start of EVERY session:

```bash
pip install requests beautifulsoup4 lxml yfinance openpyxl pandas python-dateutil feedparser pypdf rapidfuzz reportlab python-docx --break-system-packages
```

Bash mount: `/sessions/<session-name>/mnt/Conan/unified_system/` maps to `C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\unified_system\`. The exact session-name is per-session — use `request_cowork_directory` or check the system prompt.

Atomic file writes (D-052): every write goes `tmp + fsync + rename`. Prevents truncation on interruption.

Subprocess isolation (D-014): each scanner runs as its own subprocess with a 120s hard kill. Scanner crashes don't take down the pipeline.

EDGAR wall-clock budget (D-018): 35s per scanner call. Stale sandboxes may be slow.

---

## Anti-Early-Stop Rules

These are non-negotiable. They kept Tool 1 running for 67+ sessions.

1. **Running the scanners is step 1 of maybe 15.** Do not declare victory after scanner runs.
2. **After scanners, there is ALWAYS** entity resolution validation, scoring verification, convergence review, existing-candidate kill-sweeps, primary-source reads, documentation updates, SESSION_STATE snapshot.
3. **Usage limit is the only stop condition.** If token budget remains, there is more work.
4. **"All clear" is NOT a stop condition.** An all-clear session should deepen research on existing candidates, improve open-question investigation, update precedent tables.

---

## Legacy References

Archived systems in `C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\_ARCHIVED_*`. Do not modify. For historical decisions (Tool 1 DECISIONS.md D-014, D-018, D-047, D-052), reference the archived files — do not migrate the entire history. Key decisions carried forward are summarized in this system's DECISIONS.md.
