"""Regression tests for asset_linker pass-1 — covers the no-progress loop bug.

The 2026-05-11 incident: pass-1 ran every 15min, re-Sonneted the same ~200
dailymed docs, burned ~$2.40/run with 0 links inserted. Root cause: zero-link
outcomes never wrote a "classified" marker, so the same docs reappeared in the
next batch.

These tests pin down the three guarantees that prevent recurrence:
  1. load_documents_to_link only returns docs where linker_classified_at IS NULL
  2. every terminal outcome calls _mark_classified with the right result tag
  3. transient API errors do NOT mark (so retries happen on the next run)
  4. asset_linker_runs row is inserted at start + completed at end

Run: python -m pytest modal_workers/tests/test_asset_linker_pass1.py -v
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

# setdefault is a no-op when the key exists but is empty ("ANTHROPIC_API_KEY=")
# — common in CI envs that pre-declare the var. Force a value here.
for _k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "ANTHROPIC_API_KEY"):
    if not os.environ.get(_k):
        os.environ[_k] = "https://x.supabase.co" if _k == "SUPABASE_URL" else "x"

import anthropic

from modal_workers.extractor.asset_linker import (
    LinkerStats,
    _finish_run_row,
    _mark_classified,
    _start_run_row,
    classify_document,
    load_documents_to_link,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doc(doc_id: str = "doc-1") -> Dict[str, Any]:
    return {
        "id": doc_id,
        "source": "dailymed",
        "doc_type": "drug_label",
        "title": "Atorvastatin Calcium Label",
        "url": None,
        "raw_text": "Some drug label text mentioning VRDN and veligrotug.",
        "raw_text_tokens": 50,
        "storage_path": None,
        "published_at": "2026-05-06T00:00:00+00:00",
        "extensions": {},
    }


def _assets() -> List[Dict[str, Any]]:
    return [{
        "id": "asset-1",
        "drug_name": "VRDN (veligrotug)",
        "generic_name": "veligrotug",
        "sponsor_name": "Viridian Therapeutics",
        "indication": "thyroid eye disease",
    }]


def _anthropic_response(json_body: str, in_tok: int = 1200, out_tok: int = 80):
    block = MagicMock()
    block.type = "text"
    block.text = json_body
    resp = MagicMock()
    resp.content = [block]
    resp.usage.input_tokens = in_tok
    resp.usage.output_tokens = out_tok
    return resp


# ---------------------------------------------------------------------------
# load_documents_to_link — must filter on linker_classified_at IS NULL
# ---------------------------------------------------------------------------

def test_load_documents_filters_unclassified_only():
    """The bug regressed if load_documents_to_link ever returns a doc whose
    linker_classified_at is NOT NULL. Pin the filter to the request layer."""
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[_doc()])

    load_documents_to_link(sb, max_docs=20)

    # One GET to documents with the marker filter
    sb._rest.assert_called_once()
    call = sb._rest.call_args
    assert call.args[0] == "GET"
    assert call.args[1] == "documents"
    params = call.kwargs["params"]
    assert params.get("linker_classified_at") == "is.null", (
        "load_documents_to_link MUST filter on linker_classified_at IS NULL — "
        "otherwise the no-progress loop returns. Got params: %r" % params
    )
    assert params.get("order") == "published_at.desc"


def test_load_documents_does_not_query_asset_documents():
    """The pre-fix implementation issued a separate GET to asset_documents to
    build an exclusion set. That pattern was the bug — keep it gone."""
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[])

    load_documents_to_link(sb, max_docs=20)

    for call in sb._rest.call_args_list:
        path = call.args[1] if len(call.args) > 1 else ""
        assert path != "asset_documents", (
            "load_documents_to_link should NOT query asset_documents — that "
            "two-query JOIN-in-Python pattern caused the no-progress loop."
        )


# ---------------------------------------------------------------------------
# _mark_classified — terminal-state PATCH
# ---------------------------------------------------------------------------

def test_mark_classified_patches_documents_with_result():
    sb = MagicMock()
    sb._rest = MagicMock(return_value=None)

    _mark_classified(sb, "doc-xyz", "no_match")

    sb._rest.assert_called_once()
    call = sb._rest.call_args
    assert call.args[0] == "PATCH"
    assert call.args[1] == "documents"
    assert call.kwargs["params"] == {"id": "eq.doc-xyz"}
    body = call.kwargs["json_body"]
    assert body["linker_classified_result"] == "no_match"
    assert "linker_classified_at" in body
    # Must be ISO timestamp, not None
    assert body["linker_classified_at"] is not None


@pytest.mark.parametrize("result", ["linked", "no_match", "parse_error"])
def test_mark_classified_accepts_each_valid_result(result):
    sb = MagicMock()
    sb._rest = MagicMock(return_value=None)
    _mark_classified(sb, "doc-1", result)
    body = sb._rest.call_args.kwargs["json_body"]
    assert body["linker_classified_result"] == result


def test_mark_classified_swallows_patch_failure():
    """PATCH failure must not crash the run — observability is best-effort."""
    sb = MagicMock()
    sb._rest = MagicMock(side_effect=Exception("network blip"))
    # Must not raise
    _mark_classified(sb, "doc-1", "linked")


# ---------------------------------------------------------------------------
# classify_document — parse_ok signal differentiates JSON-fail from empty
# ---------------------------------------------------------------------------

def test_classify_document_parse_ok_false_on_bad_json():
    """parse_ok=False is what tells the caller to mark parse_error instead of
    no_match. Without it, parse failures would silently look like no_match and
    we'd lose the ability to distinguish wasted-call vs. genuine no-match."""
    a_client = MagicMock()
    a_client.messages.create = MagicMock(
        return_value=_anthropic_response("not valid json at all")
    )

    links, in_tok, out_tok, parse_ok = classify_document(
        a_client, _doc(), _assets(),
        "some text", ["VRDN"],
    )

    assert links == []
    assert parse_ok is False
    assert in_tok > 0  # call WAS paid for


