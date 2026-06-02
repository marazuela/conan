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
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from orchestrator_runtime.client import OrchestratorClient, parse_json_or_none

logger = logging.getLogger(__name__)

SONNET_MODEL = "claude-sonnet-4-5-20250929"
# Bumped from 6 → 12 (2026-05-08) after literature sub-agent hit
# `max_turns=6 without end_turn` on a single AXS-05 dispatch. Tool-heavy
# roles (literature, regulatory_history) regularly need 4-5 search turns +
# 1-2 fetch turns + 1 synthesis turn, leaving zero headroom at 6.
DEFAULT_MAX_TURNS = 12
# Output-token cap per synthesis turn. Bumped 4096 -> 8192 (2026-06-02): roles
# with large schemas (commercial_opportunity_v1 = TAM + standard-of-care array +
# side-effects + ~2k-char competitive_landscape_summary) need >4096 to emit a
# COMPLETE JSON object; 4096 truncated commercial mid-payload (stop_reason=
# max_tokens, Round-7). max_tokens is a ceiling, not a charge — smaller-schema
# roles (competitive/regulatory ~1.5k tokens) are unaffected.
DEFAULT_MAX_OUTPUT_TOKENS = int(os.environ.get("ORCH_SUB_AGENT_MAX_OUTPUT_TOKENS", "8192"))

