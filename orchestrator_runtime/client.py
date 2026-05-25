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

from orchestrator_runtime.pricing import estimate_cost as _estimate_cost

logger = logging.getLogger(__name__)

# Model selection — swap to Opus 4.7 + xhigh thinking when rate-limit tier permits
DEFAULT_MODEL = os.environ.get(
    "ORCHESTRATOR_MODEL", "claude-sonnet-4-5-20250929")
DEFAULT_EXTRACTOR_MODEL = os.environ.get(
    "ORCHESTRATOR_EXTRACTOR_MODEL", "claude-sonnet-4-5-20250929")

# Anthropic's current Claude 4.5+ / 4.7 models reject explicit temperature
# with "temperature is deprecated for this model". Keep temperature for older
# models where it is still accepted, but omit it for the production family so
# callers cannot accidentally brick live runs by passing a stale diversity knob.
_MODELS_REJECTING_TEMPERATURE = (
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-opus-4-7",
)


def model_accepts_temperature(model: str) -> bool:
    normalized = model.lower()
    return not any(marker in normalized for marker in _MODELS_REJECTING_TEMPERATURE)


class BudgetExceededError(RuntimeError):
    """Raised by OrchestratorClient.call() when the per-run cost ceiling is
    breached *after* the in-flight call returns. The runtime detaches the
    budget; the drain handler converts this into status='killed_budget' and
    writes the partial cost. Distinct from skipped_budget (pre-flight skip)."""

    def __init__(self, run_id: Optional[str], ceiling_usd: float,
                 accumulated_usd: float):
        self.run_id = run_id
        self.ceiling_usd = ceiling_usd
        self.accumulated_usd = accumulated_usd
        super().__init__(
            f"Budget exceeded for run {run_id}: "
            f"${accumulated_usd:.4f} > ${ceiling_usd:.2f}"
        )


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


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Cache-aware cost estimate. Delegates to orchestrator_runtime.pricing.

    Kept here for backwards compat with callers that import from client.py.
    """
    return _estimate_cost(
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
    )


class OrchestratorClient:
    def __init__(self, api_key: Optional[str] = None):
        # The SDK reads ANTHROPIC_API_KEY from env if not passed; explicit None
        # is intentional — never log/store the key.
        self._client = anthropic.Anthropic(api_key=api_key)
        # Per-run cost ceiling — see attach_budget(). None when not active.
        self._budget_run_id: Optional[str] = None
        self._budget_ceiling_usd: Optional[float] = None
        self._budget_accumulated_usd: float = 0.0

    def attach_budget(
        self, run_id: Optional[str], hard_kill_usd: float,
    ) -> None:
        """Activate per-run hard-kill ceiling. Each subsequent call() returns
        normally if cumulative cost ≤ ceiling, else raises BudgetExceededError.
        Call detach_budget() when the run finishes (success or failure)."""
        self._budget_run_id = run_id
        self._budget_ceiling_usd = float(hard_kill_usd)
        self._budget_accumulated_usd = 0.0

    def detach_budget(self) -> float:
        """Clear the active budget. Returns the accumulated cost so the
        caller can write it to orchestrator_runs.cost_actual_usd."""
        accumulated = self._budget_accumulated_usd
        self._budget_run_id = None
        self._budget_ceiling_usd = None
        self._budget_accumulated_usd = 0.0
        return accumulated

    def get_accumulated_cost(self) -> float:
        """Read-only view of the live budget accumulator. Useful in
        BudgetExceededError handlers that need to PATCH the partial cost
        before detach_budget() is called."""
        return self._budget_accumulated_usd

    def call(
        self,
        *,
        system: str | List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        model: str = DEFAULT_MODEL,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        thinking_effort: Optional[str] = None,    # 'low'|'medium'|'high'|'xhigh' on Opus 4.7
        thinking_budget_tokens: Optional[int] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Dict[str, Any]] = None,
    ) -> CallResult:
        kwargs: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        # Ensemble diversity can pass temperature through this wrapper for
        # legacy models. Current Claude 4.5+ / 4.7 models reject the parameter,
        # so enforce the provider contract here as the final boundary.
        if (
            temperature is not None
            and model_accepts_temperature(model)
            and not ((thinking_budget_tokens or thinking_effort) and "opus" in model)
        ):
            kwargs["temperature"] = temperature
        if thinking_budget_tokens and "opus" in model:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget_tokens,
            }
        elif thinking_effort and "opus" in model:
            # Adaptive thinking — Opus 4.7+ accepts effort string
            kwargs["thinking"] = {"type": "enabled", "effort": thinking_effort}

        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        headers: Dict[str, str] = {}
        # Stream 3.1: default-inject interleaved-thinking beta header for Opus runs.
        # Sonnet/Haiku skip the header (cheaper, no thinking tokens consumed).
        if "opus" in model:
            headers["anthropic-beta"] = "interleaved-thinking-2025-05-14"
        if extra_headers:
            headers.update(extra_headers)

        t0 = time.time()
        # Transient-error retry. 529 OverloadedError + 429 RateLimitError +
        # APIConnectionError are upstream capacity / network blips and recover
        # within seconds. Permanent errors (auth, bad-request) are NOT retried.
        # Worst case: 2 + 4 + 8 = 14s extra wall-clock before giving up.
        _RETRY_MAX_ATTEMPTS = int(os.environ.get("ORCH_RETRY_MAX_ATTEMPTS", "4"))
        _RETRY_BASE_DELAY_S = float(os.environ.get("ORCH_RETRY_BASE_DELAY_S", "2.0"))
        _TRANSIENT = (
            anthropic.APIConnectionError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
        )
        # OverloadedError exists only in newer SDKs; fall back to status check.
        _OverloadedError = getattr(anthropic, "OverloadedError", None)
        if _OverloadedError is not None:
            _TRANSIENT = _TRANSIENT + (_OverloadedError,)

        attempt = 0
        last_exc: Optional[Exception] = None
        while attempt < _RETRY_MAX_ATTEMPTS:
            try:
                if headers:
                    resp = self._client.messages.create(extra_headers=headers, **kwargs)
                else:
                    resp = self._client.messages.create(**kwargs)
                break
            except _TRANSIENT as exc:
                attempt += 1
                last_exc = exc
                if attempt >= _RETRY_MAX_ATTEMPTS:
                    logger.error(
                        "Anthropic transient error after %d attempts: %s",
                        attempt, exc,
                    )
                    raise
                delay = _RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
                logger.warning(
                    "Anthropic transient error (attempt %d/%d, sleeping %.1fs): %s",
                    attempt, _RETRY_MAX_ATTEMPTS, delay, exc,
                )
                time.sleep(delay)
            except anthropic.APIStatusError as exc:
                # Some SDK versions surface 529 as APIStatusError instead of
                # OverloadedError. Treat status_code 429/529/5xx as transient.
                code = getattr(exc, "status_code", None)
                if code in (429, 529) or (code is not None and 500 <= code < 600):
                    attempt += 1
                    last_exc = exc
                    if attempt >= _RETRY_MAX_ATTEMPTS:
                        logger.error(
                            "Anthropic status %s after %d attempts: %s",
                            code, attempt, exc,
                        )
                        raise
                    delay = _RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
                    logger.warning(
                        "Anthropic status %s (attempt %d/%d, sleeping %.1fs): %s",
                        code, attempt, _RETRY_MAX_ATTEMPTS, delay, exc,
                    )
                    time.sleep(delay)
                else:
                    logger.error("Anthropic API error: %s", exc)
                    raise
            except anthropic.APIError as exc:
                logger.error("Anthropic API error: %s", exc)
                raise
        else:
            # _RETRY_MAX_ATTEMPTS exhausted with no break — re-raise last_exc.
            if last_exc is not None:
                raise last_exc

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

        cost = estimate_cost(
            model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_creation_tokens=cache_create,
            cache_read_tokens=cache_read,
        )

        # Per-run budget accumulator. Raise *after* tallying so the caller's
        # exception handler sees the full partial spend (this call has been
        # paid for already; we can't unwind it).
        if self._budget_ceiling_usd is not None:
            self._budget_accumulated_usd += cost
            if self._budget_accumulated_usd > self._budget_ceiling_usd:
                raise BudgetExceededError(
                    run_id=self._budget_run_id,
                    ceiling_usd=self._budget_ceiling_usd,
                    accumulated_usd=self._budget_accumulated_usd,
                )

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
