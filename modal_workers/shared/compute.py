"""Compute helpers — Stage 4 reference-class anchoring + Stage 8 isotonic
calibration + post-mortem refit math.

Plan §"compute-mcp" (plugin README, Phase 4.7) lists five tools:
  - base_rate              : empirical FDA approval rate for a reference class
  - similar_resolved_cases : top-k resolved cases sharing the class
  - isotonic               : apply / fit isotonic regression for conviction
  - brier_calibration      : Brier score (re-exported from fda_calibration_math)
  - verify_claim           : RAG-backed claim check (deferred — needs Phase 4.7
                             internal_rag_mcp; raises NotImplementedError so
                             callers get a clear signal instead of silent skip)

This module is the *canonical* implementation. The runtime imports the
DB-aware helpers in-process for Stage 4 (no MCP overhead on the critical
path). The plugin's compute_mcp.py FastMCP server (Phase 4.7) wraps the
same functions for Cowork bulk and operator-triggered tool use.

Pure-math helpers live in fda_calibration_math (brier_score, etc.); this
module re-exports brier_score for parity with the MCP tool surface.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from modal_workers.shared.fda_calibration_math import brier_score  # noqa: F401
from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)


def get_internal_config(
    sb: SupabaseClient,
    key: str,
    default: Optional[str] = None,
) -> Optional[str]:
    """Read a single string value from public.internal_config.

    Returns `default` (None unless overridden) when the key is absent or the
    fetch fails. Used by feature flags such as `renormalize_priors_dry_run`
    where a missing key should mean "safest behavior" — i.e. dry-run is the
    default until the operator explicitly flips it to 'false'.
    """
    try:
        rows = sb._rest(
            "GET", "internal_config",
            params={"select": "value", "key": f"eq.{key}", "limit": "1"},
        ) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_internal_config(%s) failed: %s", key, exc)
        return default
    if not rows:
        return default
    return rows[0].get("value", default)


def is_renormalize_priors_dry_run(sb: SupabaseClient) -> bool:
    """True when renormalize_priors should compute deltas but NOT mutate
    `hypothesis.prior_estimate_pct`. Default: True (safest — preserves
    pre-PR-1 behavior when the flag is absent).
    """
    val = get_internal_config(sb, "renormalize_priors_dry_run", default="true")
    return (val or "").strip().lower() != "false"


def compute_document_set_hash(
    sb: SupabaseClient,
    asset_id: str,
) -> Optional[str]:
    """md5 over the asset's material primary asset_documents.document_id set.

    Mirror of the reactor's `computeDocSetHash` (supabase/functions/reactor/
    index.ts) — must use identical criteria so the reactor's content-dedup
    check can compare apples to apples against
    convergence_assessments.document_set_hash.

    Returns None when the asset has zero material primary docs; the reactor
    treats None as "skip content-dedup" so cold-start assets still enqueue.
    """
    rows = sb._rest(
        "GET", "asset_documents",
        params={
            "select": "document_id",
            "asset_id": f"eq.{asset_id}",
            "link_type": "eq.primary",
            "is_material": "eq.true",
        },
    ) or []
    if not rows:
        return None
    doc_ids = sorted(r["document_id"] for r in rows if r.get("document_id"))
    if not doc_ids:
        return None
    return hashlib.md5(",".join(doc_ids).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Stage 4: reference-class anchoring
# ---------------------------------------------------------------------------


@dataclass
class BaseRateResult:
    """One row of `reference_class_base_rates`, materialized for callers."""
    reference_class: str
    n_cases: int
    approval_rate: float                          # 0.0 – 1.0
    approval_rate_ci_low: Optional[float] = None
    approval_rate_ci_high: Optional[float] = None
    median_realized_move_pct: Optional[float] = None
    refit_at: Optional[str] = None

    def as_pct(self) -> float:
        return self.approval_rate * 100.0

    def ci_pct(self) -> Optional[Tuple[float, float]]:
        if self.approval_rate_ci_low is None or self.approval_rate_ci_high is None:
            return None
        return (self.approval_rate_ci_low * 100.0,
                self.approval_rate_ci_high * 100.0)


@dataclass
class SimilarResolvedCase:
    """One row from `eval_harness` joined with its asset's identifying fields."""
    eval_harness_id: str
    asset_id: str
    asset_ticker: Optional[str]
    asset_drug_name: Optional[str]
    reference_assessment_date: str
    realized_outcome: str
    realized_move_pct: Optional[float]
    notes: Optional[str] = None


