-- v4 Phase 9c — prompt_proposals table.
--
-- Mirrors rubric_proposals shape (migration 20260613008000) but applied
-- to Stage 1 / Stage 9 prompt text rather than rubric weights. The
-- quarterly prompt_retrospective Cowork skill writes one row per run
-- with status='pending_operator_review'. The Phase 9d dashboard surface
-- reads pending rows for operator approval; an accepted proposal does
-- NOT auto-deploy (prompts require a code change + redeploy — the
-- operator manually copies proposed_prompt_text into runtime.py and
-- opens a PR).
--
-- Phase 9e's A/B harness pre-filters: proposals that fail the D-103
-- gate (paired-bootstrap p>=0.05 OR AUC delta<0.05 OR n<200) get
-- status='failed_eval_gate' and never reach the dashboard. Only
-- gate-passing candidates surface for human review.
--
-- Plan: ~/.claude/plans/phases-6-and-7-staged-hedgehog.md (Phase 9c).

create table if not exists public.prompt_proposals (
  id                    uuid primary key default gen_random_uuid(),

  -- which prompt is being changed: STAGE_1_SYSTEM or STAGE_9_SYSTEM
  -- (matches the `stage` column on prompt_versions: 'stage_1_system' /
  -- 'stage_9_system'). Constrained to the same valid-set the registry
  -- enforces in code.
  stage                 text not null
    check (stage in ('stage_1_system', 'stage_9_system')),

  -- baseline: which prompt_version this proposal targets as the
  -- "current" snapshot. Lets the dashboard show the exact diff and
  -- detect stale proposals (active prompt has since changed).
  current_prompt_version_id uuid
    references public.prompt_versions(id) on delete set null,
  current_prompt_text   text not null,

  -- the proposed replacement. Stored as the full text (not a diff)
  -- so the operator can copy-paste straight into runtime.py.
  proposed_prompt_text  text not null,

  -- structured rationale + per-section diff for the dashboard.
  -- prompt_diff is a {added: [...], removed: [...], changed: [...]}
  -- shape the retro skill emits to make the dashboard rendering
  -- deterministic; see .claude/skills/prompt_retrospective.md for
  -- the exact schema.
  rationale             text not null,
  prompt_diff           jsonb,

  -- cohort the retro reasoned over. cohort_window is 90 days by
  -- default; cohort_size is the number of resolved post-mortems that
  -- passed the prompt_version_id NOT NULL filter (Phase 9c skips
  -- prompts with < 200 resolved cases since the D-103 gate needs n>=200).
  cohort_window_start   date not null,
  cohort_window_end     date not null,
  cohort_size           integer not null check (cohort_size >= 0),

  -- D-103 gate scores from Phase 9e A/B harness. Populated by the
  -- harness BEFORE the proposal is even visible to the operator.
  -- NULL means "not yet evaluated" (proposal hasn't been through 9e);
  -- failed_eval_gate status surfaces the gate scores so the operator
  -- can see why a candidate was filtered out.
  brier_delta           numeric,
  paired_bootstrap_p    numeric,
  auc_delta             numeric,
  n_eval_cases          integer,

  agent_version         text not null default 'prompt_retrospective_v0',

  -- status workflow:
  --   'pending_eval_gate'        — A/B harness hasn't scored it yet
  --   'failed_eval_gate'         — D-103 gate failed; dashboard hides it
  --   'pending_operator_review'  — gate passed; awaiting Pedro
  --   'accepted'                 — operator approved; awaiting manual PR
  --   'applied'                  — manual PR landed; new prompt deployed
  --   'rejected'                 — operator rejected with rationale
  status                text not null default 'pending_eval_gate'
    check (status in (
      'pending_eval_gate',
      'failed_eval_gate',
      'pending_operator_review',
      'accepted',
      'applied',
      'rejected'
    )),

  -- approval audit trail (mirrors rubric_proposals).
  approved_by           text,
  approved_at           timestamptz,

  -- when status='applied', the prompt_versions row that was created
  -- by the manual PR's Modal redeploy + first orchestrator run.
  applied_prompt_version_id uuid
    references public.prompt_versions(id) on delete set null,
  applied_at            timestamptz,

  rejected_reason       text,
  rejected_by           text,
  rejected_at           timestamptz,

  metadata              jsonb default '{}'::jsonb,

  created_at            timestamptz not null default now(),

  -- consistency CHECK constraints (mirror rubric_proposals pattern)
  constraint prompt_proposals_approval_consistent check (
    (status = 'accepted' and approved_by is not null and approved_at is not null)
    or status <> 'accepted'
  ),
  constraint prompt_proposals_application_consistent check (
    (status = 'applied' and applied_at is not null) or status <> 'applied'
  ),
  constraint prompt_proposals_rejection_consistent check (
    (status = 'rejected' and rejected_reason is not null
                         and rejected_by is not null
                         and rejected_at is not null)
    or status <> 'rejected'
  )
);

-- Pending operator review = the dashboard's primary query. Partial index
-- so it stays small (most rows transition out of this state quickly).
create index if not exists idx_prompt_proposals_pending
  on public.prompt_proposals (created_at desc)
  where status = 'pending_operator_review';

-- Gate-passing proposals by stage, for the "show me the latest accepted
-- Stage 1 change" dashboard widget.
create index if not exists idx_prompt_proposals_by_stage
  on public.prompt_proposals (stage, created_at desc);

-- Failed-gate proposals: keep them queryable for the post-mortem audit
-- ("what did we propose that the harness rejected") without polluting
-- the primary pending view.
create index if not exists idx_prompt_proposals_failed_gate
  on public.prompt_proposals (stage, created_at desc)
  where status = 'failed_eval_gate';

comment on table public.prompt_proposals is
  'v4 Phase 9c: Opus-generated prompt-change proposals from the quarterly '
  'prompt_retrospective skill. Mirrors rubric_proposals shape but for '
  'Stage 1 / Stage 9 prompt text. Operator approval marks status=accepted '
  'but does NOT auto-deploy — prompts require manual PR + redeploy.';

comment on column public.prompt_proposals.current_prompt_text is
  'Snapshot of the prompt text that was active when the retro ran. Lets '
  'the dashboard show the exact diff and detect stale proposals if the '
  'active prompt has since changed.';

comment on column public.prompt_proposals.prompt_diff is
  'Structured per-section diff: {added: [...], removed: [...], changed: '
  '[...]}. Emitted by prompt_retrospective skill so the dashboard render '
  'is deterministic.';

comment on column public.prompt_proposals.status is
  'pending_eval_gate -> failed_eval_gate | pending_operator_review -> '
  'accepted -> applied (or rejected at any review step).';

comment on column public.prompt_proposals.applied_prompt_version_id is
  'FK to the prompt_versions row that was minted when the manual PR '
  'landed and the first orchestrator_run_one with the new prompt '
  'persisted. Closes the loop: proposal -> approved -> applied -> '
  'specific live prompt version.';
