"""Focused tests for ensemble temperature handling.

Run: python -m pytest orchestrator_runtime/tests/test_ensemble_temperature.py -v
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("ANTHROPIC_API_KEY", "x")

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
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _message("cited synthesis prose")
        return _message(_stage_9_json())

    client = SimpleNamespace(
        _client=SimpleNamespace(
            messages=SimpleNamespace(create=MagicMock(side_effect=fake_create))
        )
    )

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
    assert "temperature" not in calls[0]
    assert calls[0]["messages"] == [{"role": "user", "content": "stage 1 user"}]


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

    client = SimpleNamespace(
        _client=SimpleNamespace(
            messages=SimpleNamespace(
                create=MagicMock(return_value=_message(_stage_9_json())),
                batches=SimpleNamespace(
                    create=MagicMock(side_effect=fake_batch_create),
                    retrieve=MagicMock(),
                    results=MagicMock(side_effect=fake_results),
                ),
            )
        )
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
