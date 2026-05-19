-- ============================================================================
-- SOTA biotech discovery funnel, resolver states, and evidence-packet gates
-- ============================================================================
-- Additive layer on top of the v3 FDA spine. This migration makes the
-- discovery -> identity -> evidence -> review funnel queryable without changing
-- historical scanner contracts.

-- ---------------------------------------------------------------------------
-- 0. operator_flags source for discovery/resolver audits
-- ---------------------------------------------------------------------------

DO $$
DECLARE
  v_def text;
BEGIN
  SELECT pg_get_constraintdef(oid) INTO v_def
  FROM pg_constraint
  WHERE conrelid = 'public.operator_flags'::regclass
    AND conname = 'operator_flags_source_check';

  IF v_def IS NULL THEN
    RAISE NOTICE 'operator_flags_source_check not found — skipping';
  ELSIF v_def LIKE '%biotech_discovery%' THEN
    RAISE NOTICE 'biotech_discovery already allowed in operator_flags.source';
  ELSE
    ALTER TABLE public.operator_flags DROP CONSTRAINT operator_flags_source_check;
    ALTER TABLE public.operator_flags ADD CONSTRAINT operator_flags_source_check
      CHECK (source = ANY (ARRAY[
        'translation_health','scanner_probe','scanner_liveness','convergence_qa',
        'candidate_aging','thesis_writer','reactor','reporting_weekly',
        'litigation_baselines','edgar_runtime_health','scanner_failure_streak',
        'rollback_monitor','orchestrator_cost','thesis_jobs','manual',
        'v3_pipeline_watchdog','aging_review','challenger_retro',
        'constitutional_check','memory_writeback','tier2_quality',
        'orphan_sweeper','backfill_v3_assessment',
        'bridge_signal_to_v3',
        'signal_entity_resolver_hard_halt',
        'signal_entity_resolver_run',
        'biotech_discovery'
      ]));
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 1. Discovery lanes and raw candidate audit surface
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.fda_discovery_lanes (
  lane text PRIMARY KEY CHECK (lane IN (
    'regulatory_calendar',
    'pre_readout',
    'evidence_delta',
    'market_disagreement'
  )),
  description text NOT NULL,
  priority smallint NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
  created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO public.fda_discovery_lanes (lane, description, priority)
VALUES
  ('regulatory_calendar', 'PDUFA, AdCom, CRL, approval, and date-change discovery', 1),
  ('pre_readout', 'ClinicalTrials.gov Phase 3 readout and EOP2 early-warning discovery', 2),
  ('evidence_delta', 'New primary documents, labels, briefing docs, and trial-status deltas', 2),
  ('market_disagreement', 'Options, price, volume, and post-event drift prioritization', 3)
ON CONFLICT (lane) DO UPDATE
  SET description = EXCLUDED.description,
      priority = EXCLUDED.priority;

CREATE TABLE IF NOT EXISTS public.fda_discovery_candidates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  lane text NOT NULL REFERENCES public.fda_discovery_lanes(lane),
  source text NOT NULL,
  source_doc_id text,
  source_url text,
  source_content_hash text,
  signal_id text REFERENCES public.signals(signal_id) ON DELETE SET NULL,
  asset_id uuid REFERENCES public.fda_assets(id) ON DELETE SET NULL,
  ticker text,
  drug_name text,
  sponsor_name text,
  event_type text,
  event_date date,
  identity_confidence numeric(4,3)
    CHECK (identity_confidence IS NULL OR identity_confidence BETWEEN 0 AND 1),
  review_priority smallint CHECK (review_priority IS NULL OR review_priority BETWEEN 1 AND 5),
  resolver_state text NOT NULL DEFAULT 'candidate'
    CHECK (resolver_state IN (
      'candidate',
      'resolved_public_asset',
      'ambiguous_public_asset',
      'private_or_non_tradeable',
      'foreign_unmapped',
      'missing_drug_name',
      'missing_sponsor',
      'duplicate_asset_candidate',
      'needs_alias',
      'resolved_by_operator',
      'rejected_weak_evidence',
      'stale_catalyst',
      'review_queued',
      'reviewed'
    )),
  failure_reason text,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  discovered_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, source_content_hash)
);

