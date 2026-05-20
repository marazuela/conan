"""
Polygon provider adapter tests.

All tests mock requests.Session — no live network calls. Failure modes covered:
  - 404 returns None
  - 429 / 500 retries with backoff (exhausted -> raises)
  - Illiquid options chain returns None
  - Empty news returns None / [] correctly
"""

from __future__ import annotations

import os
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

from modal_workers.providers.polygon.base import (
    POLYGON_BASE,
    PolygonClient,
    PolygonError,
)
from modal_workers.providers.polygon.market_data import PolygonMarketData
from modal_workers.providers.polygon.news_data import PolygonNewsData
from modal_workers.providers.polygon.options_data import PolygonOptionsData


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _fake_session(responses):
    """Build a Session whose .request() returns the given queue of responses."""
    queue = list(responses)
    session = MagicMock(spec=requests.Session)

    def _request(method, url, params=None, timeout=None):
        if not queue:
            raise AssertionError(f"unexpected request {method} {url}")
        return queue.pop(0)

    session.request.side_effect = _request
    return session, queue


@pytest.fixture(autouse=True)
def _polygon_env(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")


@pytest.fixture
def client_factory():
    def make(responses):
        session, queue = _fake_session(responses)
        client = PolygonClient(session=session)
        return client, session, queue
    return make


# ---------------------------------------------------------------------------
# PolygonClient
# ---------------------------------------------------------------------------


def test_client_requires_api_key(monkeypatch):
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="POLYGON_API_KEY"):
        PolygonClient()


def test_client_404_returns_none(client_factory):
    client, _, _ = client_factory([_FakeResponse(404)])
    assert client.get("/v2/aggs/ticker/MISSING/prev") is None


def test_client_5xx_retries_then_raises(client_factory):
    client, _, _ = client_factory([
        _FakeResponse(500, text="boom"),
        _FakeResponse(500, text="still"),
        _FakeResponse(500, text="dead"),
    ])
    with patch("modal_workers.providers.polygon.base.time.sleep"):
        with pytest.raises(PolygonError) as exc:
            client.get("/v2/aggs/ticker/X/prev")
    assert exc.value.status == 500


def test_client_5xx_then_200_ok(client_factory):
    client, _, _ = client_factory([
        _FakeResponse(500, text="transient"),
        _FakeResponse(200, payload={"results": [{"c": 50.0}]}),
    ])
    with patch("modal_workers.providers.polygon.base.time.sleep"):
        body = client.get("/v2/aggs/ticker/X/prev")
    assert body == {"results": [{"c": 50.0}]}


def test_client_429_retries(client_factory):
    client, _, _ = client_factory([
        _FakeResponse(429),
        _FakeResponse(200, payload={"ok": True}),
    ])
    with patch("modal_workers.providers.polygon.base.time.sleep"):
        body = client.get("/v2/foo")
    assert body == {"ok": True}


def test_client_400_no_retry_raises(client_factory):
    client, _, _ = client_factory([_FakeResponse(400, text="bad request")])
    with pytest.raises(PolygonError) as exc:
        client.get("/v2/foo")
    assert exc.value.status == 400


def test_client_injects_api_key(client_factory):
    client, session, _ = client_factory([_FakeResponse(200, payload={"ok": True})])
    client.get("/v2/foo", params={"x": "1"})
    call = session.request.call_args
    assert call.kwargs["params"]["apiKey"] == "test-key"
    assert call.kwargs["params"]["x"] == "1"


def test_client_paginate_follows_next_url(client_factory):
    page1 = {
        "results": [{"id": 1}, {"id": 2}],
        "next_url": f"{POLYGON_BASE}/v3/snapshot/options/AXSM?cursor=abc",
    }
    page2 = {"results": [{"id": 3}]}
    client, _, _ = client_factory([
        _FakeResponse(200, payload=page1),
        _FakeResponse(200, payload=page2),
    ])
    pages = list(client.paginate("/v3/snapshot/options/AXSM"))
    ids = [r["id"] for p in pages for r in p["results"]]
    assert ids == [1, 2, 3]


# ---------------------------------------------------------------------------
# PolygonMarketData
# ---------------------------------------------------------------------------


def test_market_get_quote_extracts_ohlc(client_factory):
    client, _, _ = client_factory([
        _FakeResponse(200, payload={"results": [
            {"c": 50.5, "o": 50.0, "h": 51.0, "l": 49.5, "v": 1_000_000, "vw": 50.2, "t": 1700000000000}
        ]})
    ])
    md = PolygonMarketData(client)
    q = md.get_quote("AXSM")
    assert q["ticker"] == "AXSM"
    assert q["close"] == 50.5
    assert q["volume"] == 1_000_000


def test_market_get_quote_404_returns_none(client_factory):
    client, _, _ = client_factory([_FakeResponse(404)])
    md = PolygonMarketData(client)
    assert md.get_quote("ZZZZ") is None


