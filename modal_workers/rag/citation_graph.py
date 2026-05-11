"""Citation graph builder + walker.

Sources of edges (hybrid):
  - Inline parsers: PubMed has explicit <Reference> XML; EDGAR rarely has
    structured cross-refs (parsed best-effort from links).
  - Explicit joins:
      DailyMed labels → ClinicalTrials NCT (label_for_nct)
      FDA approval letter → ClinicalTrials NCT (approval_for_nct)
      FDA warning letter responds_to prior 483 (responds_to)
      PubMed papers about the same compound (same_compound; via asset linker)

Edges are written to citation_graph_cache; readers query via
get_citation_graph(doc_id, depth, direction).
"""
from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

NCT_RE = re.compile(r"\bNCT\d{8}\b")
PUBMED_REF_RE = re.compile(
    r"<PMID[^>]*?>\s*(\d+)\s*</PMID>", re.IGNORECASE,
)


@dataclass
class CitationEdge:
    from_doc_id: str
    to_doc_id: str
    relation: str
    confidence: float = 1.0
    source_method: str = "inline_parser"


# ---------------------------------------------------------------------------
# Inline parsers
# ---------------------------------------------------------------------------

def parse_pubmed_references(
    sb, doc_id: str, raw_text: str,
) -> List[CitationEdge]:
    """Extract PMID-formatted citations from PubMed XML/text."""
    pmids = set(PUBMED_REF_RE.findall(raw_text))
    if not pmids:
        return []
    in_clause = ",".join(f'"{p}"' for p in pmids)
    rows = sb._rest(
        "GET", "documents",
        params={
            "source": "eq.pubmed",
            "source_doc_id": f"in.({in_clause})",
            "select": "id",
        },
    ) or []
    return [
        CitationEdge(
            from_doc_id=doc_id, to_doc_id=r["id"], relation="cites",
            source_method="inline_parser",
        )
        for r in rows
    ]


def parse_nct_references(
    sb, doc_id: str, source: str, raw_text: str,
) -> List[CitationEdge]:
    """Extract NCT references; relation depends on source.
    DailyMed → label_for_nct, fda_advisory → approval_for_nct, others → cites.
    """
    nct_ids = set(NCT_RE.findall(raw_text))
    if not nct_ids:
        return []
    in_clause = ",".join(f'"{n}"' for n in nct_ids)
    rows = sb._rest(
        "GET", "documents",
        params={
            "source": "eq.clinicaltrials",
            "source_doc_id": f"in.({in_clause})",
            "select": "id",
        },
    ) or []
    relation = {
        "dailymed": "label_for_nct",
        "fda_advisory": "approval_for_nct",
    }.get(source, "cites")
    return [
        CitationEdge(
            from_doc_id=doc_id, to_doc_id=r["id"], relation=relation,
            source_method="explicit_join",
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Edge writer
# ---------------------------------------------------------------------------

def write_edges(sb, edges: List[CitationEdge]) -> int:
    inserted = 0
    for e in edges:
        if e.from_doc_id == e.to_doc_id:
            continue
        try:
            sb._rest(
                "POST", "citation_graph_cache",
                json_body={
                    "from_doc_id": e.from_doc_id,
                    "to_doc_id": e.to_doc_id,
                    "relation": e.relation,
                    "confidence": round(e.confidence, 2),
                    "source_method": e.source_method,
                },
                prefer="resolution=ignore-duplicates",
            )
            inserted += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("citation_graph insert failed (%s -> %s): %s",
                         e.from_doc_id, e.to_doc_id, exc)
    return inserted


def build_for_document(sb, doc: Dict[str, Any]) -> int:
    """Build citation edges for one document. Routes to the right parser."""
    src = doc.get("source", "")
    raw = doc.get("raw_text") or ""
    edges: List[CitationEdge] = []
    if src in ("pubmed", "biorxiv", "medrxiv"):
        edges.extend(parse_pubmed_references(sb, doc["id"], raw))
    if src in ("dailymed", "fda_advisory", "fda_warning_letter", "fda_483",
               "openfda", "edgar", "press_release"):
        edges.extend(parse_nct_references(sb, doc["id"], src, raw))
    return write_edges(sb, edges)


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------

def get_citation_graph(
    sb, doc_id: str, depth: int = 1, direction: str = "both",
) -> Dict[str, Any]:
    """BFS walk up to `depth` levels. direction in {'out','in','both'}.
    Returns {nodes: [doc_id], edges: [{from, to, relation, confidence}]}.
    """
    if depth < 1:
        return {"nodes": [doc_id], "edges": []}
    visited: Set[str] = {doc_id}
    edges_out: List[Dict[str, Any]] = []
    queue: deque[Tuple[str, int]] = deque([(doc_id, 0)])
    while queue:
        cur, d = queue.popleft()
        if d >= depth:
            continue
        out_edges: List[Dict[str, Any]] = []
        if direction in ("out", "both"):
            rows = sb._rest(
                "GET", "citation_graph_cache",
                params={
                    "from_doc_id": f"eq.{cur}",
                    "select": "from_doc_id,to_doc_id,relation,confidence",
                },
            ) or []
            for r in rows:
                out_edges.append(r)
        if direction in ("in", "both"):
            rows = sb._rest(
                "GET", "citation_graph_cache",
                params={
                    "to_doc_id": f"eq.{cur}",
                    "select": "from_doc_id,to_doc_id,relation,confidence",
                },
            ) or []
            for r in rows:
                out_edges.append(r)
        for e in out_edges:
            edges_out.append(e)
            for nxt in (e["from_doc_id"], e["to_doc_id"]):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append((nxt, d + 1))
    return {"nodes": sorted(visited), "edges": edges_out}
