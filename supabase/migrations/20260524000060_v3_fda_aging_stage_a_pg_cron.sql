-- 20260524000060_v3_fda_aging_stage_a_pg_cron.sql
-- pg_cron schedule for the deterministic Stage A aging sweep.
-- Plan ref: /Users/Pico/.claude/plans/plan-it-thoroughly-tingly-fountain.md (M6)
--
-- The Stage A SQL function `v3_fda_aging_stage_a()` is a pure SQL sweep — no
-- Modal call, no Anthropic API. Runs daily at 05:55 UTC, 5 minutes before
-- the Cowork-resident fda_aging_review skill (06:00 UTC) so Stage A's writes
-- to fda_aging_verdicts + fda_assets.aging_state are visible to the skill.
--
-- The Cowork skills (fda_aging_review @ daily 06:00 UTC, fda_challenger_replay
-- @ Sun 09:00 UTC) are scheduled in Pedro's Cowork UI — they are NOT pg_cron
-- jobs. Cowork's seat-based dispatch makes the Claude work cost-free; pg_cron
-- wouldn't be cheaper. Only Stage A (pure SQL, no Claude) is in pg_cron.
--
-- Rollback: `select cron.unschedule('v3-fda-aging-stage-a');`. The function
-- itself stays available for manual invocation.

create extension if not exists pg_cron with schema extensions cascade;

do $$
declare
  v_existing_jobid bigint;
begin
  select jobid into v_existing_jobid
    from cron.job
   where jobname = 'v3-fda-aging-stage-a';

  if v_existing_jobid is not null then
    perform cron.unschedule(v_existing_jobid);
  end if;

  perform cron.schedule(
    'v3-fda-aging-stage-a',
    '55 5 * * *',
    $cron$
      select public.v3_fda_aging_stage_a();
    $cron$
  );
end
$$;
