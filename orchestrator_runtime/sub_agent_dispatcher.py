"""sub_agent_dispatcher — routes Stage 1 tool calls to the four sub-agent runners.

Exposes one Anthropic tool definition (`dispatch_sub_agent`) and a single
handler function the Stage 1 tool-use loop calls. Internally:

  - Maps role → runner class via modal_workers.sub_agents.ROLE_REGISTRY.
  - Runs the chosen runner (synchronous; concurrency is handled at the Stage 1
    level via parallel tool-use — Anthropic emits multiple tool_use blocks in
    one assistant turn, and Stage 1's loop dispatches them in parallel).
  - Logs every dispatch to the sub_agent_calls table.
  - Enforces a per-assessment aggregate token budget across all sub-agent
    calls (default ORCH_SUB_AGENT_BUDGET_TOKENS=200000). Hooks (Stream 4.6)
    enforce the same cap from outside; this is the in-process safeguard.
  - Schema-validation failures land in failed_reactor_events with
    source='sub_agent.<role>' (per memory: failed_reactor_events_shared_dlq).

Stage 1 wires this in via:

    from orchestrator_runtime.sub_agent_dispatcher import (
        DISPATCH_TOOL_DEF, dispatch_sub_agent_tool,
    )
    tools = [..., DISPATCH_TOOL_DEF]
    # in the tool-use loop:
    if name == "dispatch_sub_agent":
        result = dispatch_sub_agent_tool(input, assessment_id=...)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Dict, List, Optional

from modal_workers.shared.supabase_client import SupabaseClient
from modal_workers.sub_agents import ROLE_REGISTRY, SubAgentResult, SubAgentSchemaError

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_TOKENS = int(os.environ.get("ORCH_SUB_AGENT_BUDGET_TOKENS", "200000"))


def _is_role_disabled(role: str) -> bool:
    """Per-role kill switch. ORCH_DISABLE_LITERATURE=1 etc. lets us turn off
    a single misbehaving runner without disabling the whole dispatch loop.

    Comparison is case-insensitive on the env-var side; role values are the
    canonical lowercase strings from DISPATCH_TOOL_DEF.input_schema.
    """
    if not role:
        return False
    flag = f"ORCH_DISABLE_{role.upper()}"
    return os.environ.get(flag) == "1"


DISPATCH_TOOL_DEF: Dict[str, Any] = {
    "name": "dispatch_sub_agent",
    "description": (
        "Spawn a parallel research sub-agent. Use to gather: 'literature' for "
        "peer-reviewed + preprint papers; 'competitive' for competitor pipeline + "
        "moat analysis; 'regulatory_history' for prior AdComms / analogous "
        "approvals / FDA-staff concerns; 'options_microstructure' for "
        "straddle-implied move + IV term + OI concentration before the catalyst "
        "date; 'commercial_opportunity' (v4 only) for TAM, standard-of-care drugs "
        "+ their side effects, unmet-need severity, and regulatory incentives. "
        "Issue parallel tool_use blocks for independent topics. Each call returns "
        "a JSON object validated against the sub-agent's _v1 schema; on schema "
        "failure the result has schema_pass=false and the error in errors[]. Do "
        "NOT retry on schema_pass=false — flag the gap in your uncertainties "
        "output instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "role": {
                "type": "string",
                "enum": [
                    "literature",
                    "competitive",
                    "regulatory_history",
                    "options_microstructure",
                    "commercial_opportunity",
                ],
            },
            "question": {
                "type": "string",
                "description": "Research question or scoped query for the sub-agent (1-3 sentences).",
            },
            "priority": {
                "type": "string",
                "enum": ["high", "normal"],
                "default": "normal",
            },
        },
        "required": ["role", "question"],
    },
}


@dataclass
class DispatchOutcome:
    """In-memory dispatch outcome wrapped for sub_agent_calls + Stage 1 consumption."""
    role: str
    schema_pass: bool
    output: Dict[str, Any]
    errors: List[str]
    tokens: int
    cost_usd: float
    latency_ms: int
    sub_agent_call_id: Optional[str] = None


_sb: Optional[SupabaseClient] = None
_running_total_tokens = 0


def _client() -> SupabaseClient:
    global _sb
    if _sb is None:
        _sb = SupabaseClient()
    return _sb


def _log_to_dlq(role: str, errors: List[str], payload: Dict[str, Any]) -> None:
    try:
        _client()._rest(
            "POST", "failed_reactor_events",
            json_body={
                "payload": {
                    "source": f"sub_agent.{role}",
                    "schema_filename": f"{role}_review_v1.json"
                    if role == "literature" else f"{role}_v1.json",
                    "raw_output": payload,
                },
                "error_message": "; ".join(errors)[:1000],
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("sub_agent_dispatcher: DLQ write failed: %s", exc)


def _log_call(
    *,
    assessment_id: Optional[str],
    orchestrator_run_id: Optional[str],
    role: str,
    question: str,
    output: Dict[str, Any],
    schema_pass: bool,
    schema_retries: int,
    tokens: int,
    cost_usd: float,
    latency_ms: int,
) -> Optional[str]:
    try:
        rows = _client()._rest(
            "POST", "sub_agent_calls",
            json_body={
                "assessment_id": assessment_id,
                "orchestrator_run_id": orchestrator_run_id,
                "role": role,
                "query": question[:8000],
                "output": output,
                "schema_pass": schema_pass,
                "schema_retries": schema_retries,
                "tokens": tokens,
                "cost_usd": round(cost_usd, 4),
                "latency_ms": latency_ms,
            },
            prefer="return=representation",
        ) or []
        if rows and isinstance(rows, list):
            return rows[0].get("id")
    except Exception as exc:  # noqa: BLE001
        logger.warning("sub_agent_dispatcher: sub_agent_calls insert failed: %s", exc)
    return None


def reset_budget() -> None:
    """Call once per assessment before the first dispatch."""
    global _running_total_tokens
    _running_total_tokens = 0


def dispatch_sub_agent(
    role: str,
    question: str,
    *,
    asset_context: Optional[Dict[str, Any]] = None,
    assessment_id: Optional[str] = None,
    orchestrator_run_id: Optional[str] = None,
    priority: str = "normal",
    budget_token_cap: Optional[int] = None,
) -> DispatchOutcome:
    """Run one sub-agent and return its outcome. Used by Stage 1's tool-use loop."""
    global _running_total_tokens

    runner_cls = ROLE_REGISTRY.get(role)
    if runner_cls is None:
        return DispatchOutcome(
            role=role,
            schema_pass=False,
            output={},
            errors=[f"unknown role: {role}"],
            tokens=0,
            cost_usd=0.0,
            latency_ms=0,
        )

    if _is_role_disabled(role):
        logger.info(
            "sub_agent_dispatcher: role=%s disabled via ORCH_DISABLE_%s — skipping",
            role, role.upper(),
        )
        return DispatchOutcome(
            role=role,
            schema_pass=False,
            output={},
            errors=[f"role_disabled: ORCH_DISABLE_{role.upper()}=1"],
            tokens=0,
            cost_usd=0.0,
            latency_ms=0,
        )

    cap = budget_token_cap or DEFAULT_BUDGET_TOKENS
    remaining = max(0, cap - _running_total_tokens)
    if remaining <= 0:
        return DispatchOutcome(
            role=role,
            schema_pass=False,
            output={},
            errors=[f"sub_agent_budget_exhausted: {_running_total_tokens}/{cap} tokens"],
            tokens=0,
            cost_usd=0.0,
            latency_ms=0,
        )

    runner = runner_cls()
    schema_pass = True
    errors: List[str] = []
    output: Dict[str, Any] = {}
    tokens = 0
    cost = 0.0
    latency = 0

    try:
        result: SubAgentResult = runner.run(
            question=question,
            asset_context=asset_context or {},
            budget_token_cap=remaining,
        )
        output = result.output
        tokens = result.tokens_input + result.tokens_output
        cost = result.cost_usd
        latency = result.latency_ms
    except SubAgentSchemaError as exc:
        schema_pass = False
        errors = exc.errors
        output = exc.payload or {}
        # Capture partial-run metrics so sub_agent_calls records real burn (not 0).
        # See audit/sub_agent_schema_drift_2026-05-23.md §S-2.
        tokens = (exc.tokens_input or 0) + (exc.tokens_output or 0)
        cost = exc.cost_usd or 0.0
        latency = exc.latency_ms or 0
        _log_to_dlq(role, errors, output)
    except Exception as exc:  # noqa: BLE001
        schema_pass = False
        errors = [f"{type(exc).__name__}: {exc}"]
        logger.exception("sub_agent_dispatcher[%s] runtime error", role)

    _running_total_tokens += tokens
    call_id = _log_call(
        assessment_id=assessment_id,
        orchestrator_run_id=orchestrator_run_id,
        role=role,
        question=question,
        output=output,
        schema_pass=schema_pass,
        schema_retries=0,
        tokens=tokens,
        cost_usd=cost,
        latency_ms=latency,
    )

    return DispatchOutcome(
        role=role,
        schema_pass=schema_pass,
        output=output,
        errors=errors,
        tokens=tokens,
        cost_usd=cost,
        latency_ms=latency,
        sub_agent_call_id=call_id,
    )


