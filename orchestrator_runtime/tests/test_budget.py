"""Tests for BudgetExceededError + OrchestratorClient.attach_budget()
cost-ceiling enforcement (Stream 6 step 4).

Run: python -m pytest orchestrator_runtime/tests/test_budget.py -v
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from orchestrator_runtime.client import BudgetExceededError, OrchestratorClient


def test_budget_exceeded_is_runtime_error():
    err = BudgetExceededError(run_id="run-1", ceiling_usd=15.0,
                              accumulated_usd=15.50)
    assert isinstance(err, RuntimeError)


def test_budget_exceeded_attributes():
    err = BudgetExceededError(run_id="run-abc", ceiling_usd=15.0,
                              accumulated_usd=18.42)
    assert err.run_id == "run-abc"
    assert err.ceiling_usd == 15.0
    assert err.accumulated_usd == 18.42


def test_budget_exceeded_message():
    err = BudgetExceededError(run_id="r1", ceiling_usd=15.0,
                              accumulated_usd=16.123)
    msg = str(err)
    assert "r1" in msg
    assert "$16.12" in msg or "$16.1230" in msg
    assert "$15.00" in msg


def test_budget_exceeded_none_run_id_ok():
    # Pre-flight skip / unknown run scenarios pass run_id=None
    err = BudgetExceededError(run_id=None, ceiling_usd=15.0,
                              accumulated_usd=15.01)
    assert err.run_id is None
    assert "None" in str(err)


def test_budget_exceeded_distinct_from_other_exceptions():
    err = BudgetExceededError(run_id="r", ceiling_usd=1.0, accumulated_usd=2.0)
    # Drain handler should be able to catch this distinctly
    try:
        raise err
    except BudgetExceededError as caught:
        assert caught is err
    except RuntimeError:
        pytest.fail("BudgetExceededError caught as plain RuntimeError before "
                    "the BudgetExceededError handler — order matters!")


# ---------------------------------------------------------------------------
# attach_budget / detach_budget / get_accumulated_cost
# ---------------------------------------------------------------------------

def _stub_anthropic_response(in_tok=10_000, out_tok=2_000):
    """Build a fake anthropic.messages.create() response."""
    block = MagicMock()
    block.type = "text"
    block.text = "ok"
    resp = MagicMock()
    resp.content = [block]
    resp.usage.input_tokens = in_tok
    resp.usage.output_tokens = out_tok
    resp.usage.cache_read_input_tokens = 0
    resp.usage.cache_creation_input_tokens = 0
    return resp


def _client_with_response(resp):
    c = OrchestratorClient(api_key="dummy")
    c._client = MagicMock()
    c._client.messages.create = MagicMock(return_value=resp)
    return c


def test_attach_budget_initializes_accumulator():
    c = OrchestratorClient(api_key="dummy")
    c.attach_budget("run-1", 15.0)
    assert c._budget_run_id == "run-1"
    assert c._budget_ceiling_usd == 15.0
    assert c._budget_accumulated_usd == 0.0
    assert c.get_accumulated_cost() == 0.0


def test_detach_budget_returns_accumulated_and_clears():
    c = OrchestratorClient(api_key="dummy")
    c.attach_budget("run-1", 15.0)
    c._budget_accumulated_usd = 7.5  # simulate a few calls
    accumulated = c.detach_budget()
    assert accumulated == 7.5
    assert c._budget_run_id is None
    assert c._budget_ceiling_usd is None
    assert c._budget_accumulated_usd == 0.0


def test_detach_budget_idempotent_when_inactive():
    c = OrchestratorClient(api_key="dummy")
    # Never attached
    assert c.detach_budget() == 0.0
    assert c.detach_budget() == 0.0


def test_call_with_no_budget_does_not_raise():
    # 100k in + 50k out at sonnet rates = 100k*$3 + 50k*$15 / 1M = $1.05
    c = _client_with_response(_stub_anthropic_response(100_000, 50_000))
    # No attach_budget — call should not raise even at high cost
    result = c.call(system="s", messages=[{"role": "user", "content": "x"}])
    assert result.cost_usd > 0.0


def test_call_under_ceiling_accumulates():
    # 1k in + 500 out at sonnet ≈ $0.0105
    c = _client_with_response(_stub_anthropic_response(1_000, 500))
    c.attach_budget("run-1", hard_kill_usd=15.0)
    result = c.call(system="s", messages=[{"role": "user", "content": "x"}])
    assert c.get_accumulated_cost() == result.cost_usd
    assert c.get_accumulated_cost() < 15.0


def test_call_above_ceiling_raises_budget_exceeded():
    # 10M in + 0 out at sonnet = $30 — over $15 ceiling
    c = _client_with_response(_stub_anthropic_response(10_000_000, 0))
    c.attach_budget("run-1", hard_kill_usd=15.0)
    with pytest.raises(BudgetExceededError) as exc_info:
        c.call(system="s", messages=[{"role": "user", "content": "x"}])
    assert exc_info.value.run_id == "run-1"
    assert exc_info.value.ceiling_usd == 15.0
    assert exc_info.value.accumulated_usd >= 15.0


def test_call_accumulates_across_multiple_calls():
    c = _client_with_response(_stub_anthropic_response(100_000, 50_000))
    c.attach_budget("run-1", hard_kill_usd=15.0)
    c.call(system="s", messages=[{"role": "user", "content": "x"}])
    c.call(system="s", messages=[{"role": "user", "content": "y"}])
    c.call(system="s", messages=[{"role": "user", "content": "z"}])
    # ~$1.05 per call * 3 = $3.15
    assert 3.0 < c.get_accumulated_cost() < 3.5


def test_call_raises_only_after_ceiling_breached_not_before():
    # 5M in + 0 out at sonnet = $15 exactly. Fine on first call.
    c = _client_with_response(_stub_anthropic_response(5_000_000, 0))
    c.attach_budget("run-1", hard_kill_usd=15.0)
    # First call: $15 — at ceiling, not over. accumulated_usd = 15.0
    # ($15.0 > $15.0 is False)
    c.call(system="s", messages=[{"role": "user", "content": "x"}])
    assert c.get_accumulated_cost() == 15.0
    # Second call: pushes over the ceiling
    with pytest.raises(BudgetExceededError):
        c.call(system="s", messages=[{"role": "user", "content": "y"}])


def test_get_accumulated_cost_after_breach_includes_breaching_call():
    """The breaching call has already been paid for; the accumulator
    reflects the full partial spend so the drain handler writes accurate
    cost_actual_usd."""
    c = _client_with_response(_stub_anthropic_response(10_000_000, 0))
    c.attach_budget("run-1", hard_kill_usd=15.0)
    with pytest.raises(BudgetExceededError):
        c.call(system="s", messages=[{"role": "user", "content": "x"}])
    # After the raise, accumulated reflects the call that breached
    assert c.get_accumulated_cost() >= 15.0


def test_attach_budget_resets_accumulator_for_new_run():
    c = _client_with_response(_stub_anthropic_response(100_000, 50_000))
    c.attach_budget("run-1", hard_kill_usd=15.0)
    c.call(system="s", messages=[{"role": "user", "content": "x"}])
    first_run_cost = c.get_accumulated_cost()
    c.detach_budget()
    # New run on the same client — accumulator should reset
    c.attach_budget("run-2", hard_kill_usd=15.0)
    assert c.get_accumulated_cost() == 0.0
    c.call(system="s", messages=[{"role": "user", "content": "x"}])
    # New accumulator has only the new run's cost
    assert c.get_accumulated_cost() < first_run_cost * 1.5


def test_detach_after_breach_returns_partial_cost():
    c = _client_with_response(_stub_anthropic_response(10_000_000, 0))
    c.attach_budget("run-1", hard_kill_usd=15.0)
    with pytest.raises(BudgetExceededError):
        c.call(system="s", messages=[{"role": "user", "content": "x"}])
    accumulated = c.detach_budget()
    assert accumulated >= 15.0
    # After detach, the accumulator is cleared
    assert c.get_accumulated_cost() == 0.0
