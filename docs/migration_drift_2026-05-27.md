# Migration drift audit — 2026-05-27

Generated post-PR #152 + #153 land. Snapshots reconciliation between the
173 local `.sql` files in `supabase/migrations/` and the 186 rows in live
conan's `supabase_migrations.schema_migrations` (project `xvwvwbnxdsjpnealarkh`).

Source script: `/tmp/reconcile.py` (DDL fingerprint after comment-strip +
whitespace-collapse + lowercase). Raw classification CSV at
`/tmp/migration_reconciliation.csv`.

## Headline

| Class | Count | Action |
|---|---|---|
| **APPLIED_EXACT** — version + name + body all match | 1 | leave alone |
| **MARK_APPLIED_BY_NAME** — same name, identical DDL, different version | 62 | `supabase migration repair --status applied <local_version>` then delete local file |
| **MARK_APPLIED_BY_FINGERPRINT** — different name, identical DDL | 4 | same as above |
| **APPLIED_NAME_DIFF_BODY** — same version + name on live, but local body drifted | 15 | byte-diff against live, decide whether to amend live or revert local |
| **RENAME_SAME_NAME_DIFF_BODY** — name matches a remote row, DDL differs | 55 | per-file review: is local a later iteration, or just a rebase artifact? |
| **NEW_DDL_DUP_VERSION** — genuinely new, sharing a version with another file | 13 | renumber to fresh timestamp, then apply |
| **NEW_DDL** — genuinely new, unique version | 23 | apply via `supabase db push` after the above |
| **Total local** | **173** | |
| **Total remote** | **186** | |
| **Remote rows with no local file** | **120** | applied via MCP one-shots that never produced a tracked file |

## Critical sub-finding — v4 Phase 6/7 schema NEVER applied

The v4 Phase 6c code has been deployed since 2026-05-26 (PR #150). The
following migrations are in `NEW_DDL` class — **the columns and tables they
define do not exist on live**:

| Migration | Defines | Code that depends on it |
|---|---|---|
| `20260613000000_v4_foundation_assessment_schema` | `convergence_assessments.commercial_dimensions`, `.orchestrator_version_v4`, `.signal_category`; `post_mortem_queue.signal_category`; partial index | every v4 Stage 10 persist |
| `20260613002000_skill_run_tracker` | `skill_runs` table | sub-agent dispatch telemetry |
| `20260613006000_v4_thesis_emitted_at` | `convergence_assessments.thesis_emitted_at` | thesis_transcriber gate |
| `20260613007000_v4_rubrics_v2_seed` | `rubrics` rows with `insider_pressure` + `shareholder_structure` dims | Phase 5 rubric weights |
| `20260613008000_v4_feedback_retrospective_schema` | `rubric_proposals`, `feedback_category_metrics` tables | Phase 7 weekly retro |

Verified `false` for all six columns/tables via `information_schema`. Since
`jsonb_populate_record` ignores unknown keys silently, **every v4 assessment
written since Phase 6a flip (2026-05-26) has been missing its v4-specific
metadata**. The rows landed, just without the commercial/category fields.

**Recommended fix order**: apply `20260613000000` first, before any other
v4-pending migration. Without it the column-write fan-out from a real Stage 10
persist will continue to be a silent data-loss problem.

## Root cause

Local files use a `YYYYMMDD000000` block convention (manual rounded timestamps
batched per day). MCP `apply_migration` and `supabase migration new` stamp
`YYYYMMDDHHMMSS` (UTC second-precision). Same DDL applied via either path
gets a different version string, and Supabase tracks by version not name —
so it sees them as totally separate migrations.

The two histories started diverging on 2026-04-27 and have been growing apart
ever since. Compounded by 17 cases where multiple unrelated migrations
collided on the same `000000` block (mostly the 5-file pileup on
`20260527000000`).

## Reconciliation order (recommended)

### Phase R-1: stop the bleeding (≈5 min, zero risk)
1. Adopt one convention going forward. Recommendation: drop the `000000` block
   pattern, always use `YYYYMMDDHHMMSS` from `date -u +%Y%m%d%H%M%S`. Set this
   in `CLAUDE.md` and a pre-commit hook.
2. Pre-commit check: reject any new file whose timestamp collides with an
   existing one in either `supabase/migrations/` or the live
   `schema_migrations` table.

### Phase R-2: fix the v4 schema gap (≈20 min, low risk)
1. Apply `20260613000000_v4_foundation_assessment_schema` via MCP one-shot
   (the columns are NULL-defaulted, no backfill needed).
2. Apply `20260613006000_v4_thesis_emitted_at` (single column add).
3. Apply `20260613007000_v4_rubrics_v2_seed` (seed; check `rubrics` table first
   in case rows already exist with the new dims).
4. Apply `20260613002000_skill_run_tracker` + `20260613008000_v4_feedback_retrospective_schema`.
5. Backfill: nothing — v4 rows already written before this fix simply lack
   commercial_dimensions / signal_category. Either re-run the assessments via
   Tier-1 trigger, or accept the data gap.

### Phase R-3: mark-applied the 66 zero-risk matches (≈30 min, low risk)
For each `MARK_APPLIED_BY_NAME` (62) and `MARK_APPLIED_BY_FINGERPRINT` (4):
```bash
supabase migration repair --status applied <local_version>
git rm supabase/migrations/<filename>.sql
```
Or, alternatively, delete the local file outright since the remote already
has the DDL under a different version — the file is provenance-only. Pick one;
don't do both, because `repair` will then complain on next run about a
recorded version with no file.

### Phase R-4: triage the 70 ambiguous cases (≈2-4 hours, medium risk)
For each `APPLIED_NAME_DIFF_BODY` (15) and `RENAME_SAME_NAME_DIFF_BODY` (55):
1. Diff local vs remote (`SELECT array_to_string(statements, E'\n;\n') FROM
   supabase_migrations.schema_migrations WHERE version=?`).
2. Decide: which of {local, remote, merged} is the intended state?
3. If local has additional DDL not in remote → write a SUPERSEDE migration
   with a fresh timestamp containing only the delta.
4. If local is older / less complete than remote → delete local file, mark
   applied via `repair`.

This phase is the slog. The CSV at `/tmp/migration_reconciliation.csv` lists
all 70 with their suggested remote-version partners.

### Phase R-5: ship the genuinely-new work (≈30 min, low risk if R-2 done)
The 13 NEW_DDL_DUP_VERSION files need a renumber first (`git mv` to a fresh
`YYYYMMDDHHMMSS` timestamp), then they + the 23 NEW_DDL files apply via
`supabase db push` from the xenodochial worktree.

After R-1 through R-5, `supabase migration list` should show 0 local-only
versions and 0 remote-only versions.

## Memory note update needed

`~/.claude/projects/-Users-Pico-Documents-Claude-Projects-Conan/memory/supabase_migrations_drift.md`
currently says "live DB is ahead of local .sql files with different version
numbers." Update post-reconciliation with:
- new convention (HHMMSS, no 000000 block)
- post-R-5 state assertion (local == remote)
- the v4 schema gap finding as a checked precedent

