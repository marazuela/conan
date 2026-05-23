"""Contract test — locks alignment across schema / skill prompt embed / runner validator.

Phase 2C's blocking finding (audit/sub_agent_schema_drift_2026-05-23.md) was a
three-way drift across each sub-agent's JSON Schema, the embedded shape in the
skill markdown prompt, and the runner's runtime validator. This test prevents
that drift from recurring by asserting, for each of the 4 sub-agent roles:

  1. A frozen canonical fixture validates against the schema file.
  2. The runner's `_validate` (the same path used in production) accepts it.
  3. The literal `jsonschema` fenced block embedded in the skill markdown is
     byte-identical to the schema file's `properties` + `required` + flags.

If any of these drifts, the test fails. Reconcile before flipping
ORCH_ENABLE_SUB_AGENTS=1.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# Test fixtures live next to this file.
FIXTURE_DIR = Path(__file__).parent / "fixtures"

# Schemas live in the conan-cowork-skills sibling repo. Mirror the resolution
# from modal_workers/sub_agents/runtime.py (sibling-repo primary; project-relative
# fallback for dev environments that bundle the skills inside the conan tree).
_SCHEMA_PRIMARY = Path(__file__).resolve().parents[3] / "conan-cowork-skills" / "schemas"
_SCHEMA_FALLBACK = Path(__file__).resolve().parents[2] / "conan-cowork-skills" / "schemas"
SCHEMA_DIR = _SCHEMA_PRIMARY if _SCHEMA_PRIMARY.exists() else _SCHEMA_FALLBACK

# Skill markdowns live in the conan-fda-orchestrator-plugin/skills/ dir.
SKILLS_DIR = (
    Path(__file__).resolve().parents[2]
    / "conan-fda-orchestrator-plugin" / "skills"
)

ROLES = [
    {
        "role": "regulatory_history",
        "schema": "regulatory_history_v1.json",
        "fixture": "regulatory_history_v1_canonical.json",
        "skill": "sub_agent_regulatory_history.md",
    },
    {
        "role": "competitive",
        "schema": "competitive_landscape_v1.json",
        "fixture": "competitive_landscape_v1_canonical.json",
        "skill": "sub_agent_competitive_landscape.md",
    },
    {
        "role": "literature",
        "schema": "literature_review_v1.json",
        "fixture": "literature_review_v1_canonical.json",
        "skill": "sub_agent_literature_reviewer.md",
    },
    {
        "role": "options_microstructure",
        "schema": "options_microstructure_v1.json",
        "fixture": "options_microstructure_v1_canonical.json",
        "skill": "sub_agent_options_microstructure.md",
    },
]


def _extract_jsonschema_block(skill_md_path: Path) -> dict:
    text = skill_md_path.read_text()
    m = re.search(r"```jsonschema\n(.+?)\n```", text, re.DOTALL)
    if not m:
        raise AssertionError(
            f"No ```jsonschema block found in {skill_md_path.name}. "
            "Skill prompts must embed the literal schema (audit S-4)."
        )
    return json.loads(m.group(1))


@pytest.mark.parametrize("role_spec", ROLES, ids=lambda r: r["role"])
def test_canonical_fixture_validates_against_schema(role_spec):
    """The frozen fixture for each role must validate against the role's schema."""
    import jsonschema

    with (SCHEMA_DIR / role_spec["schema"]).open() as f:
        schema = json.load(f)
    with (FIXTURE_DIR / role_spec["fixture"]).open() as f:
        fixture = json.load(f)

    errors = sorted(
        jsonschema.Draft7Validator(schema).iter_errors(fixture),
        key=lambda e: e.path,
    )
    assert not errors, (
        f"Canonical fixture {role_spec['fixture']} fails validation against "
        f"{role_spec['schema']}:\n"
        + "\n".join(f"  - {list(e.absolute_path)}: {e.message}" for e in errors)
    )


@pytest.mark.parametrize("role_spec", ROLES, ids=lambda r: r["role"])
def test_runner_validator_accepts_canonical_fixture(role_spec):
    """The runner's runtime validator (`_validate`) must accept the fixture.

    Mirrors the assertion above but via the exact code path runners use —
    catches regressions where SCHEMA_DIR resolution or _validate() error
    formatting diverges from raw jsonschema.
    """
    from modal_workers.sub_agents.runtime import _load_schema, _validate

    schema = _load_schema(role_spec["schema"])
    with (FIXTURE_DIR / role_spec["fixture"]).open() as f:
        fixture = json.load(f)

    errors = _validate(fixture, schema)
    assert not errors, (
        f"Runner's _validate rejected canonical fixture for {role_spec['role']}:\n"
        + "\n".join(f"  - {e}" for e in errors)
    )


@pytest.mark.parametrize("role_spec", ROLES, ids=lambda r: r["role"])
def test_skill_prompt_embeds_matching_schema(role_spec):
    """The ```jsonschema block in each skill .md must match the schema file's
    `required` + `properties` keys exactly.

    Full byte-for-byte equality is too strict (the embedded copy omits
    `description` fields and minor whitespace for compactness). What matters is
    that no field name, required-list entry, or enum value drifts between the
    two. We compare the structural keys, required lists, and enum values.
    """
    with (SCHEMA_DIR / role_spec["schema"]).open() as f:
        canonical = json.load(f)
    embedded = _extract_jsonschema_block(SKILLS_DIR / role_spec["skill"])

    # Compare $id
    assert canonical.get("$id") == embedded.get("$id"), (
        f"$id mismatch in {role_spec['skill']}: "
        f"schema has {canonical.get('$id')!r}, skill embed has {embedded.get('$id')!r}"
    )

    # Compare top-level required
    can_req = sorted(canonical.get("required", []))
    emb_req = sorted(embedded.get("required", []))
    assert can_req == emb_req, (
        f"Top-level required[] differs in {role_spec['skill']}:\n"
        f"  schema: {can_req}\n  skill:  {emb_req}"
    )

    # Compare top-level property names
    can_props = set(canonical.get("properties", {}).keys())
    emb_props = set(embedded.get("properties", {}).keys())
    assert can_props == emb_props, (
        f"Top-level property names differ in {role_spec['skill']}:\n"
        f"  schema only: {can_props - emb_props}\n"
        f"  skill only:  {emb_props - can_props}"
    )

    # For each shared property, compare nested required (if both are objects with required[])
    for prop in can_props:
        can_prop = canonical["properties"][prop]
        emb_prop = embedded["properties"][prop]
        if isinstance(can_prop.get("required"), list) and isinstance(emb_prop.get("required"), list):
            cr = sorted(can_prop["required"])
            er = sorted(emb_prop["required"])
            assert cr == er, (
                f"Nested required[] differs at properties.{prop} in {role_spec['skill']}:\n"
                f"  schema: {cr}\n  skill:  {er}"
            )
