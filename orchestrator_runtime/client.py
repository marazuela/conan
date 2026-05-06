"""Anthropic client wrapper for the orchestrator runtime.

Reads ANTHROPIC_API_KEY from env. Provides a thin shim that:
  - Selects the model + thinking effort + cache control per call
  - Rolls up usage tokens into stage metrics
  - Wraps API errors with diagnostic detail
  - Strips markdown fences from JSON responses

Production model: claude-opus-4-7 with extended thinking effort='xhigh' +
mixed-TTL prompt caching + interleaved-thinking-2025-05-14 beta. MVP runs on
claude-sonnet-4-5-20250929 to fit Tier-1 rate limits (30k input tokens/min);
swap is one constant change once Tier-2+ is available.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import anthropic

logger = logging.getLogger(__name__)

# Model selection — swap to Opus 4.7 + xhigh thinking when rate-limit tier permits
DEFAULT_MODEL = os.environ.get(
    "ORCHESTRATOR_MODEL", "claude-sonnet-4-5-20250929")
DEFAULT_EXTRACTOR_MODEL = os.environ.get(
    "ORCHESTRATOR_EXTRACTOR_MODEL", "claude-sonnet-4-5-20250929")

# Pricing per 1M tokens (USD). Sonnet 4.5 indicative.
COST_TABLE = {
    "claude-sonnet-4-5-20250929": (3.0, 15.0),       # input, output
    "claude-opus-4-7-20260101": (15.0, 75.0),         # placeholder; bump when GA pricing known
    "claude-haiku-4-5-20251001": (0.80, 4.0),
}


@dataclass
class CallResult:
    text: str
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    latency_ms: int
    model: str
    raw_message: Optional[anthropic.types.Message] = None


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = COST_TABLE.get(model)
    if not rates:
        return 0.0
    return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000


class OrchestratorClient:
    def __init__(self, api_key: Optional[str] = None):
        # The SDK reads ANTHROPIC_API_KEY from env if not passed; explicit None
        # is intentional — never log/store the key.
        self._client = anthropic.Anthropic(api_key=api_key)

    def call(
        self,
        *,
        system: str | List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        model: str = DEFAULT_MODEL,
        max_tokens: int = 4096,
        thinking_effort: Optional[str] = None,    # 'low'|'medium'|'high'|'xhigh' on Opus 4.7
        thinking_budget_tokens: Optional[int] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> CallResult:
        kwargs: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if thinking_budget_tokens and "opus" in model:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget_tokens,
            }
        elif thinking_effort and "opus" in model:
            # Adaptive thinking — Opus 4.7+ accepts effort string
            kwargs["thinking"] = {"type": "enabled", "effort": thinking_effort}

        headers = {}
        if extra_headers:
            headers.update(extra_headers)

        t0 = time.time()
        try:
            if headers:
                resp = self._client.messages.create(extra_headers=headers, **kwargs)
            else:
                resp = self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            logger.error("Anthropic API error: %s", exc)
            raise

        latency_ms = int((time.time() - t0) * 1000)
        text = "".join(b.text for b in resp.content if b.type == "text")
        thinking_tokens = sum(
            getattr(b, "tokens", 0) for b in resp.content if b.type == "thinking"
        )

        usage = resp.usage
        in_tok = usage.input_tokens
        out_tok = usage.output_tokens
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0

        cost = estimate_cost(model, in_tok, out_tok)

        return CallResult(
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            thinking_tokens=thinking_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_create,
            cost_usd=cost,
            latency_ms=latency_ms,
            model=model,
            raw_message=resp,
        )


def strip_json_fences(text: str) -> str:
    """Remove markdown code fences from a model response so json.loads works."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text


def parse_json_or_none(text: str) -> Optional[Any]:
    """Lenient JSON parse: try fence-strip first; on failure, slice from the
    first '{' to the last '}' and try again. Last-resort: return None and let
    the caller log."""
    cleaned = strip_json_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Slice from first '{' to last '}'
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        candidate = cleaned[start:end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            logger.warning("parse_json_or_none failed even after slice: %s; "
                           "head[:200]=%r tail[-200:]=%r",
                           exc, candidate[:200], candidate[-200:])
            return None
    logger.warning("parse_json_or_none: no '{...}' found in text[:300]=%r",
                   cleaned[:300])
    return None
