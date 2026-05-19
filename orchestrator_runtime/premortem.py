"""Stage 3 — adversarial pre-mortem.

Plan ref: /Users/Pico/.claude/plans/stage-2-3-robust-wolf.md

Reads (HypothesisResult from Stage 2, ctx) and emits a per-hypothesis
verdict in {survives, weakened, falsified}. Each hypothesis is tested
against named failure modes (severity ∈ {kill, weaken, tail}) with
[F:short] citations or speculative=true flags for reasoning-only failures.

The overall_verdict ∈ {all_survive, partial, all_falsified} feeds the
Stage 9 wrapper, which caps conviction_pct ≤ 30 on all_falsified.

Strict-sourcing: every non-speculative failure mode requires a fact
citation. Speculative failures are allowed but flagged so Stage 7 can
audit. Stage 7 walks the failure_modes list and raises severity=error
findings on missing citations of non-speculative claims.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from orchestrator_runtime.client import (
    OrchestratorClient,
    parse_json_or_none,
)
from orchestrator_runtime.hypothesis import Hypothesis, HypothesisResult

logger = logging.getLogger(__name__)


VALID_VERDICTS = {"survives", "weakened", "falsified"}
VALID_SEVERITIES = {"kill", "weaken", "tail"}
VALID_OVERALL = {"all_survive", "partial", "all_falsified"}


@dataclass
class PreMortemFinding:
    severity: str        # 'info', 'warning', 'error'
    check: str
    detail: str
    affected_id: Optional[str] = None


@dataclass
class FailureMode:
    description: str
    severity: str                          # kill | weaken | tail
    evidence_fact_ids: List[str] = field(default_factory=list)
    speculative: bool = False


@dataclass
class HypothesisVerdict:
    hypothesis_id: str
    verdict: str                           # survives | weakened | falsified
    failure_modes: List[FailureMode] = field(default_factory=list)
    disconfirming_searches: List[str] = field(default_factory=list)
    update_triggers: List[str] = field(default_factory=list)
    # v2 thesis_challenger semantic intent layer. Optional input field
    # `challenger_verdict` ∈ {confirm, challenge, kill, decline}. Maps onto
    # verdict (confirm→survives, challenge→weakened, kill→falsified) with
    # `decline` carrying the v2 "this signal doesn't support a real thesis"
    # semantics via is_declined flag (verdict left at failure-modes rollup).
    is_declined: bool = False


@dataclass
class PreMortemResult:
    pass_: bool
    overall_verdict: str                   # all_survive | partial | all_falsified
    surviving_hypothesis_ids: List[str] = field(default_factory=list)
    verdicts: List[HypothesisVerdict] = field(default_factory=list)
    findings: List[PreMortemFinding] = field(default_factory=list)
    raw_response: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    # Cache token bookkeeping — Stage 3 reuses the shared cached system prefix
    # (D-119). Surfacing these here keeps StageMetric.cache_* honest and lets
    # cost reconciliation match Anthropic's actual billing.
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0


STAGE_3_SYSTEM = """You are a skeptical IC reviewer running a pre-mortem on \
a set of competing investment hypotheses. The committee has just received \
the enumerated hypotheses (each with a claim, a mechanism, and a list of \
kill_conditions). Your job is to identify what makes each hypothesis WRONG.

For every hypothesis, list `failure_modes`. Each failure mode has:
  - description: one sentence on what goes wrong
  - severity: 'kill' (single failure invalidates the hypothesis), 'weaken' \
(meaningfully reduces probability), or 'tail' (low-probability downside)
  - evidence_fact_ids: 8-char fact_id_short citations supporting the failure \
mode (when grounded in observed evidence)
  - speculative: true if the failure mode is reasoning-only (no fact \
citation). False otherwise.

**A hypothesis with no named failure modes means you have not thought hard \
enough — list at least 2 failure modes per hypothesis, including at least \
one with severity 'kill' or 'weaken' if the kill_conditions admit one.**

Verdict per hypothesis:
  - 'survives': failure modes are tail-only (no kill, no weaken). The \
hypothesis remains live for committee.
  - 'weakened': at least one weaken-severity failure mode is present.
  - 'falsified': at least one kill-severity failure mode is confirmed by \
cited evidence (not speculative).

