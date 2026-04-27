"""Pagination resilience tests for congressional_trading scanner (F-108).

The pre-fix _fetch_trades broke immediately on:
  - any fetch / parse exception (one transient blip ended the scan)
  - the first empty page (one HTML render glitch silently dropped pages 4..N)

The fixed _fetch_trades retries fetch+parse once with a backoff and tolerates
one empty page (empty_streak=1) before stopping. These tests lock that behaviour.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from requests.exceptions import ConnectionError as ReqConnError

from modal_workers.scanners import congressional_trading as ct


def _row(**overrides: Any) -> Dict[str, str]:
    """Capitol-Trades cell shape. _extract_ticker needs the issuer cell to contain
    `TICKER:US` (or `TICKER:XX` for any 2-letter MIC), per congressional_trading.py:270.
    """
    base = {
        "politician": "Senator Foo (R-TX)",
        "issuer": "Tesla Inc TSLA:US",
        "_unused2": "ignored",
        "date": "2026-04-25",
        "_unused4": "ignored",
        "owner": "self",
        "trade": "buy",
        "size": "$1K - $15K",
    }
    base.update(overrides)
    return base


def _page_html(rows: List[Dict[str, str]]) -> str:
    """Capitol-Trades-shaped HTML with the 8 cells _fetch_trades reads."""
    if not rows:
        return (
            "<html><body><table><thead><tr><th>h</th></tr></thead>"
            "<tbody></tbody></table></body></html>"
        )
    body = ""
    for r in rows:
        body += "<tr>" + "".join(
            f"<td>{r[k]}</td>"
            for k in [
                "politician", "issuer", "_unused2", "date",
                "_unused4", "owner", "trade", "size",
            ]
        ) + "</tr>"
    return (
        "<html><body><table>"
        "<thead><tr><th>h</th></tr></thead>"
        f"<tbody>{body}</tbody></table></body></html>"
    )


def _make_response(html: str) -> MagicMock:
    resp = MagicMock()
    resp.text = html
    resp.raise_for_status.return_value = None
    return resp


def _patches(side_effects: Any):
    return (
        patch.object(ct.requests, "get", side_effect=side_effects),
        patch.object(ct.time, "sleep"),
    )


def _run(side_effects, max_pages: int = 10) -> tuple[List[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    deadline = time.time() + 100
    get_patch, sleep_patch = _patches(side_effects)
    with get_patch, sleep_patch:
        trades = ct._fetch_trades(max_pages=max_pages, budget_deadline=deadline,
                                  warnings=warnings)
    return trades, warnings


class TestFetchTradesPagination:
    def test_full_then_empty_terminates_cleanly(self):
        """Page 1 full, page 2 empty, page 3 empty → stop after empty_streak=2."""
        trades, warnings = _run([
            _make_response(_page_html([_row()] * 12)),  # page 1: 12 rows
            _make_response(_page_html([])),             # page 2: empty
            _make_response(_page_html([])),             # page 3: empty (streak=2 → break)
        ])
        assert len(trades) == 12
        assert sum("has no rows" in w for w in warnings) == 2

    def test_sparse_middle_empty_does_not_terminate(self):
        """F-108 regression guard: full → empty → full → empty → empty.
        Old behaviour stopped at the first empty page and lost page 3's data."""
        trades, _ = _run([
            _make_response(_page_html([_row()] * 12)),  # page 1: full
            _make_response(_page_html([])),             # page 2: transient empty
            _make_response(_page_html([_row()] * 12)),  # page 3: full again
            _make_response(_page_html([])),             # page 4: empty (streak=1)
            _make_response(_page_html([])),             # page 5: empty (streak=2 → break)
        ])
        assert len(trades) == 24

    def test_transient_fetch_failure_recovers_via_retry(self):
        """One ConnectionError on attempt 1 → retry succeeds on attempt 2.
        No data lost, no premature break."""
        trades, warnings = _run([
            ReqConnError("transient"),                  # page 1 attempt 1: fails
            _make_response(_page_html([_row()] * 12)),  # page 1 attempt 2: success
            _make_response(_page_html([])),             # page 2: empty
            _make_response(_page_html([])),             # page 3: empty (streak=2 → break)
        ])
        assert len(trades) == 12
        # No "fetch failed after retry" warning — the retry succeeded.
        assert not any("failed after retry" in w for w in warnings)

    def test_persistent_fetch_failure_breaks_after_one_retry(self):
        """Both attempts on page 1 fail → break with explicit warning."""
        trades, warnings = _run(ReqConnError("persistent"))
        assert trades == []
        assert any(
            "fetch/parse page 1 failed after retry" in w and "ConnectionError" in w
            for w in warnings
        )

    def test_no_table_breaks_with_helpful_warning(self):
        """No <table> on page (bot protection or schema change) → break with hint."""
        trades, warnings = _run([
            _make_response("<html><body><div>access denied</div></body></html>"),
        ])
        assert trades == []
        assert any(
            "no table on page 1" in w and "bot protection" in w for w in warnings
        )

    def test_wall_clock_budget_breaks_immediately(self):
        """budget_deadline already past → loop never fetches."""
        warnings: List[str] = []
        get_mock = MagicMock()
        with patch.object(ct.requests, "get", get_mock), \
                patch.object(ct.time, "sleep"):
            trades = ct._fetch_trades(
                max_pages=10,
                budget_deadline=time.time() - 1.0,
                warnings=warnings,
            )
        assert trades == []
        get_mock.assert_not_called()
        assert any("wall-clock budget exceeded" in w for w in warnings)

    def test_max_pages_one_does_not_polite_delay(self):
        """If only one page is requested, no SCRAPE_DELAY sleep should fire."""
        sleep_mock = MagicMock()
        with patch.object(ct.requests, "get", side_effect=[
                _make_response(_page_html([_row()] * 12))]), \
                patch.object(ct.time, "sleep", sleep_mock):
            trades = ct._fetch_trades(
                max_pages=1, budget_deadline=time.time() + 100,
                warnings=[],
            )
        assert len(trades) == 12
        # sleep may still be called by retry-on-fail (didn't fire here) but not for
        # the polite inter-page delay since page < max_pages is False.
        sleep_mock.assert_not_called()


class TestFetchAndParseHelper:
    """The helper isolated from the page loop."""

    def test_succeeds_on_first_attempt(self):
        warnings: List[str] = []
        with patch.object(ct.requests, "get",
                          return_value=_make_response("<html><body>ok</body></html>")):
            soup = ct._fetch_and_parse(
                "http://x", {"User-Agent": "t"}, warnings, page=1,
            )
        assert soup is not None
        assert warnings == []

    def test_succeeds_on_retry(self):
        warnings: List[str] = []
        with patch.object(ct.requests, "get", side_effect=[
                ReqConnError("blip"),
                _make_response("<html><body>ok</body></html>"),
        ]), patch.object(ct.time, "sleep"):
            soup = ct._fetch_and_parse(
                "http://x", {"User-Agent": "t"}, warnings, page=2,
            )
        assert soup is not None
        assert warnings == []

    def test_returns_none_after_two_failures(self):
        warnings: List[str] = []
        with patch.object(ct.requests, "get", side_effect=ReqConnError("dead")), \
                patch.object(ct.time, "sleep"):
            soup = ct._fetch_and_parse(
                "http://x", {"User-Agent": "t"}, warnings, page=3,
            )
        assert soup is None
        assert len(warnings) == 1
        assert "page 3 failed after retry" in warnings[0]
