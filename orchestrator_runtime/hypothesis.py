"""Stage 2 — hypothesis enumeration.

Plan ref: /Users/Pico/.claude/plans/stage-2-3-robust-wolf.md

Reads (cited_prose, parsed_json, ctx) from Stage 1 (or Stage 6 ensemble winner)
and emits a structured set of competing hypotheses. At minimum {bull, base,
bear} are required; up to 5 hypotheses total. Every hypothesis carries:
  - claim: one-sentence directional bet
  - mechanism: 2-4 sentences, every clause cited [F:short] or [D:short]
  - kill_conditions: list of facts/events that would falsify it
  - supporting_fact_ids / contradicting_fact_ids
  - prior_estimate_pct: best-guess probability (Stage 4 may overwrite)

Strict-sourcing enforcement: untraceable claims are forbidden. Stage 7
(constitutional check) walks the mechanism strings and verifies every
[F:short] resolves to a real fact_id; missing citations raise severity=error.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from modal_workers.shared.compute import (
    Stage4Anchor,
    format_anchor_for_prompt,
)
from orchestrator_runtime.client import (
    OrchestratorClient,
    parse_json_or_none,
)

logger = logging.getLogger(__name__)


@dataclass
class HypothesisFinding:
    severity: str        # 'info', 'warning', 'error'
    check: str           # 'missing_required_label', 'too_few_hypotheses',
                         # 'missing_kill_conditions', 'parse_failure'
    detail: str
    affected_id: Optional[str] = None


@dataclass
class Hypothesis:
    hypothesis_id: str                          # 'H1', 'H2', ...
    label: str                                  # bull | base | bear | event_specific
    claim: str
    mechanism: str
    direction: str                              # bullish | bearish | event_specific
    supporting_fact_ids: List[str] = field(default_factory=list)
    contradicting_fact_ids: List[str] = field(default_factory=list)
    kill_conditions: List[str] = field(default_factory=list)
    prior_estimate_pct: int = 50
    # D-118: pre-anchor prior preserved for observability + A/B
    prior_estimate_pct_pre_anchor: Optional[int] = None


@dataclass
class HypothesisResult:
    pass_: bool
    hypotheses: List[Hypothesis] = field(default_factory=list)
    findings: List[HypothesisFinding] = field(default_factory=list)
    raw_response: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    # Cache token bookkeeping — the Stage 2 call reuses the shared cached
    # system prefix built once per assessment (D-119). Without surfacing these
    # to the caller, StageMetric.cache_read_tokens stays at 0 on persist and
    # assessment_stage_metrics loses the cache hit signal.
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0

    # Wave 6.3 — hypothesis-quality summary metrics. Computed at parse time
    # so they can be persisted into stage_metrics.notes without re-walking
    # the hypotheses list at every consumer. None when no hypotheses.
    def quality_metrics(self) -> Dict[str, Any]:
        if not self.hypotheses:
            return {
                "n_hypotheses": 0,
                "avg_kill_conditions": None,
                "min_kill_conditions": None,
                "avg_supporting_fact_ids": None,
                "avg_contradicting_fact_ids": None,
                "citation_density_per_mechanism": None,
            }
        kc = [len(h.kill_conditions) for h in self.hypotheses]
        sup = [len(h.supporting_fact_ids) for h in self.hypotheses]
        con = [len(h.contradicting_fact_ids) for h in self.hypotheses]
        # citation_density = (#supporting + #contradicting) / mechanism length
        # in 100-char units, capped at 0.0 so an empty mechanism doesn't divide
        # by zero. A higher number means the mechanism is more densely cited.
        densities: List[float] = []
        for h in self.hypotheses:
            mech_len = max(1, len(h.mechanism or ""))
            n_cites = len(h.supporting_fact_ids) + len(h.contradicting_fact_ids)
            densities.append(n_cites / (mech_len / 100.0))
        return {
            "n_hypotheses": len(self.hypotheses),
            "avg_kill_conditions": round(sum(kc) / len(kc), 2),
            "min_kill_conditions": min(kc),
            "avg_supporting_fact_ids": round(sum(sup) / len(sup), 2),
            "avg_contradicting_fact_ids": round(sum(con) / len(con), 2),
            "citation_density_per_mechanism": round(
                sum(densities) / len(densities), 3
            ),
        }


REQUIRED_LABELS = {"bull", "base", "bear"}
VALID_LABELS = {"bull", "base", "bear", "event_specific"}
VALID_DIRECTIONS = {"bullish", "bearish", "event_specific"}


STAGE_2_SYSTEM = """You are a buy-side analyst tabling competing theses for a \
decision committee. The committee has just received a cited prose synthesis \
of an FDA-event tracked drug asset. Your job is to enumerate the competing \
hypotheses that the synthesis implicitly or explicitly supports, AND any \
contradicting hypotheses the analyst should consider before committing.