def dispatch_sub_agent_tool(
    tool_input: Dict[str, Any],
    *,
    asset_context: Optional[Dict[str, Any]] = None,
    assessment_id: Optional[str] = None,
    orchestrator_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Stage 1 tool-handler. Called when Claude emits a `dispatch_sub_agent` tool_use block."""
    role = tool_input.get("role", "")
    question = tool_input.get("question", "")
    priority = tool_input.get("priority", "normal")
    outcome = dispatch_sub_agent(
        role,
        question,
        asset_context=asset_context,
        assessment_id=assessment_id,
        orchestrator_run_id=orchestrator_run_id,
        priority=priority,
    )
    # Trim large outputs returned to the model — schema-validated payload + status
    return {
        "role": outcome.role,
        "schema_pass": outcome.schema_pass,
        "errors": outcome.errors,
        "output": outcome.output,
        "metadata": {
            "tokens": outcome.tokens,
            "cost_usd": outcome.cost_usd,
            "latency_ms": outcome.latency_ms,
            "sub_agent_call_id": outcome.sub_agent_call_id,
        },
    }


async def dispatch_parallel(
    requests: List[Dict[str, Any]],
    *,
    asset_context: Optional[Dict[str, Any]] = None,
    assessment_id: Optional[str] = None,
    orchestrator_run_id: Optional[str] = None,
) -> List[DispatchOutcome]:
    """Fire multiple dispatch_sub_agent calls in parallel via asyncio.

    Each request is {role, question, priority?}. Anthropic's parallel tool-use
    feature already emits multiple tool_use blocks in one assistant turn; this
    helper is for callers that want to dispatch a batch directly (eg. the
    eval_harness runner).
    """
    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(
            None,
            lambda r=req: dispatch_sub_agent(
                r["role"], r["question"],
                asset_context=asset_context,
                assessment_id=assessment_id,
                orchestrator_run_id=orchestrator_run_id,
                priority=r.get("priority", "normal"),
            ),
        )
        for req in requests
    ]
    return await asyncio.gather(*tasks)


def backfill_assessment_id(
    *,
    orchestrator_run_id: str,
    assessment_id: str,
) -> int:
    """Back-fill sub_agent_calls.assessment_id for all rows from this run.

    Called by runtime after stage_10_persist returns the parent assessment_id.
    Sub-agent calls fire in Stage 1 before the parent assessment exists, so
    assessment_id is NULL at INSERT time. orchestrator_run_id (set at INSERT)
    is the join key.

    Returns the count of rows updated. Best-effort: logs and returns 0 on error.
    No-op if either id is empty — guards against a malformed orchestrator_run_id
    being interpreted as "match every row" by PostgREST.
    """
    if not orchestrator_run_id or not assessment_id:
        return 0
    try:
        rows = _client()._rest(
            "PATCH", "sub_agent_calls",
            params={
                "orchestrator_run_id": f"eq.{orchestrator_run_id}",
                "assessment_id": "is.null",
            },
            json_body={"assessment_id": assessment_id},
            prefer="return=representation",
        ) or []
        n = len(rows) if isinstance(rows, list) else 0
        if n:
            logger.info(
                "sub_agent_dispatcher: back-filled assessment_id=%s on %d sub_agent_calls "
                "rows for orchestrator_run_id=%s",
                assessment_id, n, orchestrator_run_id,
            )
        return n
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "sub_agent_dispatcher: assessment_id back-fill failed (run=%s, assessment=%s): %s",
            orchestrator_run_id, assessment_id, exc,
        )
        return 0
