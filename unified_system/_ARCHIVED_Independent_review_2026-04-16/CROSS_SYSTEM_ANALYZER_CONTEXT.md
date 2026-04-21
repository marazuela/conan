# CROSS-SYSTEM ANALYZER — CONTEXT & INSTRUCTIONS

**Purpose of this file.** This is the canonical briefing document for an independent analyzer tool whose job is to read the outputs of *all* investment-discovery systems the operator builds, combine and cross-reference their signals, and surface the highest-conviction targets that no single system could find alone. It is not a scanner. It does not produce primary signals. It is a pure reader and synthesizer.

Drop this file into the new analyzer project's root folder as `CONTEXT.md` (or equivalent) so any cold-start session has the full mandate, the architectural rules, and the cross-system conventions required to operate safely.

---

## PART 1 — WHAT THIS TOOL IS AND IS NOT

**It IS:**
- A pure *reader* of other systems' outputs.
- A *synthesizer* that detects cross-system convergence, contradiction, and complementarity.
- A *ranker* that combines evidence from multiple systems into a single conviction score per entity.
- A *reporter* that produces its own deliverables (daily / weekly cross-system digests, unified candidate dossiers).
- *Write-isolated* — it never writes into any source system's folder.

**It IS NOT:**
- A scanner of primary sources (EDGAR, TDnet, etc.). Those belong to the upstream systems.
- A replacement for any upstream system's candidate pipeline. Upstream systems keep producing their own candidates; this tool adds a cross-system layer on top.
- A coordinator of the upstream systems. It doesn't tell tool 1 or tool 2 what to do. It only reads their outputs.
- Authorized to modify, delete, or repair upstream files. If it detects corruption in an upstream file, it logs the observation and moves on.

**Why the strict separation.** Each upstream system uses the project template's concurrency model: a `SESSION_LOCK.md` that is overwrite-only, with two writer tasks (operational + maintenance) per system. If this analyzer writes into any upstream folder, it becomes a third writer and breaks the lock invariant. By restricting it to reads only + writes to its own folder, it composes cleanly with any number of upstream systems without ever contending for a lock.

---

## PART 2 — MANDATE

**Primary goal.** Given N upstream investment-discovery systems, produce a unified stream of cross-system intelligence that identifies the small set of entities for which multiple independent systems, using uncorrelated data sources, have surfaced evidence. These cross-system convergences are, in principle, higher-conviction than any within-system signal because the sources are structurally independent: different data, different regulators, different languages, different event types.

**Outputs:**
- **Daily cross-system digest** — one markdown file per day listing all entities appearing in 2+ upstream systems' active signals or candidates within a rolling 14-day window.
- **Unified candidate dossier** — when an entity appears in 2+ systems AND at least one system has scored it above threshold, produce a combined writeup pulling evidence from every system that has touched it.
- **Contradiction report** — when two systems disagree on an entity (one flags long, another flags short / distress), flag it for review — either the thesis is wrong or the opportunity is structural.
- **Coverage report** — weekly summary of each upstream system's signal production, uptime, and contribution to cross-system findings. This is how the operator learns which upstream systems are earning their keep.
- **Gap report** — names appearing in exactly one system that *should*, by the operator's thesis, appear in another. Identifies blind spots in individual systems.

**Non-goals:**
- Do not try to replicate any upstream system's analysis. If tool 1 has scored an entity at 32/42, trust that score and use it as input; do not re-score from scratch.
- Do not chase marginal signal flow. The point is conviction compounding, not volume.

---

## PART 3 — ARCHITECTURAL NON-NEGOTIABLES

These are derived from the parent project template and must not be relaxed:

1. **Write-scope isolation (absolute).** This tool writes only inside its own project folder. It must never open a writable file handle anywhere under any upstream system's folder. Not for convenience, not for a one-off fix, never. Violating this breaks the upstream systems' concurrency model.

2. **Read-only access to upstream folders.** The tool is configured with a list of upstream system root paths. It opens files there only for reading. File reads should tolerate partial writes (an upstream system may be mid-write when this tool reads) by retrying after a short delay if JSON is malformed, and giving up gracefully after N retries.

