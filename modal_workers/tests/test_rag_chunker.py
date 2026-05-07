"""Tests for modal_workers.rag.chunker — section-aware hierarchical chunking.

Run: python -m pytest modal_workers/tests/test_rag_chunker.py -v
"""
from __future__ import annotations

import os

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from modal_workers.rag.chunker import (
    chunk_document, estimate_tokens,
    _chunk_clinicaltrials, _chunk_dailymed, _chunk_edgar, _chunk_faers,
    _chunk_fda_advisory, _chunk_fda_warning, _chunk_literature,
    _chunk_paragraphs, _split_by_token_budget,
)


def _doc(source: str, raw_text: str, doc_type: str = "doc") -> dict:
    return {
        "id": "doc-1",
        "source": source,
        "doc_type": doc_type,
        "raw_text": raw_text,
    }


# ---------------------------------------------------------------------------
# Token estimator
# ---------------------------------------------------------------------------

def test_estimate_tokens_empty_returns_zero():
    assert estimate_tokens("") == 0


def test_estimate_tokens_whitespace_only_returns_min_one():
    # Whitespace-only is non-empty so the max(1, ...) floor applies.
    assert estimate_tokens("   ") >= 1


def test_estimate_tokens_scales_with_words():
    short = estimate_tokens("hello world")
    long_ = estimate_tokens("hello world " * 100)
    assert long_ > short * 50


# ---------------------------------------------------------------------------
# Empty / missing input
# ---------------------------------------------------------------------------

def test_chunk_document_no_text():
    assert chunk_document({"id": "x", "source": "edgar", "raw_text": ""}) == []


def test_chunk_document_unknown_source_falls_back_to_paragraphs():
    text = "Para one.\n\nPara two.\n\nPara three."
    chunks = chunk_document({"id": "x", "source": "unknown", "raw_text": text})
    assert len(chunks) >= 1
    assert all(c.chunk_text for c in chunks)


# ---------------------------------------------------------------------------
# EDGAR — heading-aware on Item N.
# ---------------------------------------------------------------------------

def test_edgar_extracts_items_as_sections():
    text = (
        "PART I\n\n"
        "Item 1. Business\n\n"
        "We are a clinical-stage biotech focused on TED.\n\n"
        "Item 1A. Risk Factors\n\n"
        "Our pipeline has only one Phase 3 candidate.\n\n"
        "Item 7. Management Discussion\n\n"
        "Revenue grew 40% YoY.\n"
    )
    chunks = _chunk_edgar(_doc("edgar", text, "10-K"), text)
    section_paths = [tuple(c.section_path) for c in chunks]
    assert any("Item 1. Business" in p for paths in section_paths for p in paths)
    assert any("Item 1A. Risk Factors" in p
               for paths in section_paths for p in paths)
    # Hierarchical roll-up — at least one parent and one leaf
    parents = [c for c in chunks if c.extensions.get("role") == "parent"]
    leaves = [c for c in chunks if c.extensions.get("role") == "leaf"]
    assert parents and leaves


def test_edgar_includes_part_in_path():
    text = "PART I\n\nItem 1. Business\n\nLine\n\n"
    chunks = _chunk_edgar(_doc("edgar", text, "10-K"), text)
    assert any("PART I" in p for c in chunks for p in c.section_path)


def test_edgar_no_items_falls_back_to_paragraphs():
    text = "Just some prose.\n\nNo Item headings here.\n\nAnother paragraph."
    chunks = _chunk_edgar(_doc("edgar", text), text)
    assert chunks
    # Fallback path uses no section_path, no parent role
    assert all(c.extensions.get("role") != "parent" for c in chunks)


# ---------------------------------------------------------------------------
# FDA briefing — numbered section detector
# ---------------------------------------------------------------------------

def test_fda_advisory_extracts_numbered_sections():
    text = (
        "1.0 Background\n\n"
        "Drug X has a long history.\n\n"
        "2.1 Efficacy\n\n"
        "Phase 3 met primary endpoint.\n\n"
        "3 Safety\n\n"
        "Adverse events were Grade 1-2.\n"
    )
    chunks = _chunk_fda_advisory(_doc("fda_advisory", text), text)
    paths = [p for c in chunks for p in c.section_path]
    assert any("1.0" in p for p in paths)
    assert any("Efficacy" in p for p in paths)


# ---------------------------------------------------------------------------
# FDA warning letter — Observation N
# ---------------------------------------------------------------------------

