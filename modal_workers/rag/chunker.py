"""Section-aware hierarchical chunker.

`chunk_document(doc) -> list[Chunk]` dispatches on doc.source + doc.doc_type.
Each leaf chunk gets a parent_chunk_id pointing to a section-level summary
chunk; both leaf and parent are stored in document_chunks.

Token counting uses a heuristic word-rate estimate (~0.75 tokens/word for
English). Anthropic's tokenizer would be more accurate but adds an SDK dep
to the chunker hot path; the estimate is good enough for budget planning.

Per-source strategies:

  EDGAR 10-K/10-Q/8-K:     heading-aware on `^Item \\d+\\w?\\.`, `^PART [IVX]+`
  FDA briefing/AdComm:     heading-aware on numbered sections + Q&A markers
  FDA warning letter/483:  Observation \\d+, Response, Closure
  DailyMed:                XML-section-aware (LOINC <section> tags)
  PubMed/preprint abstr.:  whole abstract = one chunk
  PubMed full-text:        IMRAD-aware (Introduction/Methods/Results/Discussion)
  FAERS XML (per-ICSR):    one chunk per Individual Case Safety Report
  ClinicalTrials.gov:      field-aware (eligibility, outcomes, results)
  federal_register:        paragraph sliding window
  polygon_news/PR:         paragraph sliding window

Output Chunk objects are written to document_chunks via insert_chunks().
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class Chunk:
    chunk_index: int
    chunk_text: str
    chunk_tokens: int
    section_path: List[str] = field(default_factory=list)
    parent_index: Optional[int] = None  # index into the same chunk list
    extensions: Dict[str, Any] = field(default_factory=dict)


# Token-budget defaults per source family.
DEFAULT_CHUNK_BUDGET = {
    "edgar": (800, 2400, 100),               # leaf, parent, overlap
    "federal_register": (500, 2000, 100),
    "fda_advisory": (700, 2000, 100),
    "fda_warning_letter": (600, 1800, 80),
    "fda_483": (600, 1800, 80),
    "dailymed": (500, 1500, 0),
    "openfda": (500, 1500, 0),
    "faers": (600, 1800, 0),
    "pubmed": (400, 1200, 0),
    "biorxiv": (700, 2000, 80),
    "medrxiv": (700, 2000, 80),
    "clinicaltrials": (500, 1500, 0),
    "polygon_news": (500, 1200, 100),
    "press_release": (500, 1200, 100),
}

# Word-to-token heuristic for budget math (English; underestimates code/numbers).
TOKENS_PER_WORD = 0.75


def estimate_tokens(text: str) -> int:
    """Cheap word-count estimate. Use Anthropic's tokenizer for precision in
    callers that need it (chunker only uses estimates for budget decisions)."""
    if not text:
        return 0
    return max(1, int(len(text.split()) * TOKENS_PER_WORD))


# ---------------------------------------------------------------------------
# Common splitters
# ---------------------------------------------------------------------------

def _split_by_token_budget(
    text: str, target_tokens: int, overlap_tokens: int = 0,
) -> List[str]:
    """Greedy paragraph-aware split into chunks of ~target_tokens. Falls back
    to sentence split when paragraphs exceed the budget."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    cur: List[str] = []
    cur_tokens = 0
    for p in paras:
        p_tokens = estimate_tokens(p)
        if p_tokens > target_tokens * 1.5:
            # Single paragraph too big — sentence-split.
            if cur:
                chunks.append("\n\n".join(cur))
                cur, cur_tokens = [], 0
            chunks.extend(_sentence_split(p, target_tokens))
            continue
        if cur_tokens + p_tokens > target_tokens and cur:
            chunks.append("\n\n".join(cur))
            if overlap_tokens > 0 and cur:
                tail = cur[-1]
                cur = [tail] if estimate_tokens(tail) <= overlap_tokens else []
                cur_tokens = estimate_tokens(cur[0]) if cur else 0
            else:
                cur, cur_tokens = [], 0
        cur.append(p)
        cur_tokens += p_tokens
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def _sentence_split(text: str, target_tokens: int) -> List[str]:
    """Sentence-aware split for paragraphs that exceed the chunk budget."""
    sents = re.split(r"(?<=[.!?])\s+", text)
    out: List[str] = []
    cur: List[str] = []
    cur_tokens = 0
    for s in sents:
        st = estimate_tokens(s)
        if cur_tokens + st > target_tokens and cur:
            out.append(" ".join(cur))
            cur, cur_tokens = [], 0
        cur.append(s)
        cur_tokens += st
    if cur:
        out.append(" ".join(cur))
    return out