Additionally, you MAY emit a `challenger_verdict` field per hypothesis with \
the v2 thesis_challenger semantic intent: confirm | challenge | kill | \
decline. The first three are aliases for survives | weakened | falsified \
and should agree with the verdict you derived from failure_modes. Reserve \
`decline` for the case where the hypothesis itself doesn't engage with a \
real thesis — widely-watched event with no named edge, hallucinated \
catalyst, cosmetic kill_conditions only. `decline` is sparingly used and \
does NOT cap conviction; it flags the asset for operator review.

Also emit:
  - disconfirming_searches: short list of queries / data we'd want to look up \
to test this hypothesis ("phase 3 SUSTAIN-1 hepatic AE rate", "FDA AdComm \
voting record on similar opioid-receptor agonists"). 1-3 per hypothesis.
  - update_triggers: specific catalysts that, if they occurred, would shift \
confidence ("AdComm vote in 2026-Q3", "interim Phase 3 readout"). 1-3 per \
hypothesis.

Overall verdict:
  - 'all_survive': every hypothesis verdict is 'survives'.
  - 'all_falsified': every hypothesis verdict is 'falsified'.
  - 'partial': anything else (mix of survives/weakened/falsified).

If all hypotheses are falsified, say so plainly — do not invent a surviving \
thesis.

Output ONLY a JSON object — no commentary, no markdown fences:

{
  "verdicts": [
    {
      "hypothesis_id": "H1",
      "verdict": "survives",
      "challenger_verdict": "confirm | challenge | kill | decline",
      "failure_modes": [
        {"description": "<sentence>", "severity": "weaken|kill|tail",
         "evidence_fact_ids": ["abc12345"], "speculative": false},
        ...
      ],
      "disconfirming_searches": ["...", ...],
      "update_triggers": ["...", ...]
    },
    ...
  ],
  "overall_verdict": "all_survive | partial | all_falsified",
  "surviving_hypothesis_ids": ["H1", "H3"]
}

`challenger_verdict` is optional; if omitted, it's derived from `verdict` \
via confirm=survives, challenge=weakened, kill=falsified. Emit `decline` \
only when the hypothesis itself is structurally unsupported."""


def _serialize_hypothesis(h: Hypothesis) -> str:
    kc_lines = "\n".join(f"      - {k}" for k in h.kill_conditions) or "      (none)"
    sup = ", ".join(h.supporting_fact_ids) or "(none)"
    con = ", ".join(h.contradicting_fact_ids) or "(none)"
    return (
        f"  {h.hypothesis_id} [{h.label}, direction={h.direction}, "
        f"prior={h.prior_estimate_pct}%]\n"
        f"    claim: {h.claim}\n"
        f"    mechanism: {h.mechanism}\n"
        f"    supporting: {sup}\n"
        f"    contradicting: {con}\n"
        f"    kill_conditions:\n{kc_lines}"
    )


def _build_stage_3_user_content(
    *,
    hypothesis_result: HypothesisResult,
) -> str:
    """D-119: asset preamble + fact layer have moved to the cached shared
    system prefix. Returns only the dynamic Stage 3 content: serialized
    Stage 2 hypotheses + the per-stage instruction."""
    hyps_section = "\n\n".join(
        _serialize_hypothesis(h) for h in hypothesis_result.hypotheses
    )
    return f"""## Stage 2 enumerated hypotheses

{hyps_section}

Run a pre-mortem against each hypothesis per the system prompt. Cite \
evidence_fact_ids by 8-char short id from the structured fact layer in the \
cached prefix. Output JSON only."""


