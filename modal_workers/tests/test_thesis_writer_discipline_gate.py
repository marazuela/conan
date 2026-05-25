"""
Tests for §6.7 discipline gate (WI-1) — port of v2_skills compose-thesis-with-discipline.

Lock in the 6-field discipline check that gates thesis_writer promotion ahead of
the §6.8 challenger. The gate is shadow-mode by default
(internal_config.discipline_gate_enabled='false'); these tests pin the verdict
logic so flipping the flag to 'true' doesn't surprise routing.

Run: python -m pytest modal_workers/tests/test_thesis_writer_discipline_gate.py -v
"""
from __future__ import annotations

import pytest

from modal_workers.shared.candidate_gate import (
    DISCIPLINE_FIELDS,
    DISCIPLINE_MIN_CHARS,
    assess_discipline_v2,
)


def _valid_thesis() -> dict:
    """Minimal thesis that passes the §6.7 discipline gate."""
    return {
        # v1+v2 fields that exist independently of discipline (not exercised
        # here — assess_thesis_v2 covers them).
        "situation": "AXSM AXS-05 PDUFA action date 2026-09-15 unchanged. Sponsor has Breakthrough designation and Priority Review.",
        "why_underpriced": "Sell-side prices 50% approval on conservative read of Phase 3 ADA endpoint. We weight new draft guidance on RCT-of-fact more heavily and arrive at 65%.",
        "next_catalyst": "PDUFA action date 2026-09-15.",
        "next_catalyst_date": "2026-09-15",
        "kill_conditions": "FDA issues CRL with manufacturing finding; AdCom convened with negative outcome; sponsor withdraws label.",
        "steelman": "Bear case: FDA staff reviewer raises pre-spec endpoint concerns at briefing; division historically requires AdCom for psychiatric indications.",
        "structured_kill_conditions": [
            {
                "id": "K1",
                "description": "FDA issues a Complete Response Letter with manufacturing finding",
                "observable": {"source_type": "edgar_8k_item_801", "search_pattern": "Complete Response Letter"},
                "date_bound": "2026-09-30",
            },
            {
                "id": "K2",
                "description": "AdCom convened and votes against approval",
                "observable": {"source_type": "fda_advisory_committee", "search_pattern": "voted against"},
            },
            {
                "id": "K3",
                "description": "Sponsor withdraws the indication before PDUFA",
                "observable": {"source_type": "edgar_8k_item_801", "search_pattern": "withdraw"},
            },
        ],
        # The 6 v2 discipline fields:
        "variant_perception": "We price 65% approval probability vs sell-side consensus of 50%; the delta is grounded in FDA's new draft guidance on RCT-of-fact endpoints applied to the ADA Phase 3 pre-spec.",
        "preconditions": "PDUFA action date 2026-09-15 unchanged; no AdCom convened; CMC inspection of Bedminster facility closed without 483 findings.",
        "return_distribution": "P(approval, T+30) = 0.65 -> +35% to $7.20; P(CRL, T+30) = 0.30 -> -55% to $2.40; P(delay, T+30) = 0.05 -> flat $4.05; EV = +18%.",
        "time_horizon": "Horizon = PDUFA 2026-09-15 (T+0); pre-decision read on advisory committee minutes 2026-08-30; AdCom-trigger watch through 2026-08-15.",
        "sizing_inputs": "Max position = min(market_cap * 0.01, $5M); ADV clip = 5% of 30-day median dollar volume; unit-risk position size scales to expected drawdown -55% on CRL.",
    }


# ---------------------------------------------------------------------------
# Case 1 — all 6 fields present at sufficient length → discipline_pass
# ---------------------------------------------------------------------------


class TestDisciplinePass:
    def test_all_fields_present_passes(self):
        v = assess_discipline_v2(_valid_thesis())
        assert v["verdict"] == "discipline_pass"
        assert v["missing_fields"] == []
        assert v["present_but_too_short"] == []
        assert v["min_chars_required"] == DISCIPLINE_MIN_CHARS

    def test_min_chars_required_includes_all_prose_fields(self):
        v = assess_discipline_v2(_valid_thesis())
        # kill_criteria is derived from structured_kill_conditions[] — no min_chars entry.
        for field in ("variant_perception", "preconditions", "return_distribution",
                      "time_horizon", "sizing_inputs"):
            assert field in v["min_chars_required"]


# ---------------------------------------------------------------------------
# Case 2 — one field totally missing → discipline_decline (no retry)
# ---------------------------------------------------------------------------


