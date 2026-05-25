"""Stage 7 (constitutional check) MUST route through OrchestratorClient.call
so it inherits budget accounting, transient-error retry, and cache-aware
cost accounting. Earlier this stage went straight to the raw SDK, which
silently dropped cache tokens and could push a run over the per-run hard
kill without triggering BudgetExceededError.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from orchestrator_runtime.client import CallResult  # noqa: E402
from orchestrator_runtime.constitutional import check_semantics  # noqa: E402


def _stage_7_json() -> str:
    return (
        '{"internal_contradictions":[],'
        '"direction_evidence_alignment":{"aligned":true,"detail":""},'
        '"base_rate_check":{"within_sane_bounds":true,"detail":""},'
        '"overall_pass":true}'
    )


def _call_result(
    text: str,
    *,
    in_tok: int = 1500,
    out_tok: int = 80,
    cache_read: int = 800,
    cache_create: int = 0,
    cost: float = 0.012,
    latency_ms: int = 240,
) -> CallResult:
    return CallResult(
        text=text,
        input_tokens=in_tok,
        output_tokens=out_tok,
        thinking_tokens=0,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_create,
        cost_usd=cost,
        latency_ms=latency_ms,
        model="claude-sonnet-4-5-20250929",
    )


def test_check_semantics_routes_through_client_call_not_raw_sdk():
    a_client = MagicMock()
    a_client.call.return_value = _call_result(_stage_7_json())

    findings, in_tok, out_tok, cost, latency_ms, overall_pass = check_semantics(
        a_client,
        cited_prose="thesis prose with citations [F:abc123]",
        facts=[{"id": "abc12345", "fact_type": "approval_history", "fact_text": "f", "confidence": 0.9}],
        thesis_direction="long",
        conviction_pct=70.0,
        reference_class=None,
        reference_class_base_rate=None,
        model="claude-sonnet-4-5-20250929",
    )

    assert overall_pass is True
    assert findings == []
    assert a_client.call.call_count == 1
    a_client._client.messages.create.assert_not_called()
    # Cost / token aggregation must reflect the wrapper's CallResult, including
    # cache-read attribution that the raw SDK path dropped.
    assert in_tok == 1500
    assert out_tok == 80
    assert cost == pytest.approx(0.012)
    assert latency_ms == 240


def test_check_semantics_uses_cached_system_blocks_when_supplied():
    a_client = MagicMock()
    a_client.call.return_value = _call_result(_stage_7_json())
    system_blocks = [{"type": "text", "text": "shared prefix", "cache_control": {"type": "ephemeral"}}]

    check_semantics(
        a_client,
        cited_prose="prose",
        facts=[],
        thesis_direction="long",
        conviction_pct=72.0,
        reference_class="psychiatry",
        reference_class_base_rate=0.55,
        model="claude-sonnet-4-5-20250929",
        system_blocks=system_blocks,
    )

    call_kwargs = a_client.call.call_args.kwargs
    # When cached system blocks are passed, the structured fact layer must
    # NOT be duplicated in the user content (it lives in the cached prefix).
    assert call_kwargs["system"] is system_blocks
    assert "Structured fact layer" not in call_kwargs["messages"][0]["content"]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
