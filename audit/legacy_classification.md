# Legacy `unified_system/` Classification

**Date:** 2026-04-27
**Source findings:** F-313, F-314, F-315, F-316

| Path | Classification | Notes |
|---|---|---|
| `unified_system/unified_system/config/scanner_registry.json` | **migration-only** | Read only by `modal_workers/migrations/seed_registry.py`. Live source of truth is `scanners` table (migration 20260420200000). Update docstring + JSON header. |
| `unified_system/unified_system/strategies/*.md` | **reference-only** | ~20 files. Most don't map 1:1 to v2's six profiles (`merger_arb`, `activist_governance`, `binary_catalyst`, `short_positioning`, `litigation`, `takeover_candidate`). Keep for operator reference; add README clarifying historical status. |
| `unified_system/unified_system/tools/` | **likely DEAD** | ~59 files. Imported only by `migrations/import_candidates.py` and `migrations/seed_registry.py`. Equivalents in v2 (`modal_workers/shared/candidate_gate.py`, `observability.py`, etc.). Move to `_archived_tools/` after final grep-confirm. |
| `unified_system/unified_system/candidates/` | **migration-only / reference** | Curated rationale snapshots. Read by `import_candidates.py` (one-time migration). Subdirs `_archived_post_edge/`, `rejected_pending_thesis/` are historical. |
| `unified_system/unified_system/signals/` | **DEAD (snapshots)** | JSON snapshots from v1 era (signal_log, dedup files). No inbound code dependency. Safe to archive. |
| `unified_system/unified_system/working/` | **DEAD (snapshots)** | Convergence reports from April 2026. Reference only. |
| `unified_system/unified_system/framework/` | **reference-only** | Profile description markdowns. Document v2 thinking; not imported by code. |
| `unified_system/_ARCHIVED_*` | **DEAD (already archived)** | Pre-v2 directories explicitly marked archived. |

## Action items

- F-313: spawn-task — add idempotency marker to `import_candidates.py`.
- F-314: defer — clarify in `seed_registry.py` docstring + JSON header.
- F-315: defer — add README to `strategies/` clarifying status.
- F-316: defer — final grep across full repo, then move tools/ to `_archived_tools/`.

No deletes during this audit pass — find-only mode.