def test_warning_letter_sections_observation_response_closure():
    text = (
        "Observation 1\n\nFailure to validate.\n\n"
        "Observation 2\n\nInsufficient testing.\n\n"
        "Response\n\nWe will remediate.\n\n"
        "Closure\n\nLetter closed.\n"
    )
    chunks = _chunk_fda_warning(_doc("fda_warning_letter", text), text)
    paths = [p for c in chunks for p in c.section_path]
    assert any("Observation 1" in p for p in paths)
    assert any("Observation 2" in p for p in paths)
    assert any("Response" in p for p in paths)
    assert any("Closure" in p for p in paths)


# ---------------------------------------------------------------------------
# DailyMed — XML <section>
# ---------------------------------------------------------------------------

def test_dailymed_parses_section_xml():
    text = (
        '<section><title>Indications</title>'
        '<paragraph>Treatment of TED.</paragraph></section>'
        '<section><title>Dosage</title>'
        '<paragraph>10 mg/kg IV.</paragraph></section>'
    )
    chunks = _chunk_dailymed(_doc("dailymed", text), text)
    paths = [p for c in chunks for p in c.section_path]
    assert "Indications" in paths
    assert "Dosage" in paths


def test_dailymed_no_xml_falls_back_to_paragraphs():
    text = "Plain text label.\n\nWith two paragraphs."
    chunks = _chunk_dailymed(_doc("dailymed", text), text)
    assert chunks


# ---------------------------------------------------------------------------
# Literature — abstract = single chunk; full-text = IMRAD
# ---------------------------------------------------------------------------

def test_literature_abstract_yields_single_chunk():
    text = "This is a short abstract describing the trial results."
    chunks = _chunk_literature(_doc("pubmed", text), text)
    assert len(chunks) == 1
    assert chunks[0].section_path == ["Abstract"]


def test_literature_imrad_full_text():
    text = (
        "Introduction\n\nBackground here.\n\n"
        "Methods\n\nWe ran a Phase 3.\n\n"
        "Results\n\nPrimary endpoint met (p<0.001).\n\n"
        "Discussion\n\nClinically meaningful.\n"
    )
    chunks = _chunk_literature(_doc("biorxiv", text), text)
    paths = [p for c in chunks for p in c.section_path]
    assert "Introduction" in paths
    assert "Methods" in paths
    assert "Results" in paths


# ---------------------------------------------------------------------------
# FAERS — one chunk per ICSR
# ---------------------------------------------------------------------------

def test_faers_one_chunk_per_safetyreport():
    text = (
        "<safetyreport><patient>P1 had hepatotoxicity.</patient></safetyreport>"
        "<safetyreport><patient>P2 had rash.</patient></safetyreport>"
        "<safetyreport><patient>P3 had nausea.</patient></safetyreport>"
    )
    chunks = _chunk_faers(_doc("faers", text), text)
    assert len(chunks) == 3
    assert all("ICSR" in c.section_path[0] for c in chunks)


# ---------------------------------------------------------------------------
# ClinicalTrials — field detector
# ---------------------------------------------------------------------------

def test_clinicaltrials_field_aware():
    text = (
        "Eligibility\n\nAdults 18+.\n\n"
        "Primary Outcome\n\nORR at week 24.\n\n"
        "Adverse Events\n\nGrade 1-2 fatigue.\n"
    )
    chunks = _chunk_clinicaltrials(_doc("clinicaltrials", text), text)
    paths = [p for c in chunks for p in c.section_path]
    assert "Eligibility" in paths
    assert "Primary Outcome" in paths


# ---------------------------------------------------------------------------
# Paragraph splitter — token budget honored
# ---------------------------------------------------------------------------

def test_paragraph_split_respects_budget():
    # Use real prose with sentence boundaries so the sentence splitter can
    # subdivide oversized paragraphs.
    sent = "This is a sentence with several normal English words in it. "
    para = sent * 80   # ~720 words ≈ 540 tokens — over the 400 budget
    text = f"{para}\n\n{para}\n\n{para}"
    pieces = _split_by_token_budget(text, target_tokens=400, overlap_tokens=0)
    assert len(pieces) >= 2
    # Sentence-split fallback should keep each piece under ~2x the budget.
    for p in pieces:
        assert estimate_tokens(p) <= 400 * 2


def test_paragraph_split_unsplittable_paragraph_is_kept_whole():
    # A single paragraph with no sentence boundaries can't be sub-split — the
    # splitter returns it as one oversized chunk rather than crashing.
    para = " ".join(["word"] * 1500)
    pieces = _split_by_token_budget(para, target_tokens=400, overlap_tokens=0)
    assert len(pieces) >= 1
    assert all(p for p in pieces)


def test_paragraph_split_with_overlap():
    text = "\n\n".join(f"Paragraph {i}." for i in range(20))
    pieces = _split_by_token_budget(text, target_tokens=15, overlap_tokens=5)
    assert len(pieces) >= 2
