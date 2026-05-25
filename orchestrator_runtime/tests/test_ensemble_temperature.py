"""Focused tests for ensemble temperature handling.

Run: python -m pytest orchestrator_runtime/tests/test_ensemble_temperature.py -v
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from orchestrator_runtime.client import CallResult, OrchestratorClient  # noqa: E402
from orchestrator_runtime.ensemble import (  # noqa: E402
    _run_one_streaming,
    _stage_1_request_params,
    run_batch_ensemble,
)


def _message(text: str, input_tokens: int = 10, output_tokens: int = 5):
    block = SimpleNamespace(type="text", text=text)
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(content=[block], usage=usage)


def _stage_9_json(conviction: int = 70) -> str:
    return (
        '{"thesis_direction":"long","conviction_pct":'
        f'{conviction},"evidence_quality":0.8,'
        '"key_facts":[],"uncertainties":[]}'
    )


def _call_result(
    text: str,
    *,
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cost_usd: float = 0.01,
    latency_ms: int = 100,
) -> CallResult:
    return CallResult(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=0,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        model="claude-sonnet-4-5-20250929",
    )


def test_stage_1_request_params_omits_temperature_for_current_tier1_model():
    params = _stage_1_request_params(
        model="claude-sonnet-4-5-20250929",
        max_tokens=4096,
        temperature=0.8,
        stage_1_system="system",
        stage_1_user_content="user",
    )

    assert params["model"] == "claude-sonnet-4-5-20250929"
    assert "temperature" not in params


def test_stage_1_request_params_keeps_temperature_for_legacy_models():
    params = _stage_1_request_params(
        model="claude-3-5-sonnet-20241022",
        max_tokens=4096,
        temperature=0.8,
        stage_1_system="system",
        stage_1_user_content="user",
    )

    assert params["temperature"] == 0.8


def test_run_one_streaming_omits_rejected_temperature_from_stage_1_call():
    client = MagicMock()
    client.call.side_effect = [
        _call_result("cited synthesis prose"),
        _call_result(_stage_9_json()),
    ]

    run = _run_one_streaming(
        client,
        "stage 1 system",
        "stage 1 user",
        "stage 9 system",
        "claude-sonnet-4-5-20250929",
        "claude-sonnet-4-5-20250929",
        0,
        0.8,
        4096,
        8192,
    )

    assert run is not None
    assert run.direction == "long"
    assert client.call.call_count == 2
    client._client.messages.create.assert_not_called()
    stage_1_kwargs = client.call.call_args_list[0].kwargs
    stage_9_kwargs = client.call.call_args_list[1].kwargs
    assert "temperature" not in stage_1_kwargs or stage_1_kwargs["temperature"] is None
    assert "temperature" not in stage_9_kwargs or stage_9_kwargs["temperature"] is None
    assert stage_1_kwargs["messages"] == [{"role": "user", "content": "stage 1 user"}]


def test_run_one_streaming_routes_through_wrapper_and_aggregates_call_results():
    client = MagicMock()
    client.call.side_effect = [
        _call_result(
            "cited synthesis prose",
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=20,
            cache_creation_tokens=5,
            cost_usd=0.10,
            latency_ms=200,
        ),
        _call_result(
            _stage_9_json(),
            input_tokens=30,
            output_tokens=40,
            cache_read_tokens=7,
            cache_creation_tokens=0,
            cost_usd=0.02,
            latency_ms=80,
        ),
    ]

    run = _run_one_streaming(
        client,
        "stage 1 system",
        "stage 1 user",
        "stage 9 system",
        "claude-3-5-sonnet-20241022",
        "claude-sonnet-4-5-20250929",
        0,
        0.8,
        4096,
        8192,
    )

    assert run is not None
    client._client.messages.create.assert_not_called()
    assert client.call.call_args_list[0].kwargs["temperature"] == 0.8
    assert "temperature" not in client.call.call_args_list[1].kwargs
    assert run.input_tokens == 130
    assert run.output_tokens == 90
    assert run.cache_read_tokens == 27
    assert run.cache_creation_tokens == 5
    assert run.cost_usd == pytest.approx(0.12)
    assert run.latency_ms == 280


def test_batch_ensemble_omits_rejected_temperature_from_stage_1_requests():
    captured = {}

    def fake_batch_create(**kwargs):
        captured["requests"] = kwargs["requests"]
        return SimpleNamespace(
            id="batch-1",
            processing_status="ended",
            request_counts=SimpleNamespace(processing=0, succeeded=2, errored=0),
        )

    def fake_results(batch_id):
        assert batch_id == "batch-1"
        return [
            SimpleNamespace(
                custom_id="ensemble-s1-0",
                result=SimpleNamespace(
                    type="succeeded",
                    message=_message("batch synthesis 0", input_tokens=11, output_tokens=7),
                ),
            ),
            SimpleNamespace(
                custom_id="ensemble-s1-1",
                result=SimpleNamespace(
                    type="succeeded",
                    message=_message("batch synthesis 1", input_tokens=13, output_tokens=7),
                ),
            ),
        ]

    # Stage 9 now goes through OrchestratorClient.call (PR routing batch
    # ensemble through the wrapper for budget + retry). The batches API
    # itself has no wrapper, so we still mock that surface on _client.
    stage_9_call_result = _call_result(_stage_9_json())
    client = SimpleNamespace(
        call=MagicMock(return_value=stage_9_call_result),
        _client=SimpleNamespace(
            messages=SimpleNamespace(
                batches=SimpleNamespace(
                    create=MagicMock(side_effect=fake_batch_create),
                    retrieve=MagicMock(),
                    results=MagicMock(side_effect=fake_results),
                ),
            )
        ),
    )

    result = run_batch_ensemble(
        client,
        stage_1_system="stage 1 system",
        stage_1_user_content="stage 1 user",
        stage_9_system="stage 9 system",
        n=2,
        model="claude-sonnet-4-5-20250929",
        extractor_model="claude-sonnet-4-5-20250929",
        temperature=0.8,
        poll_interval_s=0,
        max_wait_s=0,
    )

    assert result.n == 2
    assert len(captured["requests"]) == 2
    assert all("temperature" not in req["params"] for req in captured["requests"])


def test_orchestrator_client_omits_rejected_temperature_at_final_boundary():
    client = OrchestratorClient(api_key="test-key")
    message = _message("ok", input_tokens=12, output_tokens=4)
    client._client.messages.create = MagicMock(return_value=message)

    client.call(
        system="system",
        messages=[{"role": "user", "content": "user"}],
        model="claude-sonnet-4-5-20250929",
        temperature=0.8,
    )

    kwargs = client._client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-5-20250929"
    assert "temperature" not in kwargs


def test_orchestrator_client_keeps_temperature_for_legacy_models():
    client = OrchestratorClient(api_key="test-key")
    message = _message("ok", input_tokens=12, output_tokens=4)
    client._client.messages.create = MagicMock(return_value=message)

    client.call(
        system="system",
        messages=[{"role": "user", "content": "user"}],
        model="claude-3-5-sonnet-20241022",
        temperature=0.8,
    )

    kwargs = client._client.messages.create.call_args.kwargs
    assert kwargs["temperature"] == 0.8
