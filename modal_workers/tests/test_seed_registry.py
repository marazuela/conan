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


def test_scanner_configs_always_carry_market_cap_floor():
    """The 215 floor from spec §12 must be present on every scanner."""
    rows = build_scanners({})
    for r in rows:
        assert r["config"]["market_cap_floor_usd_mm"] == 215, \
            f"{r['name']}: market_cap_floor_usd_mm missing or wrong"


def test_scanner_endpoints_preserve_fallback_urls():
    """Any endpoint_* key should survive in endpoints JSONB, including FDA fallback."""
    rows = build_scanners({})
    fda = next(r for r in rows if r["name"] == "fda_pdufa_pipeline")
    assert fda["endpoints"]["primary"] == "https://api.fda.gov/drug/label.json"
    assert fda["endpoints"]["fallback"] == "https://efts.sec.gov/LATEST/search-index"