class TestDisciplineDecline:
    def test_missing_preconditions_declines(self):
        t = _valid_thesis()
        del t["preconditions"]
        v = assess_discipline_v2(t)
        assert v["verdict"] == "discipline_decline"
        assert "preconditions" in v["missing_fields"]
        assert v["present_but_too_short"] == []

    def test_empty_string_counts_as_missing(self):
        t = _valid_thesis()
        t["sizing_inputs"] = ""
        v = assess_discipline_v2(t)
        assert v["verdict"] == "discipline_decline"
        assert "sizing_inputs" in v["missing_fields"]

    def test_whitespace_only_counts_as_missing(self):
        t = _valid_thesis()
        t["variant_perception"] = "   \n   "
        v = assess_discipline_v2(t)
        assert v["verdict"] == "discipline_decline"
        assert "variant_perception" in v["missing_fields"]

    def test_non_string_counts_as_missing(self):
        t = _valid_thesis()
        t["return_distribution"] = 42  # numeric, not a string
        v = assess_discipline_v2(t)
        assert v["verdict"] == "discipline_decline"
        assert "return_distribution" in v["missing_fields"]

    def test_multiple_missing_fields_all_reported(self):
        t = _valid_thesis()
        del t["variant_perception"]
        del t["sizing_inputs"]
        v = assess_discipline_v2(t)
        assert v["verdict"] == "discipline_decline"
        assert set(v["missing_fields"]) >= {"variant_perception", "sizing_inputs"}

    def test_kill_criteria_derived_from_structured_kill_conditions(self):
        t = _valid_thesis()
        # Only 2 structured kills — should flag kill_criteria as missing.
        t["structured_kill_conditions"] = t["structured_kill_conditions"][:2]
        v = assess_discipline_v2(t)
        assert v["verdict"] == "discipline_decline"
        assert "kill_criteria" in v["missing_fields"]

    def test_time_horizon_synthesized_from_next_catalyst_date(self):
        t = _valid_thesis()
        del t["time_horizon"]
        # next_catalyst_date is still present — should NOT flag time_horizon.
        v = assess_discipline_v2(t)
        assert "time_horizon" not in v["missing_fields"]

    def test_time_horizon_missing_when_no_catalyst_date(self):
        t = _valid_thesis()
        del t["time_horizon"]
        del t["next_catalyst_date"]
        v = assess_discipline_v2(t)
        assert v["verdict"] == "discipline_decline"
        assert "time_horizon" in v["missing_fields"]

    def test_non_dict_thesis_fails_loudly(self):
        v = assess_discipline_v2(None)
        assert v["verdict"] == "discipline_decline"
        assert set(v["missing_fields"]) == set(DISCIPLINE_FIELDS)


# ---------------------------------------------------------------------------
# Case 3 — one field present but below min_chars → discipline_retry (one budget)
# ---------------------------------------------------------------------------


class TestDisciplineRetry:
    def test_short_variant_perception_retries(self):
        t = _valid_thesis()
        t["variant_perception"] = "Too short"  # well below 80
        v = assess_discipline_v2(t)
        assert v["verdict"] == "discipline_retry"
        assert "variant_perception" in v["present_but_too_short"]
        assert v["missing_fields"] == []

    def test_return_distribution_without_digit_retries(self):
        t = _valid_thesis()
        # Long enough to clear min_chars but no digit anywhere — required by spec.
        t["return_distribution"] = (
            "Probability-weighted scenarios across approval, CRL, and delay paths "
            "with anchors derived from comparable precedents in the same indication."
        )
        v = assess_discipline_v2(t)
        assert v["verdict"] == "discipline_retry"
        assert "return_distribution" in v["present_but_too_short"]

    def test_multiple_too_short_fields_all_reported(self):
        t = _valid_thesis()
        t["preconditions"] = "PDUFA holds."  # 12 chars
        t["sizing_inputs"] = "$5M cap."  # 8 chars
        v = assess_discipline_v2(t)
        assert v["verdict"] == "discipline_retry"
        assert set(v["present_but_too_short"]) >= {"preconditions", "sizing_inputs"}

    def test_missing_beats_too_short_in_verdict_priority(self):
        # When BOTH missing and too_short exist, verdict is decline (no retry).
        t = _valid_thesis()
        del t["sizing_inputs"]
        t["variant_perception"] = "short"
        v = assess_discipline_v2(t)
        assert v["verdict"] == "discipline_decline"
        assert "sizing_inputs" in v["missing_fields"]
        assert "variant_perception" in v["present_but_too_short"]


# ---------------------------------------------------------------------------
# Case 4 — retry-then-pass: simulate the §8b corrective-prompt path
# ---------------------------------------------------------------------------


class TestRetryToPromote:
    def test_short_field_expanded_passes_on_retry(self):
        t = _valid_thesis()
        t["preconditions"] = "PDUFA holds."  # 12 chars — too short
        v1 = assess_discipline_v2(t)
        assert v1["verdict"] == "discipline_retry"

        # Drafter expands per the corrective prompt.
        t["preconditions"] = (
            "PDUFA action date 2026-09-15 unchanged; no AdCom convened; CMC inspection "
            "closed without 483; no GMP findings logged."
        )
        v2 = assess_discipline_v2(t)
        assert v2["verdict"] == "discipline_pass"
        assert v2["present_but_too_short"] == []

    def test_return_distribution_digit_added_passes_on_retry(self):
        t = _valid_thesis()
        t["return_distribution"] = (
            "Probability-weighted scenarios across approval, CRL, and delay paths "
            "with anchors derived from comparable precedents."
        )
        assert assess_discipline_v2(t)["verdict"] == "discipline_retry"

        # Drafter re-emits with explicit math.
        t["return_distribution"] = (
            "P(approval) = 0.65 -> +35%; P(CRL) = 0.30 -> -55%; P(delay) = 0.05 -> flat; EV = +18%."
        )
        assert assess_discipline_v2(t)["verdict"] == "discipline_pass"
