"""Stage 6 ensemble — N parallel synthesis runs aggregated into one assessment.

Plan §"Stage 6 — Ensemble (Batch API)": N=7 via Batch (50% cost discount,
~1h latency, separate quota from streaming) for `scheduled` runs; N=3
streaming for `cross_source`/`market_move` hot triggers.

Why ensemble: variance across independent runs measures the model's own
uncertainty. Low dispersion + high mean = high confidence; high dispersion
indicates real ambiguity in the evidence base, regardless of mean direction.
Plan §"shrinkage" formula: final_conviction = mean - λ * stddev (λ ≈ 0.5).

Aggregation:
  thesis_direction → majority vote across N
  conviction_pct → mean shrunken by dispersion
  ensemble_dispersion → stddev of conviction_pct across N
  evidence_quality → mean across N
  key_facts / uncertainties → union (deduped by fact_id_short / question)
  cited_prose_blocks → from the run with conviction closest to mean

Modes:
  streaming (--mode streaming, --ensemble-n 3): N concurrent live API calls.
    Hits Tier-1 rate limits if N×input>30K/min — keep N≤3 at this tier.
  batch    (--mode batch, --ensemble-n 7): submit a batch of N requests via
    Messages Batches API; poll until complete. ~1h max; 50% pricing.

This module wraps Stage 1 + Stage 9 calls. The single-shot path remains in
runtime.run_one() for triggers where ensemble is too expensive.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import anthropic

from orchestrator_runtime.client import (
    OrchestratorClient,
    estimate_cost,
    model_accepts_temperature,
    parse_json_or_none,
)

logger = logging.getLogger(__name__)


@dataclass
class EnsembleRun:
    run_idx: int
    cited_prose: str
    direction: str
    conviction_pct: float
    evidence_quality: Optional[float]
    parsed_json: Dict[str, Any]
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    latency_ms: int
    custom_id: Optional[str] = None


@dataclass
class EnsembleResult:
    n: int
    runs: List[EnsembleRun]
    direction: str                  # majority-vote winner
    direction_distribution: Dict[str, int]
    raw_mean_conviction: float
    dispersion: float               # stddev of conviction across runs
    shrinkage_factor: float
    final_conviction: float         # raw_mean - shrinkage_factor * dispersion (clamped 0..100)
    evidence_quality_mean: Optional[float]
    cited_prose_winner: str         # prose from the run closest to the mean
    aggregated_key_facts: List[Dict[str, Any]]
    aggregated_uncertainties: List[Dict[str, Any]]
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_thinking_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0
    total_latency_ms: int = 0
    mode: str = "streaming"


SHRINKAGE_FACTOR_LAMBDA = 0.5

# Dispersion-based abstain. The ensemble's shrinkage formula
# (final_conviction = mean - 0.5 * stddev) already penalizes disagreement,
# but doesn't stop a borderline-immediate signal from getting an email
# when the ensemble actively disagrees on DIRECTION. This gate downgrades
# the band from 'immediate' to 'watchlist' when either:
#   (a) ensemble stddev > ENSEMBLE_DISPERSION_ABSTAIN_PCT  — convictions diverge wildly, OR
#   (b) majority-direction fraction < ENSEMBLE_DIRECTION_ABSTAIN_FRAC — direction
#       vote is too contested.
# Env flag DISABLE_ENSEMBLE_DISPERSION_ABSTAIN=1 reverts to legacy (no downgrade).
ENSEMBLE_DISPERSION_ABSTAIN_PCT = float(
    os.environ.get("ORCH_ENSEMBLE_DISPERSION_ABSTAIN_PCT", "15.0")
)
ENSEMBLE_DIRECTION_ABSTAIN_FRAC = float(
    os.environ.get("ORCH_ENSEMBLE_DIRECTION_ABSTAIN_FRAC", "0.6")
)


def compute_dispersion_abstain(
    ensemble_payload: Optional[Dict[str, Any]],
    band: str,
) -> tuple[str, Optional[str]]:
    """Decide whether ensemble disagreement should downgrade an immediate
    band to watchlist. Returns (final_band, abstain_reason_or_none).

    Only applies when N≥2 (no dispersion exists at N=1) and current band is
    'immediate' — never promotes a lower band. When the dispersion gate fires
    we record a structured reason so downstream observability can attribute
    the band downgrade to ensemble disagreement rather than a low conviction.
    """
    if os.environ.get("ORCH_DISABLE_ENSEMBLE_DISPERSION_ABSTAIN") == "1":
        return band, None
    if band != "immediate" or not ensemble_payload:
        return band, None
    n = ensemble_payload.get("n") or 0
    if n < 2:
        return band, None

    dispersion = ensemble_payload.get("dispersion")
    if isinstance(dispersion, (int, float)) and dispersion > ENSEMBLE_DISPERSION_ABSTAIN_PCT:
        return "watchlist", (
            f"ensemble_dispersion_abstain "
            f"(stddev={dispersion:.1f} > {ENSEMBLE_DISPERSION_ABSTAIN_PCT:.1f}pp; n={n})"
        )

    direction_dist = ensemble_payload.get("direction_distribution") or {}
    if isinstance(direction_dist, dict) and direction_dist:
        max_count = max(direction_dist.values())
        total = sum(direction_dist.values())
        if total > 0:
            majority_frac = max_count / total
            if majority_frac < ENSEMBLE_DIRECTION_ABSTAIN_FRAC:
                return "watchlist", (
                    f"ensemble_direction_abstain "
                    f"(majority_frac={majority_frac:.2f} < "
                    f"{ENSEMBLE_DIRECTION_ABSTAIN_FRAC:.2f}; "
                    f"distribution={direction_dist})"
                )

    return band, None


def _stage_1_request_params(
    *,
    model: str,
    max_tokens: int,
    temperature: float,
    stage_1_system: Any,
    stage_1_user_content: str,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": stage_1_system,
        "messages": [{"role": "user", "content": stage_1_user_content}],
    }
    if model_accepts_temperature(model):
        params["temperature"] = temperature
    else:
        logger.info(
            "Omitting deprecated temperature parameter for ensemble model %s",
            model,
        )
    return params


# ===========================================================================
# Streaming mode (N concurrent live calls)
# ===========================================================================

def run_streaming_ensemble(
    a_client: OrchestratorClient,
    *,
    stage_1_system: Any,                  # str | List[Dict[str, Any]]
    stage_1_user_content: str,
    stage_9_system: str,
    n: int,
    model: str,
    extractor_model: str,
    temperature: float = 0.8,
    max_tokens_synth: int = 4096,
    max_tokens_extract: int = 8192,
) -> EnsembleResult:
    """Run N synthesis+extract pairs sequentially. For Tier-1 rate limits we
    serialize to stay under the 30K input tokens/min cap; once Tier-2+ is
    available, switch to asyncio.gather for true parallelism."""
    runs: List[EnsembleRun] = []
    last_exc: Optional[Exception] = None
    for idx in range(n):
        logger.info("Ensemble streaming run %d/%d", idx + 1, n)
        try:
            r = _run_one_streaming(
                a_client, stage_1_system, stage_1_user_content,
                stage_9_system, model, extractor_model, idx,
                temperature, max_tokens_synth, max_tokens_extract,
            )
        except anthropic.APIError as exc:
            logger.warning("Ensemble run %d failed: %s; continuing", idx + 1, exc)
            last_exc = exc
            continue
        if r:
            runs.append(r)
            logger.info("Run %d/%d: direction=%s conviction=%.1f cost=$%.3f",
                        idx + 1, n, r.direction, r.conviction_pct, r.cost_usd)

    if not runs:
        cause = (
            f"; last error: {type(last_exc).__name__}: {last_exc}"
            if last_exc is not None
            else "; no API exception captured (Stage 9 JSON parse failed every slot)"
        )
        raise RuntimeError(f"Ensemble produced 0 successful runs out of {n} attempted{cause}")

    return _aggregate(runs, mode="streaming")


def _run_one_streaming(
    a_client: OrchestratorClient,
    stage_1_system: Any,                  # str | List[Dict[str, Any]]; forwarded as-is
    stage_1_user_content: str,
    stage_9_system: str,
    model: str,
    extractor_model: str,
    run_idx: int,
    temperature: float,
    max_tokens_synth: int,
    max_tokens_extract: int,
) -> Optional[EnsembleRun]:
    stage_1_kwargs = _stage_1_request_params(
        model=model,
        max_tokens=max_tokens_synth,
        temperature=temperature,
        stage_1_system=stage_1_system,
        stage_1_user_content=stage_1_user_content,
    )
    s1 = a_client.call(
        system=stage_1_kwargs["system"],
        messages=stage_1_kwargs["messages"],
        model=stage_1_kwargs["model"],
        max_tokens=stage_1_kwargs["max_tokens"],
        temperature=stage_1_kwargs.get("temperature"),
    )
    s1_text = s1.text

    # Stage 9 is deterministic, so do not pass a temperature override.
    s9 = a_client.call(
        system=stage_9_system,
        messages=[{"role": "user", "content": f"Cited prose to extract:\n\n{s1_text}"}],
        model=extractor_model,
        max_tokens=max_tokens_extract,
    )
    s9_text = s9.text
    parsed = parse_json_or_none(s9_text)

    if not parsed:
        logger.warning("Run %d: Stage 9 JSON parse failed", run_idx)
        return None

    direction = parsed.get("thesis_direction", "neutral")
    if direction not in {"long", "short", "neutral", "straddle"}:
        direction = "neutral"
    try:
        conviction = float(parsed.get("conviction_pct", 50.0))
    except (TypeError, ValueError):
        conviction = 50.0
    conviction = max(0.0, min(100.0, conviction))
    evidence_quality = parsed.get("evidence_quality")
    try:
        evidence_quality = float(evidence_quality) if evidence_quality is not None else None
    except (TypeError, ValueError):
        evidence_quality = None
    if evidence_quality is not None:
        evidence_quality = max(0.0, min(1.0, evidence_quality))

    return EnsembleRun(
        run_idx=run_idx,
        cited_prose=s1_text,
        direction=direction,
        conviction_pct=conviction,
        evidence_quality=evidence_quality,
        parsed_json=parsed,
        input_tokens=s1.input_tokens + s9.input_tokens,
        output_tokens=s1.output_tokens + s9.output_tokens,
        thinking_tokens=s1.thinking_tokens + s9.thinking_tokens,
        cache_read_tokens=s1.cache_read_tokens + s9.cache_read_tokens,
        cache_creation_tokens=s1.cache_creation_tokens + s9.cache_creation_tokens,
        cost_usd=s1.cost_usd + s9.cost_usd,
        latency_ms=s1.latency_ms + s9.latency_ms,
    )


# ===========================================================================
# Batch mode (N submitted via Messages Batches API)
# ===========================================================================

def run_batch_ensemble(
    a_client: OrchestratorClient,
    *,
    stage_1_system: Any,                  # str | List[Dict[str, Any]]
    stage_1_user_content: str,
    stage_9_system: str,
    n: int,
    model: str,
    extractor_model: str,
    temperature: float = 0.8,
    max_tokens_synth: int = 4096,
    max_tokens_extract: int = 8192,
    poll_interval_s: float = 30.0,
    max_wait_s: float = 3600.0,
) -> EnsembleResult:
    """Submit N Stage-1 syntheses via Messages Batches API. After they
    complete, run Stage 9 extractions on each (cheap, sequential, single-shot
    cost). 50% Batch discount + separate quota from streaming."""
    # Step 1: build N synthesis requests
    s1_requests = []
    for idx in range(n):
        s1_requests.append({
            "custom_id": f"ensemble-s1-{idx}",
            "params": _stage_1_request_params(
                model=model,
                max_tokens=max_tokens_synth,
                temperature=temperature,
                stage_1_system=stage_1_system,
                stage_1_user_content=stage_1_user_content,
            ),
        })

    logger.info("Submitting Batch with %d Stage-1 requests", n)
    batch = a_client._client.messages.batches.create(requests=s1_requests)
    batch_id = batch.id
    logger.info("Batch submitted: id=%s status=%s", batch_id, batch.processing_status)

    # Step 2: poll until complete
    waited = 0.0
    while batch.processing_status not in {"ended", "canceled"}:
        if waited > max_wait_s:
            raise TimeoutError(f"Batch {batch_id} did not complete within {max_wait_s}s")
        time.sleep(poll_interval_s)
        waited += poll_interval_s
        batch = a_client._client.messages.batches.retrieve(batch_id)
        logger.info("Batch %s: status=%s waited=%ds processing=%d succeeded=%d errored=%d",
                    batch_id, batch.processing_status, int(waited),
                    batch.request_counts.processing,
                    batch.request_counts.succeeded,
                    batch.request_counts.errored)

    if batch.processing_status == "canceled":
        raise RuntimeError(f"Batch {batch_id} was canceled")

    # Step 3: collect Stage 1 results
    s1_results: Dict[str, Any] = {}
    for result in a_client._client.messages.batches.results(batch_id):
        cid = result.custom_id
        if result.result.type == "succeeded":
            s1_results[cid] = result.result.message
        else:
            err = getattr(result.result, "error", None)
            logger.warning("Batch run %s errored: %s", cid, err)

    if not s1_results:
        raise RuntimeError(f"Batch {batch_id} produced no successful runs")

    # Step 4: per result, run Stage 9 streaming (cheap, sequential)
    runs: List[EnsembleRun] = []
    for cid, msg in s1_results.items():
        idx = int(cid.rsplit("-", 1)[-1])
        s1_text = "".join(b.text for b in msg.content if b.type == "text")
        s1_in = msg.usage.input_tokens
        s1_out = msg.usage.output_tokens
        s1_thinking = sum(getattr(b, "tokens", 0) for b in msg.content if b.type == "thinking")
        s1_cache_read = getattr(msg.usage, "cache_read_input_tokens", 0) or 0
        s1_cache_create = getattr(msg.usage, "cache_creation_input_tokens", 0) or 0
        # Batch discount: 50% off the full cache-aware list price.
        s1_cost = estimate_cost(
            model,
            input_tokens=s1_in,
            output_tokens=s1_out,
            cache_creation_tokens=s1_cache_create,
            cache_read_tokens=s1_cache_read,
        ) * 0.5

        try:
            # Route through OrchestratorClient.call so we get budget
            # accounting, retry on 429/529, and accurate cache cost. The
            # earlier raw-SDK call here meant batch-ensemble Stage 9 spend
            # was invisible to the per-run hard-kill ceiling and could push
            # the run over without triggering BudgetExceededError.
            s9 = a_client.call(
                system=stage_9_system,
                messages=[{"role": "user", "content": f"Cited prose to extract:\n\n{s1_text}"}],
                model=extractor_model,
                max_tokens=max_tokens_extract,
            )
            s9_text = s9.text
            s9_in = s9.input_tokens
            s9_out = s9.output_tokens
            s9_cache_read = s9.cache_read_tokens
            s9_cache_create = s9.cache_creation_tokens
            s9_cost = s9.cost_usd
            s9_latency = s9.latency_ms
        except anthropic.APIError as exc:
            logger.warning("Batch result %s: Stage 9 failed: %s", cid, exc)
            continue

        parsed = parse_json_or_none(s9_text)
        if not parsed:
            logger.warning("Batch result %s: Stage 9 JSON parse failed", cid)
            continue

        direction = parsed.get("thesis_direction", "neutral")
        if direction not in {"long", "short", "neutral", "straddle"}:
            direction = "neutral"
        try:
            conviction = float(parsed.get("conviction_pct", 50.0))
        except (TypeError, ValueError):
            conviction = 50.0
        conviction = max(0.0, min(100.0, conviction))
        evidence_quality = parsed.get("evidence_quality")
        try:
            evidence_quality = float(evidence_quality) if evidence_quality is not None else None
        except (TypeError, ValueError):
            evidence_quality = None
        if evidence_quality is not None:
            evidence_quality = max(0.0, min(1.0, evidence_quality))

        runs.append(EnsembleRun(
            run_idx=idx,
            custom_id=cid,
            cited_prose=s1_text,
            direction=direction,
            conviction_pct=conviction,
            evidence_quality=evidence_quality,
            parsed_json=parsed,
            input_tokens=s1_in + s9_in,
            output_tokens=s1_out + s9_out,
            thinking_tokens=s1_thinking,
            cache_read_tokens=s1_cache_read + s9_cache_read,
            cache_creation_tokens=s1_cache_create + s9_cache_create,
            cost_usd=s1_cost + s9_cost,
            latency_ms=s9_latency,
        ))

    if not runs:
        raise RuntimeError(f"Batch {batch_id} ensemble: 0 runs survived Stage 9")

    return _aggregate(runs, mode="batch")


# ===========================================================================
# Aggregation
# ===========================================================================

def _aggregate(runs: List[EnsembleRun], mode: str) -> EnsembleResult:
    n = len(runs)

    # Direction: majority vote
    direction_counts = Counter(r.direction for r in runs)
    direction, _ = direction_counts.most_common(1)[0]
    direction_distribution = dict(direction_counts)

    # Conviction stats
    convictions = [r.conviction_pct for r in runs]
    raw_mean = sum(convictions) / n
    dispersion = statistics.stdev(convictions) if n >= 2 else 0.0
    final_conviction = raw_mean - SHRINKAGE_FACTOR_LAMBDA * dispersion
    final_conviction = max(0.0, min(100.0, final_conviction))

    # Evidence quality
    eqs = [r.evidence_quality for r in runs if r.evidence_quality is not None]
    eq_mean = sum(eqs) / len(eqs) if eqs else None

    # Pick the cited prose from the run closest to the mean
    closest = min(runs, key=lambda r: abs(r.conviction_pct - raw_mean))
    cited_prose_winner = closest.cited_prose

    # Aggregate key_facts (union, deduped by fact_id_short)
    seen_kf: Dict[str, Dict[str, Any]] = {}
    for r in runs:
        for kf in (r.parsed_json.get("key_facts") or []):
            sid = kf.get("fact_id_short") or kf.get("text", "")[:40]
            if sid and sid not in seen_kf:
                seen_kf[sid] = kf
    aggregated_kf = list(seen_kf.values())

    # Aggregate uncertainties (union by question)
    seen_q: Dict[str, Dict[str, Any]] = {}
    for r in runs:
        for u in (r.parsed_json.get("uncertainties") or []):
            q = (u.get("question") or "")[:80]
            if q and q not in seen_q:
                seen_q[q] = u
    aggregated_unc = list(seen_q.values())

    return EnsembleResult(
        n=n,
        runs=runs,
        direction=direction,
        direction_distribution=direction_distribution,
        raw_mean_conviction=raw_mean,
        dispersion=dispersion,
        shrinkage_factor=SHRINKAGE_FACTOR_LAMBDA,
        final_conviction=final_conviction,
        evidence_quality_mean=eq_mean,
        cited_prose_winner=cited_prose_winner,
        aggregated_key_facts=aggregated_kf,
        aggregated_uncertainties=aggregated_unc,
        total_input_tokens=sum(r.input_tokens for r in runs),
        total_output_tokens=sum(r.output_tokens for r in runs),
        total_thinking_tokens=sum(r.thinking_tokens for r in runs),
        total_cache_read_tokens=sum(r.cache_read_tokens for r in runs),
        total_cache_creation_tokens=sum(r.cache_creation_tokens for r in runs),
        total_cost_usd=sum(r.cost_usd for r in runs),
        total_latency_ms=sum(r.latency_ms for r in runs),
        mode=mode,
    )