3. **No lock acquisition in upstream systems.** This tool does not touch any upstream `SESSION_LOCK.md`. It does not respect upstream locks either — it can read while a writer is active, because JSON read-retry handles the race. (If an upstream system wrote partial files without atomic rename, a fix belongs in that upstream system, not here.)

4. **Its own concurrency model.** This tool uses its own `SESSION_LOCK.md` inside its own folder. Standard overwrite-only semantics, 4-hour stale window, LOCKED / UNLOCKED two-state plaintext. If this tool has two scheduled tasks (e.g., a digest generator and a deep-dossier generator), they coordinate via that single lock the same way upstream systems do.

5. **Files are the only memory.** Every cross-system finding, every conviction judgment, every open question is written to a file before the session ends. No exception.

6. **Verify, don't remember.** Upstream schemas evolve. Before trusting a field, verify it exists in the current files — don't rely on what the schema looked like in training data or a previous session.

7. **Never delete.** Superseded cross-system reports move to `archive/YYYY-MM-DD_reason/`. The sandbox can't delete reliably anyway.

---

## PART 4 — HOW TO DISCOVER AND REGISTER UPSTREAM SYSTEMS

The analyzer must be able to handle an evolving number of upstream systems without code changes per addition.

**Convention.** Each upstream system is registered in `upstream_registry.json` at this tool's root. The operator updates this file whenever a new upstream system goes live. Schema:

```json
{
  "upstream_systems": [
    {
      "id": "tool-1-us-catalysts",
      "display_name": "US Catalyst Discovery (Tool 1)",
      "root_path": "C:/.../investment_discovery_system",
      "signals_log_path": "signals/signal_log.json",
      "candidates_folder": "candidates",
      "reports_folder": "reports",
      "session_state_path": "SESSION_STATE.md",
      "universe_coverage": "US-listed, ≥$300M market cap",
      "signal_types": ["edgar_keyword", "esma_short", "congressional_trade", "contract_award", "pdufa_catalyst"],
      "scoring_scale_max": 42.5,
      "last_registered": "YYYY-MM-DD",
      "health_contract": "session_state_timestamp_within_6h"
    },
    {
      "id": "tool-2-non-us-primary",
      "display_name": "Non-US Primary Sources (Tool 2)",
      "root_path": "C:/.../non_us_discovery_system",
      "...": "..."
    }
  ]
}
```

The session reads this registry first and then probes each registered system's `SESSION_STATE.md` timestamp — if older than the health contract allows (e.g., 6 hours), the system is flagged as stale in the coverage report and its signals are still read but labeled as potentially behind.

---

## PART 5 — CANONICAL ENTITY RESOLUTION ACROSS SYSTEMS

The hard problem. If tool 1 writes `AAPL` and tool 2 writes `7203.T` (Toyota), the analyzer needs to know these are two distinct entities. If tool 1 writes `NESN.SW` and tool 2 writes `NSRGY` (Nestlé ADR), the analyzer needs to know they are the *same* ultimate issuer. Without this, all cross-system convergence is noise.

**Resolution rules (ordered):**

1. **Prefer FIGI (Financial Instrument Global Identifier)** as the canonical ID at instrument level. Every upstream system is expected to write a `figi` or `share_class_figi` field in its signal JSON. If absent, this tool resolves it on-the-fly via OpenFIGI (free, no auth) using ticker + exchange MIC.

2. **Roll up to issuer via `composite_figi` or `ultimate_parent_figi`** when the analysis is about the economic entity rather than the specific share class (common stock vs. preferred vs. ADR). Nestlé Swiss listing and Nestlé US ADR share an ultimate parent; they should converge.

3. **Cross-listing awareness.** Many issuers are listed on multiple exchanges (primary + ADR + secondary). The analyzer must treat all listings of the same issuer as one entity for convergence purposes, but retain the specific listing in the evidence trail.

