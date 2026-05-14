"""Tests for the sonnet_fact_extractor return-code contract.

Focused on the rc=3 exhaustion signal added 2026-05-11 alongside the lazy
fact-extraction hook in orchestrator_app — the rc lets the hook fail loud
when credits/quota are out instead of letting synthesis proceed against
zero new facts.

Run: python3 -m pytest modal_workers/tests/test_sonnet_fact_extractor.py -v
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
# Force-assign (not setdefault) because the parent env may have the key
# set to an empty string, which would defeat setdefault while still
# tripping the extractor's `if not key:` guard.
if not os.environ.get("ANTHROPIC_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = "x"

import anthropic  # noqa: E402

from modal_workers.extractor import sonnet_fact_extractor as extractor  # noqa: E402


class _FakeAPIError(anthropic.APIError):
    """anthropic.APIError requires a real httpx.Request — bypass __init__
    so tests don't depend on httpx internals."""
    def __init__(self, msg: str) -> None:
        Exception.__init__(self, msg)


def _stub_dependencies(monkeypatch,
                       *,
                       links: List[Dict[str, Any]],
                       extract_side_effect,
                       insert_returns: int = 0) -> Dict[str, Any]:
    """Stub out Supabase / Anthropic / loaders. Returns a captures dict the
    test can inspect."""
    captures: Dict[str, Any] = {"insert_calls": 0}

    class _FakeSB:
        def __init__(self) -> None:
            pass

    monkeypatch.setattr(extractor, "SupabaseClient", _FakeSB)
    monkeypatch.setattr(extractor.anthropic, "Anthropic", lambda: object())

    monkeypatch.setattr(
        extractor, "load_unextracted_links",
        lambda sb, asset_id=None, max_links=200: links,
    )
    monkeypatch.setattr(
        extractor, "load_asset",
        lambda sb, asset_id: {"id": asset_id, "drug_name": "X", "indication": "Y"},
    )
    monkeypatch.setattr(
        extractor, "load_doc_with_text",
        lambda sb, document_id: {
            "id": document_id, "source": "edgar", "doc_type": "10K",
            "raw_text": "some text", "url": "https://x", "title": "t",
        },
    )

    def _fake_extract(_a_client, _doc, _asset, _link):
        if isinstance(extract_side_effect, Exception):
            raise extract_side_effect
        return extract_side_effect

    monkeypatch.setattr(extractor, "extract_facts", _fake_extract)

    def _fake_insert(sb, asset_id, document_id, facts):
        captures["insert_calls"] += 1
        return insert_returns

    monkeypatch.setattr(extractor, "insert_facts", _fake_insert)
    return captures


def test_main_returns_3_when_all_docs_error(monkeypatch):
    """rc=3 contract: docs_seen > 0, every attempt errored, 0 facts inserted."""
    _stub_dependencies(
        monkeypatch,
        links=[
            {"asset_id": "a1", "document_id": "d1",
             "link_type": "primary", "extraction_confidence": 0.9,
             "extracted_spans": []},
            {"asset_id": "a1", "document_id": "d2",
             "link_type": "primary", "extraction_confidence": 0.9,
             "extracted_spans": []},
        ],
        extract_side_effect=_FakeAPIError("credits exhausted"),
    )

    # Avoid the 2.0s sleep on every APIError.
    monkeypatch.setattr(extractor.time, "sleep", lambda *_: None)

    rc = extractor.main(["--asset-id", "a1", "--max", "10", "--budget-usd", "1.0"])
    assert rc == 3


def test_main_returns_0_when_no_unextracted_docs(monkeypatch):
    """rc=0 (benign): nothing to do — lazy hook should NOT fail-loud."""
    _stub_dependencies(
        monkeypatch,
        links=[],
        extract_side_effect=([], 0, 0),
    )

    rc = extractor.main(["--asset-id", "a1", "--max", "10", "--budget-usd", "1.0"])
    assert rc == 0


def test_main_returns_0_on_partial_success(monkeypatch):
    """rc=0: one doc errored, one inserted facts — partial success is success
    for the lazy hook's purposes (don't fail the whole assessment)."""
    call_count = {"n": 0}

    def _flaky_extract(_a_client, _doc, _asset, _link):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _FakeAPIError("rate limited on first doc")
        # Second doc succeeds with one fact
        return (
            [{"fact_type": "pdufa_date", "fact_text": "x",
              "evidence_quote": "y", "citation_span": {"start": 0, "end": 1},
              "confidence": 0.9}],
            100, 50,
        )

    _stub_dependencies(
        monkeypatch,
        links=[
            {"asset_id": "a1", "document_id": "d1",
             "link_type": "primary", "extraction_confidence": 0.9,
             "extracted_spans": []},
            {"asset_id": "a1", "document_id": "d2",
             "link_type": "primary", "extraction_confidence": 0.9,
             "extracted_spans": []},
        ],
        extract_side_effect=None,
        insert_returns=1,
    )
    monkeypatch.setattr(extractor, "extract_facts", _flaky_extract)
    monkeypatch.setattr(extractor.time, "sleep", lambda *_: None)

    rc = extractor.main(["--asset-id", "a1", "--max", "10", "--budget-usd", "1.0"])
    assert rc == 0


def test_main_returns_2_when_api_key_missing(monkeypatch):
    """rc=2 contract is unchanged — missing key is a config bug, not exhaustion."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rc = extractor.main(["--asset-id", "a1"])
    assert rc == 2
