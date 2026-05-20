"""Static checks for the skill-based asset linker cutover migration.

Covers both the original cutover (Modal LLM asset-linker disabled, local Cursor
skill is the only path) AND the deterministic edge-prefilter layer added on
top (doc_asset_candidates + fda_asset_aliases + tsvector-based sweeper).

Run: python -m pytest modal_workers/tests/test_skill_asset_linker_cutover_migration.py -v
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260601000000_skill_asset_linker_cutover.sql"
)
GUARDRAILS_MIGRATION = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260601000010_skill_cutover_operational_guardrails.sql"
)


def _sql() -> str:
    return MIGRATION.read_text()


def _guardrails_sql() -> str:
    return GUARDRAILS_MIGRATION.read_text()


# ----------------------------------------------------------------------
# Original cutover assertions
# ----------------------------------------------------------------------

def test_asset_linker_cron_is_unscheduled() -> None:
    sql = _sql()

    assert "cron.unschedule(v_jobid)" in sql
    assert "'v3-asset-linker-pass1'" in sql
    assert "'v3-asset-linker-pass2'" in sql


def test_fact_extractor_cron_is_unscheduled_and_not_recreated() -> None:
    sql = _sql()

    assert "'v3-fact-extractor'" in sql
    assert "'fact_extractor_run'" not in sql.split(
        "CREATE OR REPLACE FUNCTION public.v3_ingestion_scheduler_watchdog()", 1
    )[1]


def test_attempt_table_is_per_edge() -> None:
    sql = _sql()

    assert "CREATE TABLE IF NOT EXISTS public.document_asset_linker_attempts" in sql
    # alias_set_hash, not asset_set_hash (renamed for consistency)
    assert "alias_set_hash text NOT NULL" in sql
    # Per-edge attempts carry asset_id
    assert "asset_id uuid NOT NULL REFERENCES public.fda_assets(id)" in sql
    assert "status IN ('linked', 'no_match', 'error', 'skipped_prefilter')" in sql
    assert "document_asset_linker_attempts_terminal_once" in sql
    assert "WHERE status IN ('linked', 'no_match', 'skipped_prefilter')" in sql
    # Terminal uniqueness is per (doc, asset, alias_set_hash)
    assert (
        "ON public.document_asset_linker_attempts (document_id, asset_id, alias_set_hash)"
        in sql
    )


def test_skill_queue_is_edge_shaped() -> None:
    sql = _sql()

    assert "CREATE OR REPLACE VIEW public.v_asset_linker_skill_queue" in sql
    # Edge-shape selects from doc_asset_candidates, not raw documents
    assert "FROM public.doc_asset_candidates e" in sql
    # Surface the candidate id, asset id, matched aliases, and strength
    assert "e.id              AS candidate_id" in sql
    assert "e.asset_id" in sql
    assert "e.matched_aliases" in sql
    assert "e.match_strength" in sql
    # Filtered to current alias_set_hash
    assert "asset_linker_alias_set_hash" in sql
    assert "e.alias_set_hash = h.alias_set_hash" in sql
    # 24h backoff on errors, per-edge
    assert "att.status = 'error'" in sql
    assert "att.created_at > now() - interval '24 hours'" in sql
    assert "att.asset_id = e.asset_id" in sql


def test_skill_queue_prioritizes_high_signal_current_edges() -> None:
    sql = _sql()

    queue_sql = sql.split(
        "CREATE OR REPLACE VIEW public.v_asset_linker_skill_queue AS", 1
    )[1].split("COMMENT ON VIEW public.v_asset_linker_skill_queue", 1)[0]

    assert "jsonb_array_elements(e.matched_aliases)" in queue_sql
    for high_signal_kind in (
        "'drug_name'",
        "'generic'",
        "'brand'",
        "'nct_id'",
        "'code'",
        "'ticker'",
        "'abbreviation'",
    ):
        assert high_signal_kind in queue_sql
    assert "THEN 0 ELSE 1 END" in queue_sql
    assert "d.published_at > now() + interval '30 days'" in queue_sql
    assert "d.published_at DESC NULLS LAST" in queue_sql


def test_guardrails_queue_preserves_high_signal_priority() -> None:
    sql = _guardrails_sql()
    queue_sql = sql.split(
        "CREATE OR REPLACE VIEW public.v_asset_linker_skill_queue AS", 1
    )[1].split("CREATE OR REPLACE FUNCTION public._v3_pipeline_watchdog()", 1)[0]

    assert "jsonb_array_elements(e.matched_aliases)" in queue_sql
    assert "hit->>'kind' IN (" in queue_sql
    assert "'drug_name'" in queue_sql
    assert "'ticker'" in queue_sql
    assert "d.published_at > now() + interval '30 days'" in queue_sql
    # Guardrails should demote, not filter out, far-future backlog edges.
    assert "d.published_at <= now() + interval '30 days'" not in queue_sql


def test_guardrails_watchdog_flags_empty_alias_and_ticker_only_queue() -> None:
    sql = _guardrails_sql()

    assert "fda_asset_aliases_empty" in sql
    assert "FROM public.fda_asset_aliases" in sql
    assert "WHERE active = true" in sql
    assert "asset_linker_skill_queue_ticker_only" in sql
    assert "Layer-1 alias lookup or supplemental alias seeding is not active" in sql


def test_skill_assets_filter_noisy_placeholder_assets() -> None:
    sql = _sql()

    assert "CREATE OR REPLACE VIEW public.v_asset_linker_skill_assets" in sql
    for noisy in ("(auto-discovered)", "ex-99", "peptide", "concept", "nucleotide"):
        assert noisy in sql


def test_asset_linker_runs_accepts_skill_and_seed_passes() -> None:
    sql = _sql()

    assert "DROP CONSTRAINT IF EXISTS asset_linker_runs_pass_check" in sql
    assert (
        "CHECK (pass IN ('pass1','pass2','cowork_backfill','skill','seed'))"
        in sql
    )


def test_project_skill_exists_and_forbids_modal_linker() -> None:
    skill = REPO_ROOT / ".cursor" / "skills" / "asset-linker" / "SKILL.md"
    body = skill.read_text()

    assert "name: asset-linker" in body
    assert "Do not call Modal `asset_linker_run`" in body
    assert "document_asset_linker_attempts" in body
    assert "model='cursor-agent-skill'" in body


def test_project_skill_consumes_edge_queue() -> None:
    """The skill must describe the edge-shaped flow: process per-edge candidates,
    stamp doc_asset_candidates.analyzed_at, write per-edge attempts including
    asset_id, and route missed aliases through operator_flags rather than direct
    inserts into fda_asset_aliases."""
    skill_dir = REPO_ROOT / ".cursor" / "skills" / "asset-linker"
    skill = (skill_dir / "SKILL.md").read_text()
    ref = (skill_dir / "REFERENCE.md").read_text()

    # Edge-shaped queue understanding
    assert "edge-shaped" in skill.lower() or "edge-shaped" in ref.lower()
    assert "doc_asset_candidates" in ref
    assert "candidate_id" in ref
    assert "match_strength" in ref

    # Per-edge attempts (asset_id present, no candidate_asset_ids array)
    assert "alias_set_hash" in ref
    assert "candidate_asset_ids" not in ref, (
        "REFERENCE.md must NOT describe the old array-of-candidates schema"
    )

    # Stamping doc_asset_candidates is part of the contract
    assert "analyzed_at" in skill or "analyzed_at" in ref
    assert "analysis_run_id" in ref

    # Missed-alias suggestions go to operator_flags, not direct insert
    assert "operator_flags" in ref
    assert "asset_linker_skill_missed_alias" in ref


# ----------------------------------------------------------------------
# Deterministic edge-prefilter assertions
# ----------------------------------------------------------------------

def test_documents_has_tsvector_column_and_gin_index() -> None:
    sql = _sql()

    # Materialized full-text vector on documents (simple config — no
    # stemming, no stop-word stripping).
    assert "ADD COLUMN IF NOT EXISTS raw_text_tsv tsvector" in sql
    assert "GENERATED ALWAYS AS (" in sql
    assert "to_tsvector('simple'," in sql
    assert "documents_raw_text_tsv_gin_idx" in sql
    assert "USING GIN (raw_text_tsv)" in sql


def test_fda_asset_aliases_table_shape() -> None:
    sql = _sql()

    assert "CREATE TABLE IF NOT EXISTS public.fda_asset_aliases" in sql

    # Alias kinds — ticker intentionally excluded (lives on fda_assets.ticker
    # with case-sensitive matching).
    assert (
        "alias_kind       text NOT NULL CHECK (alias_kind IN ("
        in sql
    )
    for kind in (
        "'brand'",
        "'generic'",
        "'code'",
        "'nct_id'",
        "'abbreviation'",
        "'sponsor_alias'",
        "'sponsor_stem'",
        "'drug_name'",
    ):
        assert kind in sql, f"missing alias_kind: {kind}"

    # Tickers are NOT a valid kind. We assert by checking the alias_kind CHECK
    # clause does not contain 'ticker' as a list element — the surrounding
    # comment in the migration does mention "ticker" but only to explain why
    # it's absent.
    check_clause_start = sql.index(
        "alias_kind       text NOT NULL CHECK (alias_kind IN ("
    )
    check_clause_end = sql.index(")),", check_clause_start)
    check_clause = sql[check_clause_start:check_clause_end]
    assert "'ticker'" not in check_clause, (
        "alias_kind CHECK constraint must NOT include 'ticker' — "
        "tickers are matched case-sensitively from fda_assets.ticker directly."
    )

    # Source provenance
    assert (
        "source           text NOT NULL CHECK (source IN ("
        in sql
    )
    for source in (
        "'curated_map'",
        "'openfda_label'",
        "'clinicaltrials_v2'",
        "'extensions_mining'",
        "'operator'",
        "'synthetic'",
    ):
        assert source in sql, f"missing source: {source}"

    # NCT shape constraint
    assert "fda_asset_aliases_nct_shape CHECK" in sql
    assert "^nct[0-9]{8}$" in sql

    # Normalized alias guards
    assert "alias_normalized = lower(trim(alias_normalized))" in sql
    for blocked in ("'peptide'", "'concept'", "'default'", "'ex-99'", "'nucleotide'"):
        assert blocked in sql, f"missing alias_normalized blocklist entry: {blocked}"

    # Precompiled tsquery column
    assert "alias_tsquery    tsquery GENERATED ALWAYS AS" in sql
    assert "phraseto_tsquery('simple', alias_normalized)" in sql

    # Lookup index
    assert "fda_asset_aliases_lookup_idx" in sql


def test_doc_asset_candidates_table_shape() -> None:
    sql = _sql()

    assert "CREATE TABLE IF NOT EXISTS public.doc_asset_candidates" in sql
    assert "document_id     uuid NOT NULL REFERENCES public.documents(id)" in sql
    assert "asset_id        uuid NOT NULL REFERENCES public.fda_assets(id)" in sql
    assert "matched_aliases jsonb NOT NULL" in sql
    assert "match_strength  smallint NOT NULL CHECK (match_strength >= 1)" in sql
    assert "alias_set_hash  text NOT NULL" in sql
    assert "analyzed_at     timestamptz" in sql
    assert "UNIQUE (document_id, asset_id, alias_set_hash)" in sql
    assert "doc_asset_candidates_unprocessed_idx" in sql
    assert "WHERE analyzed_at IS NULL" in sql


def test_doc_asset_prefilter_runs_table_shape() -> None:
    sql = _sql()

    assert "CREATE TABLE IF NOT EXISTS public.doc_asset_prefilter_runs" in sql
    assert "PRIMARY KEY (document_id, alias_set_hash)" in sql
    assert "candidate_count integer NOT NULL DEFAULT 0 CHECK (candidate_count >= 0)" in sql


def test_alias_set_hash_function_replaces_old_hash() -> None:
    sql = _sql()

    # New hash function
    assert "CREATE OR REPLACE FUNCTION public.asset_linker_alias_set_hash()" in sql
    assert "FROM public.fda_asset_aliases a WHERE a.active = true" in sql
    # Layer-1 asset fields included so asset add/update/remove invalidates hash too
    assert "FROM public.v_asset_linker_skill_assets fa" in sql
    assert "coalesce(fa.ticker, '')" in sql
    assert "coalesce(fa.drug_name, '')" in sql
    assert "coalesce(fa.generic_name, '')" in sql
    assert "coalesce(fa.sponsor_name, '')" in sql
    # Old hash function name is gone (renamed)
    assert "asset_linker_skill_asset_set_hash" not in sql


def test_asset_alias_lookup_includes_layer1_asset_fields() -> None:
    sql = _sql()

    assert "CREATE OR REPLACE VIEW public.v_asset_alias_lookup" in sql
    assert "fa.drug_name AS alias" in sql
    assert "'drug_name'::text AS alias_kind" in sql
    assert "fa.generic_name AS alias" in sql
    assert "'generic'::text AS alias_kind" in sql
    assert "fa.sponsor_name AS alias" in sql
    assert "'sponsor_alias'::text AS alias_kind" in sql
    assert "FROM public.fda_asset_aliases a" in sql
    assert "WHERE a.active = true" in sql


def test_sweeper_function_has_three_match_paths() -> None:
    sql = _sql()

    assert (
        "CREATE OR REPLACE FUNCTION public.fn_generate_doc_asset_candidates("
        in sql
    )
    # Three named paths in the CTE chain
    assert "tsv_hits AS (" in sql
    assert "exact_hits AS (" in sql
    assert "ticker_hits AS (" in sql

    # tsv path uses tsvector @@ tsquery against Layer 1 + Layer 2 aliases,
    # excludes nct_id/code kinds
    assert "JOIN public.v_asset_alias_lookup a" in sql
    assert "td.raw_text_tsv @@ a.alias_tsquery" in sql
    assert "a.alias_kind NOT IN ('nct_id', 'code')" in sql

    # exact path uses word-boundary regex, NCT/code only
    assert "a.alias_kind IN ('nct_id', 'code')" in sql
    assert "~* ('\\m' || a.alias_normalized || '\\M')" in sql

    # ticker path is case-sensitive (~ not ~*)
    assert "~ ('\\m' || fa.ticker || '\\M')" in sql
    # And reads from fda_assets via the assets view, NOT from fda_asset_aliases
    assert "JOIN public.v_asset_linker_skill_assets fa" in sql

    # Always marks scanned docs (including zero-match) to prevent rescan
    assert "INSERT INTO public.doc_asset_prefilter_runs (" in sql
    # Cron sweeps current docs before far-future backlog rows.
    assert "d.published_at > now() + interval '30 days'" in sql

    # Returns docs_scanned, edges_emitted, alias_set_hash
    assert "'docs_scanned'" in sql
    assert "'edges_emitted'" in sql
    assert "'alias_set_hash'" in sql


def test_watchdog_protects_new_crons() -> None:
    sql = _sql()

    # Watchdog v_expected covers all three v3 ingestion crons
    assert "'v3-fact-extractor'" in sql
    assert "'v3-doc-asset-prefilter'" in sql
    assert "'v3-asset-alias-weekly-refresh'" in sql

    # Watchdog re-schedules new crons when missing
    assert (
        "IF 'v3-doc-asset-prefilter' = ANY (v_missing) THEN"
        in sql
    )
    assert (
        "IF 'v3-asset-alias-weekly-refresh' = ANY (v_missing) THEN"
        in sql
    )
    # Watchdog flag body reflects the edge-queue mode
    assert "'asset_linker_mode', 'cursor_skill_edge_queue'" in sql


def test_cron_schedules_are_set_in_migration() -> None:
    sql = _sql()

    # Migration itself schedules the crons (don't wait for watchdog tick)
    assert "cron.schedule(" in sql
    assert "'v3-doc-asset-prefilter'" in sql
    assert "'*/2 * * * *'" in sql
    assert "fn_generate_doc_asset_candidates(2000)" in sql

    assert "'v3-asset-alias-weekly-refresh'" in sql
    assert "'0 3 * * 1'" in sql
    assert "'seed_fda_asset_aliases_refresh'" in sql


def test_recent_auto_aliases_review_view() -> None:
    sql = _sql()

    assert "CREATE OR REPLACE VIEW public.v_recent_auto_aliases" in sql
    # Only auto-populated aliases (not operator manual entries)
    for src in ("'openfda_label'", "'clinicaltrials_v2'", "'extensions_mining'"):
        assert src in sql
    assert "now() - interval '14 days'" in sql