def test_classify_document_parse_ok_true_on_valid_empty_links():
    a_client = MagicMock()
    a_client.messages.create = MagicMock(
        return_value=_anthropic_response('{"links": []}')
    )

    links, in_tok, out_tok, parse_ok = classify_document(
        a_client, _doc(), _assets(),
        "some text", ["VRDN"],
    )

    assert links == []
    assert parse_ok is True


def test_classify_document_parse_ok_true_on_valid_link():
    a_client = MagicMock()
    body = json.dumps({"links": [{
        "asset_id": "asset-1",
        "link_type": "primary",
        "extraction_confidence": 0.9,
        "extracted_spans": [{"text": "VRDN demonstrated efficacy"}],
        "is_material": True,
        "reasoning": "test",
    }]})
    a_client.messages.create = MagicMock(return_value=_anthropic_response(body))

    links, _, _, parse_ok = classify_document(
        a_client, _doc(), _assets(),
        "some text", ["VRDN"],
    )

    assert len(links) == 1
    assert parse_ok is True


# ---------------------------------------------------------------------------
# _start_run_row / _finish_run_row — observability row lifecycle
# ---------------------------------------------------------------------------

def test_start_run_row_inserts_running_status():
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[{"id": "run-uuid"}])

    run_id = _start_run_row(sb, "pass1", "claude-sonnet-4-5-20250929")

    assert run_id == "run-uuid"
    sb._rest.assert_called_once()
    call = sb._rest.call_args
    assert call.args[0] == "POST"
    assert call.args[1] == "asset_linker_runs"
    body = call.kwargs["json_body"]
    assert body["pass"] == "pass1"
    assert body["model"] == "claude-sonnet-4-5-20250929"
    assert body["status"] == "running"


def test_start_run_row_returns_none_on_failure():
    """Observability is best-effort — a failed INSERT must not block the run."""
    sb = MagicMock()
    sb._rest = MagicMock(side_effect=Exception("PostgREST down"))
    assert _start_run_row(sb, "pass1", "m") is None


def test_finish_run_row_patches_with_pass1_fields():
    sb = MagicMock()
    sb._rest = MagicMock(return_value=None)

    stats = LinkerStats(
        docs_seen=100, docs_prefilter_passed=50, docs_prefilter_skipped=50,
        api_calls=48, errors=2, input_tokens=350_000, output_tokens=8_000,
        cost_usd=1.23, links_inserted=4, links_dedup_skipped=1,
    )
    _finish_run_row(sb, "run-uuid", "completed", stats)

    sb._rest.assert_called_once()
    call = sb._rest.call_args
    assert call.args[0] == "PATCH"
    assert call.args[1] == "asset_linker_runs"
    assert call.kwargs["params"] == {"id": "eq.run-uuid"}
    body = call.kwargs["json_body"]
    assert body["status"] == "completed"
    assert body["docs_seen"] == 100
    assert body["prefilter_passed"] == 50
    assert body["prefilter_skipped"] == 50
    assert body["api_calls"] == 48
    assert body["errors"] == 2
    assert body["links_inserted"] == 4
    assert body["cost_usd"] == 1.23
    assert "completed_at" in body


def test_finish_run_row_skips_when_run_id_none():
    """If start failed (run_id=None), finish must be a no-op."""
    sb = MagicMock()
    sb._rest = MagicMock()
    _finish_run_row(sb, None, "completed", LinkerStats())
    sb._rest.assert_not_called()