def _validate_and_parse_verdicts(
    parsed: Optional[Dict[str, Any]],
    hypothesis_ids: List[str],
    fact_short_set: set[str],
) -> tuple[List[HypothesisVerdict], str, List[str], List[PreMortemFinding]]:
    findings: List[PreMortemFinding] = []
    if not parsed or not isinstance(parsed, dict):
        findings.append(PreMortemFinding(
            severity="error", check="parse_failure",
            detail="Stage 3 response did not parse as a JSON object."))
        return [], "all_falsified", [], findings

    raw_verdicts = parsed.get("verdicts") or []
    overall = str(parsed.get("overall_verdict") or "").strip().lower()
    surviving = parsed.get("surviving_hypothesis_ids") or []
    if not isinstance(raw_verdicts, list):
        findings.append(PreMortemFinding(
            severity="error", check="parse_failure",
            detail="verdicts field is not a list."))
        return [], "all_falsified", [], findings

    verdicts: List[HypothesisVerdict] = []
    seen_ids: set[str] = set()
    for idx, v in enumerate(raw_verdicts):
        if not isinstance(v, dict):
            findings.append(PreMortemFinding(
                severity="warning", check="parse_failure",
                detail=f"verdicts[{idx}] is not an object; skipped."))
            continue
        hyp_id = str(v.get("hypothesis_id") or "").strip()
        verdict = str(v.get("verdict") or "").strip().lower()
        if hyp_id not in hypothesis_ids:
            findings.append(PreMortemFinding(
                severity="warning", check="unknown_hypothesis_id",
                detail=f"verdicts[{idx}] hypothesis_id={hyp_id!r} not in "
                       f"Stage 2 set {hypothesis_ids}; skipped.",
                affected_id=hyp_id))
            continue
        if verdict not in VALID_VERDICTS:
            findings.append(PreMortemFinding(
                severity="warning", check="invalid_verdict",
                detail=f"verdicts[{idx}] verdict={verdict!r} not in "
                       f"{VALID_VERDICTS}; defaulting to 'weakened'.",
                affected_id=hyp_id))
            verdict = "weakened"

        raw_fms = v.get("failure_modes") or []
        failure_modes: List[FailureMode] = []
        for fm_idx, fm in enumerate(raw_fms):
            if not isinstance(fm, dict):
                continue
            sev = str(fm.get("severity") or "tail").strip().lower()
            if sev not in VALID_SEVERITIES:
                sev = "tail"
            speculative = bool(fm.get("speculative", False))
            ev_ids = [str(s).strip() for s in (fm.get("evidence_fact_ids") or [])
                      if isinstance(s, str) and str(s).strip()]
            description = str(fm.get("description") or "").strip()
            if not description:
                continue
            # Strict-sourcing: non-speculative failure modes need a fact_id.
            if not speculative and not ev_ids:
                findings.append(PreMortemFinding(
                    severity="error", check="missing_citation_non_speculative",
                    detail=f"verdicts[{idx}] failure_modes[{fm_idx}] is "
                           f"non-speculative but has no evidence_fact_ids; "
                           f"strict-sourcing requires a citation.",
                    affected_id=hyp_id))
            for ev in ev_ids:
                if ev.lower() not in fact_short_set:
                    findings.append(PreMortemFinding(
                        severity="warning", check="unresolved_evidence_fact_id",
                        detail=f"verdicts[{idx}] failure_modes[{fm_idx}] "
                               f"evidence_fact_id {ev!r} not in fact set.",
                        affected_id=ev))
            failure_modes.append(FailureMode(
                description=description,
                severity=sev,
                evidence_fact_ids=ev_ids,
                speculative=speculative,
            ))

        if len(failure_modes) < 1:
            findings.append(PreMortemFinding(
                severity="warning", check="no_failure_modes",
                detail=f"verdicts[{idx}] has no failure_modes; pre-mortem "
                       f"incomplete for {hyp_id}.",
                affected_id=hyp_id))

        disconfirming = [str(s).strip() for s in (v.get("disconfirming_searches") or [])
                         if isinstance(s, str) and str(s).strip()]
        triggers = [str(s).strip() for s in (v.get("update_triggers") or [])
                    if isinstance(s, str) and str(s).strip()]

        # v2 thesis_challenger semantic-intent layer. confirm/challenge/kill
        # are aliases for the verdict we already derived (survives/weakened/
        # falsified). `decline` is sparingly used — flag is_declined and leave
        # verdict at the failure-modes rollup so the Stage 9 cap logic still
        # runs as if the hypothesis had a normal verdict.
        challenger_verdict = str(v.get("challenger_verdict") or "").strip().lower()
        is_declined = (challenger_verdict == "decline")
        if challenger_verdict and challenger_verdict not in (
            "confirm", "challenge", "kill", "decline"
        ):
            findings.append(PreMortemFinding(
                severity="warning", check="invalid_challenger_verdict",
                detail=f"verdicts[{idx}] challenger_verdict="
                       f"{challenger_verdict!r} not in "
                       f"{{confirm,challenge,kill,decline}}; ignored.",
                affected_id=hyp_id))

        verdicts.append(HypothesisVerdict(
            hypothesis_id=hyp_id,
            verdict=verdict,
            failure_modes=failure_modes,
            disconfirming_searches=disconfirming,
            update_triggers=triggers,
            is_declined=is_declined,
        ))
        seen_ids.add(hyp_id)

    missing = [h for h in hypothesis_ids if h not in seen_ids]
    if missing:
        findings.append(PreMortemFinding(
            severity="error", check="missing_verdict",
            detail=f"no verdict emitted for hypotheses: {missing}"))

    # Recompute surviving + overall locally; trust verdicts over the model's
    # rollup so the cap stays consistent with per-hypothesis truth.
    local_surviving = [v.hypothesis_id for v in verdicts if v.verdict == "survives"]
    n_falsified = sum(1 for v in verdicts if v.verdict == "falsified")
    n_total = len(verdicts)
    if n_total == 0:
        local_overall = "all_falsified"
    elif n_falsified == n_total:
        local_overall = "all_falsified"
    elif len(local_surviving) == n_total:
        local_overall = "all_survive"
    else:
        local_overall = "partial"

    if overall in VALID_OVERALL and overall != local_overall:
        findings.append(PreMortemFinding(
            severity="info", check="overall_verdict_mismatch",
            detail=f"model said overall_verdict={overall!r} but per-hypothesis "
                   f"rollup is {local_overall!r}; using local rollup."))

    # Same for surviving — accept model's list only when it matches local.
    surviving_ids = local_surviving
    if isinstance(surviving, list):
        model_surviving = [str(s).strip() for s in surviving if isinstance(s, str)]
        if set(model_surviving) != set(local_surviving):
            findings.append(PreMortemFinding(
                severity="info", check="surviving_ids_mismatch",
                detail=f"model surviving_hypothesis_ids={model_surviving} "
                       f"differs from per-hypothesis rollup {local_surviving}; "
                       f"using local rollup."))

    return verdicts, local_overall, surviving_ids, findings


