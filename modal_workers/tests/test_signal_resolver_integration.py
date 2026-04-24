"""
Integration tests for the signal_resolver flow at the Python layer.

The `signal_resolver` skill itself is a Cowork-scheduled markdown skill
(.claude/skills/signal_resolver.md) executed by Claude at runtime — its SQL
orchestration can't run under pytest. What we CAN test here is the Python
contract the skill depends on end-to-end:

  scanner payload (raw_data) + AI-estimated dims
    → rescore_with_dims (rubric_engine)
      → score + band + auto_caps
        → terminal state decision (which thesis_jobs.status to set)

These tests simulate the skill's path for the three analyst-driven rubrics that
are always unscored at ingest (activist_governance, merger_arb, litigation)
across the full decision tree. Live reactor behavior is slightly broader: it can
enqueue any known profile into `needs_scoring` when required payload keys are
missing and no heuristic dimensions were produced.
  - full-evidence dims that land at immediate → draft thesis path
  - mid-tier dims that land at watchlist → scoring_complete_below_immediate
  - weak/capped dims that land at archive/discard → scoring_complete_below_immediate
"""
from __future__ import annotations

from modal_workers.shared.rubric_engine import (
    WEIGHTS,
    build_scoring_meta,
    rescore_with_dims,
)


# ----------------------------------------------------------------------
# Terminal-state decision: what status should the skill set after rescore?
# This mirrors the branch at step 8 of signal_resolver.md.
# ----------------------------------------------------------------------

def _terminal_status(band: str | None) -> str:
    if band is None:
        return "scoring_complete_below_immediate"  # unscored → terminal (unresolved)
    if band == "immediate":
        return "drafting"  # continues to thesis step; not terminal yet
    return "scoring_complete_below_immediate"


# ----------------------------------------------------------------------
# activist_governance
# ----------------------------------------------------------------------

def test_resolver_activist_governance_strong_signal_lands_immediate():
    """A 13D with proxy-consent language + tight activist track record scores
    high across signal_strength / information_asymmetry / activist_track_record
    and should trigger the inline-thesis path."""
    raw_payload = {
        "keyword": "13D proxy consent solicitation",
        "filing_type": "SC 13D",
        "cik": "0001234567",
    }
    # All 7 dims pegged high (mirrors what an AI estimator with strong evidence would emit).
    dims = {k: 5 for k in WEIGHTS["activist_governance"]}
    out = rescore_with_dims("activist_governance", raw_payload, dims)
    # 5 × (2+2+1.5+1.5+1+1+1) = 5 × 10 = 50 → immediate
    assert out["score"] == 50.0
    assert out["band"] == "immediate"
    assert _terminal_status(out["band"]) == "drafting"


def test_resolver_activist_governance_middling_signal_lands_watchlist():
    raw_payload = {"keyword": "13D general governance", "filing_type": "SC 13D"}
    dims = {k: 3 for k in WEIGHTS["activist_governance"]}
    out = rescore_with_dims("activist_governance", raw_payload, dims)
    # 3 × 10 = 30 → watchlist (25-34)
    assert out["score"] == 30.0
    assert out["band"] == "watchlist"
    assert _terminal_status(out["band"]) == "scoring_complete_below_immediate"


def test_resolver_activist_governance_weak_signal_lands_archive():
    raw_payload = {"keyword": "13G passive"}
    # 2 across the board: weak filer, no track record, no clear catalyst.
    dims = {k: 2 for k in WEIGHTS["activist_governance"]}
    out = rescore_with_dims("activist_governance", raw_payload, dims)
    # 2 × 10 = 20 → archive
    assert out["score"] == 20.0
    assert out["band"] == "archive"
    assert _terminal_status(out["band"]) == "scoring_complete_below_immediate"


# ----------------------------------------------------------------------
# merger_arb
# ----------------------------------------------------------------------

def test_resolver_merger_arb_wide_spread_all_cash_deal_lands_immediate():
    """Wide spread + all-cash no-regulatory + financing secured → immediate."""
    raw_payload = {"form": "DEFM14A", "annualized_return_pct": 35.0}
    dims = {"spread_size": 5, "deal_certainty": 5, "annualized_return": 5,
            "break_risk": 4, "liquidity": 4}
    out = rescore_with_dims("merger_arb", raw_payload, dims)
    # 5×3 + 5×2.5 + 5×2 + 4×1.5 + 4×1 = 15+12.5+10+6+4 = 47.5 → immediate
    assert out["score"] == 47.5
    assert out["band"] == "immediate"


def test_resolver_merger_arb_auto_cap_sub_scale_return_downgrades_to_watchlist():
    """Even a high-score merger_arb gets capped to watchlist when the
    annualized_return_pct is below RFR+3%."""
    raw_payload = {"form": "DEFM14A", "annualized_return_pct": 5.0}  # below 4.3+3=7.3
    dims = {"spread_size": 5, "deal_certainty": 5, "annualized_return": 5,
            "break_risk": 5, "liquidity": 5}
    out = rescore_with_dims("merger_arb", raw_payload, dims)
    assert out["band"] == "watchlist"
    assert "merger_arb.rule_A_sub_scale_return" in out["auto_caps_triggered"]


def test_resolver_merger_arb_weak_spread_lands_archive():
    raw_payload = {"form": "8-K"}
    dims = {"spread_size": 2, "deal_certainty": 2, "annualized_return": 2,
            "break_risk": 2, "liquidity": 2}
    out = rescore_with_dims("merger_arb", raw_payload, dims)
    # 2 × 10 = 20 → archive
    assert out["score"] == 20.0
    assert out["band"] == "archive"