# Per-tool-result content cap. Sonnet's context window is 200k tokens; with
# 4 tool_uses × 60k+ chars each (PubMed full text, OpenFDA labels), a single
# turn can blow past 200k input tokens on the NEXT call. Truncating each
# tool_result to ~30k chars (~7-8k tokens) caps per-turn growth and lets
# the loop run multiple search rounds without overflowing. Truncated content
# carries an explicit marker so the model knows to drill in on a smaller slice.
MAX_TOOL_RESULT_CHARS = int(os.environ.get("ORCH_SUB_AGENT_TOOL_RESULT_CHAR_CAP", "30000"))
# Soft input-token cap for the tool-use loop. When accumulated input tokens
# exceed this, the next call is sent WITHOUT tools (forcing the model to
# synthesize from what it has). Set well below 200k to leave room for the
# system prompt + assistant turn + the final user message.
SOFT_INPUT_TOKEN_CAP = int(os.environ.get("ORCH_SUB_AGENT_SOFT_INPUT_CAP", "150000"))

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
    def __init__(
        self,
        role: str,
        errors: List[str],
        payload: Optional[Dict[str, Any]] = None,
        *,
        tokens_input: int = 0,
        tokens_output: int = 0,
        cost_usd: float = 0.0,
        latency_ms: int = 0,
        final_text: str = "",
        stop_reason: Optional[str] = None,
    ):
        super().__init__(f"sub_agent[{role}] schema validation failed: {errors[:1]}")
        self.role = role
        self.errors = errors
        self.payload = payload
        self.final_text = final_text
        self.stop_reason = stop_reason
        # Metrics from the partial-but-spent run. Without these, the dispatcher's
        # `except SubAgentSchemaError` branch logs 0-cost rows even though real
        # Anthropic tokens were burned — corrupting cost soak. See audit/
        # sub_agent_schema_drift_2026-05-23.md §S-2.
        self.tokens_input = tokens_input
        self.tokens_output = tokens_output
        self.cost_usd = cost_usd
        self.latency_ms = latency_ms


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
    final_text: str = ""
    stop_reason: Optional[str] = None


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

    def _build_cached_system(self, skill_text: str) -> List[Dict[str, Any]]:
        """Wrap the static skill markdown in a single system block with
        cache_control. With 4-12 turn tool loops, the skill (180-230 lines,
        ~1k+ tokens) is re-sent every turn. Marking it as an ephemeral cache
        breakpoint lets Anthropic serve subsequent turns from cache at ~10%
        of the input-token cost, with a one-time ~25% write premium on turn 0.
        Opt out via ORCH_SUB_AGENT_DISABLE_PROMPT_CACHE=1 if upstream caching
        misbehaves on a particular role.
        """
        schema_block = ""
        if self.schema_filename:
            try:
                schema = _load_schema(self.schema_filename)
                schema_block = (
                    "\n\n## Runtime JSON Schema Contract\n\n"
                    "Your final answer MUST validate against this exact Draft-7 "
                    "schema. Do not invent top-level keys that are not allowed "
                    "by the schema. If a provider/tool is unavailable, emit the "
                    "schema's degraded/partial-output shape instead of a "
                    "plausible narrative substitute.\n\n"
                    "```jsonschema\n"
                    f"{json.dumps(schema, indent=2, sort_keys=True)}\n"
                    "```"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "sub_agent[%s] could not embed schema %s: %s",
                    self.role, self.schema_filename, exc,
                )
        block: Dict[str, Any] = {"type": "text", "text": skill_text + schema_block}
        if os.environ.get("ORCH_SUB_AGENT_DISABLE_PROMPT_CACHE") != "1":
            block["cache_control"] = {"type": "ephemeral"}
        return [block]

    def _tools_with_cache_control(
        self, tools: Optional[List[Dict[str, Any]]]
    ) -> Optional[List[Dict[str, Any]]]:
        """Mark the LAST tool definition with cache_control so the tools
        prefix (which sits before system in Anthropic's cache key) is
        cached across turns. Returns a shallow copy with the last entry
        modified — never mutates the caller's list.
        """
        if not tools:
            return tools
        if os.environ.get("ORCH_SUB_AGENT_DISABLE_PROMPT_CACHE") == "1":
            return tools
        copied = list(tools)
        last = dict(copied[-1])
        last["cache_control"] = {"type": "ephemeral"}
        copied[-1] = last
        return copied

    def run(
        self,
        *,
        question: str,
        asset_context: Dict[str, Any],
        budget_token_cap: Optional[int] = None,
    ) -> SubAgentResult:
        skill_text = self._load_skill()
        system = self._build_cached_system(skill_text)
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
        stop_reason: Optional[str] = None
        partial = False

        for turn in range(self.max_turns):
            # Soft input-token cap: once we cross SOFT_INPUT_TOKEN_CAP, drop
            # `tools` from subsequent calls so the model is forced to synthesize
            # final JSON from what it already has. Without this, sub-agents that
            # accumulate large tool_results cross 200k on the next request and
            # crash with `prompt is too long`.
            tools_for_call = effective_tools or None
            tools_dropped = False
            # Force synthesis when EITHER the input-token cap is crossed OR we're
            # on the last allowed turn — drop tools so the model MUST emit final
            # JSON instead of calling yet another tool and exhausting max_turns
            # with nothing written (the commercial_opportunity {}/max_turns mode;
            # see sub_agent_schema_drift_2026-05-23.md Round-6).
            last_turn = turn >= self.max_turns - 1
            if total_in >= SOFT_INPUT_TOKEN_CAP or last_turn:
                tools_for_call = None
                tools_dropped = True
                if turn > 0:
                    # Nudge the model: replace last tool_result chain with a
                    # synthesis instruction. Cheaper than appending a user turn
                    # because we still send a single user message.
                    reason = (
                        "Tool budget exhausted"
                        if total_in >= SOFT_INPUT_TOKEN_CAP
                        else "Final turn"
                    )
                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "text",
                            "text": (
                                f"[synthesize] {reason}. Return ONLY the final "
                                f"JSON object matching {self.schema_filename}. Do not call "
                                "any more tools; fill unknown fields per the schema's "
                                "honest-uncertainty rules rather than omitting them."
                            ),
                        }],
                    })

            res = self._client.call(
                system=system,
                messages=messages,
                model=self.model,
                max_tokens=self.max_output_tokens,
                tools=self._tools_with_cache_control(tools_for_call),
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

            # If we dropped tools but the model still emitted tool_use blocks
            # (rare but possible if it ignored the synthesis instruction),
            # bail with whatever text content it produced.
            if tools_dropped:
                logger.warning(
                    "sub_agent[%s] emitted tool_use after context cap — bailing",
                    self.role,
                )
                final_text = res.text
                partial = True
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
                    serialized = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
                    is_error = True
                # Per-result cap: prevents a single oversized tool_result from
                # blowing past the next call's input window. Truncated payloads
                # carry an explicit marker so the model knows the slice is partial.
                if len(serialized) > MAX_TOOL_RESULT_CHARS:
                    truncated = serialized[: MAX_TOOL_RESULT_CHARS - 200]
                    serialized = (
                        truncated
                        + f'... [truncated by orchestrator: original was {len(serialized)} chars,'
                        f' kept {MAX_TOOL_RESULT_CHARS - 200}. Narrow the query to drill in.]'
                    )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": serialized,
                    "is_error": is_error,
                })

            messages.append({"role": "user", "content": tool_results})
        else:
            partial = True
            stop_reason = "max_turns"
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
            raise SubAgentSchemaError(
                self.role, errors, payload,
                tokens_input=total_in,
                tokens_output=total_out,
                cost_usd=total_cost,
                latency_ms=latency_ms,
                final_text=final_text,
                stop_reason=stop_reason,
            )

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
            final_text=final_text,
            stop_reason=stop_reason,
        )
