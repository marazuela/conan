"""
Tests for market_snapshot cache buckets, liveness stamping, and skeleton
behavior when upstream is unavailable.

The module's yfinance call is not exercised here — we only test the cache/age
logic and the "always return a skeleton with source_liveness" contract. Live
yfinance fetches are covered by manual QA.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List

import pytest

from modal_workers.shared import market_snapshot


class FakeCacheClient:
    """Minimal stand-in for SupabaseClient with just the cache surface."""

    def __init__(self) -> None:
        self.store: Dict[str, bytes] = {}
        self.writes: List[Dict[str, Any]] = []

    def read_cache(self, bucket: str, path: str, timeout: float = 4.0):
        return self.store.get(f"{bucket}/{path}")

    def write_cache(self, bucket: str, path: str, body: bytes, content_type: str = "application/json"):
        self.store[f"{bucket}/{path}"] = body
        self.writes.append({"bucket": bucket, "path": path, "content_type": content_type})


def _seed_cache(client: FakeCacheClient, key: str, snapshot: Dict[str, Any], cached_at: float) -> None:
    payload = {"cached_at": cached_at, "snapshot": snapshot}
    client.store[f"market-snapshots/{key}.json"] = json.dumps(payload).encode("utf-8")


@pytest.fixture(autouse=True)
def _reset_memo():
    market_snapshot._MEMO.clear()
    yield
    market_snapshot._MEMO.clear()


def _base_snapshot() -> Dict[str, Any]:
    return {
        "market_snapshot_source": "yfinance",
        "market_snapshot_symbol": "ACME",
        "market_snapshot_at": "2026-04-22T00:00:00Z",
        "adv_usd": 5_000_000.0,
        "market_cap_usd": 100_000_000.0,
        "valuation_cushion_pct": 12.5,
        "price_vs_5y_median_pct": 12.5,
    }


def test_cache_hit_within_fresh_ttl_is_live():
    client = FakeCacheClient()
    key = market_snapshot._cache_key("ACME", None)
    _seed_cache(client, key, _base_snapshot(), cached_at=time.time() - 120)

    snap = market_snapshot.load_market_snapshot("ACME", client=client)

    assert snap is not None
    assert snap["source_liveness"] == "live"
    assert snap["age_seconds"] >= 120
    assert snap["adv_usd"] == 5_000_000.0


def test_cache_hit_between_fresh_and_serve_stale_is_stale_served():
    client = FakeCacheClient()
    key = market_snapshot._cache_key("ACME", None)
    _seed_cache(client, key, _base_snapshot(), cached_at=time.time() - (market_snapshot.FRESH_TTL_S + 600))

    snap = market_snapshot.load_market_snapshot("ACME", client=client)

    assert snap is not None
    assert snap["source_liveness"] == "stale_served"
    assert snap["age_seconds"] >= market_snapshot.FRESH_TTL_S


def test_cache_hit_beyond_serve_stale_ttl_is_miss(monkeypatch):
    client = FakeCacheClient()
    key = market_snapshot._cache_key("ACME", None)
    _seed_cache(client, key, _base_snapshot(), cached_at=time.time() - (market_snapshot.SERVE_STALE_TTL_S + 60))

    # Prevent a real yfinance fetch from trying to run.
    monkeypatch.setattr(market_snapshot, "_fetch_market_snapshot",
                        lambda ticker, mic: market_snapshot._unavailable_snapshot(ticker, mic))

    snap = market_snapshot.load_market_snapshot("ACME", client=client)

    # A too-old cache entry is treated as a miss; we fell through to fetch
    # and got an unavailable skeleton.
    assert snap is not None
    assert snap["source_liveness"] == "unavailable"


def test_unavailable_skeleton_on_fetch_failure(monkeypatch):
    client = FakeCacheClient()
    monkeypatch.setattr(market_snapshot, "_fetch_market_snapshot",
                        lambda ticker, mic: market_snapshot._unavailable_snapshot(ticker, mic))

    snap = market_snapshot.load_market_snapshot("ACME", client=client)

    assert snap is not None
    assert snap["source_liveness"] == "unavailable"
    assert snap["adv_usd"] is None
    assert snap["market_cap_usd"] is None
    assert snap["valuation_cushion_pct"] is None
    assert snap["market_snapshot_symbol"] == "ACME"


def test_unavailable_snapshot_is_not_persisted_to_cache(monkeypatch):
    client = FakeCacheClient()
    monkeypatch.setattr(market_snapshot, "_fetch_market_snapshot",
                        lambda ticker, mic: market_snapshot._unavailable_snapshot(ticker, mic))

    market_snapshot.load_market_snapshot("ACME", client=client)

    assert client.writes == [], "unavailable snapshot must not be cached"


def test_live_fetch_is_persisted_to_cache(monkeypatch):
    client = FakeCacheClient()
    live_snapshot = {
        **_base_snapshot(),
        "source_liveness": "live",
        "age_seconds": 0,
    }
    monkeypatch.setattr(market_snapshot, "_fetch_market_snapshot",
                        lambda ticker, mic: live_snapshot)

    snap = market_snapshot.load_market_snapshot("ACME", client=client)

    assert snap["source_liveness"] == "live"
    assert len(client.writes) == 1
    assert client.writes[0]["bucket"] == "market-snapshots"


def test_empty_ticker_returns_none():
    assert market_snapshot.load_market_snapshot("") is None


def test_memoized_call_returns_same_object(monkeypatch):
    client = FakeCacheClient()
    live_snapshot = {
        **_base_snapshot(),
        "source_liveness": "live",
        "age_seconds": 0,
    }
    calls = {"n": 0}

    def fake_fetch(ticker, mic):
        calls["n"] += 1
        return live_snapshot

    monkeypatch.setattr(market_snapshot, "_fetch_market_snapshot", fake_fetch)

    a = market_snapshot.load_market_snapshot("ACME", client=client)
    b = market_snapshot.load_market_snapshot("ACME", client=client)

    assert calls["n"] == 1
    assert a is b


def test_unavailable_fetch_is_not_memoized_and_recovers(monkeypatch):
    """Regression: a transient upstream outage must not poison _MEMO for the
    worker pod's lifetime. After the fetcher recovers, the next call should
    re-fetch and return live, not serve the sticky unavailable skeleton."""
    client = FakeCacheClient()
    live_snapshot = {
        **_base_snapshot(),
        "source_liveness": "live",
        "age_seconds": 0,
    }
    calls = {"n": 0}

    def flaky_fetch(ticker, mic):
        calls["n"] += 1
        if calls["n"] == 1:
            return market_snapshot._unavailable_snapshot(ticker, mic)
        return live_snapshot

    monkeypatch.setattr(market_snapshot, "_fetch_market_snapshot", flaky_fetch)

    first = market_snapshot.load_market_snapshot("ACME", client=client)
    assert first is not None
    assert first["source_liveness"] == "unavailable"

    key = market_snapshot._cache_key("ACME", None)
    assert key not in market_snapshot._MEMO, "unavailable must not be memoized"

    second = market_snapshot.load_market_snapshot("ACME", client=client)
    assert second is not None
    assert second["source_liveness"] == "live", "second call must recover to live fetch"
    assert calls["n"] == 2, "fetcher must be re-invoked after unavailable"
