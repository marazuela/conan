"""Static checks for the asset-linker edge-prefilter recall fix migration.

The recall fix lives in 20260601000020_asset_linker_recall_fix.sql, which
re-applies five DDL objects on top of the original cutover so the Layer-1
alias path, hash invalidation, and watchdog probes actually deploy on
environments that already applied the earlier migrations.

Run: python -m pytest modal_workers/tests/test_asset_linker_recall_fix_migration.py -v
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260601000020_asset_linker_recall_fix.sql"
)


def _sql() -> str:
    return MIGRATION.read_text()


def test_v_asset_alias_lookup_unions_layer1_and_layer2() -> None:
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


def test_alias_set_hash_folds_in_layer1_fields() -> None:
    sql = _sql()

    assert "CREATE OR REPLACE FUNCTION public.asset_linker_alias_set_hash()" in sql
    assert "FROM public.fda_asset_aliases a WHERE a.active = true" in sql
    assert "FROM public.v_asset_linker_skill_assets fa" in sql
    assert "coalesce(fa.ticker, '')" in sql
    assert "coalesce(fa.drug_name, '')" in sql
    assert "coalesce(fa.generic_name, '')" in sql
    assert "coalesce(fa.sponsor_name, '')" in sql


def test_sweeper_joins_combined_alias_lookup() -> None:
    sql = _sql()

    assert "CREATE OR REPLACE FUNCTION public.fn_generate_doc_asset_candidates" in sql
    assert "JOIN public.v_asset_alias_lookup a" in sql
    assert "a.alias_kind NOT IN ('nct_id', 'code')" in sql
    # NCT IDs / codes still come straight from fda_asset_aliases
    assert "JOIN public.fda_asset_aliases a" in sql
    assert "a.alias_kind IN ('nct_id', 'code')" in sql
    # Current docs drained before far-future backlog
    assert "d.published_at > now() + interval '30 days'" in sql


def test_skill_queue_demotes_rather_than_excludes_far_future() -> None:
    sql = _sql()

    queue_sql = sql.split(
        "CREATE OR REPLACE VIEW public.v_asset_linker_skill_queue AS", 1
    )[1].split(
        "CREATE OR REPLACE FUNCTION public._v3_pipeline_watchdog()", 1
    )[0]

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
    # Demote in ORDER BY, do not filter out in WHERE.
    assert "d.published_at <= now() + interval '30 days'" not in queue_sql


def test_watchdog_adds_layer1_recall_probes() -> None:
    sql = _sql()

    assert "fda_asset_aliases_empty" in sql
    assert "Supplemental FDA asset alias table is empty" in sql

    assert "asset_linker_skill_queue_ticker_only" in sql
    assert "Local asset-linker queue is ticker-only" in sql
    assert "Layer-1 alias lookup or supplemental alias seeding is not active" in sql
