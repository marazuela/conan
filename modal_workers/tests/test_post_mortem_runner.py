"""Tests for post_mortem_runner pure helpers (Stream 2).

Covers:
  - realized_outcome_score across (direction, hit) matrix
  - wilson_interval edge cases (n=0, all-success, all-fail)
  - median utility
  - _merge_memory_file: idempotency, empty start, section injection
"""

from __future__ import annotations

import pytest

from modal_workers.shared.post_mortem_runner import (
    ResolvedOutcome,
    _merge_memory_file,
    median,
    realized_outcome_score,
    wilson_interval,
)


# ---------------------------------------------------------------------------
# realized_outcome_score
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction,hit,expected", [
    ("long",   True,  100.0),  # long thesis correct, big up move
    ("long",   False, 0.0),    # long thesis wrong
    ("short",  True,  0.0),    # HIT means stock UP — short was wrong
    ("short",  False, 100.0),  # no upside move — short partially validated
    ("neutral", True,  50.0),  # one-sided event but neutral predicted no move
    ("neutral", False, 50.0),  # no big move — neutral OK
    ("straddle", True, 50.0),
    ("",        True, 50.0),   # unknown direction → middle
    (None,      False, 50.0),  # None direction → middle
    ("LONG",    True, 100.0),  # case-insensitive
])
def test_realized_outcome_score(direction, hit, expected):
    assert realized_outcome_score(hit=hit, predicted_direction=direction) == expected


# ---------------------------------------------------------------------------
# wilson_interval
# ---------------------------------------------------------------------------

def test_wilson_interval_n_zero_returns_full_range():
    lo, hi = wilson_interval(0, 0)
    assert lo == 0.0 and hi == 1.0


def test_wilson_interval_all_success_pulls_lower_bound_up():
    lo, hi = wilson_interval(50, 50)
    assert lo > 0.9
    assert hi == 1.0  # upper bound clamps


def test_wilson_interval_all_fail_pulls_upper_bound_down():
    lo, hi = wilson_interval(0, 50)
    assert lo == 0.0
    assert hi < 0.1


def test_wilson_interval_centered_at_50_50():
    lo, hi = wilson_interval(50, 100)
    assert 0.40 < lo < 0.50
    assert 0.50 < hi < 0.60
    # Symmetric around 0.5
    assert abs((hi + lo) / 2 - 0.5) < 0.01


def test_wilson_interval_narrows_with_n():
    lo10, hi10 = wilson_interval(5, 10)
    lo1000, hi1000 = wilson_interval(500, 1000)
    assert (hi10 - lo10) > (hi1000 - lo1000), "CI must narrow as n grows"


# ---------------------------------------------------------------------------
# median
# ---------------------------------------------------------------------------

def test_median_empty_returns_none():
    assert median([]) is None


def test_median_odd():
    assert median([3.0, 1.0, 2.0]) == 2.0


def test_median_even_averages():
    assert median([1.0, 2.0, 3.0, 4.0]) == 2.5


def test_median_with_negatives():
    assert median([-10.0, 5.0, 2.0, -3.0]) == -0.5


# ---------------------------------------------------------------------------
# _merge_memory_file
# ---------------------------------------------------------------------------

def _outcome(asmt_id: str, direction: str = "long", error: float = -10.0) -> ResolvedOutcome:
    return ResolvedOutcome(
        queue_id="q-1",
        assessment_id=asmt_id,
        asset_id="asset-1",
        predicted_conviction_pct=70.0,
        predicted_direction=direction,
        status="post_mortem_complete",
        prediction_error=error,
        post_mortem_text="Predicted long; realized hit. Reference class held.",
        realized_outcome={
            "hit": True,
            "windows": [
                {"days": 30, "status": "ok", "return_pct": 25.5},
                {"days": 60, "status": "ok", "return_pct": 30.0},
            ],
        },
        reference_class="oncology_phase3_breakthrough",
    )


def _asset() -> dict:
    return {
        "id": "asset-1",
        "primary_ticker": "AVNS",
        "asset_name": "ABC-123",
        "brand_name": None,
        "indication": "oncology",
        "indication_normalized": "oncology_hematologic",
        "sponsor": "Avenas Bio",
    }


def _assessment() -> dict:
    return {
        "id": "asmt-1",
        "thesis_summary": "Long thesis on AVNS PDUFA approval",
        "reference_class": "oncology_phase3_breakthrough",
    }


def test_merge_memory_file_creates_full_template_when_empty():
    asset = _asset()
    out = _outcome("asmt-1")
    text = _merge_memory_file(None, asset, _assessment(), out)
    assert text.startswith("# AVNS · ABC-123 (oncology_hematologic)")
    assert "## Active hypotheses" in text
    assert "## Resolved post-mortems" in text
    assert "## Open uncertainties" in text
    assert "## Recent assessments" in text
    # Entry inside Resolved section.
    assert "predicted 70% long" in text
    assert "+25.50%" in text
    assert "(HIT, T+30)" in text
    assert "<!-- assessment:asmt-1 -->" in text


def test_merge_memory_file_idempotent_on_same_assessment():
    asset = _asset()
    out = _outcome("asmt-1")
    first = _merge_memory_file(None, asset, _assessment(), out)
    second = _merge_memory_file(first, asset, _assessment(), out)
    assert first == second, "re-merging the same assessment must be idempotent"


def test_merge_memory_file_appends_new_entry_at_top_of_section():
    asset = _asset()
    first = _merge_memory_file(None, asset, _assessment(), _outcome("asmt-1"))
    second = _merge_memory_file(first, asset, _assessment(), _outcome("asmt-2", error=15.0))
    # Both markers present.
    assert "<!-- assessment:asmt-1 -->" in second
    assert "<!-- assessment:asmt-2 -->" in second
    # asmt-2 (newer) appears BEFORE asmt-1 in the file.
    pos2 = second.index("<!-- assessment:asmt-2 -->")
    pos1 = second.index("<!-- assessment:asmt-1 -->")
    assert pos2 < pos1, "newest-first ordering required"


def test_merge_memory_file_injects_section_when_missing():
    asset = _asset()
    # A pre-existing file from Stream 3 that hasn't yet seen any post-mortems.
    existing = (
        "# AVNS · ABC-123 (oncology_hematologic)\n\n"
        "## Active hypotheses\n\n- bull · prior 65%\n\n"
        "## Open uncertainties\n\n- AdComm vote\n"
    )
    out = _outcome("asmt-1")
    text = _merge_memory_file(existing, asset, _assessment(), out)
    # Section was injected; existing sections preserved.
    assert "## Resolved post-mortems" in text
    assert "## Active hypotheses" in text
    assert "## Open uncertainties" in text
    # Resolved section is BEFORE Open uncertainties.
    assert text.index("## Resolved post-mortems") < text.index("## Open uncertainties")
    # Active hypotheses content survived.
    assert "bull · prior 65%" in text