4. **Private entity handling.** Some signals reference private entities (a private contract counterparty, a private biotech licensee). These get a stable ID derived from a hash of `legal_name + jurisdiction` and are tracked separately. They cannot produce tradeable candidates, but they can link two systems' evidence about a public entity (e.g., Company A public + Company B private partner mentioned in both systems' filings).

5. **Unresolvable signals** — if a signal has no resolvable identifier after all the above, it is logged to `working/unresolved_entities.md` and excluded from cross-system matching. Do not guess. Guessing silently destroys the analyzer's output quality.

**Implementation note.** Build a small `tools/entity_resolver.py` that maintains a local cache of `(ticker, exchange) → figi → issuer` mappings. Refresh the cache weekly. OpenFIGI rate-limits; respect batch size 10 for unauthenticated calls.

---

## PART 6 — THE COMBINATION LOGIC

This is the analytical core. What does it mean to "combine" signals from multiple systems?

**Convergence (the strongest primitive).**
An entity is *convergent* if it appears in ≥2 upstream systems within a rolling 14-day window. Convergence types are not equal:

- **Same-direction convergence** (both systems imply "something material is happening here, positive or negative in the same direction") — highest conviction.
- **Orthogonal-evidence convergence** (tool 1 flags a catalyst, tool 2 flags unusual regional filings — both informative, direction not specified) — high conviction.
- **Contradiction convergence** (tool 1 long thesis, tool 2 distress thesis) — flag for manual review; do not auto-score.

**Conviction scoring.** Do not smuggle a new rubric in. Instead, adopt this composite:

```
cross_system_conviction = (
    (upstream_score_sum / upstream_score_max_sum)
    × diversity_multiplier
    × recency_multiplier
    × direction_multiplier
)
```

- `upstream_score_sum` — sum of each system's score for this entity.
- `diversity_multiplier` — 1.0 for 2 systems, 1.3 for 3, 1.5 for 4+. Rewards independent sources.
- `recency_multiplier` — 1.0 if both signals are within 7 days, 0.8 if within 14, exclude beyond 14.
- `direction_multiplier` — 1.2 for same-direction, 1.0 for orthogonal, 0.5 for contradiction (deliberately penalized but kept visible).

Threshold conventions:
- `cross_system_conviction ≥ 0.85` → immediate unified dossier.
- `0.60 – 0.85` → watchlist entry in daily digest.
- `< 0.60` → log only.

Tune these after the first month of real data. Do not tune based on backtest guesses.

**Contradiction handling.** Contradictions are *features*, not bugs. They usually mean: (a) one system has stale data, (b) the entity has a genuinely complicated thesis (distressed-but-recovering, etc.), or (c) one system's scoring rubric misfired. The contradiction report names the two systems, the direction each inferred, the dates, and leaves the resolution to the operator.

