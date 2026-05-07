"""SubAgentRunner — base class for the four v3 sub-agents.

Each runner:
  1. Builds a Sonnet prompt from its skill markdown + the question + asset context.
  2. Runs an Anthropic tool-use loop, routing tool_use blocks to in-process
     handlers that wrap the same functions exposed by the plugin's MCP servers.
  3. Validates the final assistant text (parsed as JSON) against its
     `_v1.json` schema using jsonschema. Hard fail → SubAgentSchemaError; the
     dispatcher logs the error to failed_reactor_events with
     source='sub_agent.<role>' and surfaces a typed result.
  4. Returns a SubAgentResult with the validated JSON payload + observability
     metadata (tokens, cost, latency, schema_pass).

The in-process handler choice (vs subprocess MCP) is the same compromise made
by compute_mcp's runtime path: avoid subprocess overhead inside the orchestrator
while keeping the MCP servers as the operator/Cowork surface.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from orchestrator_runtime.client import OrchestratorClient, parse_json_or_none

logger = logging.getLogger(__name__)

SONNET_MODEL = "claude-sonnet-4-5-20250929"
DEFAULT_MAX_TURNS = 6
DEFAULT_MAX_OUTPUT_TOKENS = 4096

SCHEMA_DIR = (
    Path(__file__).resolve().parents[3]
    / "conan-cowork-skills" / "schemas"
)
# Fallback to the project-relative path used in dev if the sibling repo isn't found.
if not SCHEMA_DIR.exists():
    _alt = Path(__file__).resolve().parents[2] / "conan-cowork-skills" / "schemas"
    if _alt.exists():
        SCHEMA_DIR = _alt


class SubAgentSchemaError(RuntimeError):
    def __init__(self, role: str, errors: List[str], payload: Optional[Dict[str, Any]] = None):
        super().__init__(f"sub_agent[{role}] schema validation failed: {errors[:1]}")
        self.role = role
        self.errors = errors
        self.payload = payload


@dataclass
class SubAgentResult:
    role: str
    schema_pass: bool
    schema_retries: int
    output: Dict[str, Any]
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    tool_call_log: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# Tool handler signature: (tool_name, tool_input) -> dict
ToolHandler = Callable[[str, Dict[str, Any]], Dict[str, Any]]


def _block_to_dict(block: Any) -> Dict[str, Any]:
    """Anthropic SDK content block → dict for re-passing in messages array."""
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": block.text}
    if btype == "thinking":
        return {
            "type": "thinking",
            "thinking": getattr(block, "thinking", ""),
            "signature": getattr(block, "signature", ""),
        }
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    # Fallback: best-effort dict conversion
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return dict(block) if isinstance(block, dict) else {"type": btype or "unknown"}


def _load_schema(schema_filename: str) -> Dict[str, Any]:
    path = SCHEMA_DIR / schema_filename
    if not path.exists():
        raise FileNotFoundError(
            f"Schema file not found: {path}. Expected at conan-cowork-skills/schemas/."
        )
    with path.open("r") as f:
        return json.load(f)


def _validate(payload: Dict[str, Any], schema: Dict[str, Any]) -> List[str]:
    """Validate payload; return list of error strings (empty = pass)."""
    try:
        import jsonschema
    except ImportError:
        logger.warning("jsonschema not installed — schema validation skipped (treated as pass)")
        return []
    validator = jsonschema.Draft7Validator(schema)
    return [
        f"{list(e.absolute_path)}: {e.message}"
        for e in sorted(validator.iter_errors(payload), key=lambda e: e.path)
    ]


# ROLE_REGISTRY populated by sub_agents/__init__.py at import time.
ROLE_REGISTRY: Dict[str, "SubAgentRunner"] = {}


class SubAgentRunner:
    """Base class. Subclasses set role, skill_path, schema_filename, tool_defs,
    and implement build_handler().

    Opt-in shared tools: setting `internal_rag_default_corpus` (one of
    literature/filings/labels_aes/news/all) injects the two `internal_rag_*`
    tools into `effective_tool_defs()` and chains the corresponding handler
    via `_rag_tools.chain_handlers`. Setting `compute_tools_enabled=True`
    additionally injects `compute_similar_resolved_cases`. Subclasses still
    define their role-specific tools and handler — the merge is additive."""

    role: str = ""
    skill_path: Optional[Path] = None
    schema_filename: str = ""
    tool_defs: List[Dict[str, Any]] = []
    model: str = SONNET_MODEL
    max_turns: int = DEFAULT_MAX_TURNS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS

    # Opt-in shared tools.
    internal_rag_default_corpus: Optional[str] = None
    compute_tools_enabled: bool = False

    def __init__(self, client: Optional[OrchestratorClient] = None):
        self._client = client or OrchestratorClient()

    def effective_tool_defs(self) -> List[Dict[str, Any]]:
        """Role tool_defs + opt-in shared tool defs."""
        defs: List[Dict[str, Any]] = list(self.tool_defs or [])
        if self.internal_rag_default_corpus:
            from modal_workers.sub_agents._rag_tools import (
                internal_rag_tool_defs,
            )
            defs.extend(internal_rag_tool_defs(self.internal_rag_default_corpus))
        if self.compute_tools_enabled:
            from modal_workers.sub_agents._rag_tools import compute_tool_defs
            defs.extend(compute_tool_defs())
        return defs

    def _wrap_handler(self, role_handler: ToolHandler) -> ToolHandler:
        """Chain role handler with opt-in shared handlers."""
        if not (self.internal_rag_default_corpus or self.compute_tools_enabled):
            return role_handler
        from modal_workers.sub_agents._rag_tools import (
            chain_handlers, make_compute_handler, make_internal_rag_handler,
        )
        chain: List[ToolHandler] = [role_handler]
        if self.internal_rag_default_corpus:
            chain.append(make_internal_rag_handler())
        if self.compute_tools_enabled:
            chain.append(make_compute_handler())
        return chain_handlers(*chain)

    # ---------- abstract-ish hooks ----------

    def build_handler(self) -> ToolHandler:
        """Return a callable that routes tool_name+input → result dict.

        Subclasses override to wire in-process MCP-equivalent functions.
        """
        raise NotImplementedError

    def build_user_content(self, question: str, asset_context: Dict[str, Any]) -> str:
        """Default user prompt: question + asset_context json. Subclasses can override."""
        return (
            f"Asset context:\n```json\n{json.dumps(asset_context, indent=2, default=str)}\n```\n\n"
            f"Question:\n{question}\n\n"
            f"Use the available tools to gather evidence, then return ONLY a JSON "
            f"object matching the {self.schema_filename} schema. No prose outside the JSON."
        )

    # ---------- core loop ----------

    def _load_skill(self) -> str:
        if self.skill_path and self.skill_path.exists():
            return self.skill_path.read_text()
        # Fallback minimal system prompt — keeps the runner functional even
        # if the skill markdown isn't present in this checkout.
        return (
            f"You are the v3 {self.role} sub-agent. Use the provided tools to "
            f"gather evidence and return strict JSON matching {self.schema_filename}."
        )

    def run(
        self,
        *,
        question: str,
        asset_context: Dict[str, Any],
        budget_token_cap: Optional[int] = None,
    ) -> SubAgentResult:
        system = self._load_skill()
        user_text = self.build_user_content(question, asset_context)
        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": [{"type": "text", "text": user_text}]}
        ]

        handler = self._wrap_handler(self.build_handler())
        effective_tools = self.effective_tool_defs()
        tool_log: List[Dict[str, Any]] = []
        total_in = total_out = 0
        total_cost = 0.0
        t0 = time.time()
        final_text = ""
        partial = False

        for turn in range(self.max_turns):
            res = self._client.call(
                system=system,
                messages=messages,
                model=self.model,
                max_tokens=self.max_output_tokens,
                tools=effective_tools or None,
            )
            total_in += res.input_tokens
            total_out += res.output_tokens
            total_cost += res.cost_usd
            if budget_token_cap and (total_in + total_out) > budget_token_cap:
                partial = True
                logger.warning(
                    "sub_agent[%s] exceeded budget=%d (in=%d out=%d) — stopping",
                    self.role, budget_token_cap, total_in, total_out,
                )
                final_text = res.text
                break

            msg = res.raw_message
            if msg is None:
                final_text = res.text
                break

            # Append assistant turn
            messages.append({
                "role": "assistant",
                "content": [_block_to_dict(b) for b in msg.content],
            })

            stop_reason = getattr(msg, "stop_reason", "end_turn")
            if stop_reason != "tool_use":
                final_text = res.text
                break

            # Route tool calls
            tool_results: List[Dict[str, Any]] = []
            for block in msg.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                tool_log.append({
                    "name": block.name,
                    "input": block.input,
                    "turn": turn,
                })
                try:
                    out = handler(block.name, dict(block.input or {}))
                    serialized = json.dumps(out, default=str)
                    is_error = False
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "sub_agent[%s] tool=%s raised %s",
                        self.role, block.name, exc,
                    )
                    serialized = json.dumps({"error": str(exc)})
                    is_error = True
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": serialized,
                    "is_error": is_error,
                })

            messages.append({"role": "user", "content": tool_results})
        else:
            partial = True
            logger.warning(
                "sub_agent[%s] hit max_turns=%d without end_turn",
                self.role, self.max_turns,
            )

        latency_ms = int((time.time() - t0) * 1000)
        payload = parse_json_or_none(final_text) or {}
        if partial and isinstance(payload, dict):
            payload["partial_output"] = True

        # Validate
        schema = _load_schema(self.schema_filename)
        errors = _validate(payload, schema)
        schema_pass = not errors

        if not schema_pass:
            raise SubAgentSchemaError(self.role, errors, payload)

        return SubAgentResult(
            role=self.role,
            schema_pass=True,
            schema_retries=0,
            output=payload,
            tokens_input=total_in,
            tokens_output=total_out,
            cost_usd=total_cost,
            latency_ms=latency_ms,
            tool_call_log=tool_log,
        )
