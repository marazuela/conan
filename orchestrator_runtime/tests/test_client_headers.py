"""Test Stream 3.1 — interleaved-thinking beta header default-injected for Opus.

Sonnet calls do NOT get the header (no thinking tokens, cheaper).

Run: python -m pytest orchestrator_runtime/tests/test_client_headers.py -v
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from orchestrator_runtime.client import OrchestratorClient


def _build_client_with_capture():
    client = OrchestratorClient()
    captured: dict = {}

    def _fake_create(*args, **kwargs):
        captured["kwargs"] = kwargs
        # Return a minimal usable Message stub
        resp = MagicMock()
        resp.content = []
        resp.usage = MagicMock(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )
        return resp

    client._client.messages.create = MagicMock(side_effect=_fake_create)
    return client, captured


def test_opus_call_injects_beta_header():
    client, captured = _build_client_with_capture()
    client.call(
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        model="claude-opus-4-7-20260101",
    )
    headers = captured["kwargs"].get("extra_headers") or {}
    assert headers.get("anthropic-beta") == "interleaved-thinking-2025-05-14"


def test_sonnet_call_does_not_inject_beta_header():
    client, captured = _build_client_with_capture()
    client.call(
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        model="claude-sonnet-4-5-20250929",
    )
    # Either no extra_headers passed, or empty dict
    extra = captured["kwargs"].get("extra_headers")
    assert not extra or "anthropic-beta" not in extra


def test_caller_can_override_beta_header():
    """A user-supplied extra_headers can override / augment the default."""
    client, captured = _build_client_with_capture()
    client.call(
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        model="claude-opus-4-7-20260101",
        extra_headers={"anthropic-beta": "custom-beta-2026"},
    )
    headers = captured["kwargs"].get("extra_headers") or {}
    assert headers["anthropic-beta"] == "custom-beta-2026"


def test_tools_param_passes_through():
    client, captured = _build_client_with_capture()
    tool_def = [{"name": "x", "description": "y", "input_schema": {"type": "object"}}]
    client.call(
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        model="claude-sonnet-4-5-20250929",
        tools=tool_def,
    )
    assert captured["kwargs"].get("tools") == tool_def
