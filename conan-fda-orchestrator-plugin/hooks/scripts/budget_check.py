#!/usr/bin/env python3
"""budget_check — PreToolUse hook for sub-agent + MCP tool calls.

Reads the Claude Code hook invocation envelope from stdin (per Anthropic spec):

  {
    "session_id": "...",
    "hook_event_name": "PreToolUse",
    "tool_name": "dispatch_sub_agent",
    "tool_input": {...},
    "transcript_path": "...",
    "cwd": "..."
  }

Aggregates per-assessment token usage from sub_agent_calls (when assessment_id
is recoverable from env or session metadata) and blocks the call if the total
exceeds ORCH_SUB_AGENT_BUDGET_TOKENS. Exit codes:

  0 → allow
  2 → block (Claude Code prints stderr to the model so it backs off)

For the in-process orchestrator path, the dispatcher's own budget gate
(`reset_budget` + the per-call check) is the authoritative enforcement; this
hook is the second line of defense for Cowork sessions where the dispatcher
runs in a subprocess.
"""

from __future__ import annotations

import json
import os
import sys


DEFAULT_BUDGET = int(os.environ.get("ORCH_SUB_AGENT_BUDGET_TOKENS", "200000"))


def _running_total() -> int:
    """Best-effort lookup of running total for the current assessment.

    Reads ORCH_SUB_AGENT_RUNNING_TOKENS from env if the parent process exports
    it; falls back to 0 (no aggregate visibility). Replace with a Supabase
    SELECT sum(tokens) FROM sub_agent_calls WHERE assessment_id=? once the
    assessment id is plumbed into the hook payload.
    """
    try:
        return int(os.environ.get("ORCH_SUB_AGENT_RUNNING_TOKENS", "0"))
    except ValueError:
        return 0


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        # Hook envelope unparseable — fail-open. The in-process gate still applies.
        return 0

    tool_name = payload.get("tool_name", "")
    if not tool_name:
        return 0

    total = _running_total()
    if total >= DEFAULT_BUDGET:
        sys.stderr.write(
            f"[budget_check] sub-agent token budget exhausted: "
            f"{total}/{DEFAULT_BUDGET} tokens. Tool {tool_name} blocked.\n"
            f"Stop dispatching new sub-agents and synthesize from what you have.\n"
        )
        return 2

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
