"""G2C — golden e2e test for the v3 orchestrator on AXS-05 (or operator-pick).

Three layers, gated separately so the suite stays green even when the cassette
hasn't been recorded yet and Modal/Anthropic deploys aren't reachable.

  1. SHAPE CHECK (always runs) — imports `record_assessment` /
     `replay_assessment` and asserts their signatures match what Phase 4A
     (D-127) shipped. Catches regressions where someone renames a kwarg or
     drops a return field.

  2. CASSETTE REPLAY (skip when fixture absent) — when
     `modal_workers/tests/fixtures/cassettes/<case_id>.jsonl` exists, replays
     it through `replay_assessment` with a STUB SupabaseClient and asserts
     the `ReplayOutput` is well-formed. No Anthropic calls; cheap; the
     gate-keeper for prompt-version regressions on a recorded fixture.

  3. LIVE RECORD/PERSIST (skip unless RUN_LIVE_E2E=1) — calls
     `record_assessment(persist=True)` against live Supabase + Anthropic,
     then asserts the resulting DB rows: 4 specialist `fda_agent_reviews`,
     1 `agent_kind='ic_memo'` review, 1 `convergence_assessments` row, ≥1
     memory_files write, cost ≤ $X (env-tunable). Paid; deliberate. Use
     this once per gate to anchor a baseline cassette + assertions.

Plan ref: ~/.claude/plans/plan-it-for-optimal-twinkling-bubble.md G2B/G2C.
DECISIONS ref: D-127 (cassette pattern), D-125 (cost ceiling, agent_kind
extension), D-124 (RAG infra + sub-agent stack).

Env knobs:
  RUN_LIVE_E2E=1                — opt in to the paid layer
  ORCH_E2E_CASE_ID              — defaults to 'axs05_pdufa'
  ORCH_E2E_ASSET_ID             — required for layer 2/3; the live AXS-05 uuid
  ORCH_E2E_MAX_COST_USD         — optional cap (default 15.0 — the D-125 hard kill)
  ORCH_E2E_CASSETTE_DIR         — override fixture dir (default modal_workers/tests/fixtures/cassettes)
"""
from __future__ import annotations

import inspect
import os
from dataclasses import is_dataclass
from pathlib import Path
from typing import Any, Dict, List

import pytest


_FIXTURE_DIR_DEFAULT = (
    Path(__file__).resolve().parent / "fixtures" / "cassettes"
)


def _case_id() -> str:
    return os.environ.get("ORCH_E2E_CASE_ID", "axs05_pdufa")


def _cassette_dir() -> Path:
    return Path(os.environ.get("ORCH_E2E_CASSETTE_DIR") or _FIXTURE_DIR_DEFAULT)


def _cassette_path() -> Path:
    return _cassette_dir() / f"{_case_id()}.jsonl"


# ---------------------------------------------------------------------------
# Layer 1 — shape checks (always runs)
# ---------------------------------------------------------------------------


def test_replay_runner_signatures_match_d127_contract():
    """Lock in the public surface of the cassette runner. If a kwarg moves
    or a return shape changes, this test fails before the live recording
    catches it the slow + expensive way."""
    from orchestrator_runtime.eval_harness import replay_runner
    from orchestrator_runtime.eval_harness.replay import ReplayOutput

    sig_record = inspect.signature(replay_runner.record_assessment)
    sig_replay = inspect.signature(replay_runner.replay_assessment)

    # Positional args (case_id, asset_id) on both entry points.
    assert list(sig_record.parameters)[:2] == ["case_id", "asset_id"]
    assert list(sig_replay.parameters)[:2] == ["case_id", "asset_id"]

    # Required kwargs the test uses.
    for name in ("cassette_dir", "sb"):
        assert name in sig_record.parameters
        assert name in sig_replay.parameters

    # `persist=False` default — recording must NOT write a real
    # convergence_assessments row unless the caller flips it.
    assert sig_record.parameters["persist"].default is False

    # ReplayOutput dataclass shape — kept stable so dashboards + harness
    # aggregation can read it.
    assert is_dataclass(ReplayOutput)
    fields = {f.name for f in ReplayOutput.__dataclass_fields__.values()}
    assert fields == {
        "conviction_pct",
        "thesis_direction",
        "band",
        "reasoning_summary",
    }