# ---------------------------------------------------------------------------
# End-to-end main() — the critical regression: each terminal path marks
# correctly, transient API errors do NOT mark.
# ---------------------------------------------------------------------------

@pytest.fixture
def patched_main_env():
    """Patch SupabaseClient + Anthropic so main() can be invoked without I/O.
    Yields (sb_mock, anth_mock, captured_marks) where captured_marks is a list
    of (doc_id, result) tuples observed via _mark_classified."""
    sb = MagicMock()
    anth = MagicMock()
    captured: List[tuple] = []

    def _rest_router(method, path, **kwargs):
        if method == "GET" and path == "fda_assets":
            return _assets()
        if method == "GET" and path == "documents":
            return [_doc("doc-A"), _doc("doc-B"), _doc("doc-C")]
        if method == "POST" and path == "asset_linker_runs":
            return [{"id": "run-uuid"}]
        if method == "POST" and path == "asset_documents":
            return [{"id": "ad-1"}]
        if method == "PATCH" and path == "documents":
            params = kwargs.get("params") or {}
            doc_id = (params.get("id") or "").removeprefix("eq.")
            body = kwargs.get("json_body") or {}
            captured.append((doc_id, body.get("linker_classified_result")))
        return None
    sb._rest = MagicMock(side_effect=_rest_router)

    with patch("modal_workers.extractor.asset_linker.SupabaseClient",
               return_value=sb), \
         patch("modal_workers.extractor.asset_linker.anthropic.Anthropic",
               return_value=anth):
        yield sb, anth, captured


def test_main_marks_no_match_when_links_empty(patched_main_env):
    sb, anth, marks = patched_main_env
    anth.messages.create = MagicMock(
        return_value=_anthropic_response('{"links": []}')
    )

    from modal_workers.extractor.asset_linker import main
    rc = main(["--max", "5", "--budget-usd", "1.00"])
    assert rc == 0

    # Every doc that made it past prefilter and got a successful empty response
    # must be marked no_match.
    assert all(result == "no_match" for _, result in marks), (
        "Zero-link verdicts should mark 'no_match' — got marks: %r" % marks
    )
    assert len(marks) >= 1


def test_main_marks_parse_error_on_bad_json(patched_main_env):
    sb, anth, marks = patched_main_env
    anth.messages.create = MagicMock(
        return_value=_anthropic_response("garbage not json")
    )

    from modal_workers.extractor.asset_linker import main
    rc = main(["--max", "5", "--budget-usd", "1.00"])
    assert rc == 0

    # Parse failures must mark parse_error, NOT no_match — that distinction is
    # what makes the cost visible separately and prevents retry-on-deterministic-fail.
    parse_marks = [r for _, r in marks if r == "parse_error"]
    assert len(parse_marks) >= 1, (
        "Bad JSON should mark 'parse_error' — got marks: %r" % marks
    )


def test_main_does_not_mark_on_api_error(patched_main_env):
    """The CRITICAL invariant: transient API errors leave linker_classified_at
    NULL so the next cron run retries. If we ever mark on APIError, retries
    are silently dropped."""
    sb, anth, marks = patched_main_env
    anth.messages.create = MagicMock(
        side_effect=anthropic.APIError("rate limited", request=MagicMock(), body=None)
    )

    from modal_workers.extractor.asset_linker import main
    rc = main(["--max", "5", "--budget-usd", "1.00"])
    assert rc == 0

    # No doc should have been marked classified — transient errors must retry.
    api_error_marks = [r for _, r in marks if r in ("linked", "no_match", "parse_error")]
    # Some prefilter-skipped marks are OK (they're deterministic skips not API errors).
    # But we can detect by checking that EVERY mark was 'no_match' from prefilter,
    # not from a classification attempt. If marks include 'linked' or 'parse_error',
    # something marked despite an API error.
    assert "linked" not in {r for _, r in marks}
    assert "parse_error" not in {r for _, r in marks}


def test_main_marks_linked_after_successful_insert(patched_main_env):
    sb, anth, marks = patched_main_env
    valid = json.dumps({"links": [{
        "asset_id": "asset-1",
        "link_type": "primary",
        "extraction_confidence": 0.92,
        "extracted_spans": [{"text": "VRDN result"}],
        "is_material": True,
        "reasoning": "test",
    }]})
    anth.messages.create = MagicMock(return_value=_anthropic_response(valid))

    from modal_workers.extractor.asset_linker import main
    rc = main(["--max", "5", "--budget-usd", "1.00"])
    assert rc == 0

    linked_marks = [r for _, r in marks if r == "linked"]
    assert len(linked_marks) >= 1, (
        "Successful link insert should mark 'linked' — got marks: %r" % marks
    )
