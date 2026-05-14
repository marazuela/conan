-- v3 reference_class_base_rates literature seed — PR-1 of the cross-cutting
-- orchestrator fix.
--
-- Problem: reference_class_base_rates is EMPTY in production. compute_base_rate
-- returns None for every lookup, Stage 4 anchor degrades, renormalize_priors
-- silently no-ops. Across 11 VRDN runs every assessment has reference_class set
-- but reference_class_base_rate=NULL; conviction is locked at 85% (model prior)
-- because no Bayesian update happens. Bayes is system-wide off.
--
-- Cause: the table is fed by post_mortem_runner._refit_reference_class on
-- resolved cases. No resolved cases exist yet (v3 is cold-start), so the table
-- never populates. Classic chicken-and-egg.
--
-- Fix: seed the table with literature-derived Phase-3-to-approval priors from
-- data/legacy/phase3_approval_base_rates.json (BIO 2011-2020, Hay 2014,
-- Wong/Siah/Lo 2019, Mullard 2023). Each seed row carries source='literature'
-- and effective_n derived from the literature cohort size. The companion
-- patch to _refit_reference_class (post_mortem_runner.py) Beta-Binomial
-- blends incoming empirical updates with the literature prior until empirical
-- n >= 10, then takes over fully.
--
-- ALSO: every active fda_asset has reference_class_signature=NULL today, which
-- means Stage 4 never even reaches compute_base_rate. We auto-pin signatures
-- from indication_normalized via an internal_indication_class_map seeded in
-- this migration; assets that don't map get the 'phase3_default' fallback.
--
-- SHADOW MODE: renormalize_priors_dry_run='true' is seeded into internal_config.
-- This makes hypothesis.py:renormalize_priors compute the renorm and log to
-- notes.renorm_debug WITHOUT mutating priors. Operator reviews ~1 week of
-- shadow output, then flips the flag to 'false' to activate. Avoids retroactive
-- Stage-5 conviction shifts on the first live run after PR-1 lands.
--
-- Rollback:
--   delete from internal_config where key = 'renormalize_priors_dry_run';
--   delete from reference_class_base_rates where source = 'literature';
--   update fda_assets set reference_class_signature = null
--     where reference_class_signature like 'phase3_%';
--   drop table internal_indication_class_map;
--   alter table reference_class_base_rates drop column source;
--   alter table reference_class_base_rates drop column effective_n;
--
-- Sequencing: PR-1 of 5. Lands AFTER PR-3 (sweeper) + PR-2 (content-dedup) so
-- the noise floor is already in place when the anchor activates.

-- ---------------------------------------------------------------------------
-- 1. Extend reference_class_base_rates schema.
-- ---------------------------------------------------------------------------

alter table public.reference_class_base_rates
  add column if not exists source text;

alter table public.reference_class_base_rates
  add column if not exists effective_n int;

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'reference_class_base_rates_source_check'
  ) then
    alter table public.reference_class_base_rates
      add constraint reference_class_base_rates_source_check
      check (source is null or source in ('literature','empirical','blended'));
  end if;
end $$;

comment on column public.reference_class_base_rates.source is
  'literature = seeded from BIO/Hay/Wong-Siah-Lo meta-estimates; empirical = derived from resolved post-mortems with n_empirical >= 10; blended = Beta-Binomial posterior of literature prior + sub-threshold empirical evidence.';

comment on column public.reference_class_base_rates.effective_n is
  'Effective prior sample size for Beta-Binomial blending. For literature rows: min(literature_n, 20) — caps how strongly the prior anchors when empirical evidence arrives. For empirical/blended: cumulative resolved-case count.';

-- ---------------------------------------------------------------------------
-- 2. Indication → canonical class mapping table.
-- ---------------------------------------------------------------------------

create table if not exists public.internal_indication_class_map (
  indication_normalized text primary key,
  canonical_class_key text not null,
  notes text
);

comment on table public.internal_indication_class_map is
  'Maps fda_assets.indication_normalized (coarse) to the fine-grained seed-key used in reference_class_base_rates. The reference_class_signature pinned on fda_assets is "phase3_" || canonical_class_key. New indications go here, not into ad-hoc UPDATEs.';