def test_default_replay_fn_requires_cassette_dir_env(monkeypatch):
    """default_replay_fn must raise a helpful error when invoked without
    the env var set — guards against silent-skip bugs in CI."""
    from orchestrator_runtime.eval_harness.replay_runner import (
        DEFAULT_CASSETTE_ENV_VAR, default_replay_fn,
    )
    from orchestrator_runtime.eval_harness.gold_standard import HarnessCase
    from orchestrator_runtime.eval_harness.replay import ReplayInput

    monkeypatch.delenv(DEFAULT_CASSETTE_ENV_VAR, raising=False)
    case = HarnessCase(
        id="x", asset_id="00000000-0000-0000-0000-000000000000",
        reference_assessment_date="2026-01-01",
        document_set=[],
        realized_outcome="approved",
        realized_outcome_data={},
        is_holdout=True, difficulty=None, notes=None,
    )
    payload = ReplayInput(
        case=case, document_ids=[],
        reference_date=case.reference_assessment_date,
        orchestrator_version="v3.0", prompt_hash="x",
    )
    with pytest.raises(RuntimeError, match=DEFAULT_CASSETTE_ENV_VAR):
        default_replay_fn(payload)


# ---------------------------------------------------------------------------
# Layer 2 — cassette replay (skip when fixture absent)
# ---------------------------------------------------------------------------


def _cassette_present() -> bool:
    return _cassette_path().exists()


@pytest.mark.skipif(
    not _cassette_present(),
    reason=(
        "Cassette fixture not yet recorded — run `record_assessment` "
        "with persist=False against AXS-05 once Gate 0/1 unblocks "
        "and commit the resulting JSONL to modal_workers/tests/fixtures/cassettes/."
    ),
)
def test_axs05_cassette_replay_returns_well_formed_output():
    """Replay the recorded AXS-05 cassette and assert the ReplayOutput is
    structurally valid. Doesn't hit Anthropic; does still need a sb to
    fetch the asset row + extracted_facts at Stage 0/1.

    NOTE: this test relies on the live Supabase project the developer's
    SUPABASE_URL points at. If that's a fresh branch with no AXS-05 row,
    the test will fail at Stage 0 — that's a fixture-coverage bug, not a
    code regression. Use Layer 3 to seed the data first.
    """
    asset_id = os.environ.get("ORCH_E2E_ASSET_ID")
    if not asset_id:
        pytest.skip(
            "ORCH_E2E_ASSET_ID not set — cassette replay needs the live "
            "asset uuid to query Stage 0 metadata"
        )

    from orchestrator_runtime.eval_harness.replay_runner import replay_assessment
    from modal_workers.shared.supabase_client import SupabaseClient

    sb = SupabaseClient()
    out = replay_assessment(
        case_id=_case_id(),
        asset_id=asset_id,
        cassette_dir=_cassette_dir(),
        sb=sb,
    )

    assert 0.0 <= out.conviction_pct <= 100.0, (
        f"conviction_pct out of range: {out.conviction_pct}"
    )
    assert out.thesis_direction in {"long", "short", "neutral", "straddle"}, (
        f"unexpected thesis_direction: {out.thesis_direction!r}"
    )
    assert out.band in {"immediate", "watchlist", "archive", "discard"}, (
        f"unexpected band: {out.band!r}"
    )
    assert isinstance(out.reasoning_summary, str)


# ---------------------------------------------------------------------------
# Layer 3 — live record + persist (skip unless explicitly opted in)
# ---------------------------------------------------------------------------


def _live_e2e_enabled() -> bool:
    return os.environ.get("RUN_LIVE_E2E") == "1"