# ---------------------------------------------------------------------------
# EDGAR — heading-aware on ^Item \d+\w?\.
# ---------------------------------------------------------------------------

EDGAR_ITEM_RE = re.compile(r"(?im)^\s*Item\s+(\d+\w?)\.\s*([^\n]+)")
EDGAR_PART_RE = re.compile(r"(?im)^\s*PART\s+([IVX]+)\s*$")


def _chunk_edgar(doc: Dict[str, Any], text: str) -> List[Chunk]:
    leaf_target, parent_target, overlap = DEFAULT_CHUNK_BUDGET["edgar"]
    chunks: List[Chunk] = []

    # Walk Items in order; PART boundaries become path prefixes.
    parts = list(EDGAR_PART_RE.finditer(text))
    items = list(EDGAR_ITEM_RE.finditer(text))
    if not items:
        return _chunk_paragraphs(doc, text, "edgar")

    def _part_for(pos: int) -> Optional[str]:
        cur = None
        for p in parts:
            if p.start() <= pos:
                cur = p.group(1)
            else:
                break
        return cur

    sections: List[Tuple[List[str], str]] = []
    for i, m in enumerate(items):
        start = m.start()
        end = items[i + 1].start() if i + 1 < len(items) else len(text)
        body = text[start:end].strip()
        if not body:
            continue
        item_no = m.group(1)
        item_title = m.group(2).strip()
        part_label = _part_for(start)
        path = [f"PART {part_label}"] if part_label else []
        path.append(f"Item {item_no}. {item_title}")
        sections.append((path, body))

    return _build_hierarchical(doc, sections, leaf_target, parent_target, overlap)


# ---------------------------------------------------------------------------
# FDA briefing / AdComm — heading-aware on numbered sections
# ---------------------------------------------------------------------------

FDA_SECTION_RE = re.compile(r"(?im)^\s*(\d+(?:\.\d+)?)\s+([A-Z][^\n]{2,80})$")


