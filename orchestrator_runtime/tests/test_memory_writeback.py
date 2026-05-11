"""Tests for Phase 3B — D-123 Contract C5 append-only `## Recent assessments`
section in Stage 10's asset memory writeback.

Run: python -m pytest orchestrator_runtime/tests/test_memory_writeback.py -v
"""
from __future__ import annotations

import os
from typing import Any, Dict

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


def _asset() -> Dict[str, Any]:
    return {
        "id": "asset-uuid-1",
        "ticker": "AXSM",
        "drug_name": "AXS-05",
        "indication": "MDD",
        "reference_class_signature": "psych_NDA",
    }


def _parsed() -> Dict[str, Any]:
    return {
        "reasoning_summary": "Approval likely on safety + efficacy.",
        "uncertainties": [
            {"question": "Adcomm convened?"},
            {"question": "CMC clean?"},
        ],
    }


def test_recent_assessments_section_present_in_first_run():
    from orchestrator_runtime.runtime import _build_asset_memory_summary

    out = _build_asset_memory_summary(
        asset=_asset(), parsed=_parsed(), cited_prose="prose",
        conviction_calibrated=72.0, band="immediate", direction="long",
        assessment_id="aaaaaaaaaaaa-1",
    )
    assert "## Recent assessments" in out
    assert "id=aaaaaaaa" in out
    assert "band=immediate" in out
    assert "dir=long" in out


def test_recent_assessments_appends_newest_first():
    from orchestrator_runtime.runtime import _build_asset_memory_summary

    prior = (
        "# x\n\n## Recent assessments\n\n"
        "- 2026-05-06 18:00:00Z · id=11111111 · band=watchlist · dir=neutral · conv=42.0\n"
    )
    out = _build_asset_memory_summary(
        asset=_asset(), parsed=_parsed(), cited_prose="prose",
        conviction_calibrated=72.0, band="immediate", direction="long",
        assessment_id="22222222-2222-2222-2222-222222222222",
        prior_text=prior,
    )
    # Find the ## Recent assessments section
    section = out.split("## Recent assessments", 1)[1]
    lines = [ln for ln in section.splitlines() if ln.strip().startswith("- ")]
    # newest-first: id=22222222 before id=11111111
    assert "id=22222222" in lines[0]
    assert "id=11111111" in lines[1]


def test_recent_assessments_idempotent_on_assessment_id():
    """Re-running the same assessment should not duplicate the entry."""
    from orchestrator_runtime.runtime import _build_asset_memory_summary

    aid = "33333333-3333-3333-3333-333333333333"
    prior = (
        "# x\n\n## Recent assessments\n\n"
        f"- 2026-05-07 12:00:00Z · id=33333333 · band=immediate · dir=long · conv=70.0\n"
    )
    out = _build_asset_memory_summary(
        asset=_asset(), parsed=_parsed(), cited_prose="prose",
        conviction_calibrated=72.0, band="immediate", direction="long",
        assessment_id=aid, prior_text=prior,
    )
    section = out.split("## Recent assessments", 1)[1]
    occurrences = section.count("id=33333333")
    assert occurrences == 1


def test_recent_assessments_capped_at_5():
    """Old entries beyond RECENT_ASSESSMENTS_CAP get dropped."""
    from orchestrator_runtime.runtime import (
        RECENT_ASSESSMENTS_CAP,
        _build_asset_memory_summary,
    )

    prior_lines = "\n".join(
        f"- 2026-05-{i:02d} 12:00:00Z · id={i:08d} · band=watchlist · dir=neutral · conv={i:.1f}"
        for i in range(1, RECENT_ASSESSMENTS_CAP + 3)  # cap+2 prior entries
    )
    prior = f"# x\n\n## Recent assessments\n\n{prior_lines}\n"

    out = _build_asset_memory_summary(
        asset=_asset(), parsed=_parsed(), cited_prose="p",
        conviction_calibrated=72.0, band="immediate", direction="long",
        assessment_id="newuuuu-1234-5678-9abc-def012345678",
        prior_text=prior,
    )
    section = out.split("## Recent assessments", 1)[1]
    bullet_count = sum(
        1 for ln in section.splitlines() if ln.strip().startswith("- ")
    )
    assert bullet_count == RECENT_ASSESSMENTS_CAP


def test_parse_recent_assessments_returns_empty_for_no_section():
    from orchestrator_runtime.runtime import _parse_recent_assessments

    assert _parse_recent_assessments("") == []
    assert _parse_recent_assessments("# x\n\n## Other\n\n- a\n") == []


def test_parse_recent_assessments_stops_at_next_section():
    """Bullets after the section are not picked up."""
    from orchestrator_runtime.runtime import _parse_recent_assessments

    text = (
        "# x\n\n## Recent assessments\n\n"
        "- entry-1\n- entry-2\n\n"
        "## Resolved post-mortems\n\n- post-1\n- post-2\n"
    )
    out = _parse_recent_assessments(text)
    assert out == ["entry-1", "entry-2"]


def test_recent_assessments_handles_no_prior_text():
    """Cold-start — first assessment, prior_text='' — should still produce a
    section with one entry."""
    from orchestrator_runtime.runtime import _build_asset_memory_summary

    out = _build_asset_memory_summary(
        asset=_asset(), parsed=_parsed(), cited_prose="p",
        conviction_calibrated=50.0, band="watchlist", direction="neutral",
        assessment_id="aaaa1111-2222-3333-4444-555566667777",
        prior_text="",
    )
    section = out.split("## Recent assessments", 1)[1]
    bullets = [
        ln for ln in section.splitlines() if ln.strip().startswith("- ")
    ]
    assert len(bullets) == 1
    assert "aaaa1111" in bullets[0]
