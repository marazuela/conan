# Legacy `unified_system/` Classification

**Date:** 2026-04-27
**Source findings:** F-313, F-314, F-315, F-316

| Path | Classification | Notes |
|---|---|---|
| `data/legacy/*.json` | **preserved migration/reference data** | Small legacy inputs retained after deleting `unified_system/`: scanner registry, PE filer allowlist, phase-3 base rates, curated rationales, and PDUFA watchlist export. |
| `unified_system/unified_system/config/scanner_registry.json` | **migrated** | Replaced by `data/legacy/scanner_registry.json` for `migrations/seed_registry.py`. Live source of truth is still the `scanners` table (migration 20260420200000). |
| `unified_system/unified_system/strategies/*.md` | **reference-only** | ~20 files. Most don't map 1:1 to v2's six profiles (`merger_arb`, `activist_governance`, `binary_catalyst`, `short_positioning`, `litigation`, `takeover_candidate`). Keep for operator reference; add README clarifying historical status. |
| `unified_system/unified_system/tools/` | **likely DEAD** | ~59 files. Imported only by `migrations/import_candidates.py` and `migrations/seed_registry.py`. Equivalents in v2 (`modal_workers/shared/candidate_gate.py`, `observability.py`, etc.). Move to `_archived_tools/` after final grep-confirm. |
| `unified_system/unified_system/candidates/` | **migration-only / reference** | Curated rationale snapshots. Read by `import_candidates.py` (one-time migration). Subdirs `_archived_post_edge/`, `rejected_pending_thesis/` are historical. |
| `unified_system/unified_system/signals/` | **DEAD (snapshots)** | JSON snapshots from v1 era (signal_log, dedup files). No inbound code dependency. Safe to archive. |
| `unified_system/unified_system/working/` | **DEAD (snapshots)** | Convergence reports from April 2026. Reference only. |
| `unified_system/unified_system/framework/` | **reference-only** | Profile description markdowns. Document v2 thinking; not imported by code. |
| `unified_system/_ARCHIVED_*` | **DEAD (already archived)** | Pre-v2 directories explicitly marked archived. |

## Action items

- F-313: spawn-task — add idempotency marker to `import_candidates.py`.
- F-314: done — `seed_registry.py` now reads the preserved `data/legacy/` JSONs.
- F-315: defer — add README to `strategies/` clarifying status.
- F-316: defer — final grep across full repo, then move tools/ to `_archived_tools/`.

No deletes during this audit pass — find-only mode.
