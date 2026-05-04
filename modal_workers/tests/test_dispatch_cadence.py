"""Regression tests for the per-scanner dispatch cadence config.

These tests guard the data-structure constants in modal_workers/app.py — the
Modal-decorated dispatcher functions themselves require Modal runtime context
and are exercised in deployment.
"""
from __future__ import annotations

from modal_workers import app as dispatch_app


def test_fda_pdufa_secondary_hour_is_21_utc():
    """FDA scanner must fire at 21 UTC (post-close US) in addition to its
    DB-configured 13 UTC primary slot. Captures CRL announcements made between
    13:01 and 21:00 UTC same-day, which would otherwise wait ~23h for the next
    13 UTC bucket. See _SCANNERS_SECONDARY_HOUR comment for rationale."""
    assert "fda_pdufa_pipeline" in dispatch_app._SCANNERS_SECONDARY_HOUR.get(21, [])


def test_no_secondary_hour_overlap_with_fetchers():
    """A scanner shouldn't appear in both _FETCHERS_AT_HOUR and
    _SCANNERS_SECONDARY_HOUR for the same hour — would cause double-spawn
    even after dedup if names ever drift."""
    for hour, names in dispatch_app._SCANNERS_SECONDARY_HOUR.items():
        fetchers = set(dispatch_app._FETCHERS_AT_HOUR.get(hour, []))
        for name in names:
            assert name not in fetchers, (
                f"{name} listed in both _FETCHERS_AT_HOUR[{hour}] and "
                f"_SCANNERS_SECONDARY_HOUR[{hour}]"
            )


def test_secondary_hours_are_valid_dispatch_hours():
    """Every hour in _SCANNERS_SECONDARY_HOUR must match one of the cron-fired
    dispatch_release_times hours (6, 8, 13, 17, 21). Otherwise the entry would
    silently never fire."""
    valid_hours = {6, 8, 13, 17, 21}
    for hour in dispatch_app._SCANNERS_SECONDARY_HOUR:
        assert hour in valid_hours, (
            f"_SCANNERS_SECONDARY_HOUR has hour {hour} which isn't in the "
            f"dispatch_release_times cron schedule {sorted(valid_hours)}; "
            f"the entry would never fire."
        )