# ----------------------------------------------------------------------
# litigation
# ----------------------------------------------------------------------

def test_resolver_litigation_material_claim_confirmed_party_lands_immediate():
    # NOS 410 = antitrust per CourtListener's nature-of-suit codes. universe_resolved=True
    # marks the caption as having been matched against the SEC issuer index — required
    # for the litigation.universe_miss_cap (added in courtlistener selectivity rework)
    # not to demote the band to archive on a high-confidence-party + high-score row.
    raw_payload = {"nos": "410", "case_name": "Acme v. BigCorp",
                   "universe_resolved": True}
    dims = {"financial_materiality": 5, "legal_outcome_probability": 4,
            "market_pricing": 5, "resolution_timeline": 4, "liquidity": 4,
            "party_resolution_confidence": 5}
    out = rescore_with_dims("litigation", raw_payload, dims)
    # 5×3 + 4×2 + 5×2 + 4×1.5 + 4×1 + 5×0.5 = 15+8+10+6+4+2.5 = 45.5 → immediate
    assert out["score"] == 45.5
    assert out["band"] == "immediate"


def test_resolver_litigation_ambiguous_party_capped_to_archive():
    """party_resolution_confidence < 3 → cap to archive regardless of other dims."""
    raw_payload = {"case_name": "Acme Corp v. Smith"}
    dims = {"financial_materiality": 5, "legal_outcome_probability": 5,
            "market_pricing": 5, "resolution_timeline": 5, "liquidity": 5,
            "party_resolution_confidence": 2}
    out = rescore_with_dims("litigation", raw_payload, dims)
    # Raw score immediate, but party_confidence_cap → archive.
    assert out["band"] == "archive"
    assert "litigation.party_confidence_cap" in out["auto_caps_triggered"]


def test_resolver_litigation_immaterial_claim_lands_discard():
    raw_payload = {"case_name": "Minor dispute"}
    dims = {"financial_materiality": 1, "legal_outcome_probability": 1,
            "market_pricing": 1, "resolution_timeline": 1, "liquidity": 1,
            "party_resolution_confidence": 3}
    out = rescore_with_dims("litigation", raw_payload, dims)
    # 1×3 + 1×2 + 1×2 + 1×1.5 + 1×1 + 3×0.5 = 3+2+2+1.5+1+1.5 = 11 → discard (<15)
    assert out["score"] == 11.0
    assert out["band"] == "discard"


# ----------------------------------------------------------------------
# Provenance persistence — the dims shape the skill writes to signals.dimensions
# ----------------------------------------------------------------------

def test_resolver_writes_provenance_into_dimensions_jsonb():
    """The value the skill writes to signals.dimensions must include
    `_provenance: 'ai_resolved'` so downstream tooling can distinguish
    AI-scored signals from scanner-estimated ones."""
    dims = {k: 3 for k in WEIGHTS["merger_arb"]}
    out = rescore_with_dims("merger_arb", {}, dims, provenance="ai_resolved")
    persisted = out["dimensions_with_provenance"]
    assert persisted["_provenance"] == "ai_resolved"
    for k in WEIGHTS["merger_arb"]:
        assert persisted[k] == 3
    # Pure int dims (no provenance) must also be available for convergence use.
    assert "_provenance" not in out["dimensions"]


def test_resolver_ai_resolved_metadata_clears_resolution_requirement():
    dims = {k: 4 for k in WEIGHTS["activist_governance"]}
    out = rescore_with_dims("activist_governance", {"keyword": "13D"}, dims)
    meta = build_scoring_meta(
        provenance="ai_resolved",
        supported_dims=list(WEIGHTS["activist_governance"].keys()),
        defaulted_dims=[],
        requires_resolution=False,
    )
    assert out["dimensions_with_provenance"]["_provenance"] == "ai_resolved"
    assert meta["requires_resolution"] is False
    assert meta["defaulted_dims"] == []


def test_resolver_can_upgrade_a_provisional_heuristic_shape():
    heuristic_meta = build_scoring_meta(
        provenance="heuristic",
        supported_dims=["setup_strength", "edge_freshness", "strategic_buyer_clarity"],
        defaulted_dims=["valuation_cushion", "liquidity"],
        requires_resolution=True,
    )
    assert heuristic_meta["requires_resolution"] is True

    resolved_dims = {k: 5 for k in WEIGHTS["takeover_candidate"]}
    out = rescore_with_dims("takeover_candidate", {"patterns_hit": 4}, resolved_dims)
    assert out["dimensions_with_provenance"]["_provenance"] == "ai_resolved"
    assert out["score"] == 50.0
    assert out["band"] == "immediate"


# ----------------------------------------------------------------------
# Quota-exhausted branch (skill's step 9) — exercised purely in Python as a
# decision helper. The actual SQL lives in the skill; here we verify the
# band→status decision holds for the quota-reached case.
# ----------------------------------------------------------------------

def _status_for_immediate_with_quota_state(band: str, quota_reached: bool) -> str:
    """Mirrors the skill's step 9+10 branch: immediate with quota → terminal
    early with daily_quota_reached reason; immediate under quota → drafting."""
    if band != "immediate":
        return "scoring_complete_below_immediate"
    return "scoring_complete_below_immediate" if quota_reached else "drafting"


def test_resolver_quota_exhausted_parks_immediate_signal_as_terminal():
    assert _status_for_immediate_with_quota_state("immediate", quota_reached=True) \
        == "scoring_complete_below_immediate"
    assert _status_for_immediate_with_quota_state("immediate", quota_reached=False) \
        == "drafting"
    assert _status_for_immediate_with_quota_state("watchlist", quota_reached=False) \
        == "scoring_complete_below_immediate"
