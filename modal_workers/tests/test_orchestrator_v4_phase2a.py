"""v4 prompt contract tests."""
from __future__ import annotations

import os

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


def test_stage_1_prompt_has_fda_and_commercial_mandate():
    from orchestrator_runtime.runtime import STAGE_1_SYSTEM

    for term in (
        "REGULATORY",
        "COMMERCIAL",
        "Commercial opportunity",
        "TAM",
        "standard of care",
        "unmet need",
        "competitive landscape",
        "[INF]",
    ):
        assert term in STAGE_1_SYSTEM


def test_stage_1_prompt_keeps_citation_contract():
    from orchestrator_runtime.runtime import STAGE_1_SYSTEM

    assert "[F:" in STAGE_1_SYSTEM
    assert "[D:" in STAGE_1_SYSTEM
    for direction in ("long", "short", "neutral", "straddle"):
        assert direction in STAGE_1_SYSTEM


def test_stage_9_schema_includes_commercial_dimensions():
    from orchestrator_runtime.runtime import STAGE_9_SYSTEM

    assert "commercial_dimensions" in STAGE_9_SYSTEM
    for field in (
        "tam_estimate",
        "mcap_to_peak_revenue_ratio",
        "standard_of_care",
        "soc_limitations",
        "unmet_need_severity_1_5",
        "regulatory_incentives",
        "competitive_landscape_summary",
    ):
        assert field in STAGE_9_SYSTEM
