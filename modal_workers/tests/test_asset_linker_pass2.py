"""Tests for asset_linker pass-2 verifier.

Run: python -m pytest modal_workers/tests/test_asset_linker_pass2.py -v
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from modal_workers.extractor.asset_linker import (
    PASS2_BATCH_SIZE,
    Pass2Verdict,
    _apply_pass2_verdict,
    _build_pass2_user_content,
    verify_link_pass2_batch,
)


def _row(
    ad_id: str,
    *,
    asset_drug: str = "VRDN",
    link_type: str = "primary",
    is_material: bool = True,
    confidence: float = 0.5,
    spans: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    return {
        "id": ad_id,
        "asset_id": "asset-1",
        "document_id": "doc-1",
        "link_type": link_type,
        "extraction_confidence": confidence,
        "extracted_spans": spans or [{"text": "VRDN-001 demonstrated efficacy"}],
        "is_material": is_material,
        "asset": {
            "drug_name": asset_drug,
            "generic_name": "veligrotug",
            "sponsor_name": "Viridian Therapeutics",
            "indication": "thyroid eye disease",
        },
        "document": {
            "source": "edgar",
            "doc_type": "10-K",
            "title": "Form 10-K",
            "published_at": "2026-01-01",
        },
    }


def _make_anthropic_response(verdicts: List[Dict[str, Any]]):
    """Build a minimal mock for anthropic.Anthropic().messages.create()."""
    text = json.dumps({"verdicts": verdicts})
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    resp.usage.input_tokens = 1500
    resp.usage.output_tokens = 200
    return resp


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def test_build_user_content_includes_all_rows():
    rows = [_row("ad-1"), _row("ad-2", asset_drug="other")]
    content = _build_pass2_user_content(rows)
    assert "ad-1" in content
    assert "ad-2" in content
    assert "VRDN" in content
    assert "veligrotug" in content


def test_build_user_content_handles_string_spans():
    # Some pass-1 rows have spans as raw strings; the prompt builder should
    # render both shapes without crashing.
    rows = [_row("ad-1", spans=["raw string span"])]
    content = _build_pass2_user_content(rows)
    assert "raw string span" in content


# ---------------------------------------------------------------------------
# Verifier batch parsing
# ---------------------------------------------------------------------------

def test_verify_batch_parses_kept_demoted_rejected():
    rows = [_row("ad-1"), _row("ad-2"), _row("ad-3")]
    client = MagicMock()
    client.messages.create.return_value = _make_anthropic_response([
        {"asset_documents_id": "ad-1", "verdict": "kept",
         "confidence": 0.95, "reasoning": "spans clearly support primary link"},
        {"asset_documents_id": "ad-2", "verdict": "demoted",
         "confidence": 0.7, "reasoning": "real but boilerplate"},
        {"asset_documents_id": "ad-3", "verdict": "rejected",
         "confidence": 0.85, "reasoning": "spans don't support claim"},
    ])
    verdicts, in_tok, out_tok = verify_link_pass2_batch(client, rows)
    assert in_tok == 1500
    assert out_tok == 200
    assert len(verdicts) == 3
    assert {v.verdict for v in verdicts} == {"kept", "demoted", "rejected"}


def test_verify_batch_drops_unknown_id():
    rows = [_row("ad-1")]
    client = MagicMock()
    client.messages.create.return_value = _make_anthropic_response([
        {"asset_documents_id": "ad-NOT-IN-BATCH", "verdict": "kept",
         "confidence": 0.9, "reasoning": "x"},
    ])
    verdicts, _, _ = verify_link_pass2_batch(client, rows)
    assert verdicts == []


def test_verify_batch_drops_invalid_verdict():
    rows = [_row("ad-1")]
    client = MagicMock()
    client.messages.create.return_value = _make_anthropic_response([
        {"asset_documents_id": "ad-1", "verdict": "maybe",
         "confidence": 0.9, "reasoning": "x"},
    ])
    verdicts, _, _ = verify_link_pass2_batch(client, rows)
    assert verdicts == []


def test_verify_batch_clamps_confidence():
    rows = [_row("ad-1")]
    client = MagicMock()
    client.messages.create.return_value = _make_anthropic_response([
        {"asset_documents_id": "ad-1", "verdict": "kept",
         "confidence": 1.7, "reasoning": "x"},
    ])
    verdicts, _, _ = verify_link_pass2_batch(client, rows)
    assert len(verdicts) == 1
    assert verdicts[0].confidence == 1.0


def test_verify_batch_handles_markdown_fences():
    rows = [_row("ad-1")]
    client = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = (
        "```json\n"
        + json.dumps({"verdicts": [
            {"asset_documents_id": "ad-1", "verdict": "kept",
             "confidence": 0.9, "reasoning": "ok"}
        ]})
        + "\n```"
    )
    resp = MagicMock()
    resp.content = [block]
    resp.usage.input_tokens = 100
    resp.usage.output_tokens = 50
    client.messages.create.return_value = resp
    verdicts, _, _ = verify_link_pass2_batch(client, rows)
    assert len(verdicts) == 1


def test_verify_batch_returns_empty_on_unparseable():
    rows = [_row("ad-1")]
    client = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = "I cannot parse this"
    resp = MagicMock()
    resp.content = [block]
    resp.usage.input_tokens = 50
    resp.usage.output_tokens = 10
    client.messages.create.return_value = resp
    verdicts, in_tok, out_tok = verify_link_pass2_batch(client, rows)
    assert verdicts == []
    # Still account for the cost — caller decrements budget regardless
    assert in_tok == 50 and out_tok == 10


# ---------------------------------------------------------------------------
# Verdict application — rejected sets is_material=false (no DELETE)
# ---------------------------------------------------------------------------

def test_apply_kept_verdict_does_not_flip_is_material():
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[{}])
    v = Pass2Verdict("ad-1", "kept", 0.95, "ok")
    assert _apply_pass2_verdict(sb, v) is True
    call = sb._rest.call_args
    json_body = call.kwargs["json_body"]
    assert json_body["verified_by_pass2"] is True
    assert json_body["pass2_verdict"] == "kept"
    assert "is_material" not in json_body


def test_apply_rejected_verdict_flips_is_material():
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[{}])
    v = Pass2Verdict("ad-1", "rejected", 0.9, "no support")
    _apply_pass2_verdict(sb, v)
    call = sb._rest.call_args
    json_body = call.kwargs["json_body"]
    assert json_body["is_material"] is False
    assert json_body["pass2_verdict"] == "rejected"
    # Critical: PATCH not DELETE — audit-trail survival
    assert call.args[0] == "PATCH"


def test_apply_demoted_verdict_flips_is_material():
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[{}])
    v = Pass2Verdict("ad-1", "demoted", 0.7, "boilerplate")
    _apply_pass2_verdict(sb, v)
    json_body = sb._rest.call_args.kwargs["json_body"]
    assert json_body["is_material"] is False


def test_batch_size_constant_is_5():
    assert PASS2_BATCH_SIZE == 5
