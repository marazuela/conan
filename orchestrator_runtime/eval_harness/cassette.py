"""Deterministic record/replay cassette for OrchestratorClient.

Phase 4A enables offline prompt iteration: every Anthropic call made by
`run_one()` is captured to a per-case JSONL cassette in record-mode, then
served back from disk in replay-mode without touching the live API.

This is a structural prerequisite for the calibration loop (Phase 4C/D) —
without it, every prompt change requires fresh paid runs against the live
API to produce comparable results. With it, a single recorded run produces
a fixture set that can be replayed against any number of prompt variants
(via the `_inject_response_text` hook in replay-mode tests).

Design:
- A cassette is a directory `cassette_dir/<case_id>/anthropic.jsonl`.
- Each line is one (request, response) pair. Requests serialize to a stable
  SHA-256 hash over (model, system, messages, tools, tool_choice) so replay
  can verify alignment per call.
- Replay-mode plays back responses in recording order. The hash on the next
  recorded request must match the hash of the live caller's request, else
  the cassette raises `CassetteMismatchError` with both summaries — that's
  the trip wire that catches drift between the recording-time prompt and
  whatever the caller sends today.
- `CallResult.raw_message` is reconstructed as a duck-typed shim with
  `.content` (list of text/thinking blocks), `.stop_reason`, `.usage`. This
  is enough for Stages 1/2/3/7/9/10. Tool-use loops (sub-agents) are NOT
  supported by the MVP cassette — they're feature-flagged off in replay
  mode anyway via `ORCH_ENABLE_SUB_AGENTS=0`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from orchestrator_runtime.client import (
    BudgetExceededError,
    CallResult,
    OrchestratorClient,
    estimate_cost,
)

logger = logging.getLogger(__name__)


CASSETTE_VERSION = "v1"


class CassetteMismatchError(RuntimeError):
    """Raised when a replay-mode call's request-hash does not match the
    next recorded request. Indicates the prompt or context drifted since
    the cassette was recorded — the cassette must be re-recorded or the
    drift fixed."""

    def __init__(self, expected_hash: str, actual_hash: str,
                 expected_meta: Dict[str, Any], actual_meta: Dict[str, Any],
                 position: int):
        self.expected_hash = expected_hash
        self.actual_hash = actual_hash
        self.expected_meta = expected_meta
        self.actual_meta = actual_meta
        self.position = position
        super().__init__(
            f"cassette mismatch at call #{position}: "
            f"expected {expected_hash[:12]} ({expected_meta.get('summary')}) "
            f"got {actual_hash[:12]} ({actual_meta.get('summary')})"
        )


class CassetteExhaustedError(RuntimeError):
    """Raised when replay-mode runs out of recorded calls before the
    caller stops issuing them. Indicates the runtime under test makes more
    calls than it did at recording time."""


def _stable_dump(obj: Any) -> str:
    """JSON-serialize with sorted keys + no whitespace for deterministic
    hashing across runs."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _hash_request(
    *,
    model: str,
    system: Any,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    tool_choice: Optional[Dict[str, Any]],
) -> str:
    payload = {
        "model": model,
        "system": system,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
    }
    return hashlib.sha256(_stable_dump(payload).encode("utf-8")).hexdigest()


def _summarize_request(
    model: str, messages: List[Dict[str, Any]], system: Any,
) -> str:
    """One-line request summary for cassette mismatch diagnostics."""
    last_user = next(
        (m for m in reversed(messages) if m.get("role") == "user"),
        None,
    )
    last_text = ""
    if last_user:
        content = last_user.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    last_text = block.get("text", "")[:80]
                    break
        elif isinstance(content, str):
            last_text = content[:80]
    sys_len = len(_stable_dump(system)) if system else 0
    return (
        f"model={model.split('-')[0]} "
        f"sys={sys_len}b "
        f"msgs={len(messages)} "
        f"tail={last_text!r}"
    )