def test_market_market_cap_returns_float(client_factory):
    client, _, _ = client_factory([
        _FakeResponse(200, payload={"results": {"market_cap": 12_345_678_900}})
    ])
    md = PolygonMarketData(client)
    assert md.get_market_cap("AXSM") == 12_345_678_900.0


def test_market_get_adv_averages_dollar_volume(client_factory):
    client, _, _ = client_factory([
        _FakeResponse(200, payload={"results": [
            {"c": 50.0, "v": 1_000_000},
            {"c": 52.0, "v": 2_000_000},
            {"c": 48.0, "v": 500_000},
        ]})
    ])
    md = PolygonMarketData(client)
    adv = md.get_adv("AXSM", days=30)
    expected = (50.0 * 1_000_000 + 52.0 * 2_000_000 + 48.0 * 500_000) / 3
    assert adv == pytest.approx(expected)


def test_market_get_adv_empty_returns_none(client_factory):
    client, _, _ = client_factory([_FakeResponse(200, payload={"results": []})])
    md = PolygonMarketData(client)
    assert md.get_adv("AXSM") is None


# ---------------------------------------------------------------------------
# PolygonOptionsData
# ---------------------------------------------------------------------------


def _contract(strike: float, kind: str, expiry: str, *, bid=1.0, ask=1.2, iv=0.85, oi=500):
    return {
        "details": {
            "contract_type": kind,
            "strike_price": strike,
            "expiration_date": expiry,
            "ticker": f"O:TST{expiry.replace('-','')}{kind[0].upper()}{int(strike*1000):08d}",
        },
        "implied_volatility": iv,
        "open_interest": oi,
        "last_quote": {"bid": bid, "ask": ask, "midpoint": (bid + ask) / 2},
        "underlying_asset": {"price": 50.0},
    }


def test_options_chain_too_small_returns_none_from_straddle(client_factory):
    # Only 2 contracts; below MIN_LIQUID_CONTRACTS (5).
    client, _, _ = client_factory([
        _FakeResponse(200, payload={"results": [
            _contract(50, "call", "2026-09-19"),
            _contract(50, "put", "2026-09-19"),
        ]})
    ])
    od = PolygonOptionsData(client)
    assert od.get_straddle_implied_move("AXSM", date(2026, 9, 15)) is None


def test_options_straddle_picks_atm_pair(client_factory):
    underlying = 50.0
    contracts = [
        _contract(45, "call", "2026-09-19", bid=6.0, ask=6.4),
        _contract(50, "call", "2026-09-19", bid=2.0, ask=2.2),
        _contract(55, "call", "2026-09-19", bid=0.6, ask=0.8),
        _contract(45, "put",  "2026-09-19", bid=0.8, ask=1.0),
        _contract(50, "put",  "2026-09-19", bid=2.1, ask=2.3),
        _contract(55, "put",  "2026-09-19", bid=5.5, ask=5.9),
    ]
    client, _, _ = client_factory([_FakeResponse(200, payload={"results": contracts})])
    od = PolygonOptionsData(client)
    out = od.get_straddle_implied_move("AXSM", date(2026, 9, 15))
    assert out is not None
    assert out["call_strike"] == 50.0
    assert out["put_strike"] == 50.0
    expected_call_mid = (2.0 + 2.2) / 2  # bid+ask of strike-50 call
    expected_put_mid = (2.1 + 2.3) / 2   # bid+ask of strike-50 put
    expected_straddle = expected_call_mid + expected_put_mid
    assert out["call_mid"] == pytest.approx(expected_call_mid)
    assert out["put_mid"] == pytest.approx(expected_put_mid)
    assert out["straddle_price"] == pytest.approx(expected_straddle)
    assert out["implied_move_pct"] == pytest.approx(expected_straddle / underlying * 100.0)


def test_options_straddle_all_expiries_before_event_returns_none(client_factory):
    contracts = [
        _contract(50, "call", "2026-08-15"),
        _contract(50, "put", "2026-08-15"),
        _contract(50, "call", "2026-08-22"),
        _contract(50, "put", "2026-08-22"),
        _contract(45, "call", "2026-08-15"),
    ]  # all expire BEFORE event_date 2026-09-15
    client, _, _ = client_factory([_FakeResponse(200, payload={"results": contracts})])
    od = PolygonOptionsData(client)
    assert od.get_straddle_implied_move("AXSM", date(2026, 9, 15)) is None


def test_options_event_window_liquidity_score_high():
    client = MagicMock()
    deep_chain = []
    for strike in (40, 45, 50, 55, 60, 65, 70, 75, 80, 85):
        deep_chain.append(_contract(strike, "call", "2026-09-19", oi=1500))
        deep_chain.append(_contract(strike, "put",  "2026-09-19", oi=1500))
    client.paginate.return_value = iter([{"results": deep_chain}])
    od = PolygonOptionsData(client)
    out = od.get_event_window_liquidity("AXSM", date(2026, 9, 15))
    assert out["contract_count"] == 20
    assert out["total_open_interest"] == 30000
    assert out["liquidity_score"] == 5.0