@dataclass
class Stage4Anchor:
    """Materialized Stage 4 output threaded into the runtime context."""
    reference_class: Optional[str]
    base_rate: Optional[BaseRateResult]
    similar_cases: List[SimilarResolvedCase] = field(default_factory=list)

    @property
    def has_signal(self) -> bool:
        """True when we have ANY anchor information to feed forward."""
        return self.base_rate is not None or bool(self.similar_cases)


def compute_base_rate(
    sb: SupabaseClient,
    reference_class: Optional[str],
) -> Optional[BaseRateResult]:
    """Look up the empirical approval rate for a reference class.

    Returns None when reference_class is null/empty, or when the row hasn't
    been refit yet. Callers are expected to degrade gracefully (no anchor →
    Stage 1 sees `(unknown)`, Stage 7 skips the base-rate divergence check).
    """
    if not reference_class:
        return None
    rows = sb._rest(
        "GET", "reference_class_base_rates",
        params={
            "select": ("reference_class,n_cases,approval_rate,approval_rate_ci_low,"
                       "approval_rate_ci_high,median_realized_move_pct,refit_at"),
            "reference_class": f"eq.{reference_class}",
            "limit": "1",
        },
    ) or []
    if not rows:
        return None
    r = rows[0]
    try:
        return BaseRateResult(
            reference_class=r["reference_class"],
            n_cases=int(r["n_cases"]),
            approval_rate=float(r["approval_rate"]),
            approval_rate_ci_low=(float(r["approval_rate_ci_low"])
                                  if r.get("approval_rate_ci_low") is not None else None),
            approval_rate_ci_high=(float(r["approval_rate_ci_high"])
                                   if r.get("approval_rate_ci_high") is not None else None),
            median_realized_move_pct=(float(r["median_realized_move_pct"])
                                      if r.get("median_realized_move_pct") is not None else None),
            refit_at=r.get("refit_at"),
        )
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("compute_base_rate: bad row for %s: %s", reference_class, exc)
        return None


def similar_resolved_cases(
    sb: SupabaseClient,
    reference_class: Optional[str],
    *,
    k: int = 5,
    exclude_asset_id: Optional[str] = None,
    holdout_only: bool = False,
) -> List[SimilarResolvedCase]:
    """Top-k resolved cases sharing the reference_class signature.

    Joins eval_harness with fda_assets via embedded resource. The current
    asset is excluded so an asset can't anchor against itself during a
    backtest replay. holdout_only=True restricts to the held-out gold set;
    most callers want False (broader anchoring corpus).
    """
    if not reference_class:
        return []
    params: Dict[str, str] = {
        "select": (
            "id,asset_id,reference_assessment_date,realized_outcome,"
            "realized_outcome_data,notes,"
            "fda_assets!inner(reference_class_signature,ticker,drug_name)"
        ),
        "fda_assets.reference_class_signature": f"eq.{reference_class}",
        "order": "reference_assessment_date.desc",
        "limit": str(max(k, 1)),
    }
    if holdout_only:
        params["is_holdout"] = "eq.true"
    if exclude_asset_id:
        params["asset_id"] = f"neq.{exclude_asset_id}"

    rows = sb._rest("GET", "eval_harness", params=params) or []
    out: List[SimilarResolvedCase] = []
    for r in rows:
        asset = r.get("fda_assets") or {}
        outcome_data = r.get("realized_outcome_data") or {}
        move = outcome_data.get("realized_move_pct")
        try:
            move_f: Optional[float] = float(move) if move is not None else None
        except (TypeError, ValueError):
            move_f = None
        out.append(SimilarResolvedCase(
            eval_harness_id=r["id"],
            asset_id=r["asset_id"],
            asset_ticker=asset.get("ticker"),
            asset_drug_name=asset.get("drug_name"),
            reference_assessment_date=r["reference_assessment_date"],
            realized_outcome=r["realized_outcome"],
            realized_move_pct=move_f,
            notes=r.get("notes"),
        ))
    return out


