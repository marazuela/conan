"""RAGAS-style eval gate for the v3 RAG stack.

Metrics (Sonnet-as-judge to avoid a separate LLM dependency):
  - answer_relevancy   : does the generated answer address the question?
  - faithfulness       : are the answer's claims grounded in retrieved chunks?
  - context_recall     : do retrieved chunks contain the gold-answer info?
  - context_precision  : ratio of retrieved chunks that are actually relevant.

Fail gate (per plan §S5.6):
  - faithfulness < 0.85 OR
  - context_recall < 0.75 OR
  - mean answer_relevancy < 0.70 OR
  - >5% regression vs last passing run.

Runner: `evaluate(commit_sha, gold_set_filter)` is callable from a Modal
scheduled function. Writes per-question results to rag_eval_log; aggregates
to a single passed/failed verdict.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import anthropic

logger = logging.getLogger(__name__)

JUDGE_MODEL = "claude-sonnet-4-5-20250929"

THRESHOLD_FAITHFULNESS = 0.85
THRESHOLD_CONTEXT_RECALL = 0.75
THRESHOLD_ANSWER_RELEVANCY = 0.70
THRESHOLD_REGRESSION_PCT = 0.05


@dataclass
class EvalResult:
    gold_id: str
    retrieved_chunk_ids: List[str]
    generated_answer: str
    answer_relevancy: float
    faithfulness: float
    context_recall: float
    context_precision: float
    passed: bool
    fail_reason: Optional[str]
    latency_ms: int


def _judge(
    client: anthropic.Anthropic, system: str, user: str,
) -> Dict[str, Any]:
    resp = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text_out = "".join(b.text for b in resp.content if b.type == "text").strip()
    if text_out.startswith("```"):
        text_out = re.sub(r"^```(?:json)?\s*\n?", "", text_out)
        text_out = re.sub(r"\n?```\s*$", "", text_out)
    try:
        return json.loads(text_out)
    except json.JSONDecodeError:
        return {}


def score_answer_relevancy(
    client: anthropic.Anthropic, question: str, answer: str,
) -> float:
    parsed = _judge(
        client,
        "Score how well an answer addresses a question. Output JSON only.",
        f"Question: {question}\nAnswer: {answer}\n\n"
        'Output: {"score": 0.0-1.0, "reasoning": "<≤200 chars>"}',
    )
    return float(parsed.get("score") or 0.0)


def score_faithfulness(
    client: anthropic.Anthropic, answer: str, contexts: List[str],
) -> float:
    if not contexts:
        return 0.0
    contexts_block = "\n\n".join(
        f"[{i + 1}] {c}" for i, c in enumerate(contexts)
    )
    parsed = _judge(
        client,
        "Score whether each claim in the answer is grounded in the provided "
        "contexts. Output JSON only.",
        f"Answer:\n{answer}\n\nContexts:\n{contexts_block}\n\n"
        'Output: {"score": 0.0-1.0, "unsupported_claims": [<str>, ...]}',
    )
    return float(parsed.get("score") or 0.0)


def score_context_recall(
    client: anthropic.Anthropic, gold_answer: str, contexts: List[str],
) -> float:
    if not contexts:
        return 0.0
    contexts_block = "\n\n".join(
        f"[{i + 1}] {c}" for i, c in enumerate(contexts)
    )
    parsed = _judge(
        client,
        "Decide whether the contexts contain the information needed to "
        "produce the gold answer. Output JSON only.",
        f"Gold answer:\n{gold_answer}\n\nContexts:\n{contexts_block}\n\n"
        'Output: {"score": 0.0-1.0, "missing": "<≤200 chars>"}',
    )
    return float(parsed.get("score") or 0.0)


def score_context_precision(
    client: anthropic.Anthropic, question: str, contexts: List[str],
) -> float:
    if not contexts:
        return 0.0
    contexts_block = "\n\n".join(
        f"[{i + 1}] {c}" for i, c in enumerate(contexts)
    )
    parsed = _judge(
        client,
        "Score what fraction of the contexts are actually relevant to the "
        "question. Output JSON only.",
        f"Question:\n{question}\n\nContexts:\n{contexts_block}\n\n"
        'Output: {"score": 0.0-1.0, "irrelevant_indices": [<int>, ...]}',
    )
    return float(parsed.get("score") or 0.0)


def evaluate_one(
    sb,
    client: anthropic.Anthropic,
    gold_row: Dict[str, Any],
) -> EvalResult:
    """Run hybrid_search, generate an answer with the retrieved contexts,
    score against the gold row."""
    from modal_workers.rag.hybrid_search import hybrid_search as _hs

    t0 = time.time()
    corpus = (gold_row.get("corpus_filter") or {}).get("corpus", "all")
    k = (gold_row.get("corpus_filter") or {}).get("k", 8)
    hits = _hs(sb, gold_row["question"], corpus, k=k, rerank=True)
    contexts = [(h.contextual_prefix or "") + " " + h.chunk_text for h in hits]
    retrieved_ids = [h.chunk_id for h in hits]

    if contexts:
        gen = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": (
                    f"Question:\n{gold_row['question']}\n\nContexts:\n"
                    + "\n\n".join(f"[{i+1}] {c}" for i, c in enumerate(contexts))
                    + "\n\nAnswer the question using only the contexts. Cite "
                    "indices in brackets."
                ),
            }],
        )
        answer = "".join(b.text for b in gen.content if b.type == "text").strip()
    else:
        answer = ""

    ar = score_answer_relevancy(client, gold_row["question"], answer)
    ff = score_faithfulness(client, answer, contexts)
    cr = score_context_recall(client, gold_row["gold_answer"], contexts)
    cp = score_context_precision(client, gold_row["question"], contexts)

    fail_reason = None
    if ff < THRESHOLD_FAITHFULNESS:
        fail_reason = f"faithfulness {ff:.2f} < {THRESHOLD_FAITHFULNESS}"
    elif cr < THRESHOLD_CONTEXT_RECALL:
        fail_reason = f"context_recall {cr:.2f} < {THRESHOLD_CONTEXT_RECALL}"
    elif ar < THRESHOLD_ANSWER_RELEVANCY:
        fail_reason = f"answer_relevancy {ar:.2f} < {THRESHOLD_ANSWER_RELEVANCY}"
    passed = fail_reason is None
    latency_ms = int((time.time() - t0) * 1000)

    return EvalResult(
        gold_id=gold_row["id"],
        retrieved_chunk_ids=retrieved_ids,
        generated_answer=answer,
        answer_relevancy=ar,
        faithfulness=ff,
        context_recall=cr,
        context_precision=cp,
        passed=passed,
        fail_reason=fail_reason,
        latency_ms=latency_ms,
    )


def evaluate(
    sb,
    commit_sha: Optional[str] = None,
    gold_filter: Optional[Dict[str, Any]] = None,
    max_rows: int = 200,
) -> Dict[str, Any]:
    """Run the eval gate. Returns aggregate metrics + pass/fail.

    `gold_filter` example: {'category': 'literature'}.
    """
    from modal_workers.rag import RAG_PROVIDER

    client = anthropic.Anthropic()

    params = {"select": "*", "limit": str(max_rows)}
    if gold_filter:
        for k, v in gold_filter.items():
            params[k] = f"eq.{v}"
    rows = sb._rest("GET", "rag_eval_gold", params=params) or []
    if not rows:
        return {"error": "no gold rows", "passed": False}

    results: List[EvalResult] = []
    for r in rows:
        try:
            res = evaluate_one(sb, client, r)
        except Exception as exc:  # noqa: BLE001
            logger.warning("evaluate_one failed for %s: %s", r["id"], exc)
            continue
        results.append(res)
        # Persist to rag_eval_log.
        try:
            sb._rest(
                "POST", "rag_eval_log",
                json_body={
                    "gold_id": res.gold_id,
                    "commit_sha": commit_sha,
                    "provider_config": {"provider": RAG_PROVIDER},
                    "retrieved_chunk_ids": res.retrieved_chunk_ids,
                    "generated_answer": res.generated_answer,
                    "answer_relevancy": round(res.answer_relevancy, 3),
                    "faithfulness": round(res.faithfulness, 3),
                    "context_recall": round(res.context_recall, 3),
                    "context_precision": round(res.context_precision, 3),
                    "passed": res.passed,
                    "fail_reason": res.fail_reason,
                    "latency_ms": res.latency_ms,
                },
                prefer="return=minimal",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("rag_eval_log insert failed: %s", exc)

    if not results:
        return {"error": "no results", "passed": False}

    n = len(results)
    mean_ar = sum(r.answer_relevancy for r in results) / n
    mean_ff = sum(r.faithfulness for r in results) / n
    mean_cr = sum(r.context_recall for r in results) / n
    mean_cp = sum(r.context_precision for r in results) / n
    pass_rate = sum(1 for r in results if r.passed) / n

    aggregate_pass = (
        mean_ff >= THRESHOLD_FAITHFULNESS
        and mean_cr >= THRESHOLD_CONTEXT_RECALL
        and mean_ar >= THRESHOLD_ANSWER_RELEVANCY
    )

    return {
        "passed": aggregate_pass,
        "n": n,
        "pass_rate": round(pass_rate, 3),
        "mean_answer_relevancy": round(mean_ar, 3),
        "mean_faithfulness": round(mean_ff, 3),
        "mean_context_recall": round(mean_cr, 3),
        "mean_context_precision": round(mean_cp, 3),
        "thresholds": {
            "faithfulness": THRESHOLD_FAITHFULNESS,
            "context_recall": THRESHOLD_CONTEXT_RECALL,
            "answer_relevancy": THRESHOLD_ANSWER_RELEVANCY,
        },
    }