-- Coarse-to-fine mapping. Choices favor the modal subtype within each coarse
-- bucket; see provenance in the JSON. Refine over time as the orchestrator
-- discovers more granular evidence per asset.
insert into public.internal_indication_class_map
  (indication_normalized, canonical_class_key, notes) values
  ('oncology',            'oncology_solid_tumor',      'Default subtype; refine to hematologic/rare per-asset.'),
  ('autoimmune',          'autoimmune',                'JSON has matching key.'),
  ('rare_disease',        'rare_disease_genetic',      'Most common rare-disease pathway.'),
  ('cardiovascular',      'cardiovascular',            'JSON has matching key.'),
  ('cns',                 'default',                   'Too broad — could be psych or neuro; fall back to default until refined.'),
  ('hematology',          'hematology_rare',           'Modal hematology subtype in fda_assets is rare-blood.'),
  ('infectious',          'infectious_antiviral',      'Default infectious subtype; refine to antibacterial/vaccine per-asset.'),
  ('metabolic',           'metabolic_diabetes',        'GLP-1 class is modal here; refine to obesity per-asset.'),
  ('dermatology',         'dermatology_atopic_dermatitis', 'Modal dermatology subtype.'),
  ('iga nephropathy',     'nephrology_rare',           'IgAN is the canonical rare-nephrology indication.'),
  ('ophthalmology',       'ophthalmology_wet_amd',     'Modal ophthalmology subtype; refine to rare-eye per-asset.'),
  ('respiratory',         'respiratory_asthma',        'Modal respiratory subtype.'),
  ('thyroid_eye_disease', 'ophthalmology_rare',        'TED is the canonical rare-ophthalmology indication.'),
  ('other',               'default',                   'No bucket — use overall Phase 3→approval default.')
on conflict (indication_normalized) do nothing;

-- ---------------------------------------------------------------------------
-- 3. Seed reference_class_base_rates from the JSON.
-- ---------------------------------------------------------------------------
-- ON CONFLICT DO NOTHING: protects empirical/blended rows that may have been
-- written by _refit_reference_class between migrations. The literature seed is
-- only inserted where the class is genuinely missing.

insert into public.reference_class_base_rates
  (reference_class, n_cases, approval_rate, approval_rate_ci_low,
   approval_rate_ci_high, median_realized_move_pct, refit_at,
   source, effective_n)
