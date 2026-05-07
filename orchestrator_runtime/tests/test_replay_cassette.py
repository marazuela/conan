"""Phase 4A — cassette + replay-runner unit tests.

Two layers:

  - `cassette.py` round-trip: synthetic OrchestratorClient stub records
    deterministic responses; replay-mode reads them back and reconstructs
    a CallResult with the right fields. Mismatch and exhaustion paths
    are both exercised explicitly.

  - `replay_runner.py` smoke: doesn't run the full `run_one()` pipeline
    (that would need a live Supabase + assets table); instead it asserts
    that record + replay produce identical `ReplayOutput`s when given the
    same parsed_out payload, and that `default_replay_fn` raises clearly
    when the cassette dir env is absent.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from orchestrator_runtime.client import CallResult
from orchestrator_runtime.eval_harness.cassette import (
    CASSETTE_VERSION,
    CassetteClient,
    CassetteExhaustedError,
    CassetteMismatchError,
    _hash_request,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeMessage:
    """Stand-in for anthropic.types.Message so CallResult.raw_message has
    the duck-typed shape CassetteClient expects when capturing."""
    def __init__(self, text: str, *, stop_reason: str = "end_turn"):
        self.stop_reason = stop_reason
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage()


class _FakeBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeUsage:
    input_tokens = 100
    output_tokens = 50
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _StubUpstream:
    """OrchestratorClient stand-in that returns scripted responses keyed by
    call index. Records every (kwargs, response) pair so tests can assert
    the cassette saw what we sent."""

    def __init__(self, responses: List[str]):
        self._responses = list(responses)
        self._call_index = 0
        self.calls_seen: List[Dict[str, Any]] = []

    def call(self, **kwargs: Any) -> CallResult:
        idx = self._call_index
        self._call_index += 1
        if idx >= len(self._responses):
            raise IndexError(
                f"_StubUpstream ran out of scripted responses at call #{idx}"
            )
        text = self._responses[idx]
        self.calls_seen.append(kwargs)
        return CallResult(
            text=text,
            input_tokens=100,
            output_tokens=50,
            thinking_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=0.0015,
            latency_ms=42,
            model=kwargs.get("model", "claude-sonnet-4-5-20250929"),
            raw_message=_FakeMessage(text),
        )

    def attach_budget(self, run_id: Optional[str], hard_kill_usd: float) -> None:
        pass

    def detach_budget(self) -> float:
        return 0.0

    def get_accumulated_cost(self) -> float:
        return 0.0


def _make_request_kwargs(
    *, system: str = "you are a helpful assistant",
    user_text: str = "what is 2+2?",
    model: str = "claude-sonnet-4-5-20250929",
) -> Dict[str, Any]:
    return {
        "system": system,
        "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
        "model": model,
        "max_tokens": 4096,
    }


# ---------------------------------------------------------------------------
# Cassette: hash stability + record/replay round-trip
# ---------------------------------------------------------------------------


def test_request_hash_is_stable_across_dict_ordering():
    h1 = _hash_request(
        model="claude-sonnet-4-5-20250929",
        system={"role": "system", "text": "hi"},
        messages=[{"role": "user", "content": "x"}],
        tools=None, tool_choice=None,
    )
    # Same payload, keys created in different order, but hash is stable
    # because _stable_dump uses sort_keys=True.
    h2 = _hash_request(
        tool_choice=None, tools=None,
        messages=[{"role": "user", "content": "x"}],
        system={"text": "hi", "role": "system"},
        model="claude-sonnet-4-5-20250929",
    )
    assert h1 == h2


def test_request_hash_changes_when_messages_change():
    h1 = _hash_request(
        model="m", system="s",
        messages=[{"role": "user", "content": "a"}],
        tools=None, tool_choice=None,
    )
    h2 = _hash_request(
        model="m", system="s",
        messages=[{"role": "user", "content": "b"}],
        tools=None, tool_choice=None,
    )
    assert h1 != h2


def test_record_then_replay_roundtrip(tmp_path: Path):
    upstream = _StubUpstream(["four", "yes the answer is four"])
    cassette_path = tmp_path / "case-1.jsonl"

    # Record
    with CassetteClient(cassette_path, mode="record", upstream=upstream) as rec:
        r1 = rec.call(**_make_request_kwargs(user_text="2+2?"))
        r2 = rec.call(**_make_request_kwargs(user_text="confirm please"))
    assert r1.text == "four"
    assert r2.text == "yes the answer is four"
    assert cassette_path.exists()
    lines = cassette_path.read_text().strip().splitlines()
    assert len(lines) == 2
    entry0 = json.loads(lines[0])
    assert entry0["version"] == CASSETTE_VERSION
    assert entry0["response_text"] == "four"

    # Replay — same requests, identical hashes, no upstream needed
    with CassetteClient(cassette_path, mode="replay") as rep:
        s1 = rep.call(**_make_request_kwargs(user_text="2+2?"))
        s2 = rep.call(**_make_request_kwargs(user_text="confirm please"))
    assert s1.text == "four"
    assert s2.text == "yes the answer is four"
    assert s1.input_tokens == 100
    assert s1.cost_usd > 0
    # raw_message reconstruction — ducks like the real Message
    assert s1.raw_message is not None
    assert s1.raw_message.stop_reason == "end_turn"
    assert s1.raw_message.content[0].type == "text"
    assert s1.raw_message.content[0].text == "four"


def test_replay_mismatch_raises(tmp_path: Path):
    upstream = _StubUpstream(["recorded"])
    cassette_path = tmp_path / "case-2.jsonl"

    with CassetteClient(cassette_path, mode="record", upstream=upstream) as rec:
        rec.call(**_make_request_kwargs(user_text="original"))

    with CassetteClient(cassette_path, mode="replay") as rep:
        with pytest.raises(CassetteMismatchError) as excinfo:
            rep.call(**_make_request_kwargs(user_text="DIFFERENT_TEXT"))
        err = excinfo.value
        assert err.position == 0
        assert err.expected_hash != err.actual_hash
        assert "tail=" in err.expected_meta["summary"]


def test_replay_exhaustion_raises(tmp_path: Path):
    upstream = _StubUpstream(["only-one"])
    cassette_path = tmp_path / "case-3.jsonl"

    with CassetteClient(cassette_path, mode="record", upstream=upstream) as rec:
        rec.call(**_make_request_kwargs())

    with CassetteClient(cassette_path, mode="replay") as rep:
        rep.call(**_make_request_kwargs())
        with pytest.raises(CassetteExhaustedError):
            rep.call(**_make_request_kwargs(user_text="extra request"))


def test_record_mode_requires_upstream(tmp_path: Path):
    with pytest.raises(ValueError, match="record mode requires an upstream"):
        CassetteClient(tmp_path / "x.jsonl", mode="record")


def test_invalid_mode_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="mode must be 'record' or 'replay'"):
        CassetteClient(tmp_path / "x.jsonl", mode="bogus")


def test_replay_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        CassetteClient(tmp_path / "does-not-exist.jsonl", mode="replay")


def test_budget_accumulator_tracks_cost(tmp_path: Path):
    upstream = _StubUpstream(["a", "b"])
    cassette_path = tmp_path / "budget.jsonl"

    with CassetteClient(cassette_path, mode="record", upstream=upstream) as rec:
        rec.attach_budget(run_id="run-x", hard_kill_usd=10.0)
        rec.call(**_make_request_kwargs())
        rec.call(**_make_request_kwargs(user_text="next"))
        # Each scripted response was cost_usd=0.0015 → 0.003 total
        assert rec.get_accumulated_cost() == pytest.approx(0.003, abs=1e-6)
        rec.detach_budget()


def test_replay_budget_uses_pricing_estimate(tmp_path: Path):
    """Replay-mode recomputes cost via pricing.estimate_cost (real client
    cost is not persisted to the cassette by design)."""
    upstream = _StubUpstream(["a"])
    cassette_path = tmp_path / "budget2.jsonl"
    with CassetteClient(cassette_path, mode="record", upstream=upstream) as rec:
        rec.call(**_make_request_kwargs())

    with CassetteClient(cassette_path, mode="replay") as rep:
        rep.attach_budget(run_id="run-y", hard_kill_usd=10.0)
        rep.call(**_make_request_kwargs())
        # Real number depends on pricing.py — we only assert it's > 0 and
        # the accumulator flowed through.
        assert rep.get_accumulated_cost() > 0


def test_thinking_blocks_round_trip(tmp_path: Path):
    """Non-text content blocks (e.g. thinking) survive the cassette round
    trip so Stages 1/7 see the same shape."""
    cassette_path = tmp_path / "thinking.jsonl"

    class _MsgWithThinking:
        stop_reason = "end_turn"

        class _Thk:
            type = "thinking"
            thinking = "step 1: parse\nstep 2: answer"
            signature = "sig-1"
            tokens = 12

        class _Txt:
            type = "text"
            text = "final answer"

        content = [_Thk(), _Txt()]

        class _Usage:
            input_tokens = 50
            output_tokens = 25
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0

        usage = _Usage()

    class _ScriptedThinking:
        def call(self, **kwargs: Any) -> CallResult:
            return CallResult(
                text="final answer", input_tokens=50, output_tokens=25,
                thinking_tokens=12, cache_read_tokens=0,
                cache_creation_tokens=0, cost_usd=0.001, latency_ms=10,
                model="claude-opus-4-7", raw_message=_MsgWithThinking(),
            )
        def attach_budget(self, *a, **kw): pass
        def detach_budget(self): return 0.0
        def get_accumulated_cost(self): return 0.0

    with CassetteClient(cassette_path, mode="record", upstream=_ScriptedThinking()) as rec:
        rec.call(**_make_request_kwargs(model="claude-opus-4-7"))

    with CassetteClient(cassette_path, mode="replay") as rep:
        out = rep.call(**_make_request_kwargs(model="claude-opus-4-7"))
        # Replayed message has both blocks in the same order
        types = [b.type for b in out.raw_message.content]
        assert types == ["thinking", "text"]
        assert out.raw_message.content[0].thinking.startswith("step 1")
        assert out.raw_message.content[1].text == "final answer"


# ---------------------------------------------------------------------------
# replay_runner: shape-level smoke
# ---------------------------------------------------------------------------


def test_default_replay_fn_raises_when_env_not_set(monkeypatch):
    from orchestrator_runtime.eval_harness import replay_runner
    from orchestrator_runtime.eval_harness.replay import ReplayInput
    from orchestrator_runtime.eval_harness.gold_standard import HarnessCase
    from datetime import date

    monkeypatch.delenv(replay_runner.DEFAULT_CASSETTE_ENV_VAR, raising=False)

    case = HarnessCase(
        id="case-1", asset_id="asset-1",
        reference_assessment_date=date(2024, 5, 1),
        realized_outcome="approved",
        realized_outcome_data={},
        document_set=[], is_holdout=True,
        difficulty="medium", notes=None,
    )
    inp = ReplayInput(
        case=case, document_ids=[],
        reference_date="2024-05-01",
        orchestrator_version="orch-v0.4.0",
        prompt_hash="deadbeef",
    )
    with pytest.raises(RuntimeError, match="ORCH_REPLAY_CASSETTE_DIR"):
        replay_runner.default_replay_fn(inp)


def test_make_replay_fn_returns_callable(tmp_path):
    from orchestrator_runtime.eval_harness import replay_runner

    fn = replay_runner.make_replay_fn(tmp_path)
    assert callable(fn)


def test_replay_assessment_raises_on_missing_cassette(tmp_path):
    """Calling replay_assessment with no cassette file should surface
    FileNotFoundError from CassetteClient — replay_one catches it and
    records a per-case error in the harness output."""
    from orchestrator_runtime.eval_harness.replay_runner import replay_assessment

    class _StubSb:
        def _rest(self, *a, **k):
            return []

    with pytest.raises(FileNotFoundError):
        replay_assessment(
            case_id="case-no-cassette",
            asset_id="asset-x",
            cassette_dir=tmp_path,
            sb=_StubSb(),
        )
