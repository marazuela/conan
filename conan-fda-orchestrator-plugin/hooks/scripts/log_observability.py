#!/usr/bin/env python3
"""log_observability — PostToolUse hook.

Writes latency + tool-name metrics to stderr (which Claude Code surfaces in
the developer console). For Cowork-spawned sessions, also appends to the
sub_agent_calls observability blob.

Stdin payload format (PostToolUse):
  {
    "session_id": "...",
    "tool_name": "...",
    "tool_input": {...},
    "tool_output": {...},
    "tool_error": ... | null,
    "duration_ms": 1234
  }

Exit code is always 0 — observability hooks never block.
"""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    tool_name = payload.get("tool_name") or "unknown"
    duration_ms = payload.get("duration_ms")
    tool_error = payload.get("tool_error")

    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = (
        f"[obs {timestamp}] tool={tool_name} duration_ms={duration_ms} "
        f"error={'yes' if tool_error else 'no'}"
    )
    sys.stderr.write(line + "\n")

    log_path = os.environ.get("ORCH_OBSERVABILITY_LOG")
    if log_path:
        try:
            with open(log_path, "a") as fh:
                fh.write(json.dumps({
                    "ts": timestamp,
                    "tool": tool_name,
                    "duration_ms": duration_ms,
                    "error": bool(tool_error),
                }) + "\n")
        except OSError:
            pass

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
