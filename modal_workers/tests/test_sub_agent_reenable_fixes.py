"""Tests for the 2026-06-02 sub-agent re-enable fixes.

Covers:
  - commercial_opportunity label projection (token-toxicity fix)
  - literature/commercial skill <-> runner tool-name consistency (the drift
    that let the frontmatter point at tools the runner never exposes)

Run: python -m pytest modal_workers/tests/test_sub_agent_reenable_fixes.py -v
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from modal_workers.sub_agents.commercial_opportunity import (
    CommercialOpportunityRunner,
    _project_label,
)
from modal_workers.sub_agents.literature import LiteratureRunner


# ---------- commercial label projection ----------


def test_project_label_shrinks_and_keeps_relevant_sections():
    """A full openFDA label (50-100 KB) gets reduced to brand/generic +
    requested sections, each truncated — so it can't blow the input cap."""
    huge = "X" * 50_000
    result = {
        "openfda": {
            "brand_name": ["BrandA"],
            "generic_name": ["generica"],
            "manufacturer_name": ["AcmeCo"],
            "route": ["ORAL"],  # noise — must be dropped
        },
        "adverse_reactions": [huge],
        "indications_and_usage": ["treats condition Z"],
        "spl_unclassified_section": ["noise " * 2000],  # noise — must be dropped
    }
    proj = _project_label(
        result,
        sections=("adverse_reactions", "indications_and_usage"),
        max_section_chars=1500,
    )
    assert proj["brand_name"] == "BrandA"
    assert proj["generic_name"] == "generica"
    assert proj["manufacturer_name"] == "AcmeCo"
    # adverse_reactions truncated near the cap, not the full 50k
    assert len(proj["adverse_reactions"]) < 1600
    assert proj["adverse_reactions"].endswith("...[truncated]")
    # short section passes through whole
    assert proj["indications_and_usage"] == "treats condition Z"
    # irrelevant raw fields are dropped entirely
    assert "spl_unclassified_section" not in proj
    assert "route" not in proj


def test_project_label_handles_missing_fields():
    proj = _project_label({}, sections=("adverse_reactions",))
    assert proj["brand_name"] is None
    assert "adverse_reactions" not in proj  # absent section simply omitted


# ---------- skill <-> runner tool-name consistency ----------


def _allowed_tools(skill_path: Path) -> list[str]:
    """Parse the `allowed-tools:` YAML list without a yaml dependency."""
    names: list[str] = []
    capturing = False
    for ln in skill_path.read_text().splitlines():
        if ln.strip() == "allowed-tools:":
            capturing = True
            continue
        if capturing:
            m = re.match(r"\s*-\s+(\S+)", ln)
            if m:
                names.append(m.group(1))
            elif ln and not ln[0].isspace() and not ln.lstrip().startswith("#"):
                break  # reached the next top-level frontmatter key
    return names


@pytest.mark.parametrize("runner_cls", [LiteratureRunner, CommercialOpportunityRunner])
def test_skill_allowed_tools_match_runner(runner_cls):
    """Every tool named in the skill frontmatter must actually be exposed by the
    runner. This is the exact drift that silently broke literature + commercial:
    the skill told the model to call tools that were never wired in."""
    runner = runner_cls(client=object())  # effective_tool_defs() ignores the client
    real = {t["name"] for t in runner.effective_tool_defs()}
    declared = _allowed_tools(runner_cls.skill_path)
    assert declared, f"{runner_cls.__name__} skill declares no allowed-tools"
    missing = set(declared) - real
    assert not missing, (
        f"{runner_cls.__name__} skill names tools the runner does not expose: "
        f"{sorted(missing)} (real tools: {sorted(real)})"
    )
    phantom = [t for t in declared if t.startswith("mcp__")]
    assert not phantom, f"{runner_cls.__name__} skill still has MCP-style phantom names: {phantom}"