def build_stage_4_anchor(
    sb: SupabaseClient,
    *,
    reference_class: Optional[str],
    exclude_asset_id: Optional[str] = None,
    k_similar: int = 5,
) -> Stage4Anchor:
    """One-shot Stage 4 builder: base rate + similar cases for the runtime."""
    return Stage4Anchor(
        reference_class=reference_class,
        base_rate=compute_base_rate(sb, reference_class),
        similar_cases=similar_resolved_cases(
            sb, reference_class, k=k_similar, exclude_asset_id=exclude_asset_id),
    )


def format_anchor_for_prompt(anchor: Stage4Anchor) -> Optional[str]:
    """Render a Stage 4 anchor as a Stage 1 prompt section.

    Returns None when there's no signal to inject, so the caller can skip
    the section header and avoid telling the model "(no anchor available)"
    on every cold-start asset.
    """
    if not anchor.has_signal:
        return None

    lines: List[str] = []
    cls = anchor.reference_class or "(unknown)"
    if anchor.base_rate is not None:
        br = anchor.base_rate
        ci = br.ci_pct()
        ci_str = (f" [{ci[0]:.1f}–{ci[1]:.1f}%]" if ci is not None else "")
        move_str = (f"; median realized move ±{br.median_realized_move_pct:.1f}%"
                    if br.median_realized_move_pct is not None else "")
        lines.append(
            f"Reference class: `{cls}` — empirical base rate from "
            f"{br.n_cases} resolved cases: P(approval) ≈ "
            f"{br.as_pct():.1f}%{ci_str}{move_str}."
        )
    else:
        lines.append(f"Reference class: `{cls}` — no fitted base rate available.")

    if anchor.similar_cases:
        lines.append("")
        lines.append("Similar resolved cases (anchor your conviction against these):")
        for c in anchor.similar_cases:
            ticker = c.asset_ticker or "?"
            drug = c.asset_drug_name or "?"
            move = (f", {c.realized_move_pct:+.1f}%"
                    if c.realized_move_pct is not None else "")
            lines.append(
                f"- {ticker} / {drug} ({c.reference_assessment_date}): "
                f"{c.realized_outcome}{move}"
            )

    lines.append("")
    lines.append(
        "Calibration discipline: your conviction_pct should not diverge from "
        "the base rate by more than ~30 points unless the asset-specific "
        "evidence in the fact layer materially supports the divergence. "
        "Either cite the divergence-justifying facts explicitly, or pull "
        "conviction toward the base rate."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage 8: isotonic calibration
# ---------------------------------------------------------------------------


def fit_isotonic_curve(
    raw_predictions: Sequence[float],
    outcomes: Sequence[int],
) -> Dict[str, Any]:
    """Pool-adjacent-violators isotonic regression (no scikit-learn dep).

    raw_predictions are 0–1 floats; outcomes are 0/1. Returns a dict with
    'knots' (sorted (x,y) pairs) suitable for stashing in
    calibration_curves.curve_data and reapplying via apply_isotonic_calibration.

    Raises ValueError on shape mismatch / empty input / non-binary outcomes.
    """
    n = len(raw_predictions)
    if n != len(outcomes):
        raise ValueError(
            f"fit_isotonic_curve: shape mismatch: predictions={n} outcomes={len(outcomes)}")
    if n == 0:
        raise ValueError("fit_isotonic_curve: empty input")
    for y in outcomes:
        if y not in (0, 1):
            raise ValueError(f"fit_isotonic_curve: outcome must be 0 or 1, got {y!r}")

    # Sort by raw prediction
    paired = sorted(zip((float(p) for p in raw_predictions),
                        (int(y) for y in outcomes)),
                    key=lambda t: t[0])

    # Pool Adjacent Violators. Each "block" carries (sum_y, count, x_first, x_last).
    blocks: List[List[float]] = []  # [sum_y, count, x_first, x_last]
    for x, y in paired:
        blocks.append([float(y), 1.0, x, x])
        # Merge while the previous block's mean exceeds the current block's mean
        while len(blocks) >= 2:
            prev = blocks[-2]
            curr = blocks[-1]
            prev_mean = prev[0] / prev[1]
            curr_mean = curr[0] / curr[1]
            if prev_mean <= curr_mean:
                break
            merged = [prev[0] + curr[0], prev[1] + curr[1], prev[2], curr[3]]
            blocks.pop()
            blocks.pop()
            blocks.append(merged)

    # Knots: (x_midpoint_of_block, mean_y) pairs, monotone non-decreasing in y
    knots = [
        {"x": (b[2] + b[3]) / 2.0, "y": b[0] / b[1], "n": int(b[1])}
        for b in blocks
    ]
    return {"knots": knots, "n_training": n}


def apply_isotonic_calibration(
    raw_prediction: float,
    curve: Optional[Dict[str, Any]],
) -> float:
    """Map raw_prediction (0–1) through a fitted curve via linear interp
    between knots. With no curve (cold start), returns raw_prediction
    unchanged. Outside-knot inputs clamp to the nearest knot's y.
    """
    p = max(0.0, min(1.0, float(raw_prediction)))
    if not curve:
        return p
    knots = curve.get("knots") or []
    if not knots:
        return p
    if p <= knots[0]["x"]:
        return float(knots[0]["y"])
    if p >= knots[-1]["x"]:
        return float(knots[-1]["y"])
    # Linear interp between flanking knots
    for left, right in zip(knots, knots[1:]):
        lx, ly = float(left["x"]), float(left["y"])
        rx, ry = float(right["x"]), float(right["y"])
        if lx <= p <= rx:
            if rx == lx:
                return ly
            t = (p - lx) / (rx - lx)
            return ly + t * (ry - ly)
    return p  # unreachable


def get_active_calibration_curve(sb: SupabaseClient) -> Optional[Dict[str, Any]]:
    """Read the single is_active=true row from calibration_curves, or None."""
    rows = sb._rest(
        "GET", "calibration_curves",
        params={
            "select": "version,curve_data,n_training_samples,brier_score",
            "is_active": "eq.true",
            "limit": "1",
        },
    ) or []
    if not rows:
        return None
    r = rows[0]
    return {
        "version": r["version"],
        "curve_data": r.get("curve_data") or {},
        "n_training_samples": r.get("n_training_samples"),
        "brier_score": r.get("brier_score"),
    }


# ---------------------------------------------------------------------------
# verify_claim — deferred (Phase 4.7 internal_rag_mcp dependency)
# ---------------------------------------------------------------------------


def verify_claim(claim: str, evidence_corpus_id: Optional[str] = None) -> None:
    """Stub for the verify_claim MCP tool. Wired through to internal_rag_mcp's
    hybrid_search + a Sonnet judge in Phase 4.7. Until then, callers should
    handle NotImplementedError as 'inconclusive' rather than 'fail'.
    """
    raise NotImplementedError(
        "verify_claim requires Phase 4.7 internal_rag_mcp; not yet implemented")