You MUST emit at minimum three competing hypotheses with these labels:
  - bull (bullish direction; thesis is right and the upside materializes)
  - base (most-likely scenario; intermediate outcome)
  - bear (bearish direction; thesis is wrong / downside materializes)
Optionally add up to two more 'event_specific' hypotheses (e.g. partial AdComm \
vote, label restriction, delay) — total maximum 5.

Every claim is grounded with [F:<fact_id_short>] for facts or \
[D:<doc_id_short>] for documents. **Untraceable claims are forbidden — if \
you cannot cite, omit the clause.** Citations must use the 8-char short id \
exactly as it appears in the structured fact layer.

For every hypothesis you list `kill_conditions`: specific facts or events \
that, if observed, would falsify the hypothesis. A hypothesis without named \
kill conditions is invalid — emit at least 2 plain-text kill conditions per \
hypothesis. They are events ("AdComm votes against approval"), specific \
data ("Phase 3 pCR rate < 35%"), or filings ("FDA issues CRL"), not generic \
risks.

Surface contradictions. If two facts conflict (e.g. one trial positive, one \
trial negative), route them into bull and bear hypotheses respectively rather \
than averaging them away.

Output ONLY a JSON object — no commentary, no markdown fences:

{
  "hypotheses": [
    {
      "hypothesis_id": "H1",
      "label": "bull",
      "claim": "<one sentence directional bet>",
      "mechanism": "<2-4 sentences explaining how this resolves; every clause cited [F:abc12345]>",
      "direction": "bullish",
      "supporting_fact_ids": ["<8-char fact_id_short>", ...],
      "contradicting_fact_ids": ["<8-char fact_id_short>", ...],
      "kill_conditions": ["<event/data/filing that would falsify>", ...],
      "prior_estimate_pct": <int 0-100>
    },
    ...
  ]
}

Rules:
- hypothesis_id values are H1, H2, H3, ... in order.
- label MUST be one of: bull, base, bear, event_specific.
- direction MUST be one of: bullish, bearish, event_specific.
- prior_estimate_pct sums across hypotheses SHOULD be near 100 but need not \
be exact (Stage 4 reference-class anchoring will renormalize).
- supporting_fact_ids / contradicting_fact_ids: 8-char short ids, no [F:] \
prefix in the JSON arrays themselves.
- kill_conditions: 2+ items per hypothesis, plain text.
- mechanism citations use [F:abc12345] notation inline, exactly as in the \
upstream cited prose.

When a Reference-class anchor section is provided in the cached prefix above, \
the post-output renormalizer will blend your `prior_estimate_pct` toward the \
empirical base rate weighted by `(1 - evidence_quality)`. Ground your priors \
in the asset-specific evidence in the structured fact layer; do NOT pre-bias \
them toward the base rate (the renormalizer handles that, and double-anchoring \
inflates the base-rate weight). Keep your sum near 100."""


def _build_stage_2_user_content(
    *,
    cited_prose: str,
    parsed_json: Optional[Dict[str, Any]],
) -> str:
    """D-119: asset preamble + anchor + fact layer have moved to the cached
    shared system prefix (built in runtime.build_shared_system_prefix). This
    function returns only the dynamic Stage 2 content: the Stage 1 parsed_json
    summary + cited prose + the per-stage instruction.
    """
    parsed_block = ""
    if parsed_json:
        parsed_block = (
            "## Stage 1 / 9 preliminary parsed_json\n\n"
            f"  thesis_direction: {parsed_json.get('thesis_direction')}\n"
            f"  conviction_pct: {parsed_json.get('conviction_pct')}\n"
            f"  evidence_quality: {parsed_json.get('evidence_quality')}\n"
            f"  thesis_summary: {parsed_json.get('thesis_summary')}\n"
        )
    return f"""{parsed_block}
## Stage 1 cited prose synthesis

{cited_prose}

