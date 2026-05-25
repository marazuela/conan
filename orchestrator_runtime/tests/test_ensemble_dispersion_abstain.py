"""Dispersion-based abstain: when the ensemble actively disagrees on direction
or conviction stddev is high, downgrade the band from 'immediate' to 'watchlist'
so we don't fan out a noisy signal as an alert.

This complements the shrinkage formula (final_conviction = mean - 0.5 * stddev),
which softly penalizes disagreement in the conviction number itself but does
not stop a borderline 70%-conviction signal from triggering an email when the
ensemble was 4:3:0 on direction.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from orchestrator_runtime.ensemble import compute_dispersion_abstain  # noqa: E402


def test_no_abstain_at_n1():
    """At N=1 there's no dispersion to compute — band passes through."""
    payload = {"n": 1, "dispersion": 0.0, "direction_distribution": {"long": 1}}
    band, reason = compute_dispersion_abstain(payload, "immediate")
    assert band == "immediate"
    assert reason is None


def test_no_abstain_when_band_is_not_immediate():
    """The gate never PROMOTES a lower band; it only suppresses immediate
    fanout when the ensemble is too noisy to trust."""
    payload = {"n": 3, "dispersion": 30.0, "direction_distribution": {"long": 1, "short": 2}}
    band, reason = compute_dispersion_abstain(payload, "watchlist")
    assert band == "watchlist"
    assert reason is None


def test_abstain_on_high_dispersion(monkeypatch):
    """Convictions diverging widely across runs is the canonical noisy
    signal — even if the mean lands in the immediate band, the band is
    not trustworthy."""
    monkeypatch.setattr(
        "orchestrator_runtime.ensemble.ENSEMBLE_DISPERSION_ABSTAIN_PCT", 15.0
    )
    payload = {
        "n": 3,
        "dispersion": 22.0,
        "direction_distribution": {"long": 3},
    }
    band, reason = compute_dispersion_abstain(payload, "immediate")
    assert band == "watchlist"
    assert reason is not None
    assert "ensemble_dispersion_abstain" in reason
    assert "22.0" in reason


def test_abstain_on_split_direction(monkeypatch):
    """Even if convictions are tightly clustered, a contested DIRECTION vote
    (e.g. 4-long / 3-short / 0-neutral) means we don't know which way to
    bet — the email should be suppressed."""
    monkeypatch.setattr(
        "orchestrator_runtime.ensemble.ENSEMBLE_DISPERSION_ABSTAIN_PCT", 15.0
    )
    monkeypatch.setattr(
        "orchestrator_runtime.ensemble.ENSEMBLE_DIRECTION_ABSTAIN_FRAC", 0.6
    )
    payload = {
        "n": 7,
        "dispersion": 5.0,
        "direction_distribution": {"long": 4, "short": 3},
    }
    band, reason = compute_dispersion_abstain(payload, "immediate")
    assert band == "watchlist"
    assert reason is not None
    assert "ensemble_direction_abstain" in reason


def test_no_abstain_on_consensus(monkeypatch):
    """Healthy ensemble — tight conviction stddev and clear direction
    majority — passes through with no downgrade."""
    monkeypatch.setattr(
        "orchestrator_runtime.ensemble.ENSEMBLE_DISPERSION_ABSTAIN_PCT", 15.0
    )
    monkeypatch.setattr(
        "orchestrator_runtime.ensemble.ENSEMBLE_DIRECTION_ABSTAIN_FRAC", 0.6
    )
    payload = {
        "n": 5,
        "dispersion": 4.0,
        "direction_distribution": {"long": 4, "short": 1},
    }
    band, reason = compute_dispersion_abstain(payload, "immediate")
    assert band == "immediate"
    assert reason is None


def test_env_flag_disables_abstain(monkeypatch):
    """Revert path: setting ORCH_DISABLE_ENSEMBLE_DISPERSION_ABSTAIN=1
    restores pre-PR behavior (no downgrade)."""
    monkeypatch.setenv("ORCH_DISABLE_ENSEMBLE_DISPERSION_ABSTAIN", "1")
    payload = {
        "n": 3,
        "dispersion": 40.0,
        "direction_distribution": {"long": 1, "short": 2},
    }
    band, reason = compute_dispersion_abstain(payload, "immediate")
    assert band == "immediate"
    assert reason is None


def test_no_abstain_on_empty_payload():
    """Safe-degrade: missing fields don't break the gate."""
    band, reason = compute_dispersion_abstain(None, "immediate")
    assert band == "immediate"
    assert reason is None

    band, reason = compute_dispersion_abstain({}, "immediate")
    assert band == "immediate"
    assert reason is None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
