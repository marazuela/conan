"""Tests for citation_graph parsers + walker.

Run: python -m pytest modal_workers/tests/test_rag_citation_graph.py -v
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")

from modal_workers.rag.citation_graph import (
    CitationEdge, build_for_document, get_citation_graph,
    parse_nct_references, parse_pubmed_references, write_edges,
)


def _sb_with(rest_results):
    """Build a SupabaseClient stub whose _rest returns the next result on
    each call. rest_results is a list."""
    sb = MagicMock()
    sb._rest = MagicMock(side_effect=list(rest_results))
    return sb


# ---------------------------------------------------------------------------
# PubMed PMID parser
# ---------------------------------------------------------------------------

def test_parse_pubmed_extracts_pmids():
    sb = _sb_with([
        [{"id": "doc-A"}, {"id": "doc-B"}],
    ])
    text = (
        "<Reference><PMID>12345678</PMID></Reference>"
        "<Reference><PMID>23456789</PMID></Reference>"
    )
    edges = parse_pubmed_references(sb, "src-doc", text)
    assert len(edges) == 2
    assert all(e.relation == "cites" for e in edges)
    assert all(e.from_doc_id == "src-doc" for e in edges)
    assert {e.to_doc_id for e in edges} == {"doc-A", "doc-B"}


def test_parse_pubmed_no_pmids_returns_empty():
    sb = MagicMock()
    edges = parse_pubmed_references(sb, "src", "no references here")
    assert edges == []
    sb._rest.assert_not_called()


# ---------------------------------------------------------------------------
# NCT parser — relation depends on source
# ---------------------------------------------------------------------------

def test_nct_dailymed_emits_label_for_nct():
    sb = _sb_with([[{"id": "ct-1"}]])
    edges = parse_nct_references(sb, "src", "dailymed", "Trial NCT12345678 ...")
    assert len(edges) == 1
    assert edges[0].relation == "label_for_nct"


def test_nct_fda_advisory_emits_approval_for_nct():
    sb = _sb_with([[{"id": "ct-1"}]])
    edges = parse_nct_references(
        sb, "src", "fda_advisory", "Pivotal NCT99999999 supported approval.",
    )
    assert len(edges) == 1
    assert edges[0].relation == "approval_for_nct"


def test_nct_unknown_source_emits_cites():
    sb = _sb_with([[{"id": "ct-1"}]])
    edges = parse_nct_references(sb, "src", "press_release", "NCT11111111")
    assert edges[0].relation == "cites"


def test_nct_no_match_returns_empty_no_query():
    sb = MagicMock()
    edges = parse_nct_references(sb, "src", "dailymed", "no NCT here")
    assert edges == []
    sb._rest.assert_not_called()


# ---------------------------------------------------------------------------
# Edge writer — drops self-loops
# ---------------------------------------------------------------------------

def test_write_edges_skips_self_loop():
    sb = MagicMock()
    edges = [CitationEdge("a", "a", "cites")]
    written = write_edges(sb, edges)
    assert written == 0
    sb._rest.assert_not_called()


def test_write_edges_inserts_real_edges():
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[])
    edges = [
        CitationEdge("a", "b", "cites"),
        CitationEdge("a", "c", "label_for_nct"),
    ]
    written = write_edges(sb, edges)
    assert written == 2
    assert sb._rest.call_count == 2


def test_write_edges_swallows_individual_failures():
    sb = MagicMock()
    # First insert raises, second succeeds
    sb._rest = MagicMock(side_effect=[Exception("dup"), []])
    edges = [
        CitationEdge("a", "b", "cites"),
        CitationEdge("a", "c", "cites"),
    ]
    written = write_edges(sb, edges)
    assert written == 1


# ---------------------------------------------------------------------------
# build_for_document — routes to the right parser
# ---------------------------------------------------------------------------

def test_build_for_document_pubmed_routes_to_pubmed_parser():
    sb = MagicMock()
    sb._rest = MagicMock(side_effect=[
        [],   # pubmed lookup
        [],   # NCT lookup is skipped because source not in NCT list
    ])
    doc = {"id": "d1", "source": "pubmed", "raw_text": "no PMIDs here"}
    written = build_for_document(sb, doc)
    assert written == 0


def test_build_for_document_dailymed_routes_to_nct_parser():
    sb = MagicMock()
    sb._rest = MagicMock(side_effect=[
        [{"id": "ct-1"}],   # NCT match
        [],                  # write_edges
    ])
    doc = {"id": "d1", "source": "dailymed", "raw_text": "Trial NCT12345678"}
    written = build_for_document(sb, doc)
    assert written == 1


# ---------------------------------------------------------------------------
# Walker — depth + direction + cycle handling
# ---------------------------------------------------------------------------

def test_walk_depth_zero_returns_only_seed():
    result = get_citation_graph(MagicMock(), "seed", depth=0)
    assert result == {"nodes": ["seed"], "edges": []}


def test_walk_outbound_one_hop():
    sb = MagicMock()
    # First call: out edges from 'seed'
    # Second call: in edges to 'seed' (we filter direction='out' so this is skipped)
    sb._rest = MagicMock(side_effect=[
        [{"from_doc_id": "seed", "to_doc_id": "a", "relation": "cites",
          "confidence": 1.0}],
    ])
    result = get_citation_graph(sb, "seed", depth=1, direction="out")
    assert "a" in result["nodes"]
    assert len(result["edges"]) == 1


def test_walk_handles_cycles():
    sb = MagicMock()
    # seed -> a -> seed (cycle); the visited set prevents infinite loop
    sb._rest = MagicMock(side_effect=[
        [{"from_doc_id": "seed", "to_doc_id": "a", "relation": "cites",
          "confidence": 1.0}],
        [{"from_doc_id": "a", "to_doc_id": "seed", "relation": "cited_by",
          "confidence": 1.0}],
    ])
    result = get_citation_graph(sb, "seed", depth=2, direction="out")
    assert set(result["nodes"]) == {"seed", "a"}
