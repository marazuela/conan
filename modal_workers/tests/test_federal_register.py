"""
Tests for the Federal Register adapter. All tests mock requests.Session — no
live network calls.

Failure modes covered:
  - 404 returns None
  - 429 / 5xx retries with backoff (exhausted -> raises)
  - 4xx raises immediately
  - empty results -> [] (not None)
  - normalize() picks the documented subset
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

from modal_workers.providers.federal_register import (
    FEDERAL_REGISTER_BASE,
    FederalRegisterClient,
    FederalRegisterError,
)


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
    queue = list(responses)
    session = MagicMock(spec=requests.Session)

    def _request(method, url, params=None, headers=None, timeout=None):
        if not queue:
            raise AssertionError(f"unexpected request {method} {url}")
        return queue.pop(0)

    session.request.side_effect = _request
    return session


@pytest.fixture
def client_factory():
    def make(responses):
        session = _fake_session(responses)
        return FederalRegisterClient(session=session), session
    return make


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def test_404_returns_none(client_factory):
    client, _ = client_factory([_FakeResponse(404)])
    assert client.search("axsm") is None


def test_5xx_retries_then_raises(client_factory):
    client, _ = client_factory([
        _FakeResponse(500, text="boom"),
        _FakeResponse(500, text="still"),
        _FakeResponse(500, text="dead"),
    ])
    with patch("modal_workers.providers.federal_register.time.sleep"):
        with pytest.raises(FederalRegisterError) as exc:
            client.search("axsm")
    assert exc.value.status == 500


def test_5xx_then_200_ok(client_factory):
    payload = {"results": [{"document_number": "2026-1", "title": "FDA notice"}]}
    client, _ = client_factory([
        _FakeResponse(500, text="transient"),
        _FakeResponse(200, payload=payload),
    ])
    with patch("modal_workers.providers.federal_register.time.sleep"):
        out = client.search("axsm")
    assert out is not None
    assert out[0]["document_number"] == "2026-1"


def test_429_retries(client_factory):
    payload = {"results": []}
    client, _ = client_factory([
        _FakeResponse(429),
        _FakeResponse(200, payload=payload),
    ])
    with patch("modal_workers.providers.federal_register.time.sleep"):
        out = client.search("axsm")
    assert out == []


def test_4xx_no_retry_raises(client_factory):
    client, _ = client_factory([_FakeResponse(400, text="bad request")])
    with pytest.raises(FederalRegisterError) as exc:
        client.search("axsm")
    assert exc.value.status == 400


def test_attaches_user_agent(client_factory):
    client, session = client_factory([_FakeResponse(200, payload={"results": []})])
    client.search("axsm")
    call = session.request.call_args
    assert "User-Agent" in call.kwargs["headers"]
    assert "Conan/" in call.kwargs["headers"]["User-Agent"]


def test_custom_user_agent_overrides_default(client_factory):
    session = _fake_session([_FakeResponse(200, payload={"results": []})])
    client = FederalRegisterClient(session=session, user_agent="custom-ua/1.0")
    client.search("axsm")
    assert session.request.call_args.kwargs["headers"]["User-Agent"] == "custom-ua/1.0"


# ---------------------------------------------------------------------------
# search params
# ---------------------------------------------------------------------------


def test_search_threads_query_and_dates(client_factory):
    client, session = client_factory([_FakeResponse(200, payload={"results": []})])
    client.search(
        "axsm agitation",
        since=date(2026, 1, 1),
        until=date(2026, 4, 30),
        per_page=50,
        page=2,
    )
    params = session.request.call_args.kwargs["params"]
    assert params["conditions[term]"] == "axsm agitation"
    assert params["conditions[publication_date][gte]"] == "2026-01-01"
    assert params["conditions[publication_date][lte]"] == "2026-04-30"
    assert params["per_page"] == 50
    assert params["page"] == 2
    assert params["order"] == "newest"


def test_search_passes_agency_filter(client_factory):
    client, session = client_factory([_FakeResponse(200, payload={"results": []})])
    client.search("axsm", agencies=["food-and-drug-administration"])
    params = session.request.call_args.kwargs["params"]
    assert params["conditions[agencies][0]"] == "food-and-drug-administration"


def test_search_passes_multiple_document_types(client_factory):
    client, session = client_factory([_FakeResponse(200, payload={"results": []})])
    client.search("axsm", document_types=["NOTICE", "RULE"])
    params = session.request.call_args.kwargs["params"]
    assert params["conditions[type][0]"] == "NOTICE"
    assert params["conditions[type][1]"] == "RULE"


def test_search_per_page_clamped_to_max(client_factory):
    client, session = client_factory([_FakeResponse(200, payload={"results": []})])
    client.search("axsm", per_page=99999)
    params = session.request.call_args.kwargs["params"]
    assert params["per_page"] == 1000


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


def test_search_normalizes_full_record(client_factory):
    full_record = {
        "document_number": "2026-12345",
        "title": "Notice of Advisory Committee Meeting",
        "abstract": "AdCom for AXSM agitation indication",
        "publication_date": "2026-04-15",
        "type": "Notice",
        "agency_names": ["Food and Drug Administration"],
        "html_url": "https://www.federalregister.gov/documents/2026/04/15/2026-12345",
        "pdf_url": "https://www.federalregister.gov/documents/2026/04/15/2026-12345.pdf",
        "raw_text_url": "https://www.federalregister.gov/documents/2026/04/15/2026-12345/raw",
        "topics": ["Drugs", "Reporting and recordkeeping requirements"],
        # Fields the normalizer should drop
        "irrelevant_field": "ignore me",
        "page_views": 99,
    }
    client, _ = client_factory([_FakeResponse(200, payload={"results": [full_record]})])
    out = client.search("axsm")
    assert len(out) == 1
    record = out[0]
    assert record["document_number"] == "2026-12345"
    assert "Food and Drug Administration" in record["agency_names"]
    assert record["html_url"].startswith("https://www.federalregister.gov/")
    assert "irrelevant_field" not in record
    assert "page_views" not in record


def test_search_empty_results_returns_empty_list(client_factory):
    client, _ = client_factory([_FakeResponse(200, payload={"results": []})])
    assert client.search("nonsense") == []


def test_search_results_missing_returns_none(client_factory):
    client, _ = client_factory([_FakeResponse(200, payload={"count": 0})])
    assert client.search("nonsense") is None


# ---------------------------------------------------------------------------
# get_document
# ---------------------------------------------------------------------------


def test_get_document_normalizes(client_factory):
    record = {
        "document_number": "2026-9999",
        "title": "Final Rule on Drug Labeling",
        "publication_date": "2026-03-01",
        "type": "Rule",
        "agency_names": ["Food and Drug Administration"],
        "html_url": "https://example.gov/x",
        "topics": [],
    }
    client, _ = client_factory([_FakeResponse(200, payload=record)])
    out = client.get_document("2026-9999")
    assert out["document_number"] == "2026-9999"
    assert out["type"] == "Rule"


def test_get_document_404_returns_none(client_factory):
    client, _ = client_factory([_FakeResponse(404)])
    assert client.get_document("missing") is None


def test_default_base_url_constant():
    assert FEDERAL_REGISTER_BASE == "https://www.federalregister.gov/api/v1"