@dataclass
class _CassetteEntry:
    request_hash: str
    request_meta: Dict[str, Any]
    response_text: str
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    model: str
    latency_ms: int
    stop_reason: str = "end_turn"
    content_blocks: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_call_result(
        cls, *, request_hash: str, request_meta: Dict[str, Any],
        result: CallResult,
    ) -> "_CassetteEntry":
        blocks: List[Dict[str, Any]] = []
        msg = result.raw_message
        stop_reason = "end_turn"
        if msg is not None:
            stop_reason = getattr(msg, "stop_reason", "end_turn") or "end_turn"
            for b in getattr(msg, "content", []) or []:
                btype = getattr(b, "type", None)
                if btype == "text":
                    blocks.append({"type": "text", "text": getattr(b, "text", "")})
                elif btype == "thinking":
                    blocks.append({
                        "type": "thinking",
                        "thinking": getattr(b, "thinking", ""),
                        "signature": getattr(b, "signature", ""),
                    })
                # tool_use is NOT preserved — replay does not support tool loops
        # If raw_message is None (already replayed?), fall back to a single text block.
        if not blocks and result.text:
            blocks.append({"type": "text", "text": result.text})
        return cls(
            request_hash=request_hash,
            request_meta=request_meta,
            response_text=result.text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            thinking_tokens=result.thinking_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cache_creation_tokens=result.cache_creation_tokens,
            model=result.model,
            latency_ms=result.latency_ms,
            stop_reason=stop_reason,
            content_blocks=blocks,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": CASSETTE_VERSION,
            "request_hash": self.request_hash,
            "request_meta": self.request_meta,
            "response_text": self.response_text,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thinking_tokens": self.thinking_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "stop_reason": self.stop_reason,
            "content_blocks": self.content_blocks,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "_CassetteEntry":
        return cls(
            request_hash=d["request_hash"],
            request_meta=d.get("request_meta", {}),
            response_text=d.get("response_text", ""),
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            thinking_tokens=d.get("thinking_tokens", 0),
            cache_read_tokens=d.get("cache_read_tokens", 0),
            cache_creation_tokens=d.get("cache_creation_tokens", 0),
            model=d.get("model", ""),
            latency_ms=d.get("latency_ms", 0),
            stop_reason=d.get("stop_reason", "end_turn"),
            content_blocks=d.get("content_blocks", []),
        )


class _ReplayMessage:
    """Duck-typed stand-in for `anthropic.types.Message` constructed from a
    cassette entry. Provides .content (list of duck-typed blocks),
    .stop_reason, .usage — sufficient for the runtime's downstream readers."""

    def __init__(self, entry: _CassetteEntry):
        self.stop_reason = entry.stop_reason
        self.content = [_ReplayBlock(b) for b in entry.content_blocks]
        self.usage = _ReplayUsage(
            input_tokens=entry.input_tokens,
            output_tokens=entry.output_tokens,
            cache_read_input_tokens=entry.cache_read_tokens,
            cache_creation_input_tokens=entry.cache_creation_tokens,
        )


class _ReplayBlock:
    def __init__(self, b: Dict[str, Any]):
        self.type = b.get("type", "text")
        if self.type == "text":
            self.text = b.get("text", "")
        elif self.type == "thinking":
            self.thinking = b.get("thinking", "")
            self.signature = b.get("signature", "")
            self.tokens = 0


class _ReplayUsage:
    def __init__(self, *, input_tokens: int, output_tokens: int,
                 cache_read_input_tokens: int, cache_creation_input_tokens: int):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


class CassetteClient:
    """OrchestratorClient-shaped wrapper supporting record + replay modes.

    Drop-in replacement for `OrchestratorClient` everywhere `run_one()` and
    its helpers expect a `.call(...)` method that returns `CallResult` and
    `.attach_budget()` / `.detach_budget()` / `.get_accumulated_cost()`.

    Record mode: delegates to a real `OrchestratorClient`, captures every
    response to disk. Append-only — re-running record overwrites the file.

    Replay mode: serves responses sequentially from disk; mismatches raise.
    """

    def __init__(
        self,
        cassette_path: Path,
        *,
        mode: str,
        upstream: Optional[OrchestratorClient] = None,
    ):
        if mode not in ("record", "replay"):
            raise ValueError(f"mode must be 'record' or 'replay', got {mode!r}")
        self._mode = mode
        self._cassette_path = cassette_path
        self._upstream = upstream
        self._position = 0
        self._entries: List[_CassetteEntry] = []
        self._record_handle = None

        if mode == "replay":
            self._load_cassette()
        elif mode == "record":
            if upstream is None:
                raise ValueError("record mode requires an upstream client")
            cassette_path.parent.mkdir(parents=True, exist_ok=True)
            # Truncate on (re-)record so the cassette mirrors exactly the run
            # that produced it.
            self._record_handle = cassette_path.open("w", encoding="utf-8")

        # Budget accumulator mirrors OrchestratorClient's semantics so callers
        # that attach a budget see the same numbers in either mode.
        self._budget_run_id: Optional[str] = None
        self._budget_ceiling_usd: Optional[float] = None
        self._budget_accumulated_usd: float = 0.0

    # ----- budget surface (parity with OrchestratorClient) -----

    def attach_budget(
        self, run_id: Optional[str], hard_kill_usd: float,
    ) -> None:
        self._budget_run_id = run_id
        self._budget_ceiling_usd = float(hard_kill_usd)
        self._budget_accumulated_usd = 0.0
        if self._upstream is not None:
            self._upstream.attach_budget(run_id, hard_kill_usd)

    def detach_budget(self) -> float:
        accumulated = self._budget_accumulated_usd
        self._budget_run_id = None
        self._budget_ceiling_usd = None
        self._budget_accumulated_usd = 0.0
        if self._upstream is not None:
            self._upstream.detach_budget()
        return accumulated

    def get_accumulated_cost(self) -> float:
        return self._budget_accumulated_usd

    # ----- cassette I/O -----

    def _load_cassette(self) -> None:
        if not self._cassette_path.exists():
            raise FileNotFoundError(
                f"replay-mode cassette not found at {self._cassette_path}"
            )
        with self._cassette_path.open("r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                d = json.loads(ln)
                self._entries.append(_CassetteEntry.from_dict(d))

    def close(self) -> None:
        if self._record_handle is not None:
            self._record_handle.close()
            self._record_handle = None

    def __enter__(self) -> "CassetteClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ----- core call -----

    def call(
        self,
        *,
        system: Any,
        messages: List[Dict[str, Any]],
        model: str,
        max_tokens: int = 4096,
        thinking_effort: Optional[str] = None,
        thinking_budget_tokens: Optional[int] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Dict[str, Any]] = None,
    ) -> CallResult:
        request_hash = _hash_request(
            model=model, system=system, messages=messages,
            tools=tools, tool_choice=tool_choice,
        )
        request_meta = {
            "summary": _summarize_request(model, messages, system),
        }

        if self._mode == "record":
            assert self._upstream is not None and self._record_handle is not None
            result = self._upstream.call(
                system=system, messages=messages, model=model,
                max_tokens=max_tokens,
                thinking_effort=thinking_effort,
                thinking_budget_tokens=thinking_budget_tokens,
                extra_headers=extra_headers,
                tools=tools, tool_choice=tool_choice,
            )
            entry = _CassetteEntry.from_call_result(
                request_hash=request_hash,
                request_meta=request_meta,
                result=result,
            )
            self._record_handle.write(json.dumps(entry.to_dict(), default=str) + "\n")
            self._record_handle.flush()
            self._position += 1
            self._account_for(result.cost_usd)
            return result

        # replay
        if self._position >= len(self._entries):
            raise CassetteExhaustedError(
                f"cassette {self._cassette_path} has {len(self._entries)} "
                f"entries; runtime tried to make call #{self._position + 1}"
            )
        entry = self._entries[self._position]
        if entry.request_hash != request_hash:
            raise CassetteMismatchError(
                expected_hash=entry.request_hash,
                actual_hash=request_hash,
                expected_meta=entry.request_meta,
                actual_meta=request_meta,
                position=self._position,
            )
        self._position += 1
        cost = estimate_cost(
            entry.model,
            input_tokens=entry.input_tokens,
            output_tokens=entry.output_tokens,
            cache_creation_tokens=entry.cache_creation_tokens,
            cache_read_tokens=entry.cache_read_tokens,
        )
        self._account_for(cost)
        return CallResult(
            text=entry.response_text,
            input_tokens=entry.input_tokens,
            output_tokens=entry.output_tokens,
            thinking_tokens=entry.thinking_tokens,
            cache_read_tokens=entry.cache_read_tokens,
            cache_creation_tokens=entry.cache_creation_tokens,
            cost_usd=cost,
            latency_ms=entry.latency_ms,
            model=entry.model,
            raw_message=_ReplayMessage(entry),
        )

    def _account_for(self, cost: float) -> None:
        self._budget_accumulated_usd += float(cost or 0.0)
        if (
            self._budget_ceiling_usd is not None
            and self._budget_accumulated_usd > self._budget_ceiling_usd
        ):
            raise BudgetExceededError(
                run_id=self._budget_run_id,
                ceiling_usd=self._budget_ceiling_usd,
                accumulated_usd=self._budget_accumulated_usd,
            )

    # ----- diagnostics -----

    @property
    def position(self) -> int:
        return self._position

    @property
    def total_entries(self) -> int:
        return len(self._entries) if self._mode == "replay" else self._position