**Gap detection.** For each entity surfaced by exactly one system, check whether it falls inside another system's *universe* (e.g., a US ticker surfaced by tool 1 that tool 2 should have also been scanning). If yes, that's either a missed signal in tool 2 (flag to its `OPEN_QUESTIONS.md` mentally, but write the note only to this tool's own gap report) or legitimate — tool 2's sources simply had nothing on it. Over time this report reveals which upstream systems are under-producing.

---

## PART 7 — DATA INTAKE — EXACT FILES TO READ FROM EACH UPSTREAM

For each registered upstream system, the analyzer reads:

| File | Purpose |
|------|---------|
| `SESSION_STATE.md` | Health check; extract timestamp, active warnings, and the list of current "Active work units" |
| `signals/signal_log.json` | The canonical rolling signal log — primary source for cross-system matching |
| `candidates/*.md` | Per-candidate writeups — extract ticker, current score, thesis summary, kill conditions |
| `reports/YYYY-MM-DD_daily_report.md` (most recent 14) | Secondary source for freshly flagged names not yet in signal_log |
| `DECISIONS.md` | Optional — read only if a cross-system finding needs context on *why* the upstream system made a call |

**Parsing contract.** Each upstream system commits to emitting:
- A JSON signal log with the common schema (see Part 9).
- Candidate writeups with a parseable YAML or JSON frontmatter block containing at minimum: `ticker`, `figi`, `score`, `status` (active|killed|watch), `thesis_direction` (long|short|neutral), `last_updated`.

If an upstream system doesn't emit frontmatter, the analyzer falls back to regex extraction from the markdown body. Log any parse failure to `working/parse_failures.md` and skip; don't crash the session.

---

## PART 8 — SCHEDULED TASKS

The analyzer runs two scheduled tasks, both read-only with respect to upstream systems:

| # | Task | Cron | Write scope | Purpose |
|---|------|------|-------------|---------|
| 1 | `cross-system-digest` | `30 */6 * * *` | analyzer folder only | Build daily digest, update signal-intersection cache, write coverage + gap reports |
| 2 | `cross-system-deep-dossier` | `30 2 * * *` | analyzer folder only | For every convergent entity scoring ≥0.85, produce or refresh a unified dossier |

Offsets: dossiers run at 02:30 local, digests run on 6-hour intervals starting 00:30. They never overlap with each other (≥2 hours between any two) and never overlap with upstream systems' own writes (which run at `HH:00` and `HH:50`).

Add a third `cross-system-performance-report` task weekly (Sunday 03:30) that produces the coverage and gap reports as PDFs, if the operator wants them in portable form.

---

## PART 9 — THE CANONICAL UPSTREAM SIGNAL SCHEMA

For cross-system matching to work, every upstream system must emit signals in a shape this analyzer can read. This schema is a superset of the parent project template's signal schema, with explicit cross-system fields:

```json
{
  "upstream_system_id": "tool-1-us-catalysts",
  "signal_id": "<stable unique ID across runs>",
  "entity_id_primary": "<FIGI preferred; ticker+MIC acceptable>",
  "entity_id_aux": ["<ISIN>", "<CUSIP>", "<CIK>"],
  "entity_name": "<human-readable>",
  "issuer_figi": "<composite FIGI of ultimate issuer>",
  "signal_type": "<source-specific>",
  "signal_category": "<coarse bucket>",
  "thesis_direction": "long | short | neutral | unknown",
  "strength_estimate": 3,
  "upstream_score": 28.5,
  "upstream_score_scale_max": 42.5,
  "source_url": "https://...",
  "source_date": "YYYY-MM-DD",
  "scan_date": "YYYY-MM-DDTHH:MM:SSZ",
  "raw_data": { "...": "source-specific payload" }
}
```

**Mandatory fields for cross-system matching:** `upstream_system_id`, `entity_id_primary` OR `issuer_figi`, `thesis_direction`, `upstream_score`, `upstream_score_scale_max`, `scan_date`. A signal missing any of these is logged to the analyzer's parse-failures file and excluded from convergence — do not attempt to repair it by guessing.

If an upstream system predates this schema and emits an older shape, the analyzer has per-system adapter functions in `tools/adapters/<system_id>.py` that normalize the old shape to this one at read time. Adapters are expected: as more upstream systems are built, some will have legacy schemas.

---

## PART 10 — DELIVERABLES — WHAT THE OPERATOR SEES

Final note on output: the operator should never have to open upstream systems' files to get value from the analyzer. Every relevant finding lands in one of these:

1. **`reports/YYYY-MM-DD_cross_system_digest.md`** — one per day. Structure: convergences (highest conviction first), watchlist movements, contradictions, gap flags, upstream-system-health summary.
2. **`dossiers/ISSUERID_short_name.md`** — one per convergent entity scoring ≥0.85. Combines each upstream system's evidence into a single narrative, preserving links back to the source candidate files. Include an explicit "why this is higher conviction than any single system" paragraph.
3. **`reports/weekly_coverage_YYYY-WW.md`** — one per week. Per-system signal counts, convergence contribution, gaps, health flags.
4. **`reports/contradictions.md`** — append-only. Every contradiction ever observed, with resolution notes when the operator adds them.

All four go into the analyzer's output folder. **Amendment 2026-04-15:** The per-tool `reporting_layer/` pattern described in template Part 2 has been retired. If/when the cross-system analyzer is built, its outputs should either live in its own dedicated top-level folder or be folded into `Reporting Hub/cross_tool/` alongside the other cross-tool consumer. They are never written into any upstream system.

---

## PART 11 — MANDATORY SELF-REVIEW CHECKLIST (ANALYZER-SPECIFIC)

Before any deliverable is emitted, confirm:

1. **Entity resolution integrity** — every convergence claim is keyed on an FIGI or a cross-verified issuer match, not a raw ticker string.
2. **Freshness** — both upstream signals contributing to a convergence are within their allowed windows. No stale pairings.
3. **Directionality** — the direction field is present and honored. A long signal + short signal is reported as a contradiction, never smuggled in as a convergence.
4. **Source traceability** — every claim in a dossier links back to the specific upstream file (not just "tool 1 said so" — the exact signal_id and source_url).
5. **No repair attempts** — the analyzer has not silently modified any upstream file. If it needed to, it didn't; it logged and moved on.
6. **Upstream health surfaced** — if any upstream system's SESSION_STATE timestamp is stale, the digest says so at the top, not buried.
7. **Coverage honesty** — convergences from a system known to be partially broken are labeled as such, not presented as if fresh.

---

## PART 12 — FAILURE MODES TO ANTICIPATE

Specific to cross-system work:

- **Upstream schema drift.** A system changes its signal JSON shape and the analyzer starts missing signals. Mitigation: schema validation on every read; log drift to `OPEN_QUESTIONS.md`; per-system adapter functions to absorb drift.
- **Ticker collision across exchanges.** `ABC` on NYSE is not `ABC` on LSE. Mitigation: never use ticker alone as a key; always ticker+MIC or FIGI.
- **Issuer vs. instrument confusion.** ADR and primary listing converging and being double-counted. Mitigation: roll up to `issuer_figi` for convergence, but preserve the specific instrument for the evidence trail.
- **Upstream partial write read.** Reading `signal_log.json` mid-write yields malformed JSON. Mitigation: read-retry with exponential backoff; if still malformed after 3 tries, skip this cycle and log.
- **One loud upstream system dominates the digest.** A scanner producing 500 signals/day drowns out a scanner producing 5/day. Mitigation: rank convergences by conviction score, not signal count; cap the number of single-system mentions in the digest.
- **Phantom convergence from stale signals.** An old signal never pruned from an upstream log keeps producing fake convergences. Mitigation: strictly respect the 14-day window using `scan_date`, not file modification time.
- **Adapter debt.** As upstream systems multiply, per-system adapters become a maintenance burden. Mitigation: pressure upstream systems to emit the canonical schema at source; adapters are for legacy only.

---

## PART 13 — THE STANDING QUESTION

Before every cross-system deliverable:

> "If the operator acted only on this analyzer's output — never reading a single upstream file — would they have the highest-conviction, best-explained, most-traceable picture of the portfolio opportunity set that is currently possible given what all upstream systems know? If not, what specifically is missing, and is it missing because the upstream system lacks it, or because this analyzer failed to surface it?"

If the answer is anything other than an honest yes, keep working — and if the gap is in an upstream system, log it in the gap report rather than papering over it here.

---

## HOW TO USE THIS FILE WHEN STARTING THE ANALYZER PROJECT

1. Read this file in full during the project setup session. Do not skim.
2. Walk through Parts 1–4 with the operator and confirm: which upstream systems exist today, where are they installed, what schema does each emit, is each emitting the mandatory fields.
3. Record the upstream inventory in `upstream_registry.json`.
4. Build `tools/entity_resolver.py` and `tools/adapters/<system_id>.py` for each upstream system's current schema.
5. Build the digest and dossier generators. Register the two scheduled tasks.
6. Run one manual cross-system cycle end-to-end before going autonomous.
7. Let it run for 7 days before tuning thresholds.
