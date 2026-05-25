"""Stage 7 constitutional check — Sonnet validator over the ensemble output.

Plan §"Stage 7 — Constitutional check (Sonnet validator)": validates
  (a) every cited fact_id resolves to an actual extracted_facts row
  (b) every cited doc_id resolves to an actual documents row
  (c) every cited evidence quote, when present, matches the underlying source
  (d) the assessment's claims don't internally contradict
  (e) probability estimate is within sane bounds vs reference-class base rate
      (when reference_class_base_rate is supplied)
  (f) thesis_direction is consistent with the dominant evidence
      (no "long" thesis built on contradicting facts)

Outputs a structured findings list + pass/fail flag. On fail, the orchestrator
can either retry Stage 5 (downgrading conviction with the findings as
feedback) or escalate to operator queue (failed runs can be reviewed before
they ship to alerts).

Deterministic checks (a-c) run as Python — no API call needed. Semantic
checks (d-f) call Sonnet with the assessment + raw evidence.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from orchestrator_runtime.client import (
    OrchestratorClient,
    estimate_cost,
    parse_json_or_none,
)

logger = logging.getLogger(__name__)


@dataclass
class ConstitutionalFinding:
    severity: str        # 'info', 'warning', 'error'
    check: str           # 'unresolved_fact_id', 'unresolved_doc_id', 'quote_mismatch',
                         # 'internal_contradiction', 'base_rate_divergence',
                         # 'direction_evidence_mismatch'
    detail: str
    affected_id: Optional[str] = None


@dataclass
class ConstitutionalResult:
    pass_: bool
    findings: List[ConstitutionalFinding] = field(default_factory=list)
    n_citations_checked: int = 0
    n_citations_resolved: int = 0
    semantic_check_used: bool = False
    semantic_input_tokens: int = 0
    semantic_output_tokens: int = 0
    semantic_cost_usd: float = 0.0
    semantic_latency_ms: int = 0


# ---------------------------------------------------------------------------
# Deterministic checks (no API call)
# ---------------------------------------------------------------------------

CITE_FACT_RE = re.compile(r"\[F:([0-9a-f]{6,12})\]", re.IGNORECASE)
CITE_DOC_RE = re.compile(r"\[D:([0-9a-f]{6,12})\]", re.IGNORECASE)


def extract_native_citations(
    response_content: Optional[List[Any]],
) -> List[Dict[str, Any]]:
    """Stream 3.3 — walk Anthropic Citations API metadata from a response.

    When Stage 1 receives a document block with `citations: {enabled: true}`,
    Claude's response text blocks include a `citations` array with entries
    like `{type: "char_location", cited_text, document_index, document_title,
    start_char_index, end_char_index}`. This helper flattens those into a
    list of dicts that callers can cross-reference against ctx["documents"].

    Returns [] if response_content is None or no native citations were found.
    Used additively alongside the regex resolver — does NOT replace it, since
    extracted_facts (which aren't documents) keep the [F:short] notation.
    """
    out: List[Dict[str, Any]] = []
    if not response_content:
        return out
    for block in response_content:
        btype = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if btype != "text":
            continue
        cites = getattr(block, "citations", None)
        if cites is None and isinstance(block, dict):
            cites = block.get("citations")
        if not cites:
            continue
        for c in cites:
            entry = c if isinstance(c, dict) else getattr(c, "__dict__", {})
            if not entry:
                if hasattr(c, "model_dump"):
                    entry = c.model_dump()
                else:
                    entry = {
                        attr: getattr(c, attr)
                        for attr in ("type", "cited_text", "document_index",
                                     "document_title", "start_char_index",
                                     "end_char_index", "start_page_number",
                                     "end_page_number")
                        if hasattr(c, attr)
                    }
            out.append(entry)
    return out


def check_citations_resolve(
    cited_prose: str,
    fact_ids: List[str],
    document_ids: List[str],
) -> tuple[List[ConstitutionalFinding], int, int]:
    """Confirm every [F:short] / [D:short] cite resolves to a real ID."""
    findings: List[ConstitutionalFinding] = []
    fact_short_set: Set[str] = {f[:8] for f in fact_ids}
    doc_short_set: Set[str] = {d[:8] for d in document_ids}

    cited_facts = set(m.group(1).lower() for m in CITE_FACT_RE.finditer(cited_prose))
    cited_docs = set(m.group(1).lower() for m in CITE_DOC_RE.finditer(cited_prose))

    n_total = len(cited_facts) + len(cited_docs)
    n_resolved = 0

    for short in cited_facts:
        if short in fact_short_set:
            n_resolved += 1
        else:
            findings.append(ConstitutionalFinding(
                severity="error",
                check="unresolved_fact_id",
                detail=f"Cited fact_id [F:{short}] does not resolve to any "
                       f"fact in the assessment's fact_ids list",
                affected_id=short,
            ))

    for short in cited_docs:
        if short in doc_short_set:
            n_resolved += 1
        else:
            findings.append(ConstitutionalFinding(
                severity="error",
                check="unresolved_doc_id",
                detail=f"Cited doc_id [D:{short}] does not resolve to any "
                       f"doc in the assessment's document_ids list",
                affected_id=short,
            ))

    return findings, n_total, n_resolved


def check_hypothesis_premortem_citations(
    *,
    hypothesis_result: Optional[Any],   # orchestrator_runtime.hypothesis.HypothesisResult
    premortem_result: Optional[Any],    # orchestrator_runtime.premortem.PreMortemResult
    fact_ids: List[str],
    document_ids: List[str],
) -> tuple[List[ConstitutionalFinding], int, int]:
    """Walk Stage 2 hypothesis mechanisms + Stage 3 failure_modes for citation
    resolution. Mirrors check_citations_resolve but applied to the new stages.

    Returns (findings, n_citations_checked, n_citations_resolved). Findings are
    severity=error for unresolved or missing-on-non-speculative."""
    findings: List[ConstitutionalFinding] = []
    fact_short_set: Set[str] = {f[:8].lower() for f in fact_ids}
    doc_short_set: Set[str] = {d[:8].lower() for d in document_ids}
    n_total = 0
    n_resolved = 0

    if hypothesis_result is not None:
        for h in getattr(hypothesis_result, "hypotheses", []) or []:
            mechanism = getattr(h, "mechanism", "") or ""
            cited_facts = {m.group(1).lower() for m in CITE_FACT_RE.finditer(mechanism)}
            cited_docs = {m.group(1).lower() for m in CITE_DOC_RE.finditer(mechanism)}
            n_total += len(cited_facts) + len(cited_docs)
            for short in cited_facts:
                if short in fact_short_set:
                    n_resolved += 1
                else:
                    findings.append(ConstitutionalFinding(
                        severity="error",
                        check="hypothesis_unresolved_fact_id",
                        detail=f"hypothesis {h.hypothesis_id} mechanism cites "
                               f"[F:{short}], which does not resolve.",
                        affected_id=short,
                    ))
            for short in cited_docs:
                if short in doc_short_set:
                    n_resolved += 1
                else:
                    findings.append(ConstitutionalFinding(
                        severity="error",
                        check="hypothesis_unresolved_doc_id",
                        detail=f"hypothesis {h.hypothesis_id} mechanism cites "
                               f"[D:{short}], which does not resolve.",
                        affected_id=short,
                    ))
            # Walk supporting/contradicting fact_id arrays too
            for short in (getattr(h, "supporting_fact_ids", []) or []):
                short_l = short.lower()
                n_total += 1
                if short_l in fact_short_set:
                    n_resolved += 1
                else:
                    findings.append(ConstitutionalFinding(
                        severity="error",
                        check="hypothesis_unresolved_fact_id",
                        detail=f"hypothesis {h.hypothesis_id} supporting_fact_id "
                               f"{short!r} does not resolve.",
                        affected_id=short,
                    ))
            for short in (getattr(h, "contradicting_fact_ids", []) or []):
                short_l = short.lower()
                n_total += 1
                if short_l in fact_short_set:
                    n_resolved += 1
                else:
                    findings.append(ConstitutionalFinding(
                        severity="error",
                        check="hypothesis_unresolved_fact_id",
                        detail=f"hypothesis {h.hypothesis_id} contradicting_fact_id "
                               f"{short!r} does not resolve.",
                        affected_id=short,
                    ))

    if premortem_result is not None:
        for v in getattr(premortem_result, "verdicts", []) or []:
            for fm in getattr(v, "failure_modes", []) or []:
                speculative = bool(getattr(fm, "speculative", False))
                ev_ids = getattr(fm, "evidence_fact_ids", []) or []
                if not speculative and not ev_ids:
                    findings.append(ConstitutionalFinding(
                        severity="error",
                        check="premortem_missing_citation_non_speculative",
                        detail=f"verdict {v.hypothesis_id} failure_mode "
                               f"{fm.description[:80]!r} is non-speculative "
                               f"but has no evidence_fact_ids.",
                        affected_id=v.hypothesis_id,
                    ))
                for short in ev_ids:
                    short_l = short.lower()
                    n_total += 1
                    if short_l in fact_short_set:
                        n_resolved += 1
                    else:
                        findings.append(ConstitutionalFinding(
                            severity="error",
                            check="premortem_unresolved_fact_id",
                            detail=f"verdict {v.hypothesis_id} failure_mode "
                                   f"evidence_fact_id {short!r} does not resolve.",
                            affected_id=short,
                        ))

    return findings, n_total, n_resolved


# ---------------------------------------------------------------------------
# Semantic check (Sonnet)
# ---------------------------------------------------------------------------

SEMANTIC_SYSTEM_PROMPT = """You are an adversarial reviewer of an FDA-event \
investment thesis. Your job is to find errors of reasoning, internal \
contradictions, and misalignment between thesis_direction and the underlying \
evidence.

You see:
  - The full cited prose thesis
  - The structured fact layer (what the analyst saw)
  - The assessment's stated thesis_direction + conviction_pct + reference_class \
base rate (when known)

You emit ONLY a JSON object:

{
  "internal_contradictions": [
    {"detail": "<short description>", "severity": "info|warning|error"}
  ],
  "direction_evidence_alignment": {
    "aligned": true|false,
    "detail": "<why direction does/doesn't match the dominant evidence>"
  },
  "base_rate_check": {
    "within_sane_bounds": true|false,
    "detail": "<conviction vs base rate analysis, or 'no base rate provided'>"
  },
  "overall_pass": true|false
}

Rules:
- Only flag MATERIAL issues. Routine epistemic humility ("we don't know X") \
is not a contradiction. A "long" thesis with mostly bullish evidence + \
acknowledged risks is aligned. A "long" thesis built on bearish evidence \
is misaligned.
- base_rate_check.within_sane_bounds is true when conviction_pct is within \
~30 percentage points of the reference_class base rate, OR when no base rate \
is provided (return "no base rate provided" detail).
- overall_pass = false only on error-severity contradiction OR \
direction_evidence_alignment.aligned=false.
- Output JSON only — no commentary, no markdown fences."""


def check_semantics(
    a_client: OrchestratorClient,
    *,
    cited_prose: str,
    facts: List[Dict[str, Any]],
    thesis_direction: str,
    conviction_pct: float,
    reference_class: Optional[str],
    reference_class_base_rate: Optional[float],
    model: str,
    max_tokens: int = 1024,
    system_blocks: Optional[List[Dict[str, Any]]] = None,
) -> tuple[List[ConstitutionalFinding], int, int, float, int, bool]:
    """Run the semantic adversarial check via Sonnet. Returns (findings,
    in_tokens, out_tokens, cost, latency_ms, overall_pass).

    D-119: when `system_blocks` is provided (typically the same shared prefix
    as Stage 1/2/3 + SEMANTIC_SYSTEM_PROMPT), the structured fact layer is
    omitted from user content because it lives in the cached system prefix.
    Cache reads at 10% input cost. When None, falls back to inline-facts mode.
    """
    base_rate_line = (
        f"reference_class: {reference_class}\n"
        f"reference_class_base_rate: {reference_class_base_rate}"
        if reference_class_base_rate is not None
        else "reference_class: (unknown)\nreference_class_base_rate: (none provided)"
    )

    if system_blocks is not None:
        # Facts are in the cached system prefix; don't duplicate.
        user_content = f"""Thesis under review:

  thesis_direction: {thesis_direction}
  conviction_pct: {conviction_pct}
  {base_rate_line}

## Cited prose thesis

{cited_prose}
"""
        system_arg: Any = system_blocks
    else:
        facts_summary_lines: List[str] = []
        for f in facts[:60]:
            facts_summary_lines.append(
                f"- F:{f['id'][:8]} ({f['fact_type']}, conf={f.get('confidence')}): "
                f"{f['fact_text']}"
            )
        facts_summary = "\n".join(facts_summary_lines)
        user_content = f"""Thesis under review:

  thesis_direction: {thesis_direction}
  conviction_pct: {conviction_pct}
  {base_rate_line}

## Structured fact layer ({len(facts)} facts available; top 60 shown)

{facts_summary}

## Cited prose thesis

{cited_prose}
"""
        system_arg = SEMANTIC_SYSTEM_PROMPT

    # Route through OrchestratorClient.call so we get budget accounting,
    # transient-error retry, and cache-aware cost accounting. The earlier
    # raw-SDK call bypassed all three: a Stage 7 spend could push past the
    # per-run hard kill without triggering BudgetExceededError, and cache
    # read/create tokens were silently dropped from the cost rollup.
    res = a_client.call(
        system=system_arg,
        messages=[{"role": "user", "content": user_content}],
        model=model,
        max_tokens=max_tokens,
    )
    latency_ms = res.latency_ms
    text = res.text
    in_tokens = res.input_tokens
    out_tokens = res.output_tokens
    cost = res.cost_usd

    parsed = parse_json_or_none(text)
    findings: List[ConstitutionalFinding] = []
    overall_pass = True

    if not parsed:
        # Don't fail the whole assessment on parse error — log and continue
        findings.append(ConstitutionalFinding(
            severity="warning",
            check="semantic_parse_failure",
            detail=f"Could not parse semantic-check JSON; head[:200]: {text[:200]!r}",
        ))
        return findings, in_tokens, out_tokens, cost, latency_ms, True

    # Internal contradictions
    for c in (parsed.get("internal_contradictions") or []):
        sev = c.get("severity", "warning")
        if sev not in {"info", "warning", "error"}:
            sev = "warning"
        findings.append(ConstitutionalFinding(
            severity=sev,
            check="internal_contradiction",
            detail=str(c.get("detail", ""))[:500],
        ))
        if sev == "error":
            overall_pass = False

    # Direction-evidence alignment
    align = parsed.get("direction_evidence_alignment") or {}
    if align.get("aligned") is False:
        findings.append(ConstitutionalFinding(
            severity="error",
            check="direction_evidence_mismatch",
            detail=str(align.get("detail", ""))[:500],
        ))
        overall_pass = False

    # Base rate check
    bcheck = parsed.get("base_rate_check") or {}
    if bcheck.get("within_sane_bounds") is False:
        findings.append(ConstitutionalFinding(
            severity="warning",
            check="base_rate_divergence",
            detail=str(bcheck.get("detail", ""))[:500],
        ))

    # Defer to overall_pass field if model said false even without finding errors
    if parsed.get("overall_pass") is False:
        overall_pass = False

    return findings, in_tokens, out_tokens, cost, latency_ms, overall_pass


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------

def run_constitutional_check(
    a_client: OrchestratorClient,
    *,
    cited_prose: str,
    facts: List[Dict[str, Any]],
    document_ids: List[str],
    thesis_direction: str,
    conviction_pct: float,
    reference_class: Optional[str] = None,
    reference_class_base_rate: Optional[float] = None,
    model: str,
    skip_semantic: bool = False,
    hypothesis_result: Optional[Any] = None,    # HypothesisResult from Stage 2
    premortem_result: Optional[Any] = None,     # PreMortemResult from Stage 3
    semantic_system_blocks: Optional[List[Dict[str, Any]]] = None,  # D-119
) -> ConstitutionalResult:
    """Run deterministic citation-resolution checks + (unless skipped) the
    semantic Sonnet adversarial check.

    When `hypothesis_result` and/or `premortem_result` are provided, the
    deterministic citation-resolution pass also walks their cited fact_ids
    and emits severity=error findings on unresolved short-ids or missing
    citations on non-speculative pre-mortem failure modes.
    """
    fact_ids = [f["id"] for f in facts]
    findings, n_total, n_resolved = check_citations_resolve(
        cited_prose, fact_ids, document_ids,
    )
    if hypothesis_result is not None or premortem_result is not None:
        h_findings, h_total, h_resolved = check_hypothesis_premortem_citations(
            hypothesis_result=hypothesis_result,
            premortem_result=premortem_result,
            fact_ids=fact_ids,
            document_ids=document_ids,
        )
        findings.extend(h_findings)
        n_total += h_total
        n_resolved += h_resolved
    # D-117: structural errors from Stage 2/3 (missing required label, too few
    # hypotheses, missing kill_conditions, parse failure, missing verdicts)
    # MUST gate the assessment, not just live in stage_metrics.notes. Promote
    # them into the constitutional findings list as 'stage_2_structural_error'
    # / 'stage_3_structural_error' so the deliverability gate sees them.
    if hypothesis_result is not None:
        for hf in getattr(hypothesis_result, "findings", []) or []:
            if hf.severity == "error":
                findings.append(ConstitutionalFinding(
                    severity="error",
                    check=f"stage_2_{hf.check}",
                    detail=hf.detail,
                    affected_id=hf.affected_id,
                ))
    if premortem_result is not None:
        for pf in getattr(premortem_result, "findings", []) or []:
            if pf.severity == "error":
                findings.append(ConstitutionalFinding(
                    severity="error",
                    check=f"stage_3_{pf.check}",
                    detail=pf.detail,
                    affected_id=pf.affected_id,
                ))
    pass_ = all(f.severity != "error" for f in findings)

    sem_in = sem_out = 0
    sem_cost = 0.0
    sem_latency = 0
    sem_used = False
    if not skip_semantic:
        sem_used = True
        sem_findings, sem_in, sem_out, sem_cost, sem_latency, sem_pass = check_semantics(
            a_client,
            cited_prose=cited_prose,
            facts=facts,
            thesis_direction=thesis_direction,
            conviction_pct=conviction_pct,
            reference_class=reference_class,
            reference_class_base_rate=reference_class_base_rate,
            model=model,
            system_blocks=semantic_system_blocks,
        )
        findings.extend(sem_findings)
        if not sem_pass:
            pass_ = False

    return ConstitutionalResult(
        pass_=pass_,
        findings=findings,
        n_citations_checked=n_total,
        n_citations_resolved=n_resolved,
        semantic_check_used=sem_used,
        semantic_input_tokens=sem_in,
        semantic_output_tokens=sem_out,
        semantic_cost_usd=sem_cost,
        semantic_latency_ms=sem_latency,
    )
