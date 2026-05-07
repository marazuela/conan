"""
JSON Schema validator for FDA specialist agent outputs.

Skills (medical / regulatory / microstructure) shell out to this module before
writing to fda_agent_reviews. The skill emits a JSON payload, calls
validate(agent_kind, payload), and either:
  - on valid -> writes the agent review row + an evidence row tagged
    source='agent_<kind>'
  - on invalid -> writes failed_reactor_events with payload->>'source'='fda_agent_review'
    AND emits an operator_flags row with kind='schema_validation_failed'

Schema discovery: looks for $CONAN_COWORK_SKILLS env var first, then walks up
from CONAN_ROOT to find a sibling `conan-cowork-skills/schemas/` directory.

This module is pure (no DB I/O) — the skill writes results back to Supabase
via MCP. Tests cover schema validation only; the DB writes live in the skill
prompts and are exercised by integration tests.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import jsonschema
    from jsonschema import Draft7Validator
except ImportError as exc:  # pragma: no cover — import guard for callers
    raise ImportError(
        "jsonschema is required. Install with: pip install 'jsonschema>=4.20,<5'"
    ) from exc

logger = logging.getLogger(__name__)

VALID_AGENT_KINDS = ("medical", "regulatory", "microstructure")


@dataclass
class ValidationResult:
    """Outcome of validating an agent payload against its schema."""
    valid: bool
    errors: List[str] = field(default_factory=list)
    normalized_payload: Optional[Dict[str, Any]] = None
    agent_kind: Optional[str] = None
    schema_id: Optional[str] = None


class SchemaNotFoundError(RuntimeError):
    pass


class UnknownAgentKindError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Schema discovery
# ---------------------------------------------------------------------------


def _candidate_schema_dirs() -> List[Path]:
    """Build the search path for schemas/ in priority order."""
    paths: List[Path] = []

    explicit = os.environ.get("CONAN_COWORK_SKILLS")
    if explicit:
        paths.append(Path(explicit) / "schemas")

    conan_root = os.environ.get("CONAN_ROOT")
    if conan_root:
        # Sibling: /path/to/Conan + /path/to/conan-cowork-skills
        sibling = Path(conan_root).resolve().parent / "conan-cowork-skills" / "schemas"
        paths.append(sibling)

    # Resolve relative to this module: modal_workers/shared/fda_agent_validator.py ->
    # repo root is two levels up; the cowork-skills repo is its sibling.
    here = Path(__file__).resolve()
    repo_root = here.parent.parent.parent
    paths.append(repo_root.parent / "conan-cowork-skills" / "schemas")

    # Last resort: in-repo schemas/ directory (useful in CI sandboxes that
    # don't have the cowork-skills repo cloned).
    paths.append(repo_root / "schemas")

    # De-dupe while preserving order.
    seen: set = set()
    uniq: List[Path] = []
    for p in paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def _resolve_schema_path(agent_kind: str, search_paths: Optional[List[Path]] = None) -> Path:
    if agent_kind not in VALID_AGENT_KINDS:
        raise UnknownAgentKindError(
            f"agent_kind={agent_kind!r}, expected one of {VALID_AGENT_KINDS}"
        )
    candidates = search_paths or _candidate_schema_dirs()
    filename = f"fda_agent_{agent_kind}.json"
    for d in candidates:
        path = d / filename
        if path.is_file():
            return path
    searched = ", ".join(str(d) for d in candidates)
    raise SchemaNotFoundError(
        f"could not locate {filename}. Searched: {searched}. "
        f"Set $CONAN_COWORK_SKILLS or $CONAN_ROOT, or place schemas next to the repo."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_SCHEMA_CACHE: Dict[str, Tuple[Path, Dict[str, Any]]] = {}


def load_schema(agent_kind: str, *, search_paths: Optional[List[Path]] = None) -> Dict[str, Any]:
    """Load and parse the JSON Schema for the given agent_kind."""
    if agent_kind in _SCHEMA_CACHE and search_paths is None:
        return _SCHEMA_CACHE[agent_kind][1]
    path = _resolve_schema_path(agent_kind, search_paths=search_paths)
    with path.open("r") as fh:
        schema = json.load(fh)
    if search_paths is None:
        _SCHEMA_CACHE[agent_kind] = (path, schema)
    return schema


def clear_schema_cache() -> None:
    """Test helper — drop the cached schemas so a different search path takes effect."""
    _SCHEMA_CACHE.clear()


def validate(
    agent_kind: str,
    payload: Any,
    *,
    search_paths: Optional[List[Path]] = None,
) -> ValidationResult:
    """Validate `payload` against the schema for `agent_kind`.

    Returns a ValidationResult. Never raises on a valid/invalid payload — only
    raises if the agent_kind is unknown or if the schema cannot be found at all
    (those are programmer/configuration errors, not data errors).
    """
    if agent_kind not in VALID_AGENT_KINDS:
        raise UnknownAgentKindError(
            f"agent_kind={agent_kind!r}, expected one of {VALID_AGENT_KINDS}"
        )
    schema = load_schema(agent_kind, search_paths=search_paths)
    schema_id = schema.get("$id")

    if not isinstance(payload, dict):
        return ValidationResult(
            valid=False,
            errors=[f"payload must be a JSON object, got {type(payload).__name__}"],
            agent_kind=agent_kind,
            schema_id=schema_id,
        )

    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    if errors:
        return ValidationResult(
            valid=False,
            errors=[_format_error(e) for e in errors],
            normalized_payload=None,
            agent_kind=agent_kind,
            schema_id=schema_id,
        )

    return ValidationResult(
        valid=True,
        errors=[],
        normalized_payload=_normalize(payload),
        agent_kind=agent_kind,
        schema_id=schema_id,
    )


def _format_error(err: jsonschema.ValidationError) -> str:
    path = "/" + "/".join(str(p) for p in err.absolute_path)
    return f"{path}: {err.message}"


def _normalize(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Light normalization: ensure stable key order via dict copy. Schemas
    already enforce types and bounds, so this is just a structural pass.
    """
    return {k: payload[k] for k in sorted(payload.keys())}