def _chunk_fda_advisory(doc: Dict[str, Any], text: str) -> List[Chunk]:
    leaf_target, parent_target, overlap = DEFAULT_CHUNK_BUDGET["fda_advisory"]
    matches = list(FDA_SECTION_RE.finditer(text))
    if not matches:
        return _chunk_paragraphs(doc, text, "fda_advisory")
    sections: List[Tuple[List[str], str]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if not body:
            continue
        sections.append(([f"§{m.group(1)} {m.group(2)}"], body))
    return _build_hierarchical(doc, sections, leaf_target, parent_target, overlap)


# ---------------------------------------------------------------------------
# FDA warning letter / 483 — Observation N, Response, Closure
# ---------------------------------------------------------------------------

WARN_OBS_RE = re.compile(r"(?im)^\s*(Observation\s+\d+|Response|Closure)\b")


def _chunk_fda_warning(doc: Dict[str, Any], text: str) -> List[Chunk]:
    leaf_target, parent_target, overlap = DEFAULT_CHUNK_BUDGET["fda_warning_letter"]
    matches = list(WARN_OBS_RE.finditer(text))
    if not matches:
        return _chunk_paragraphs(doc, text, "fda_warning_letter")
    sections: List[Tuple[List[str], str]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if not body:
            continue
        sections.append(([m.group(1)], body))
    return _build_hierarchical(doc, sections, leaf_target, parent_target, overlap)


# ---------------------------------------------------------------------------
# DailyMed — XML <section> w/ LOINC code
# ---------------------------------------------------------------------------

DAILYMED_SECTION_RE = re.compile(
    r"<section[^>]*?>(.*?)</section>", re.DOTALL | re.IGNORECASE,
)
DAILYMED_TITLE_RE = re.compile(
    r"<title[^>]*?>(.*?)</title>", re.DOTALL | re.IGNORECASE,
)


def _chunk_dailymed(doc: Dict[str, Any], text: str) -> List[Chunk]:
    leaf_target, parent_target, _ = DEFAULT_CHUNK_BUDGET["dailymed"]
    sections_xml = DAILYMED_SECTION_RE.findall(text)
    if not sections_xml:
        return _chunk_paragraphs(doc, text, "dailymed")
    sections: List[Tuple[List[str], str]] = []
    for sec in sections_xml:
        title_m = DAILYMED_TITLE_RE.search(sec)
        title = (title_m.group(1) if title_m else "Section").strip()
        body = re.sub(r"<[^>]+>", " ", sec)
        body = re.sub(r"\s+", " ", body).strip()
        if not body:
            continue
        sections.append(([title], body))
    return _build_hierarchical(doc, sections, leaf_target, parent_target, 0)


# ---------------------------------------------------------------------------
# PubMed / preprints — abstract is the chunk; full-text uses IMRAD if present
# ---------------------------------------------------------------------------

IMRAD_RE = re.compile(
    r"(?im)^\s*(Introduction|Background|Methods?|Results?|Discussion|Conclusions?)"
    r"\s*[:\.]?\s*$"
)


def _chunk_literature(doc: Dict[str, Any], text: str) -> List[Chunk]:
    src = doc.get("source", "pubmed")
    leaf_target, parent_target, overlap = DEFAULT_CHUNK_BUDGET.get(
        src, DEFAULT_CHUNK_BUDGET["pubmed"])
    matches = list(IMRAD_RE.finditer(text))
    if len(matches) < 2:
        # Treat as abstract — single chunk.
        return [Chunk(
            chunk_index=0,
            chunk_text=text.strip(),
            chunk_tokens=estimate_tokens(text),
            section_path=["Abstract"],
        )]
    sections: List[Tuple[List[str], str]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if not body:
            continue
        sections.append(([m.group(1)], body))
    return _build_hierarchical(doc, sections, leaf_target, parent_target, overlap)


# ---------------------------------------------------------------------------
# FAERS XML — one chunk per ICSR (Individual Case Safety Report)
# ---------------------------------------------------------------------------

FAERS_REPORT_RE = re.compile(
    r"<safetyreport[^>]*?>(.*?)</safetyreport>", re.DOTALL | re.IGNORECASE,
)


def _chunk_faers(doc: Dict[str, Any], text: str) -> List[Chunk]:
    leaf_target, _, _ = DEFAULT_CHUNK_BUDGET["faers"]
    reports = FAERS_REPORT_RE.findall(text)
    if not reports:
        return _chunk_paragraphs(doc, text, "faers")
    chunks: List[Chunk] = []
    for i, r in enumerate(reports):
        body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r)).strip()
        if not body:
            continue
        # Long narratives get split.
        for j, sub in enumerate(_split_by_token_budget(body, leaf_target, 0)):
            chunks.append(Chunk(
                chunk_index=len(chunks),
                chunk_text=sub,
                chunk_tokens=estimate_tokens(sub),
                section_path=[f"ICSR {i}"] + ([f"part {j}"] if j > 0 else []),
            ))
    return chunks


# ---------------------------------------------------------------------------
# ClinicalTrials.gov — field-aware
# ---------------------------------------------------------------------------

CT_FIELD_RE = re.compile(
    r"(?im)^\s*(Eligibility|Primary Outcome|Secondary Outcome|Study Design|"
    r"Interventions?|Arms? and Groups?|Results?|Adverse Events)\s*[:\.]?\s*$"
)


def _chunk_clinicaltrials(doc: Dict[str, Any], text: str) -> List[Chunk]:
    leaf_target, parent_target, _ = DEFAULT_CHUNK_BUDGET["clinicaltrials"]
    matches = list(CT_FIELD_RE.finditer(text))
    if len(matches) < 2:
        return _chunk_paragraphs(doc, text, "clinicaltrials")
    sections: List[Tuple[List[str], str]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if not body:
            continue
        sections.append(([m.group(1)], body))
    return _build_hierarchical(doc, sections, leaf_target, parent_target, 0)


# ---------------------------------------------------------------------------
# Paragraph fallback (federal_register, polygon_news, press_release, others)
# ---------------------------------------------------------------------------

def _chunk_paragraphs(
    doc: Dict[str, Any], text: str, src_key: str,
) -> List[Chunk]:
    leaf_target, _, overlap = DEFAULT_CHUNK_BUDGET.get(
        src_key, DEFAULT_CHUNK_BUDGET["polygon_news"])
    pieces = _split_by_token_budget(text, leaf_target, overlap)
    return [
        Chunk(
            chunk_index=i,
            chunk_text=piece,
            chunk_tokens=estimate_tokens(piece),
        )
        for i, piece in enumerate(pieces)
    ]


# ---------------------------------------------------------------------------
# Hierarchical builder — leaves + parent rollups, populating parent_index
# ---------------------------------------------------------------------------

def _build_hierarchical(
    doc: Dict[str, Any],
    sections: List[Tuple[List[str], str]],
    leaf_target: int,
    parent_target: int,
    overlap: int,
) -> List[Chunk]:
    """Each section yields one parent chunk + N leaf chunks. Parent index is
    chosen first; leaves reference it via parent_index."""
    out: List[Chunk] = []
    for path, body in sections:
        # The parent chunk is a header + the body, capped at parent_target.
        body_tokens = estimate_tokens(body)
        if body_tokens <= parent_target:
            parent_text = body
        else:
            parent_text = body[: int(parent_target / TOKENS_PER_WORD * 6)]
        parent_idx = len(out)
        out.append(Chunk(
            chunk_index=parent_idx,
            chunk_text=parent_text,
            chunk_tokens=estimate_tokens(parent_text),
            section_path=list(path),
            parent_index=None,  # parents are top-level
            extensions={"role": "parent"},
        ))
        # Leaves
        for sub in _split_by_token_budget(body, leaf_target, overlap):
            out.append(Chunk(
                chunk_index=len(out),
                chunk_text=sub,
                chunk_tokens=estimate_tokens(sub),
                section_path=list(path),
                parent_index=parent_idx,
                extensions={"role": "leaf"},
            ))
    return out


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_DISPATCH: Dict[str, Callable[[Dict[str, Any], str], List[Chunk]]] = {
    "edgar": _chunk_edgar,
    "fda_advisory": _chunk_fda_advisory,
    "fda_warning_letter": _chunk_fda_warning,
    "fda_483": _chunk_fda_warning,
    "dailymed": _chunk_dailymed,
    "openfda": _chunk_dailymed,
    "pubmed": _chunk_literature,
    "biorxiv": _chunk_literature,
    "medrxiv": _chunk_literature,
    "faers": _chunk_faers,
    "clinicaltrials": _chunk_clinicaltrials,
}


def chunk_document(doc: Dict[str, Any]) -> List[Chunk]:
    """Top-level dispatcher. doc is a `documents` row dict; raw_text is read
    from doc['raw_text']. Returns a list of Chunk objects ready to insert
    into document_chunks."""
    src = (doc.get("source") or "").strip()
    text = (doc.get("raw_text") or "").strip()
    if not text:
        logger.warning("chunk_document: doc %s has no raw_text", doc.get("id"))
        return []
    fn = _DISPATCH.get(src)
    if fn is None:
        return _chunk_paragraphs(doc, text, src or "polygon_news")
    return fn(doc, text)