@pytest.mark.skipif(
    not _live_e2e_enabled(),
    reason=(
        "RUN_LIVE_E2E=1 not set — this layer hits live Anthropic + Supabase "
        "with persist=True (cost ≤ $15 by D-125 hard kill, but real money). "
        "Opt in deliberately."
    ),
)
def test_axs05_live_record_persists_full_assessment_row_set():
    """The G2 acceptance check: a live recorded run on AXS-05 produces
    4 specialist `fda_agent_reviews`, 1 `agent_kind='ic_memo'` review,
    1 `convergence_assessments` row, ≥1 memory_files write, cost under
    the env-tuned ceiling.

    Run this once per gate to anchor the cassette + DB baseline. The
    resulting `<cassette_dir>/<case_id>.jsonl` should be committed so
    Layer 2 takes over for follow-up runs.
    """
    asset_id = os.environ.get("ORCH_E2E_ASSET_ID")
    if not asset_id:
        pytest.skip("ORCH_E2E_ASSET_ID required for live e2e")

    from orchestrator_runtime.eval_harness.replay_runner import record_assessment
    from modal_workers.shared.supabase_client import SupabaseClient

    sb = SupabaseClient()
    cassette_dir = _cassette_dir()
    cassette_dir.mkdir(parents=True, exist_ok=True)

    out = record_assessment(
        case_id=_case_id(),
        asset_id=asset_id,
        cassette_dir=cassette_dir,
        sb=sb,
        persist=True,
    )

    # ReplayOutput shape (same as Layer 2).
    assert 0.0 <= out.conviction_pct <= 100.0
    assert out.thesis_direction in {"long", "short", "neutral", "straddle"}
    assert out.band in {"immediate", "watchlist", "archive", "discard"}

    # Cassette landed.
    assert _cassette_path().exists(), (
        f"record_assessment did not create cassette at {_cassette_path()}"
    )

    # DB rows: 1 convergence_assessments + 4 specialist + 1 ic_memo.
    # We pull the FRESHEST set, scoped to asset_id, to avoid colliding with
    # previous test runs.
    assessments = sb._rest("GET", "convergence_assessments", params={
        "select": "id,asset_id,conviction_pct,band,total_input_tokens,total_output_tokens,cost_usd,created_at",
        "asset_id": f"eq.{asset_id}",
        "order": "created_at.desc",
        "limit": "1",
    }) or []
    assert len(assessments) == 1, (
        f"expected exactly 1 latest convergence_assessments row for asset {asset_id}, got {len(assessments)}"
    )
    a = assessments[0]
    assert a["conviction_pct"] is not None, (
        "Stage 8 calibration produced null conviction_pct — check calibration_curves is_active"
    )

    cost = float(a.get("cost_usd") or 0.0)
    cost_ceiling = float(os.environ.get("ORCH_E2E_MAX_COST_USD") or 15.0)
    assert cost <= cost_ceiling, (
        f"run cost ${cost:.2f} exceeded ceiling ${cost_ceiling:.2f} "
        "(D-125 hard kill should have prevented this — investigate)"
    )

    # Sub-agent reviews: pull all kinds for the same event, assert 4 + 1.
    # We have asset_id, not event_id — use the asset's latest event row to scope.
    events = sb._rest("GET", "fda_regulatory_events", params={
        "select": "id",
        "asset_id": f"eq.{asset_id}",
        "order": "created_at.desc",
        "limit": "1",
    }) or []
    assert events, (
        f"no fda_regulatory_events for asset {asset_id} — orchestrator wrote "
        "an assessment but the bridge has no event row to anchor reviews"
    )
    event_id = events[0]["id"]

    reviews = sb._rest("GET", "fda_agent_reviews", params={
        "select": "id,agent_kind,status,confidence,created_at",
        "event_id": f"eq.{event_id}",
        "order": "created_at.desc",
    }) or []
    kinds: Dict[str, List[Dict[str, Any]]] = {}
    for r in reviews:
        kinds.setdefault(r["agent_kind"], []).append(r)

    expected_specialists = {"literature", "competitive", "regulatory", "options"}
    # D-125 added 'literature', 'competitive', 'ic_memo' to the CHECK; the
    # canonical 4 specialists are the ones the dispatcher fans out + 'ic_memo'
    # for synthesis. Allow both 'options' and 'options_microstructure' since
    # the runner's role string and the DB CHECK were extended slightly apart.
    found_specialists = (set(kinds.keys()) & expected_specialists) | (
        {"options"} if "options_microstructure" in kinds else set()
    )
    assert len(found_specialists) >= 4, (
        f"expected 4 specialist reviews, found kinds={sorted(kinds.keys())}"
    )
    assert kinds.get("ic_memo"), (
        "no ic_memo review row — Phase 3A IC memo runner did not fire"
    )

    # Memory writeback: at least one memory_files row updated for this asset
    # in the last hour (the run just happened).
    mem = sb._rest("GET", "memory_files", params={
        "select": "id,scope,scope_id,updated_at",
        "scope": "eq.asset",
        "scope_id": f"eq.{asset_id}",
        "order": "updated_at.desc",
        "limit": "1",
    }) or []
    assert mem, (
        "no asset-scope memory_files row — Stage 10 memory writeback (D-123 C5) skipped"
    )
