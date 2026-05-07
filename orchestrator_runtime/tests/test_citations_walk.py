"""Stream 3.3 — native Citations API metadata walk.

Run: python -m pytest orchestrator_runtime/tests/test_citations_walk.py -v
"""

from __future__ import annotations

import os
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from orchestrator_runtime.constitutional import (
    extract_native_citations,
)
from orchestrator_runtime.runtime import _build_stage_1_user_content_blocks


# ---------- extract_native_citations ----------


def test_extract_returns_empty_for_none_or_empty():
    assert extract_native_citations(None) == []
    assert extract_native_citations([]) == []


def test_extract_walks_text_block_citations_dict_form():
    response_content = [
        {
            "type": "text",
            "text": "The trial showed efficacy.",
            "citations": [
                {
                    "type": "char_location",
                    "cited_text": "primary endpoint hit at p<0.001",
                    "document_index": 0,
                    "document_title": "Trial X NDA",
                    "start_char_index": 100,
                    "end_char_index": 200,
                },
            ],
        },
    ]
    cites = extract_native_citations(response_content)
    assert len(cites) == 1
    assert cites[0]["document_index"] == 0
    assert cites[0]["cited_text"].startswith("primary endpoint")


def test_extract_walks_text_block_citations_attr_form():
    """Anthropic SDK returns content blocks as objects with attributes."""
    cite_obj = MagicMock()
    cite_obj.type = "char_location"
    cite_obj.cited_text = "exact"
    cite_obj.document_index = 1
    cite_obj.document_title = "T"
    cite_obj.start_char_index = 0
    cite_obj.end_char_index = 5
    block = MagicMock()
    block.type = "text"
    block.citations = [cite_obj]
    out = extract_native_citations([block])
    assert len(out) == 1
    assert out[0]["document_index"] == 1


def test_extract_skips_non_text_blocks():
    response = [
        {"type": "thinking", "thinking": "internal reasoning"},
        {"type": "tool_use", "id": "x", "name": "y", "input": {}},
    ]
    assert extract_native_citations(response) == []


def test_extract_returns_empty_when_no_citations_field():
    response = [{"type": "text", "text": "no cites here"}]
    assert extract_native_citations(response) == []


# ---------- _build_stage_1_user_content_blocks ----------


def _make_ctx(docs: List[Dict[str, Any]], memory_text: str = "") -> Dict[str, Any]:
    return {
        "documents": docs,
        "memory_text": memory_text or None,
    }


def test_user_content_blocks_emits_document_block_for_uploaded_pdf():
    ctx = _make_ctx([
        {
            "id": "00000000-0000-0000-0000-000000000abc",
            "source": "openfda",
            "doc_type": "label",
            "title": "Drug X label",
            "raw_text": "label text...",
            "anthropic_file_id": "file_abc",
            "is_pdf": True,
            "published_at": "2026-01-15T00:00:00Z",
        },
    ])
    blocks = _build_stage_1_user_content_blocks(ctx)
    # 1 document block + 1 trailing text block
    assert len(blocks) == 2
    doc_block = blocks[0]
    assert doc_block["type"] == "document"
    assert doc_block["source"] == {"type": "file", "file_id": "file_abc"}
    assert doc_block["citations"] == {"enabled": True}
    text_block = blocks[1]
    assert text_block["type"] == "text"
    assert "documents with native Citations API: 1" in text_block["text"]


def test_user_content_blocks_falls_back_to_text_excerpt_when_no_file_id():
    ctx = _make_ctx([
        {
            "id": "00000000-0000-0000-0000-000000000def",
            "source": "edgar",
            "doc_type": "10k",
            "title": "10-K filing",
            "raw_text": "long text here...",
            "anthropic_file_id": None,
            "is_pdf": False,
            "published_at": "2026-01-15T00:00:00Z",
        },
    ])
    blocks = _build_stage_1_user_content_blocks(ctx)
    # No document blocks, just the bundled text block
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert "documents with native Citations API: 0" in blocks[0]["text"]
    assert "long text here" in blocks[0]["text"]


def test_user_content_blocks_mixed_uploaded_and_legacy():
    ctx = _make_ctx([
        {
            "id": "id-aaa", "source": "openfda", "doc_type": "label",
            "title": "PDF doc", "raw_text": "x", "anthropic_file_id": "file_1",
            "is_pdf": True, "published_at": "2026-01-01",
        },
        {
            "id": "id-bbb", "source": "edgar", "doc_type": "10k",
            "title": "Text doc", "raw_text": "fallback text excerpt",
            "anthropic_file_id": None, "is_pdf": False,
            "published_at": "2026-01-02",
        },
    ])
    blocks = _build_stage_1_user_content_blocks(ctx)
    # 1 doc block + 1 text block (with the fallback excerpt embedded)
    assert len(blocks) == 2
    assert blocks[0]["type"] == "document"
    assert blocks[1]["type"] == "text"
    assert "fallback text excerpt" in blocks[1]["text"]
    assert "documents with native Citations API: 1" in blocks[1]["text"]
    assert "documents shown as text excerpt below: 1" in blocks[1]["text"]
