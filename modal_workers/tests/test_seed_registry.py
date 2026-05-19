"""
Regression tests for migrations/seed_registry.py.

Locks the config-passthrough contract: the seed script must pull `requires_auth`,
`probe_skip_reason`, `notes`, and `strategy_spec` from scanner_registry.json into
scanners.config, not just `market_cap_floor_usd_mm`. Without these, re-running
the seed wipes the fields that observability.scanner_probe reads to decide
which scanners to skip.

Run: python -m pytest modal_workers/tests/test_seed_registry.py -v
"""
from __future__ import annotations

from migrations.seed_registry import build_scanners


def test_bse_nse_config_carries_probe_skip_reason():
    """bse_nse_scanner must seed with probe_skip_reason so scanner_probe skips it."""
    rows = build_scanners({})
    bse = next(r for r in rows if r["name"] == "bse_nse_scanner")
    assert "probe_skip_reason" in bse["config"], \
        "probe_skip_reason missing — scanner_probe will re-flag bse_nse as drift on next run"
    assert "geo_blocked" in bse["config"]["probe_skip_reason"]


def test_courtlistener_and_kind_config_carry_requires_auth():
    """courtlistener + kind must seed with requires_auth=true so scanner_probe skips them."""
    rows = build_scanners({})
    for name in ("courtlistener_scanner", "kind_scanner"):
        sc = next(r for r in rows if r["name"] == name)
        assert sc["config"].get("requires_auth") is True, \
            f"{name}: requires_auth missing — probe will 401 without the token and flag drift"


def test_scanner_configs_always_carry_market_cap_floor():
    """The 215 floor from spec §12 must be present on every scanner."""
    rows = build_scanners({})
    for r in rows:
        assert r["config"]["market_cap_floor_usd_mm"] == 215, \
            f"{r['name']}: market_cap_floor_usd_mm missing or wrong"


def test_scanner_configs_preserve_notes_and_strategy_spec_when_present():
    """notes + strategy_spec (where present in JSON) must flow through to config."""
    rows = build_scanners({})
    # bse_nse has both notes + strategy_spec in the JSON
    bse = next(r for r in rows if r["name"] == "bse_nse_scanner")
    assert "notes" in bse["config"]
    assert bse["config"]["strategy_spec"] == "strategies/in_bse_nse.md"


def test_scanner_configs_preserve_filter_excluded_filers():
    """Scanner-specific list config must survive seeding, not just the explicit passthrough keys."""
    rows = build_scanners({})
    congress = next(r for r in rows if r["name"] == "congressional_trading")
    assert congress["config"]["filter_excluded_filers"] == ["Ro Khanna"]


def test_esma_short_caps_seed_into_config():
    """The short scanner's ranking + promotion caps must be configurable at runtime."""
    rows = build_scanners({})
    esma = next(r for r in rows if r["name"] == "esma_short_scanner")
    assert esma["config"]["top_signal_limit"] == 25
    assert esma["config"]["daily_promotion_limit"] == 5


def test_scanner_endpoints_preserve_fallback_urls():
    """Any endpoint_* key should survive in endpoints JSONB, including FDA fallback."""
    rows = build_scanners({})
    fda = next(r for r in rows if r["name"] == "fda_pdufa_pipeline")
    assert fda["endpoints"]["primary"] == "https://api.fda.gov/drug/label.json"
    assert fda["endpoints"]["fallback"] == "https://efts.sec.gov/LATEST/search-index"