def test_options_event_window_liquidity_score_zero():
    client = MagicMock()
    client.paginate.return_value = iter([{"results": []}])
    od = PolygonOptionsData(client)
    assert od.get_event_window_liquidity("AXSM", date(2026, 9, 15)) is None


def test_options_iv_lookup_call_only(client_factory):
    contracts = [
        _contract(50, "call", "2026-09-19", iv=0.91),
        _contract(50, "put", "2026-09-19", iv=0.99),
    ]
    client, _, _ = client_factory([_FakeResponse(200, payload={"results": contracts})])
    od = PolygonOptionsData(client)
    assert od.get_iv("AXSM", 50.0, date(2026, 9, 19)) == pytest.approx(0.91)


# ---------------------------------------------------------------------------
# PolygonNewsData
# ---------------------------------------------------------------------------


def test_news_normalizes_response_shape(client_factory):
    client, _, _ = client_factory([
        _FakeResponse(200, payload={"results": [
            {
                "id": "abc",
                "title": "FDA accepts BLA",
                "publisher": {"name": "BiotechWire"},
                "published_utc": "2026-04-15T12:00:00Z",
                "article_url": "https://example.com/a",
                "tickers": ["AXSM"],
                "keywords": ["FDA", "PDUFA"],
                "description": "Summary",
            }
        ]})
    ])
    nd = PolygonNewsData(client)
    out = nd.get_news("AXSM", limit=10)
    assert len(out) == 1
    assert out[0]["publisher"] == "BiotechWire"
    assert out[0]["tickers"] == ["AXSM"]


def test_news_404_returns_none(client_factory):
    client, _, _ = client_factory([_FakeResponse(404)])
    nd = PolygonNewsData(client)
    assert nd.get_news("ZZZZ") is None


def test_news_empty_results_returns_empty_list(client_factory):
    client, _, _ = client_factory([_FakeResponse(200, payload={"results": []})])
    nd = PolygonNewsData(client)
    assert nd.get_news("AXSM") == []


# ---------------------------------------------------------------------------
# Per-instance caching — bridge perf regression guards
# ---------------------------------------------------------------------------


def test_market_cap_cached_per_instance(client_factory):
    client, session, _ = client_factory([
        _FakeResponse(200, payload={"results": {"market_cap": 100.0}}),
    ])
    md = PolygonMarketData(client)
    assert md.get_market_cap("AXSM") == 100.0
    assert md.get_market_cap("AXSM") == 100.0  # cache hit, no second response queued
    assert session.request.call_count == 1


def test_market_cap_caches_none_result(client_factory):
    client, session, _ = client_factory([_FakeResponse(404)])
    md = PolygonMarketData(client)
    assert md.get_market_cap("ZZZZ") is None
    assert md.get_market_cap("ZZZZ") is None
    assert session.request.call_count == 1


def test_adv_cached_per_ticker_days_pair(client_factory):
    client, session, _ = client_factory([
        _FakeResponse(200, payload={"results": [
            {"c": 50.0, "v": 1_000_000},
            {"c": 52.0, "v": 2_000_000},
        ]}),
    ])
    md = PolygonMarketData(client)
    first = md.get_adv("AXSM", days=30)
    second = md.get_adv("AXSM", days=30)
    assert first is not None and first == second
    assert session.request.call_count == 1


def test_options_get_chain_cached_per_ticker_expiry():
    client = MagicMock()
    contracts = [_contract(50, "call", "2026-09-19"), _contract(50, "put", "2026-09-19")]
    client.paginate.return_value = iter([{"results": contracts}])
    od = PolygonOptionsData(client)
    first = od.get_chain("AXSM")
    second = od.get_chain("AXSM")
    assert first == second
    # paginate is consumed once on the first call; the second hits the cache.
    assert client.paginate.call_count == 1


def test_options_straddle_and_liquidity_share_chain_cache():
    # Straddle and liquidity both call get_chain(ticker) — verify one fetch suffices.
    client = MagicMock()
    deep_chain = []
    for strike in (40, 45, 50, 55, 60, 65, 70, 75, 80, 85):
        deep_chain.append(_contract(strike, "call", "2026-09-19", oi=1500))
        deep_chain.append(_contract(strike, "put", "2026-09-19", oi=1500))
    client.paginate.return_value = iter([{"results": deep_chain}])
    od = PolygonOptionsData(client)
    straddle = od.get_straddle_implied_move("AXSM", date(2026, 9, 15))
    liquidity = od.get_event_window_liquidity("AXSM", date(2026, 9, 15))
    assert straddle is not None
    assert liquidity is not None
    assert client.paginate.call_count == 1
