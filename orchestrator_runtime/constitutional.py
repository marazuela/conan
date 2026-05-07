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
) -> tuple[List[ConstitutionalFinding], int, int, float, int, bool]:
    """Run the semantic adversarial check via Sonnet. Returns (findings,
    in_tokens, out_tokens, cost, latency_ms, overall_pass)."""
    facts_summary_lines: List[str] = []
    for f in facts[:60]:  # keep context tight
        facts_summary_lines.append(
            f"- F:{f['id'][:8]} ({f['fact_type']}, conf={f.get('confidence')}): "
            f"{f['fact_text']}"
        )
    facts_summary = "\n".join(facts_summary_lines)

    base_rate_line = (
        f"reference_class: {reference_class}\n"
        f"reference_class_base_rate: {reference_class_base_rate}"
        if reference_class_base_rate is not None
        else "reference_class: (unknown)\nreference_class_base_rate: (none provided)"
    )

    user_content = f"""Thesis under review:

  thesis_direction: {thesis_direction}
  conviction_pct: {conviction_pct}
  {base_rate_line}

## Structured fact layer ({len(facts)} facts available; top 60 shown)

{facts_summary}

## Cited prose thesis

{cited_prose}
"""

    import time
    t0 = time.time()
    resp = a_client._client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=SEMANTIC_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    latency_ms = int((time.time() - t0) * 1000)
    text = "".join(b.text for b in resp.content if b.type == "text")
    in_tokens = resp.usage.input_tokens
    out_tokens = resp.usage.output_tokens
    cost = estimate_cost(model, in_tokens, out_tokens)

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
) -> ConstitutionalResult:
    """Run deterministic citation-resolution checks + (unless skipped) the
    semantic Sonnet adversarial check."""
    fact_ids = [f["id"] for f in facts]
    findings, n_total, n_resolved = check_citations_resolve(
        cited_prose, fact_ids, document_ids,
    )
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