values
  -- Oncology
  ('phase3_oncology_solid_tumor',   20, 0.620, null, null, null, now(), 'literature', 20),
  ('phase3_oncology_hematologic',   18, 0.730, null, null, null, now(), 'literature', 18),
  ('phase3_oncology_rare',          15, 0.780, null, null, null, now(), 'literature', 15),
  -- Cardiovascular & metabolic
  ('phase3_cardiovascular',         20, 0.560, null, null, null, now(), 'literature', 20),
  ('phase3_metabolic_diabetes',     20, 0.680, null, null, null, now(), 'literature', 20),
  ('phase3_metabolic_obesity',      12, 0.710, null, null, null, now(), 'literature', 12),
  -- Neurology
  ('phase3_neurology_alzheimers',   18, 0.310, null, null, null, now(), 'literature', 18),
  ('phase3_neurology_als',          10, 0.420, null, null, null, now(), 'literature', 10),
  ('phase3_neurology_parkinsons',   12, 0.480, null, null, null, now(), 'literature', 12),
  ('phase3_neurology_migraine',     12, 0.720, null, null, null, now(), 'literature', 12),
  ('phase3_neurology_epilepsy',     12, 0.680, null, null, null, now(), 'literature', 12),
  -- Psychiatry
  ('phase3_psychiatry_depression',  20, 0.550, null, null, null, now(), 'literature', 20),
  ('phase3_psychiatry_schizophrenia', 18, 0.620, null, null, null, now(), 'literature', 18),
  ('phase3_psychiatry_agitation',   10, 0.580, null, null, null, now(), 'literature', 10),
  -- Infectious
  ('phase3_infectious_antiviral',   18, 0.690, null, null, null, now(), 'literature', 18),
  ('phase3_infectious_antibacterial', 15, 0.740, null, null, null, now(), 'literature', 15),
  ('phase3_infectious_vaccine',     14, 0.650, null, null, null, now(), 'literature', 14),
  -- Respiratory
  ('phase3_respiratory_asthma',     16, 0.710, null, null, null, now(), 'literature', 16),
  ('phase3_respiratory_copd',       15, 0.670, null, null, null, now(), 'literature', 15),
  ('phase3_respiratory_ipf',        10, 0.520, null, null, null, now(), 'literature', 10),
  -- GI / hepato
  ('phase3_gastro_ibd',             18, 0.680, null, null, null, now(), 'literature', 18),
  ('phase3_gastro_nash',            12, 0.380, null, null, null, now(), 'literature', 12),
  ('phase3_hepatology_hepb',        12, 0.620, null, null, null, now(), 'literature', 12),
  -- Nephrology
  ('phase3_nephrology_ckd',         12, 0.600, null, null, null, now(), 'literature', 12),
  ('phase3_nephrology_rare',        10, 0.750, null, null, null, now(), 'literature', 10),
  -- Dermatology / rheumatology
  ('phase3_dermatology_psoriasis',  18, 0.760, null, null, null, now(), 'literature', 18),
  ('phase3_dermatology_atopic_dermatitis', 14, 0.710, null, null, null, now(), 'literature', 14),
  ('phase3_rheumatology_ra',        20, 0.680, null, null, null, now(), 'literature', 20),
  -- Ophthalmology / endocrine
  ('phase3_ophthalmology_wet_amd',  16, 0.630, null, null, null, now(), 'literature', 16),
  ('phase3_ophthalmology_rare',     10, 0.720, null, null, null, now(), 'literature', 10),
  ('phase3_endocrinology_thyroid',  12, 0.700, null, null, null, now(), 'literature', 12),
  -- Rare disease
  ('phase3_rare_disease_genetic',   15, 0.770, null, null, null, now(), 'literature', 15),
  ('phase3_rare_disease_metabolic', 12, 0.740, null, null, null, now(), 'literature', 12),
  -- Pain
  ('phase3_pain_chronic',           14, 0.520, null, null, null, now(), 'literature', 14),
  ('phase3_pain_acute',             12, 0.660, null, null, null, now(), 'literature', 12),
  -- Autoimmune / hematology
  ('phase3_autoimmune',             18, 0.650, null, null, null, now(), 'literature', 18),
  ('phase3_hematology_rare',        12, 0.750, null, null, null, now(), 'literature', 12),
  ('phase3_hematology_sickle_cell',  8, 0.680, null, null, null, now(), 'literature',  8),
  -- Fallback
  ('phase3_default',                50, 0.580, null, null, null, now(), 'literature', 20)
on conflict (reference_class) do nothing;

-- ---------------------------------------------------------------------------
-- 4. Auto-pin reference_class_signature on active fda_assets.
-- ---------------------------------------------------------------------------
-- One-shot UPDATE: any active asset whose signature is NULL gets pinned to
-- the canonical class derived from indication_normalized, with phase3_default
-- as fallback. Subsequent operator-pin actions (via fda_asset_pin_reference_class
-- RPC) override the heuristic — this only seeds the cold-start state.

update public.fda_assets fa
   set reference_class_signature = coalesce(
         (select 'phase3_' || m.canonical_class_key
            from public.internal_indication_class_map m
           where lower(m.indication_normalized) = lower(fa.indication_normalized)
           limit 1),
         'phase3_default'
       ),
       updated_at = now()
 where fa.is_active = true
   and fa.reference_class_signature is null;

-- ---------------------------------------------------------------------------
-- 5. Shadow-mode flag.
-- ---------------------------------------------------------------------------
-- renormalize_priors_dry_run = 'true' → hypothesis.py:renormalize_priors writes
-- the renorm result into stage_metrics.notes.renorm_debug but returns the
-- original priors unchanged. Operator inspects ~1 week of shadow output, then:
--   UPDATE internal_config SET value='false' WHERE key='renormalize_priors_dry_run';
-- to activate. The flag's existence is the activation contract; absent → behave
-- as previously (no shadow logging, but ALSO no mutation if base_rate is None).

insert into public.internal_config (key, value, updated_at)
values ('renormalize_priors_dry_run', 'true', now())
on conflict (key) do nothing;