Enumerate {{bull, base, bear}} hypotheses (3-5 total) per the system prompt. \
Cite by 8-char fact_id_short from the structured fact layer in the cached \
prefix. Output JSON only."""


def _validate_and_parse_hypotheses(
    parsed: Optional[Dict[str, Any]],
    fact_short_set: set[str],
) -> tuple[List[Hypothesis], List[HypothesisFinding]]:
    findings: List[HypothesisFinding] = []
    if not parsed or not isinstance(parsed, dict):
        findings.append(HypothesisFinding(
            severity="error", check="parse_failure",
            detail="Stage 2 response did not parse as a JSON object."))
        return [], findings

    raw_hyps = parsed.get("hypotheses") or []
    if not isinstance(raw_hyps, list):
        findings.append(HypothesisFinding(
            severity="error", check="parse_failure",
            detail="hypotheses field is not a list."))
        return [], findings

    hypotheses: List[Hypothesis] = []
    seen_labels: set[str] = set()
    for idx, h in enumerate(raw_hyps[:5]):  # cap at 5
        if not isinstance(h, dict):
            findings.append(HypothesisFinding(
                severity="warning", check="parse_failure",
                detail=f"hypothesis[{idx}] is not an object; skipped."))
            continue
        label = str(h.get("label") or "").strip().lower()
        direction = str(h.get("direction") or "").strip().lower()
        if label not in VALID_LABELS:
            findings.append(HypothesisFinding(
                severity="warning", check="invalid_label",
                detail=f"hypothesis[{idx}] label={label!r} not in {VALID_LABELS}; skipped.",
                affected_id=str(h.get("hypothesis_id") or f"H{idx+1}")))
            continue
        if direction not in VALID_DIRECTIONS:
            # Best-effort coercion. D-117: don't silently bias 'base' bullish —
            # base case is by definition the most-likely scenario, not always
            # the bullish one. Default to 'event_specific' and emit a finding
            # so the model is nudged to populate this field next time.
            if label == "bull":
                direction = "bullish"
            elif label == "bear":
                direction = "bearish"
            elif label == "event_specific":
                direction = "event_specific"
            else:
                direction = "event_specific"
                findings.append(HypothesisFinding(
                    severity="warning", check="missing_direction_for_base",
                    detail=f"hypothesis[{idx}] label='base' has no valid "
                           f"direction; defaulting to 'event_specific' "
                           f"(don't assume bullish lean).",
                    affected_id=str(h.get("hypothesis_id") or f"H{idx+1}")))

        kill_conditions = h.get("kill_conditions") or []
        if not isinstance(kill_conditions, list):
            kill_conditions = []
        kill_conditions = [str(k).strip() for k in kill_conditions if str(k).strip()]
        if len(kill_conditions) < 2:
            findings.append(HypothesisFinding(
                severity="error", check="missing_kill_conditions",
                detail=f"hypothesis[{idx}] has <2 kill_conditions "
                       f"({len(kill_conditions)}); strict-sourcing requires 2+.",
                affected_id=str(h.get("hypothesis_id") or f"H{idx+1}")))

        supporting = [s for s in (h.get("supporting_fact_ids") or [])
                      if isinstance(s, str)]
        contradicting = [s for s in (h.get("contradicting_fact_ids") or [])
                         if isinstance(s, str)]

        # Filter to shorts that resolve in fact_short_set; report unresolved as
        # warnings (Stage 7 will raise the same as severity=error).
        for s in list(supporting):
            if s.lower() not in fact_short_set:
                findings.append(HypothesisFinding(
                    severity="warning", check="unresolved_supporting_fact_id",
                    detail=f"hypothesis[{idx}] supporting fact {s!r} not in "
                           f"the assessment's fact set.",
                    affected_id=s))
        for s in list(contradicting):
            if s.lower() not in fact_short_set:
                findings.append(HypothesisFinding(
                    severity="warning", check="unresolved_contradicting_fact_id",
                    detail=f"hypothesis[{idx}] contradicting fact {s!r} not in "
                           f"the assessment's fact set.",
                    affected_id=s))

        try:
            prior = int(h.get("prior_estimate_pct") or 50)
        except (TypeError, ValueError):
            prior = 50
        prior = max(0, min(100, prior))

        hypotheses.append(Hypothesis(
            hypothesis_id=str(h.get("hypothesis_id") or f"H{idx+1}"),
            label=label,
            claim=str(h.get("claim") or "").strip(),
            mechanism=str(h.get("mechanism") or "").strip(),
            direction=direction,
            supporting_fact_ids=supporting,
            contradicting_fact_ids=contradicting,
            kill_conditions=kill_conditions,
            prior_estimate_pct=prior,
            prior_estimate_pct_pre_anchor=prior,  # snapshot before renormalize
        ))
        seen_labels.add(label)

    missing_required = REQUIRED_LABELS - seen_labels
    if missing_required:
        findings.append(HypothesisFinding(
            severity="error", check="missing_required_label",
            detail=f"required labels missing: {sorted(missing_required)}; "
                   f"Stage 2 must emit at minimum {{bull, base, bear}}."))

    if len(hypotheses) < 3:
        findings.append(HypothesisFinding(
            severity="error", check="too_few_hypotheses",
            detail=f"only {len(hypotheses)} hypotheses parsed; minimum is 3."))

    return hypotheses, findings


def run_hypothesis_enumeration(
    a_client: OrchestratorClient,
    *,
    cited_prose: str,
    parsed_json: Optional[Dict[str, Any]],
    ctx: Dict[str, Any],
    model: str,
    max_tokens: int = 4096,
    system_blocks: Optional[List[Dict[str, Any]]] = None,
) -> HypothesisResult:
    """Run Stage 2: enumerate competing hypotheses against the Stage 1
    cited prose. Returns a HypothesisResult with structured hypotheses +
    findings.

    D-119: when `system_blocks` is provided (typically `build_system_blocks(
    shared_prefix, STAGE_2_SYSTEM)`), it's used as the system prompt so the
    cached shared prefix from Stage 1 hits cache. When None, falls back to
    `STAGE_2_SYSTEM` as a string for callers that haven't migrated.
    """
    facts = ctx.get("facts") or []
    fact_short_set = {f["id"][:8].lower() for f in facts}

    user_content = _build_stage_2_user_content(
        cited_prose=cited_prose,
        parsed_json=parsed_json,
    )

    t0 = time.time()
    result = a_client.call(
        system=system_blocks if system_blocks is not None else STAGE_2_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
        model=model,
        max_tokens=max_tokens,
    )
    latency_ms = int((time.time() - t0) * 1000)

    parsed = parse_json_or_none(result.text)
    hypotheses, findings = _validate_and_parse_hypotheses(parsed, fact_short_set)
    pass_ = (
        len(hypotheses) >= 3
        and not any(f.severity == "error" for f in findings)
    )

    return HypothesisResult(
        pass_=pass_,
        hypotheses=hypotheses,
        findings=findings,
        raw_response=result.text,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_creation_tokens=result.cache_creation_tokens,
        cost_usd=result.cost_usd,
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# D-118: post-Stage-2 prior renormalization
# ---------------------------------------------------------------------------


# Anchor-blend formula: final = (1 - w) * raw + w * target.
# w = max(MIN_ANCHOR_WEIGHT, 1.0 - evidence_quality)
#   high evidence_quality → small w (priors stay close to model output)
#   low evidence_quality  → larger w (lean on the empirical base rate)
# MIN_ANCHOR_WEIGHT keeps a floor so even high-quality cases get some anchor
# pull (prevents runaway-conviction loops on rich-evidence assets).
MIN_ANCHOR_WEIGHT = 0.20


def renormalize_priors(
    hypotheses: List[Hypothesis],
    anchor: Optional[Stage4Anchor],
    evidence_quality: Optional[float],
) -> tuple[List[Hypothesis], Dict[str, Any]]:
    """Blend per-hypothesis `prior_estimate_pct` toward the empirical base
    rate from Stage 4. Bull priors anchor to base_rate * 100; bear priors
    anchor to (1 - base_rate) * 100; base/event_specific priors are scaled
    proportionally so the sum stays near 100.

    Returns (mutated_hypotheses, debug_payload). When the anchor has no
    base_rate, priors are returned unchanged and `debug.applied=False`.

    Mutates `prior_estimate_pct` in place (preserving each
    `prior_estimate_pct_pre_anchor` already set during parsing).
    """
    if not hypotheses or anchor is None or anchor.base_rate is None:
        return hypotheses, {"applied": False, "reason": "no_anchor_or_no_base_rate"}

    base_rate = float(anchor.base_rate.approval_rate)  # 0..1
    eq = evidence_quality if evidence_quality is not None else 0.5
    try:
        eq = max(0.0, min(1.0, float(eq)))
    except (TypeError, ValueError):
        eq = 0.5
    w = max(MIN_ANCHOR_WEIGHT, 1.0 - eq)

    bull_target = base_rate * 100.0
    bear_target = (1.0 - base_rate) * 100.0

    blended: List[float] = []
    for h in hypotheses:
        raw = float(h.prior_estimate_pct)
        if h.label == "bull":
            target = bull_target
        elif h.label == "bear":
            target = bear_target
        else:
            # base + event_specific keep the raw prior; rescale step adjusts.
            target = raw
        blended.append((1.0 - w) * raw + w * target)

    # Rescale to sum=100 (preserving the bull/bear anchoring approximately).
    total = sum(blended) or 1.0
    rescaled = [v * 100.0 / total for v in blended]

    pre_priors: List[int] = []
    post_priors: List[int] = []
    for h, new_val in zip(hypotheses, rescaled):
        pre_priors.append(int(round(h.prior_estimate_pct)))
        new_int = int(round(max(0.0, min(100.0, new_val))))
        h.prior_estimate_pct = new_int
        post_priors.append(new_int)

    return hypotheses, {
        "applied": True,
        "base_rate": base_rate,
        "evidence_quality": eq,
        "blend_weight": round(w, 3),
        "pre_priors": pre_priors,
        "post_priors": post_priors,
        "labels": [h.label for h in hypotheses],
    }