CREATE INDEX IF NOT EXISTS fda_discovery_candidates_lane_state_idx
  ON public.fda_discovery_candidates(lane, resolver_state, discovered_at DESC);
CREATE INDEX IF NOT EXISTS fda_discovery_candidates_signal_idx
  ON public.fda_discovery_candidates(signal_id) WHERE signal_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS fda_discovery_candidates_asset_idx
  ON public.fda_discovery_candidates(asset_id) WHERE asset_id IS NOT NULL;

DROP TRIGGER IF EXISTS fda_discovery_candidates_updated ON public.fda_discovery_candidates;
CREATE TRIGGER fda_discovery_candidates_updated
  BEFORE UPDATE ON public.fda_discovery_candidates
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

ALTER TABLE public.fda_discovery_lanes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.fda_discovery_candidates ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS fda_discovery_lanes_select ON public.fda_discovery_lanes;
CREATE POLICY fda_discovery_lanes_select
  ON public.fda_discovery_lanes FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS fda_discovery_candidates_select ON public.fda_discovery_candidates;
CREATE POLICY fda_discovery_candidates_select
  ON public.fda_discovery_candidates FOR SELECT TO authenticated USING (true);

-- ---------------------------------------------------------------------------
-- 2. Resolver alias / override table
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.fda_asset_resolution_aliases (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  alias_type text NOT NULL CHECK (alias_type IN (
    'sponsor_name','drug_name','ticker','cik','nct_id','application_number'
  )),
  alias_value text NOT NULL,
  canonical_value text,
  asset_id uuid REFERENCES public.fda_assets(id) ON DELETE CASCADE,
  entity_id uuid REFERENCES public.entities(id) ON DELETE SET NULL,
  ticker text,
  drug_name text,
  application_number text,
  confidence numeric(4,3) NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
  source text NOT NULL DEFAULT 'operator'
    CHECK (source IN ('operator','bridge_signal_to_v3','scanner','backfill')),
  notes text,
  created_by uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS fda_asset_resolution_aliases_type_value_uniq
  ON public.fda_asset_resolution_aliases(alias_type, lower(alias_value));
CREATE INDEX IF NOT EXISTS fda_asset_resolution_aliases_asset_idx
  ON public.fda_asset_resolution_aliases(asset_id) WHERE asset_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS fda_asset_resolution_aliases_entity_idx
  ON public.fda_asset_resolution_aliases(entity_id) WHERE entity_id IS NOT NULL;

DROP TRIGGER IF EXISTS fda_asset_resolution_aliases_updated ON public.fda_asset_resolution_aliases;
CREATE TRIGGER fda_asset_resolution_aliases_updated
  BEFORE UPDATE ON public.fda_asset_resolution_aliases
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

ALTER TABLE public.fda_asset_resolution_aliases ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS fda_asset_resolution_aliases_select ON public.fda_asset_resolution_aliases;
CREATE POLICY fda_asset_resolution_aliases_select
  ON public.fda_asset_resolution_aliases FOR SELECT TO authenticated USING (true);

CREATE OR REPLACE FUNCTION public.fda_apply_resolution_aliases_to_signal()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $func$
DECLARE
  v_ticker text;
  v_drug_name text;
  v_sponsor text;
  v_nct text;
  v_application_number text;
  v_alias public.fda_asset_resolution_aliases%ROWTYPE;
  v_asset public.fda_assets%ROWTYPE;
BEGIN
  IF NEW.scoring_profile NOT IN ('binary_catalyst','fda_event') THEN
    RETURN NEW;
  END IF;

  v_ticker := COALESCE(
    NULLIF(NEW.raw_payload -> 'auto_seed_fda_asset' ->> 'ticker', ''),
    NULLIF(NEW.raw_payload ->> 'ticker', ''),
    NULLIF(NEW.raw_payload ->> 'universe_ticker', '')
  );
  v_drug_name := COALESCE(
    NULLIF(NEW.raw_payload -> 'auto_seed_fda_asset' ->> 'drug_name', ''),
    NULLIF(NEW.raw_payload ->> 'drug_name', ''),
    NULLIF(NEW.raw_payload ->> 'product_name', ''),
    NULLIF(NEW.raw_payload ->> 'generic_name', ''),
    NULLIF(NEW.raw_payload ->> 'asset_name', '')
  );
  v_sponsor := COALESCE(
    NULLIF(NEW.raw_payload -> 'auto_seed_fda_asset' ->> 'sponsor_name', ''),
    NULLIF(NEW.raw_payload ->> 'sponsor_name', ''),
    NULLIF(NEW.raw_payload ->> 'company_name', ''),
    NULLIF(NEW.raw_payload ->> 'company_name_en', ''),
    NULLIF(NEW.raw_payload ->> 'universe_title', '')
  );
  v_nct := COALESCE(
    NULLIF(NEW.raw_payload -> 'auto_seed_fda_asset' ->> 'nct_id', ''),
    NULLIF(NEW.raw_payload ->> 'nct_id', ''),
    NULLIF(NEW.raw_payload ->> 'phase3_nctid', '')
  );
  v_application_number := NULLIF(NEW.raw_payload ->> 'application_number', '');

  -- Highest-confidence aliases identify one asset directly.
  IF v_nct IS NOT NULL THEN
    SELECT * INTO v_alias
    FROM public.fda_asset_resolution_aliases
    WHERE alias_type = 'nct_id'
      AND lower(alias_value) = lower(v_nct)
      AND asset_id IS NOT NULL
    ORDER BY confidence DESC, created_at DESC
    LIMIT 1;
  END IF;

  IF v_alias.id IS NULL AND v_application_number IS NOT NULL THEN
    SELECT * INTO v_alias
    FROM public.fda_asset_resolution_aliases
    WHERE alias_type = 'application_number'
      AND lower(alias_value) = lower(v_application_number)
      AND asset_id IS NOT NULL
    ORDER BY confidence DESC, created_at DESC
    LIMIT 1;
  END IF;

  -- Drug/sponsor aliases are only allowed to canonicalize when the ticker also
  -- agrees. This prevents a common INN from pulling the signal to the wrong
  -- multi-sponsor or multi-asset issuer.
  IF v_alias.id IS NULL AND v_drug_name IS NOT NULL AND v_ticker IS NOT NULL THEN
    SELECT al.* INTO v_alias
    FROM public.fda_asset_resolution_aliases al
    JOIN public.fda_assets a ON a.id = al.asset_id
    WHERE al.alias_type = 'drug_name'
      AND lower(al.alias_value) = lower(v_drug_name)
      AND a.ticker = v_ticker
    ORDER BY al.confidence DESC, al.created_at DESC
    LIMIT 1;
  END IF;

  IF v_alias.id IS NULL AND v_sponsor IS NOT NULL AND v_ticker IS NOT NULL THEN
    SELECT al.* INTO v_alias
    FROM public.fda_asset_resolution_aliases al
    JOIN public.fda_assets a ON a.id = al.asset_id
    WHERE al.alias_type = 'sponsor_name'
      AND lower(al.alias_value) = lower(v_sponsor)
      AND a.ticker = v_ticker
    ORDER BY al.confidence DESC, al.created_at DESC
    LIMIT 1;
  END IF;

  IF v_alias.id IS NULL THEN
    RETURN NEW;
  END IF;

  SELECT * INTO v_asset
  FROM public.fda_assets
  WHERE id = v_alias.asset_id;
  IF v_asset.id IS NULL THEN
    RETURN NEW;
  END IF;

  NEW.raw_payload := NEW.raw_payload || jsonb_build_object(
    'ticker', v_asset.ticker,
    'drug_name', v_asset.drug_name,
    'application_number', v_asset.application_number,
    'company_name', COALESCE(v_asset.sponsor_name, v_sponsor),
    'resolver_alias', jsonb_build_object(
      'alias_id', v_alias.id,
      'alias_type', v_alias.alias_type,
      'alias_value', v_alias.alias_value,
      'canonical_asset_id', v_asset.id,
      'confidence', v_alias.confidence
    ),
    'auto_seed_fda_asset', jsonb_build_object(
      'ticker', v_asset.ticker,
      'drug_name', v_asset.drug_name,
      'sponsor_name', COALESCE(v_asset.sponsor_name, v_sponsor),
      'indication', v_asset.indication,
      'nct_id', v_nct
    )
  );

  RETURN NEW;
END;
$func$;

COMMENT ON FUNCTION public.fda_apply_resolution_aliases_to_signal() IS
  'BEFORE INSERT canonicalizer for FDA-family signals. Applies conservative '
  'operator/scanner aliases before bridge_signal_to_v3_row runs.';

DROP TRIGGER IF EXISTS fda_apply_resolution_aliases_to_signal_tg ON public.signals;
CREATE TRIGGER fda_apply_resolution_aliases_to_signal_tg
  BEFORE INSERT ON public.signals
  FOR EACH ROW
  EXECUTE FUNCTION public.fda_apply_resolution_aliases_to_signal();

-- ---------------------------------------------------------------------------
-- 3. Signal -> review funnel view
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW public.fda_signal_discovery_funnel AS
WITH fda_signals AS (
  SELECT
    s.*,
    COALESCE(
      s.raw_payload -> 'auto_seed_fda_asset' ->> 'ticker',
      s.raw_payload ->> 'ticker',
      s.raw_payload ->> 'universe_ticker'
    ) AS resolved_ticker_hint,
    COALESCE(
      s.raw_payload -> 'auto_seed_fda_asset' ->> 'drug_name',
      s.raw_payload ->> 'drug_name',
      s.raw_payload ->> 'product_name',
      s.raw_payload ->> 'generic_name',
      s.raw_payload ->> 'asset_name'
    ) AS resolved_drug_hint,
    COALESCE(
      s.raw_payload -> 'auto_seed_fda_asset' ->> 'sponsor_name',
      s.raw_payload ->> 'sponsor_name',
      s.raw_payload ->> 'company_name',
      s.raw_payload ->> 'company_name_en',
      s.raw_payload ->> 'universe_title'
    ) AS resolved_sponsor_hint
  FROM public.signals s
  WHERE s.scoring_profile IN ('binary_catalyst','fda_event')
    AND s.signal_type IN (
      'pre_phase3_readout','pdufa_watchlist','eop2_meeting','fda_decision',
      'pdufa_imminent','pdufa_approaching','pdufa_date_advanced','pdufa_date_delayed',
      'adcom_scheduled','clinical_readout'
    )
)
SELECT
  s.signal_id,
  s.scanner_id,
  sc.name AS scanner_name,
  COALESCE(NULLIF(s.raw_payload ->> 'discovery_lane', ''), CASE
    WHEN s.signal_type IN ('pdufa_watchlist','pdufa_imminent','pdufa_approaching',
                           'pdufa_date_advanced','pdufa_date_delayed',
                           'fda_decision','adcom_scheduled') THEN 'regulatory_calendar'
    WHEN s.signal_type IN ('pre_phase3_readout','eop2_meeting','clinical_readout') THEN 'pre_readout'
    ELSE 'evidence_delta'
  END) AS lane,
  s.signal_type,
  s.scoring_profile,
  s.source_date,
  s.scan_date,
  s.created_at,
  s.resolved_ticker_hint AS ticker,
  s.resolved_drug_hint AS drug_name,
  s.resolved_sponsor_hint AS sponsor_name,
  s.raw_payload ->> 'pdufa_date' AS pdufa_date,
  COALESCE(
    s.raw_payload -> 'auto_seed_fda_asset' ->> 'nct_id',
    s.raw_payload ->> 'nct_id',
    s.raw_payload ->> 'phase3_nctid'
  ) AS nct_id,
  d.id AS document_id,
  ad.asset_id,
  a.ticker AS asset_ticker,
  a.drug_name AS asset_drug_name,
  ad.id AS asset_document_id,
  ad.link_type,
  ad.is_material,
  r.id AS latest_run_id,
  r.tier AS latest_run_tier,
  r.status AS latest_run_status,
  ca.id AS latest_assessment_id,
  ca.tier AS latest_assessment_tier,
  ca.band AS latest_assessment_band,
  ca.conviction_pct AS latest_conviction_pct,
  ofl.id AS open_flag_id,
  ofl.kind AS open_flag_kind,
  CASE
    WHEN ca.id IS NOT NULL THEN 'reviewed'
    WHEN r.id IS NOT NULL THEN 'review_queued'
    WHEN ad.id IS NOT NULL THEN 'resolved_public_asset'
    WHEN ofl.kind = 'v3_bridge_no_asset_match'
      AND ofl.evidence ->> 'drug_name_was_garbage' = 'true' THEN 'missing_drug_name'
    WHEN ofl.kind = 'v3_bridge_no_asset_match'
      AND COALESCE(ofl.evidence ->> 'ticker', '') = '' THEN 'foreign_unmapped'
    WHEN ofl.kind = 'v3_bridge_no_asset_match'
      AND COALESCE(ofl.evidence ->> 'drug_name', '') = '' THEN 'missing_drug_name'
    WHEN ofl.id IS NOT NULL THEN 'needs_alias'
    WHEN s.resolved_ticker_hint IS NULL THEN 'foreign_unmapped'
    WHEN s.resolved_drug_hint IS NULL THEN 'missing_drug_name'
    WHEN s.resolved_sponsor_hint IS NULL THEN 'missing_sponsor'
    ELSE 'candidate'
  END AS resolver_state
FROM fda_signals s
LEFT JOIN public.scanners sc ON sc.id = s.scanner_id
LEFT JOIN public.documents d
  ON d.source = 'conan_signal'
 AND d.extensions ->> 'signal_id' = s.signal_id
LEFT JOIN public.asset_documents ad
  ON ad.document_id = d.id
 AND ad.link_type IN ('primary','safety_signal')
LEFT JOIN public.fda_assets a ON a.id = ad.asset_id
LEFT JOIN LATERAL (
  SELECT r0.*
  FROM public.orchestrator_runs r0
  WHERE r0.asset_id = ad.asset_id
  ORDER BY r0.created_at DESC
  LIMIT 1
) r ON true
LEFT JOIN LATERAL (
  SELECT ca0.*
  FROM public.convergence_assessments ca0
  WHERE ca0.asset_id = ad.asset_id
    AND ca0.superseded_at IS NULL
  ORDER BY ca0.created_at DESC
  LIMIT 1
) ca ON true
LEFT JOIN LATERAL (
  SELECT of0.*
  FROM public.operator_flags of0
  WHERE of0.signal_id = s.signal_id
    AND of0.source IN ('bridge_signal_to_v3','biotech_discovery')
    AND of0.resolved_at IS NULL
  ORDER BY of0.created_at DESC
  LIMIT 1
) ofl ON true;

COMMENT ON VIEW public.fda_signal_discovery_funnel IS
  'SOTA biotech discovery scoreboard: FDA-family signals traced through '
  'synthetic documents, asset_documents, orchestrator_runs, assessments, and '
  'resolver/operator states.';

CREATE OR REPLACE VIEW public.fda_discovery_funnel_daily AS
WITH base AS (
  SELECT
    date_trunc('day', created_at)::date AS day,
    lane,
    scanner_name,
    resolver_state,
    signal_id,
    ticker,
    drug_name,
    asset_id,
    document_id,
    asset_document_id,
    latest_run_id,
    latest_assessment_id,
    open_flag_id
  FROM public.fda_signal_discovery_funnel
),
totals AS (
  SELECT
    day,
    lane,
    scanner_name,
    count(*) AS signals_inserted,
    count(*) FILTER (WHERE ticker IS NOT NULL) AS ticker_hints,
    count(*) FILTER (WHERE drug_name IS NOT NULL) AS drug_hints,
    count(*) FILTER (WHERE asset_id IS NOT NULL) AS assets_resolved,
    count(*) FILTER (WHERE document_id IS NOT NULL) AS documents_created,
    count(*) FILTER (WHERE asset_document_id IS NOT NULL) AS asset_documents_linked,
    count(*) FILTER (WHERE latest_run_id IS NOT NULL) AS orchestrator_runs_seen,
    count(*) FILTER (WHERE latest_assessment_id IS NOT NULL) AS assessments_seen,
    count(*) FILTER (WHERE open_flag_id IS NOT NULL) AS open_flags
  FROM base
  GROUP BY day, lane, scanner_name
),
state_counts AS (
  SELECT
    day,
    lane,
    scanner_name,
    jsonb_object_agg(resolver_state, n ORDER BY resolver_state) AS resolver_state_counts
  FROM (
    SELECT day, lane, scanner_name, resolver_state, count(*) AS n
    FROM base
    GROUP BY day, lane, scanner_name, resolver_state
  ) s
  GROUP BY day, lane, scanner_name
)
SELECT
  t.*,
  COALESCE(sc.resolver_state_counts, '{}'::jsonb) AS resolver_state_counts
FROM totals t
LEFT JOIN state_counts sc
  ON sc.day = t.day
 AND sc.lane = t.lane
 AND sc.scanner_name IS NOT DISTINCT FROM t.scanner_name;

-- ---------------------------------------------------------------------------
-- 4. Evidence packet view and RPC
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW public.fda_asset_evidence_packets AS
SELECT
  a.id AS asset_id,
  a.ticker,
  a.drug_name,
  a.sponsor_name,
  a.indication,
  a.program_status,
  a.watch_priority,
  count(DISTINCT ad.document_id) AS linked_document_count,
  count(DISTINCT ad.document_id) FILTER (
    WHERE ad.is_material
      AND ad.link_type IN ('primary','safety_signal')
  ) AS material_primary_document_count,
  count(DISTINCT ef.id) AS extracted_fact_count,
  count(DISTINCT re.id) FILTER (WHERE re.event_status = 'pending') AS pending_event_count,
  latest.id AS latest_assessment_id,
  latest.tier AS latest_assessment_tier,
  latest.band AS latest_assessment_band,
  latest.conviction_pct AS latest_conviction_pct,
  (
    a.ticker IS NOT NULL
    AND a.drug_name IS NOT NULL
    AND count(DISTINCT ad.document_id) FILTER (
      WHERE ad.is_material
        AND ad.link_type IN ('primary','safety_signal')
    ) >= 1
  ) AS tier2_packet_ok,
  (
    a.ticker IS NOT NULL
    AND a.drug_name IS NOT NULL
    AND count(DISTINCT ad.document_id) FILTER (
      WHERE ad.is_material
        AND ad.link_type IN ('primary','safety_signal')
    ) >= 1
    AND count(DISTINCT ef.id) >= 1
  ) AS tier1_packet_ok,
  array_remove(ARRAY[
    CASE WHEN a.ticker IS NULL THEN 'missing_ticker' END,
    CASE WHEN a.drug_name IS NULL THEN 'missing_drug_name' END,
    CASE WHEN count(DISTINCT ad.document_id) FILTER (
      WHERE ad.is_material
        AND ad.link_type IN ('primary','safety_signal')
    ) = 0 THEN 'missing_material_primary_document' END,
    CASE WHEN count(DISTINCT ef.id) = 0 THEN 'missing_extracted_facts' END
  ], NULL) AS packet_errors
FROM public.fda_assets a
LEFT JOIN public.asset_documents ad ON ad.asset_id = a.id
LEFT JOIN public.extracted_facts ef ON ef.asset_id = a.id
LEFT JOIN public.fda_regulatory_events re ON re.asset_id = a.id
LEFT JOIN LATERAL (
  SELECT ca0.*
  FROM public.convergence_assessments ca0
  WHERE ca0.asset_id = a.id
    AND ca0.superseded_at IS NULL
  ORDER BY ca0.created_at DESC
  LIMIT 1
) latest ON true
GROUP BY
  a.id, a.ticker, a.drug_name, a.sponsor_name, a.indication,
  a.program_status, a.watch_priority,
  latest.id, latest.tier, latest.band, latest.conviction_pct;

COMMENT ON VIEW public.fda_asset_evidence_packets IS
  'Per-asset evidence packet status. Tier 2 requires identity + at least one '
  'material primary document. Tier 1 additionally requires extracted facts.';

CREATE OR REPLACE FUNCTION public.fda_evidence_packet_status(
  p_asset_id uuid,
  p_tier int DEFAULT 2
)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
  SELECT jsonb_build_object(
    'asset_id', asset_id,
    'tier', p_tier,
    'ok', CASE WHEN p_tier = 1 THEN tier1_packet_ok ELSE tier2_packet_ok END,
    'errors', CASE
      WHEN p_tier = 1 THEN to_jsonb(packet_errors)
      ELSE to_jsonb(array_remove(packet_errors, 'missing_extracted_facts'))
    END,
    'linked_document_count', linked_document_count,
    'material_primary_document_count', material_primary_document_count,
    'extracted_fact_count', extracted_fact_count,
    'pending_event_count', pending_event_count,
    'latest_assessment_id', latest_assessment_id,
    'latest_assessment_tier', latest_assessment_tier
  )
  FROM public.fda_asset_evidence_packets
  WHERE asset_id = p_asset_id;
$$;

COMMENT ON FUNCTION public.fda_evidence_packet_status(uuid, int) IS
  'Return the SOTA evidence-packet gate for one FDA asset. p_tier=2 requires '
  'identity + material primary doc; p_tier=1 also requires extracted facts.';

REVOKE ALL ON FUNCTION public.fda_evidence_packet_status(uuid, int) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.fda_evidence_packet_status(uuid, int) FROM anon;
GRANT EXECUTE ON FUNCTION public.fda_evidence_packet_status(uuid, int) TO authenticated;
GRANT EXECUTE ON FUNCTION public.fda_evidence_packet_status(uuid, int) TO service_role;

-- ---------------------------------------------------------------------------
-- 5. Assessment and discovery quality scorecard views
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW public.fda_assessment_quality_scorecard AS
SELECT
  'latest_eval_run'::text AS metric_group,
  er.orchestrator_version,
  er.created_at,
  er.brier_score,
  er.ranking_auc,
  er.passed_gate,
  er.brier_delta_vs_prod,
  er.paired_bootstrap_p,
  er.ranking_auc_delta_vs_prod,
  er.n_eval_cases,
  er.max_single_asset_contribution_pct,
  er.gate_reason,
  jsonb_build_object(
    'brier_target', 0.18,
    'tier1_tier2_brier_delta_target', 0.15,
    'operator_watchlist_worthy_target', 0.85,
    'operator_direction_agreement_target', 0.70
  ) AS targets
FROM public.eval_runs er
ORDER BY er.created_at DESC
LIMIT 25;

CREATE OR REPLACE VIEW public.fda_discovery_quality_scorecard AS
SELECT
  now() AS measured_at,
  count(*) AS active_assets,
  count(*) FILTER (WHERE tier2_packet_ok) AS tier2_ready_assets,
  count(*) FILTER (WHERE tier1_packet_ok) AS tier1_ready_assets,
  round(
    100.0 * count(*) FILTER (WHERE tier2_packet_ok) / NULLIF(count(*), 0),
    2
  ) AS tier2_ready_pct,
  round(
    100.0 * count(*) FILTER (WHERE tier1_packet_ok) / NULLIF(count(*), 0),
    2
  ) AS tier1_ready_pct,
  count(*) FILTER (
    WHERE watch_priority <= 2 AND material_primary_document_count >= 3
  ) AS high_priority_assets_with_3_docs,
  count(*) FILTER (WHERE watch_priority <= 2) AS high_priority_assets
FROM public.fda_asset_evidence_packets
WHERE EXISTS (
  SELECT 1 FROM public.fda_assets a
  WHERE a.id = fda_asset_evidence_packets.asset_id
    AND a.is_active
);
