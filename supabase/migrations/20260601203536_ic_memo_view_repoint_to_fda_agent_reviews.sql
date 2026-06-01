-- Repoint v_assessment_with_ic_memo from the dead sub_agent_calls path to the
-- live fda_agent_reviews (agent_kind='ic_memo') store. PR #94 + the v4 cutover
-- left memos written to fda_agent_reviews while this view still joined
-- convergence_assessments.ic_memo_call_id -> sub_agent_calls (0 rows), so the
-- dashboard /memos queue + sidebar/inbox counts + memo search saw ZERO of the
-- real memos (0 visible vs 11+ actually generated).
--
-- Grain: ONE row per asset = latest non-superseded assessment -> asset's latest
-- pending event -> that event's latest completed ic_memo. Matches
-- fda_signal_promote_to_thesis (asset's latest non-superseded assessment + the
-- event's review id) and dedups assets with multiple non-superseded assessments.
-- All 5 dashboard consumers are memo-oriented, so a memo-primary (INNER) view is
-- correct for every caller and makes the superseded-null-only counts count real
-- memos. Column names/types/order preserved so the generated TS types still fit;
-- cost_usd/latency_ms/superseded_at have no fda_agent_reviews equivalent -> NULL.
-- Applied live via MCP 2026-06-01 20:35 (ledger 20260601203536); committed here
-- to keep disk in sync.
CREATE OR REPLACE VIEW public.v_assessment_with_ic_memo AS
SELECT DISTINCT ON (ca.asset_id)
  ca.id                        AS assessment_id,
  ca.asset_id                  AS asset_id,
  ca.created_at                AS assessment_at,
  ca.conviction_pct_calibrated AS conviction_pct_calibrated,
  ca.band                      AS band,
  ca.thesis_direction          AS thesis_direction,
  ca.superseded_at             AS assessment_superseded_at,
  fr.id                        AS ic_memo_call_id,
  fr.structured_output         AS ic_memo_output,
  fr.created_at                AS ic_memo_created_at,
  NULL::timestamptz            AS ic_memo_superseded_at,
  NULL::numeric(8,4)           AS ic_memo_cost_usd,
  NULL::integer                AS ic_memo_latency_ms
FROM public.convergence_assessments ca
JOIN LATERAL (
  SELECT e.id AS event_id
  FROM public.fda_regulatory_events e
  WHERE e.asset_id = ca.asset_id AND e.event_status = 'pending'
  ORDER BY e.created_at DESC
  LIMIT 1
) ev ON true
JOIN LATERAL (
  SELECT r.id, r.structured_output, r.created_at
  FROM public.fda_agent_reviews r
  WHERE r.event_id = ev.event_id
    AND r.agent_kind = 'ic_memo'
    AND r.status = 'completed'
  ORDER BY r.created_at DESC
  LIMIT 1
) fr ON true
WHERE ca.superseded_at IS NULL
ORDER BY ca.asset_id, ca.created_at DESC;