def run_premortem(
    a_client: OrchestratorClient,
    *,
    hypothesis_result: HypothesisResult,
    ctx: Dict[str, Any],
    model: str,
    max_tokens: int = 4096,
    system_blocks: Optional[List[Dict[str, Any]]] = None,
) -> PreMortemResult:
    """Run Stage 3: per-hypothesis pre-mortem against the Stage 2 enumerated
    set. Returns a PreMortemResult with verdicts + overall rollup.

    D-119: when `system_blocks` is provided (typically `build_system_blocks(
    shared_prefix, STAGE_3_SYSTEM)`), uses it as the system prompt so the
    cached shared prefix from Stage 1/2 hits cache. When None, falls back to
    `STAGE_3_SYSTEM` as a string for callers that haven't migrated.
    """
    facts = ctx.get("facts") or []
    fact_short_set = {f["id"][:8].lower() for f in facts}

    hypothesis_ids = [h.hypothesis_id for h in hypothesis_result.hypotheses]
    if not hypothesis_ids:
        return PreMortemResult(
            pass_=False,
            overall_verdict="all_falsified",
            findings=[PreMortemFinding(
                severity="error", check="empty_hypothesis_set",
                detail="Stage 2 emitted no hypotheses; nothing to pre-mortem.")],
        )

    user_content = _build_stage_3_user_content(
        hypothesis_result=hypothesis_result,
    )

    t0 = time.time()
    result = a_client.call(
        system=system_blocks if system_blocks is not None else STAGE_3_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
        model=model,
        max_tokens=max_tokens,
    )
    latency_ms = int((time.time() - t0) * 1000)

    parsed = parse_json_or_none(result.text)
    verdicts, overall, surviving_ids, findings = _validate_and_parse_verdicts(
        parsed, hypothesis_ids, fact_short_set,
    )
    pass_ = (
        len(verdicts) >= 1
        and overall in VALID_OVERALL
        and overall != "all_falsified"
    )

    return PreMortemResult(
        pass_=pass_,
        overall_verdict=overall,
        surviving_hypothesis_ids=surviving_ids,
        verdicts=verdicts,
        findings=findings,
        raw_response=result.text,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_creation_tokens=result.cache_creation_tokens,
        cost_usd=result.cost_usd,
        latency_ms=latency_ms,
    )
