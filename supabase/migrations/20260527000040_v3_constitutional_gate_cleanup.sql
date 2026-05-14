-- v3 constitutional-gate cleanup — PR-5 of the cross-cutting orchestrator fix.
--
-- Two problems addressed:
--
-- 1. Pre-D-117 ghost rows. Before commit b9aff40 (2026-05-13 13:33 UTC) Stage 7
--    constitutional check did not gate Stage 10 persist — rows landed with
--    constitutional_pass=FALSE despite failing the gate. The fix went forward-
--    only; legacy FALSE rows are still readable by downstream callers (IC memo
--    synthesis, calibration refit, dashboard). As of 2026-05-14 there are
--    3 such rows surviving. Mark them superseded so downstream queries that
--    filter on `superseded_at IS NULL` stop reading them.
--
-- 2. Tier-2 NULL is ambiguous. The bulk_v0 writer in orchestrator_runtime/tier2.py
--    explicitly lists `constitutional_pass` in TIER2_FORBIDDEN_NON_NULL — Tier-2
--    rows emit with constitutional_pass=NULL. But NULL also means "Tier-1 row
--    that hasn't been evaluated yet" in transient states. Downstream callers
--    can't tell whether a NULL row was deliberately gate-skipped (Tier-2 spec)
--    or has a pending evaluation (Tier-1 mid-flight). Add a new gate_status
--    column with explicit values: 'pass', 'fail', 'tier2_skipped',
--    'not_evaluated'.
--
-- Rollback:
--   -- (a) The supersede is reversible only by restoring the prior
--   --     superseded_at values — preserved nowhere. Don't roll back.
--   alter table convergence_assessments drop column gate_status;
--   alter table convergence_assessments drop constraint convergence_assessments_gate_status_check;
--
-- Sequencing: PR-5 of 5. Independent of other PRs; can land last.

-- ---------------------------------------------------------------------------
-- 1. Backfill: mark pre-D-117 cp=FALSE rows superseded.
-- ---------------------------------------------------------------------------
-- superseded_by stays NULL because there is no replacement row — this is a
-- terminal supersede. The orchestrator never re-ran these inputs once Stage 7
-- gating activated, so these rows have no successor and the orphan_sweeper
-- correctly leaves superseded_by NULL on terminal-supersede rows.

update public.convergence_assessments
   set superseded_at = now()
 where constitutional_pass = false
   and superseded_at is null
   and created_at < '2026-05-13 13:33:00+00';

-- ---------------------------------------------------------------------------
-- 2. gate_status column.
-- ---------------------------------------------------------------------------

alter table public.convergence_assessments
  add column if not exists gate_status text;

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'convergence_assessments_gate_status_check'
  ) then
    alter table public.convergence_assessments
      add constraint convergence_assessments_gate_status_check
      check (gate_status is null
             or gate_status in ('pass', 'fail', 'tier2_skipped', 'not_evaluated'));
  end if;
end $$;

comment on column public.convergence_assessments.gate_status is
  'Constitutional gate outcome. pass = Tier-1 Stage 7 returned pass_=True; fail = Tier-1 Stage 7 returned pass_=False (these rows are also superseded by the runtime ConstitutionalFailure abort, so seeing them in live data indicates a code drift); tier2_skipped = bulk_v0 emit (Tier-2 spec exempts the path from the gate); not_evaluated = Tier-1 mid-flight or pre-D-117 legacy. Tier-1 rows must set gate_status; Tier-2 rows set tier2_skipped. NULL is reserved for pre-PR-5 legacy rows and must be backfilled before downstream filters can rely on this column.';

-- ---------------------------------------------------------------------------
-- 3. Backfill gate_status for existing rows by inferring from constitutional_pass.
-- ---------------------------------------------------------------------------
-- Best-effort backfill so the column starts in a useful state:
--   Tier-2 bulk_v0 rows → tier2_skipped (regardless of constitutional_pass; spec
--                         requires NULL in the boolean column, gate_status now
--                         carries the truth)
--   constitutional_pass=true  → pass
--   constitutional_pass=false → fail (these are mostly the legacy pre-D-117
--                         rows we just superseded — gate_status records what
--                         actually happened)
--   constitutional_pass IS NULL on Tier-1 → not_evaluated

update public.convergence_assessments
   set gate_status = case
       when orchestrator_version = 'bulk_v0' then 'tier2_skipped'
       when constitutional_pass = true       then 'pass'
       when constitutional_pass = false      then 'fail'
       else 'not_evaluated'
   end
 where gate_status is null;
