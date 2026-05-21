"""Phase 4B Tier-2 (Cowork bulk) runtime harness.

Tier-2 is the breadth lever: a single Sonnet pass per asset on a daily/weekly
cadence, ~$0.50/run vs ~$15 for Tier-1's full pipeline. The LLM call lives
in the Cowork-side `bulk_orchestrator` skill (see
`conan-fda-orchestrator-plugin/skills/bulk_orchestrator.md`) — this module
provides the deterministic harness around that call:

  1. `build_tier2_input_blob`  — assembles the JSON the skill consumes
  2. `validate_tier2_output`   — strict shape check on the skill's output
  3. `persist_tier2_assessment`— writes one convergence_assessments row with
                                 tier=2, supersedes prior non-superseded row
  4. `check_tier1_escalation`  — applies the bulk_orchestrator.md §Escalation
                                 rule (high conviction, direction change, new
                                 primary doc)
  5. `enqueue_tier1_escalation`— inserts an orchestrator_runs row with
                                 trigger_type='tier2_escalation'

Out of scope (separate PRs):
  * The Sonnet call itself (Cowork skill)
  * Modal async dispatch / polling endpoints
  * Cowork scheduled-task definition (per watch_priority cadence)
  * Memory writeback (reuses Tier-1 path via MemoryStore)

See migration `20260512000000_v3_phase_4b_convergence_assessments_tier.sql`
for the `tier` column add. See `bulk_orchestrator.md` for the contract.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)

# Tier-2 sees a wider but shallower context than Tier-1 — the synthesis is
# breadth-first rather than depth-first. Per bulk_orchestrator.md §Inputs.
TIER2_MAX_FACTS = 200
TIER2_MAX_DOCS = 50

# Tier-2 output identification. Matches the skill's `provenance` line and is
# the value persisted to convergence_assessments.orchestrator_version.
TIER2_ORCHESTRATOR_VERSION = "bulk_v0"
TIER2_MODEL_ID = "claude-sonnet-4-6"
TIER2_DEFAULT_WINDOW_DAYS = 180
TIER2_STAGE_METRIC_NAME = "tier2_bulk_synthesis"

# Escalation rule per bulk_orchestrator.md §Escalation rule.
TIER2_ESCALATION_CONVICTION_THRESHOLD = 60.0
TIER2_ESCALATION_UNCERTAINTY_CONVICTION_FLOOR = 45.0
TIER2_ESCALATION_EVIDENCE_QUALITY_FLOOR = 0.45
TIER2_PRIMARY_DOC_TYPES: frozenset[str] = frozenset({
    "label",
    "adcomm_briefing",
    "crl",
    "complete_response_letter",
    "press_release_pdufa",
})

# Required fields the skill must emit. additionalProperties=false in the
# convergence_assessment_v1 schema means missing fields must be present and
# explicitly null (not omitted).
TIER2_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "schema_version",
    "asset_id",
    "tier",
    "orchestrator_version",
    "thesis_direction",
    "raw_conviction_pct",
    "conviction_pct",
    "conviction_pct_calibrated",
    "band",
    "hypotheses",
    "cited_prose_blocks",
    "key_facts",
    "uncertainties",
    "citations",
    "reference_class",
    "reference_class_base_rate",
    "similar_resolved_case_ids",
    "evidence_quality",
})

# Fields that MUST be null in a Tier-2 emit (skill spec §Output schema).
# A non-null value here means the skill leaked Tier-1-only state.
TIER2_FORBIDDEN_NON_NULL: frozenset[str] = frozenset({
    "ensemble_n",
    "ensemble_runs",
    "ensemble_mean",
    "ensemble_dispersion",
    "shrinkage_factor",
    "pre_mortem",
    "adversarial_challenges",
    "constitutional_pass",
    "constitutional_findings",
    "market_implied_move",
    "options_iv",
})

TIER2_LIST_FIELDS: frozenset[str] = frozenset({
    "cited_prose_blocks",
    "key_facts",
    "uncertainties",
    "citations",
    "similar_resolved_case_ids",
    "document_ids",
    "citations_document_ids",
    "fact_ids",
    "citations_fact_ids",
})


def _as_list(value: Any) -> List[Any]:
    """Normalize optional JSON/array fields before PostgREST persistence."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _normalize_hypotheses(value: Any) -> Any:
    """Accept the common Cowork drifts of:

      * a JSON-string-encoded list (the jsonb → `net.http_post` → FastAPI
        boundary double-encoding observed 2026-05-19 — every Tier-2
        bulk_orchestrator persist failed with `hypotheses must be a list`),
      * a `{bull, base, bear}` keyed object,

    and canonicalize to the contract array. Anything that does not parse to a
    list/dict (or a keyed-object whose three labels are themselves dicts) is
    returned unchanged so `validate_tier2_output` still surfaces a clear error
    — no silent swallow.
    """
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return value
        if not isinstance(decoded, (list, dict)):
            return value
        value = decoded

    if not isinstance(value, dict):
        return value

    ordered: List[Dict[str, Any]] = []
    for label in ("bull", "base", "bear"):
        item = value.get(label)
        if not isinstance(item, dict):
            return value
        normalized = dict(item)
        normalized.setdefault("label", label)
        ordered.append(normalized)
    return ordered


def normalize_tier2_payload(payload: Any) -> Any:
    """Return a canonical Tier-2 payload without mutating the caller's dict.

    Repairs three production drifts seen at the Cowork-skill / Postgres-jsonb
    / Modal-FastAPI boundaries:

      1. A fully JSON-string-encoded payload body (top-level decode once).
      2. JSON-string-encoded container fields (hypotheses, cited_prose_blocks,
         key_facts, uncertainties, citations, similar_resolved_case_ids,
         document_ids/citations_document_ids, fact_ids/citations_fact_ids).
         These trip `isinstance(..., list)` in `validate_tier2_output` after
         pg_net round-trips them as strings.
      3. The `{bull, base, bear}` keyed-object hypotheses shape.

    Idempotent. Unparseable strings are left in place so validation still
    fails loudly (no silent swallow). Non-dict input is returned unchanged
    after the top-level decode attempt.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return payload
    if not isinstance(payload, dict):
        return payload

    normalized = dict(payload)
    for key in _TIER2_JSON_CONTAINER_FIELDS:
        if key in normalized:
            normalized[key] = _coerce_json_container(normalized[key])
    if "hypotheses" in normalized:
        normalized["hypotheses"] = _normalize_hypotheses(
            normalized.get("hypotheses"),
        )
    return normalized


def _default_document_window() -> tuple[str, str]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=TIER2_DEFAULT_WINDOW_DAYS)
    return start.isoformat(), end.isoformat()


def _coalesce_timestamp(value: Any, fallback: str) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value
    return fallback


@dataclass
class Tier2InputBlob:
    """The JSON payload the bulk_orchestrator skill consumes (one per asset)."""
    asset_id: str
    ticker: Optional[str]
    drug_name: Optional[str]
    indication: Optional[str]
    reference_class_signature: Optional[str]
    evidence_packet: Dict[str, Any]
    extracted_facts: List[Dict[str, Any]]
    asset_documents: List[Dict[str, Any]]
    prior_assessment: Optional[Dict[str, Any]]

    def to_json(self) -> str:
        """Serialize for handoff to Cowork. Cowork reads this from disk
        when the scheduled routine fires."""
        return json.dumps(asdict(self), default=str, ensure_ascii=False)


def validate_evidence_packet(
    *,
    asset: Dict[str, Any],
    extracted_facts: List[Dict[str, Any]],
    asset_documents: List[Dict[str, Any]],
    tier: int,
) -> Dict[str, Any]:
    """Validate the minimum evidence packet before bulk/deep review.

    Tier 2 is allowed to run on a primary linked source document before facts
    have been extracted. Tier 1 requires facts too, because the deep runtime is
    expected to ground claims in extracted evidence.
    """
    material_docs = [
        d for d in asset_documents
        if d.get("is_material") is not False
        and d.get("link_type") in ("primary", "safety_signal")
    ]
    errors: List[str] = []
    if not asset.get("ticker"):
        errors.append("missing_ticker")
    if not asset.get("drug_name"):
        errors.append("missing_drug_name")
    if not material_docs:
        errors.append("missing_material_primary_document")
    if tier == 1 and not extracted_facts:
        errors.append("missing_extracted_facts")

    return {
        "tier": tier,
        "ok": not errors,
        "errors": errors,
        "identity": {
            "ticker": asset.get("ticker"),
            "drug_name": asset.get("drug_name"),
            "sponsor_name": asset.get("sponsor_name"),
            "application_number": asset.get("application_number"),
        },
        "counts": {
            "material_primary_documents": len(material_docs),
            "extracted_facts": len(extracted_facts),
            "asset_documents": len(asset_documents),
        },
    }


def build_tier2_input_blob(
    sb: SupabaseClient,
    asset_id: str,
    *,
    max_facts: int = TIER2_MAX_FACTS,
    max_docs: int = TIER2_MAX_DOCS,
    require_evidence_packet: bool = False,
) -> Tier2InputBlob:
    """Assemble the input the bulk_orchestrator skill will consume.

    Mirrors `runtime.stage_0_load` but with Tier-2 limits (wider context).
    Memory is loaded by the skill via Cowork's MemoryStore wrapper — we
    do NOT include the memory blob inline (the skill spec calls for a
    file-system path lookup; see `bulk_orchestrator.md` §Inputs).
    """
    asset_rows = sb._rest(
        "GET", "fda_assets",
        params={
            "select": (
                "id,ticker,drug_name,generic_name,sponsor_name,indication,"
                "indication_normalized,reference_class_signature,"
                "application_number,application_type,program_status,"
                "watch_priority"
            ),
            "id": f"eq.{asset_id}",
        },
    ) or []
    if not asset_rows:
        raise ValueError(f"Tier-2: asset {asset_id} not found")
    asset = asset_rows[0]

    facts = sb._rest(
        "GET", "extracted_facts",
        params={
            "select": (
                "id,document_id,fact_type,fact_text,evidence_quote,"
                "citation_span,confidence,extracted_at"
            ),
            "asset_id": f"eq.{asset_id}",
            "order": "extracted_at.desc",
            "limit": str(max_facts),
        },
    ) or []

    asset_docs = sb._rest(
        "GET", "asset_documents",
        params={
            "select": (
                "document_id,link_type,is_material,extraction_confidence,"
                "extracted_spans,created_at"
            ),
            "asset_id": f"eq.{asset_id}",
            "order": "created_at.desc",
            "limit": str(max_docs),
        },
    ) or []

    prior_rows = sb._rest(
        "GET", "convergence_assessments",
        params={
            "select": (
                "id,tier,orchestrator_version,thesis_direction,conviction_pct,"
                "band,document_ids,created_at"
            ),
            "asset_id": f"eq.{asset_id}",
            "superseded_at": "is.null",
            "order": "created_at.desc",
            "limit": "1",
        },
    ) or []
    prior = prior_rows[0] if prior_rows else None

    evidence_packet = validate_evidence_packet(
        asset=asset,
        extracted_facts=facts,
        asset_documents=asset_docs,
        tier=2,
    )
    if require_evidence_packet and not evidence_packet["ok"]:
        raise ValueError(
            "Tier-2: evidence packet incomplete for "
            f"{asset_id}: {', '.join(evidence_packet['errors'])}"
        )

    return Tier2InputBlob(
        asset_id=asset_id,
        ticker=asset.get("ticker"),
        drug_name=asset.get("drug_name"),
        indication=(
            asset.get("indication_normalized") or asset.get("indication")
        ),
        reference_class_signature=asset.get("reference_class_signature"),
        evidence_packet=evidence_packet,
        extracted_facts=facts,
        asset_documents=asset_docs,
        prior_assessment=prior,
    )


# Structured (jsonb array/object) fields that can arrive double-encoded as a
# JSON *string* at the Postgres `net.http_post` → Modal/FastAPI boundary.
# Only these known container fields are repaired — scalar/text fields are
# left untouched so genuine bad input still fails validation loudly.
_TIER2_JSON_CONTAINER_FIELDS: tuple[str, ...] = (
    "hypotheses",
    "cited_prose_blocks",
    "key_facts",
    "uncertainties",
    "citations",
    "similar_resolved_case_ids",
    "document_ids",
    "fact_ids",
    "citations_document_ids",
    "citations_fact_ids",
)


def _coerce_json_container(value: Any) -> Any:
    """Decode a JSON-encoded string into its list/dict. Non-strings, and
    strings that don't parse to a list/dict, are returned unchanged so
    downstream validation still surfaces a clear error (no silent swallow)."""
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError):
        return value
    return decoded if isinstance(decoded, (list, dict)) else value


def validate_tier2_output(payload: Dict[str, Any]) -> List[str]:
    """Return a list of validation errors. Empty list = valid Tier-2 output.

    Checks:
      * All TIER2_REQUIRED_FIELDS present (may be null per schema)
      * `tier` == 2
      * `orchestrator_version` == TIER2_ORCHESTRATOR_VERSION
      * thesis_direction in {long, short, neutral, straddle} or null
      * raw_conviction_pct, conviction_pct, conviction_pct_calibrated in [0,100]
      * evidence_quality in [0, 1]
      * hypotheses is a list of dicts each with `kill_conditions` (≥2 per D-115)
      * TIER2_FORBIDDEN_NON_NULL fields are absent or null
    """
    errors: List[str] = []
    if not isinstance(payload, dict):
        return [f"payload must be dict, got {type(payload).__name__}"]
    payload = normalize_tier2_payload(payload)

    for key in TIER2_REQUIRED_FIELDS:
        if key not in payload:
            errors.append(f"missing required field: {key}")

    tier = payload.get("tier")
    if tier != 2:
        errors.append(f"tier must be 2, got {tier!r}")

    version = payload.get("orchestrator_version")
    if version != TIER2_ORCHESTRATOR_VERSION:
        errors.append(
            f"orchestrator_version must be {TIER2_ORCHESTRATOR_VERSION!r}, "
            f"got {version!r}"
        )

    direction = payload.get("thesis_direction")
    if direction is not None and direction not in (
        "long", "short", "neutral", "straddle",
    ):
        errors.append(f"invalid thesis_direction: {direction!r}")

    for pct_field in (
        "raw_conviction_pct", "conviction_pct", "conviction_pct_calibrated",
    ):
        v = payload.get(pct_field)
        if v is None:
            continue
        if not isinstance(v, (int, float)) or not (0 <= v <= 100):
            errors.append(f"{pct_field} must be in [0, 100], got {v!r}")

    eq = payload.get("evidence_quality")
    if eq is not None:
        if not isinstance(eq, (int, float)) or not (0 <= eq <= 1):
            errors.append(f"evidence_quality must be in [0, 1], got {eq!r}")

    for list_field in TIER2_LIST_FIELDS:
        v = payload.get(list_field)
        if v is not None and not isinstance(v, list):
            errors.append(f"{list_field} must be a list")

    hyps = payload.get("hypotheses")
    if hyps is not None:
        if not isinstance(hyps, list):
            errors.append("hypotheses must be a list")
        else:
            for i, h in enumerate(hyps):
                if not isinstance(h, dict):
                    errors.append(f"hypotheses[{i}] must be a dict")
                    continue
                kc = h.get("kill_conditions")
                if not isinstance(kc, list) or len(kc) < 2:
                    errors.append(
                        f"hypotheses[{i}].kill_conditions must be a list of "
                        f"≥2 entries (D-115), got {kc!r}"
                    )

    for forbidden in TIER2_FORBIDDEN_NON_NULL:
        if payload.get(forbidden) not in (None, 0, [], {}):
            # 0/empty are acceptable defaults; only non-trivial values flag.
            errors.append(
                f"{forbidden} must be null/empty for tier=2, got "
                f"{payload[forbidden]!r}"
            )

    return errors


def persist_tier2_assessment(
    sb: SupabaseClient,
    asset_id: str,
    payload: Dict[str, Any],
    *,
    trigger_type: str = "scheduled",
    trigger_doc_id: Optional[str] = None,
    document_window_start: Optional[str] = None,
    document_window_end: Optional[str] = None,
    cost_usd: float = 0.0,
    latency_ms: Optional[int] = None,
) -> str:
    """Insert a Tier-2 convergence_assessments row, supersede the prior
    non-superseded row, and return the new assessment_id.

    Persists ONLY the fields the Tier-2 skill emits — Tier-1-specific
    fields (ensemble_*, pre_mortem, constitutional_*, market_implied_move,
    options_iv) are left at table defaults (null/0). Caller is responsible
    for having validated `payload` via `validate_tier2_output` first.
    """
    payload = normalize_tier2_payload(payload)
    errors = validate_tier2_output(payload)
    if errors:
        raise ValueError(
            f"Tier-2 payload failed validation ({len(errors)} errors): "
            f"{errors}"
        )

    document_ids = payload.get("citations_document_ids") or payload.get(
        "document_ids"
    ) or []
    fact_ids = payload.get("citations_fact_ids") or payload.get(
        "fact_ids"
    ) or []
    default_window_start, default_window_end = _default_document_window()

    row: Dict[str, Any] = {
        "asset_id": asset_id,
        "tier": 2,
        "orchestrator_version": TIER2_ORCHESTRATOR_VERSION,
        "model_id": TIER2_MODEL_ID,
        "trigger_type": trigger_type,
        "trigger_doc_id": trigger_doc_id or None,
        "document_window_start": _coalesce_timestamp(
            payload.get("document_window_start") or document_window_start,
            default_window_start,
        ),
        "document_window_end": _coalesce_timestamp(
            payload.get("document_window_end") or document_window_end,
            default_window_end,
        ),
        "document_ids": _as_list(document_ids),
        "fact_ids": _as_list(fact_ids),
        "evidence_ledger": payload.get("evidence_ledger") or {},
        "reasoning_trace": payload.get("reasoning_trace"),
        "cited_prose_blocks": payload.get("cited_prose_blocks") or [],
        "key_facts": payload.get("key_facts") or [],
        "uncertainties": payload.get("uncertainties") or [],
        "hypotheses": payload.get("hypotheses") or [],
        "reference_class": payload.get("reference_class"),
        "reference_class_base_rate": payload.get("reference_class_base_rate"),
        "similar_resolved_case_ids": (
            payload.get("similar_resolved_case_ids") or []
        ),
        "raw_conviction_pct": payload.get("raw_conviction_pct"),
        "thesis_direction": payload.get("thesis_direction"),
        "thesis_summary": payload.get("thesis_summary"),
        "conviction_pct_calibrated": payload.get("conviction_pct_calibrated"),
        "conviction_pct": payload.get("conviction_pct"),
        "evidence_quality": payload.get("evidence_quality"),
        "band": payload.get("band"),
        "calibration_curve_version": payload.get("calibration_curve_version"),
        "cost_usd": round(cost_usd, 4),
        "latency_ms": latency_ms,
        # PR-5: Tier-2 is architecturally exempt from the Stage 7 constitutional
        # gate (TIER2_FORBIDDEN_NON_NULL above). gate_status='tier2_skipped'
        # makes the skip explicit so downstream callers can filter on
        # gate_status='pass' without conflating Tier-2 emits with Tier-1
        # "not evaluated yet" rows.
        "gate_status": "tier2_skipped",
    }

    rows = sb._rest(
        "POST", "convergence_assessments",
        json_body=row,
        prefer="return=representation",
    )
    if not rows:
        raise RuntimeError(
            "Tier-2: failed to insert convergence_assessments row"
        )
    assessment_id = rows[0]["id"]

    # The orphan sweeper defines a real assessment as one with at least one
    # stage metric child. Tier-2 skips Tier-1's stage graph, so write a compact
    # marker before the run is marked completed.
    sb._rest(
        "POST", "assessment_stage_metrics",
        json_body={
            "assessment_id": assessment_id,
            "stage_name": TIER2_STAGE_METRIC_NAME,
            "model": TIER2_MODEL_ID,
            "cost_usd": round(cost_usd, 4),
            "latency_ms": latency_ms or 0,
            "status": "completed",
            "notes": {
                "tier": 2,
                "orchestrator_version": TIER2_ORCHESTRATOR_VERSION,
                "trigger_type": trigger_type,
                "trigger_doc_id": trigger_doc_id,
                "document_count": len(_as_list(document_ids)),
                "fact_count": len(_as_list(fact_ids)),
                "gate_status": "tier2_skipped",
            },
        },
        prefer="return=minimal",
    )

    # Supersede the prior non-superseded row for this asset (skip the row we
    # just inserted). Mirrors the Tier-1 supersession pattern.
    sb._rest(
        "PATCH", "convergence_assessments",
        params={
            "asset_id": f"eq.{asset_id}",
            "superseded_at": "is.null",
            "id": f"neq.{assessment_id}",
        },
        json_body={
            "superseded_by": assessment_id,
            "superseded_at": "now()",
        },
        prefer="return=minimal",
    )

    return assessment_id


@dataclass
class EscalationDecision:
    escalate: bool
    reasons: List[str] = field(default_factory=list)


def check_tier1_escalation(
    prior: Optional[Dict[str, Any]],
    current: Dict[str, Any],
    *,
    new_primary_doc_types: Optional[Set[str]] = None,
) -> EscalationDecision:
    """Apply bulk_orchestrator.md §Escalation rule.

    Args:
      prior: latest non-superseded convergence_assessments row for the asset
             before the Tier-2 emit, or None if first assessment.
      current: the freshly-emitted Tier-2 payload.
      new_primary_doc_types: set of doc_types in `current.document_ids`
                              that are NOT in `prior.document_ids`. The
                              caller resolves these (we don't fetch
                              documents from inside this helper).

    Triggers escalation when ANY of:
      - conviction_pct >= TIER2_ESCALATION_CONVICTION_THRESHOLD
      - conviction_pct is material and evidence_quality is weak
      - thesis_direction differs from prior (and prior was non-null)
      - new_primary_doc_types intersects TIER2_PRIMARY_DOC_TYPES
    """
    reasons: List[str] = []

    conviction = current.get("conviction_pct")
    if (
        isinstance(conviction, (int, float))
        and conviction >= TIER2_ESCALATION_CONVICTION_THRESHOLD
    ):
        reasons.append(
            f"high_conviction (conviction_pct={conviction} ≥ "
            f"{TIER2_ESCALATION_CONVICTION_THRESHOLD})"
        )

    evidence_quality = current.get("evidence_quality")
    if (
        isinstance(conviction, (int, float))
        and conviction >= TIER2_ESCALATION_UNCERTAINTY_CONVICTION_FLOOR
        and isinstance(evidence_quality, (int, float))
        and evidence_quality <= TIER2_ESCALATION_EVIDENCE_QUALITY_FLOOR
    ):
        reasons.append(
            "high_uncertainty_material_asset "
            f"(conviction_pct={conviction} ≥ "
            f"{TIER2_ESCALATION_UNCERTAINTY_CONVICTION_FLOOR}, "
            f"evidence_quality={evidence_quality} ≤ "
            f"{TIER2_ESCALATION_EVIDENCE_QUALITY_FLOOR})"
        )

    if prior is not None:
        prior_dir = prior.get("thesis_direction")
        cur_dir = current.get("thesis_direction")
        if prior_dir is not None and cur_dir is not None and prior_dir != cur_dir:
            reasons.append(
                f"direction_change ({prior_dir!r} → {cur_dir!r})"
            )

    if new_primary_doc_types:
        primary_hits = new_primary_doc_types & TIER2_PRIMARY_DOC_TYPES
        if primary_hits:
            reasons.append(
                f"new_primary_doc ({sorted(primary_hits)})"
            )

    return EscalationDecision(escalate=bool(reasons), reasons=reasons)


def enqueue_tier1_escalation(
    sb: SupabaseClient,
    asset_id: str,
    *,
    triggering_assessment_id: str,
    reasons: List[str],
) -> str:
    """Insert an orchestrator_runs row with trigger_type='tier2_escalation'.

    The Tier-1 drainer (orchestrator_app.orchestrator_drain_queue, currently
    filters tier=1) will pick this up. Returns the new run_id.
    """
    if not reasons:
        raise ValueError(
            "enqueue_tier1_escalation called with no reasons (caller must "
            "consult check_tier1_escalation first)"
        )

    rows = sb._rest(
        "POST", "orchestrator_runs",
        json_body={
            "asset_id": asset_id,
            "trigger_type": "tier2_escalation",
            "tier": 1,
            "status": "pending",
            "scheduled_at": datetime.now(timezone.utc).isoformat(),
            "notes": {
                "triggering_assessment_id": triggering_assessment_id,
                "escalation_reasons": reasons,
            },
        },
        prefer="return=representation",
    )
    if not rows:
        raise RuntimeError(
            "Tier-2: failed to insert orchestrator_runs row for escalation"
        )
    return rows[0]["id"]


# ---------------------------------------------------------------------------
# Modal-endpoint orchestration helpers
#
# These wrap the primitives above with the orchestrator_runs status-machine
# transitions. They are pure Python (no Modal dependency) so they can be
# called either from a Modal endpoint OR directly from tests.
# ---------------------------------------------------------------------------

def enqueue_tier2_bulk(
    sb: SupabaseClient,
    asset_ids: List[str],
    *,
    source: str = "tier2_bulk_enqueue",
) -> Dict[str, Any]:
    """Insert pending tier=2 orchestrator_runs rows for `asset_ids` and
    return per-asset {run_id, blob}. Per-asset failures isolated."""
    from dataclasses import asdict

    out: Dict[str, Any] = {"enqueued": [], "failed": []}
    for asset_id in asset_ids:
        try:
            rows = sb._rest(
                "POST", "orchestrator_runs",
                json_body={
                    "asset_id": asset_id,
                    "trigger_type": "scheduled",
                    "tier": 2,
                    "status": "pending",
                    "notes": {"source": source},
                },
                prefer="return=representation",
            )
            if not rows:
                raise RuntimeError("orchestrator_runs insert returned no row")
            run_id = rows[0]["id"]
            blob = build_tier2_input_blob(
                sb, asset_id,
                require_evidence_packet=True,
            )
            out["enqueued"].append({
                "asset_id": asset_id,
                "run_id": run_id,
                "blob": asdict(blob),
            })
        except Exception as exc:
            out["failed"].append({
                "asset_id": asset_id,
                "error": str(exc)[:500],
            })

    out["enqueued_count"] = len(out["enqueued"])
    out["failed_count"] = len(out["failed"])
    return out


def _resolve_new_primary_doc_types(
    sb: SupabaseClient,
    *,
    current_doc_ids: List[str],
    prior_doc_ids: List[str],
) -> set:
    """Resolve doc_type values for documents added in `current_doc_ids`
    relative to `prior_doc_ids`. Used by the §Escalation new_primary_doc rule.
    """
    new_ids = list(set(current_doc_ids) - set(prior_doc_ids))
    if not new_ids:
        return set()
    ids_filter = ",".join(new_ids)
    rows = sb._rest(
        "GET", "documents",
        params={"select": "doc_type", "id": f"in.({ids_filter})"},
    ) or []
    return {r.get("doc_type") for r in rows if r.get("doc_type")}


def complete_tier2_run(
    sb: SupabaseClient,
    run_id: str,
    payload: Dict[str, Any],
    *,
    cost_usd: float = 0.0,
    latency_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """End-to-end Tier-2 completion: validate → fetch prior → persist →
    resolve new doc_types → escalation check → enqueue tier1 if triggered →
    mark run completed.

    Returns a structured result for Cowork to log:
      {run_id, asset_id, assessment_id, escalated, escalation_reasons,
       escalation_run_id, status}

    On validation failure, marks run='failed' with the validator errors and
    returns {status: 'failed_validation', errors: [...]}.
    """
    run_rows = sb._rest(
        "GET", "orchestrator_runs",
        params={
            "select": "id,asset_id,status,tier,trigger_type,trigger_doc_id",
            "id": f"eq.{run_id}",
        },
    ) or []
    if not run_rows:
        raise ValueError(f"orchestrator_runs row {run_id} not found")
    run_row = run_rows[0]
    if run_row.get("tier") != 2:
        raise ValueError(
            f"orchestrator_runs row {run_id} is tier={run_row.get('tier')}, "
            f"not 2; refusing to complete as Tier-2"
        )
    asset_id = run_row["asset_id"]
    payload = normalize_tier2_payload(payload)
    errors = validate_tier2_output(payload)
    if errors:
        sb._rest(
            "PATCH", "orchestrator_runs",
            params={"id": f"eq.{run_id}"},
            json_body={
                "status": "failed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "error_message": (
                    f"Tier-2 payload failed validation: {errors}"[:1000]
                ),
            },
            prefer="return=minimal",
        )
        return {
            "run_id": run_id,
            "asset_id": asset_id,
            "status": "failed_validation",
            "errors": errors,
        }

    sb._rest(
        "PATCH", "orchestrator_runs",
        params={"id": f"eq.{run_id}"},
        json_body={
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
        prefer="return=minimal",
    )

    prior_rows = sb._rest(
        "GET", "convergence_assessments",
        params={
            "select": "id,thesis_direction,conviction_pct,document_ids",
            "asset_id": f"eq.{asset_id}",
            "superseded_at": "is.null",
            "order": "created_at.desc",
            "limit": "1",
        },
    ) or []
    prior = prior_rows[0] if prior_rows else None

    assessment_id = persist_tier2_assessment(
        sb, asset_id, payload,
        trigger_type=run_row.get("trigger_type") or "scheduled",
        trigger_doc_id=run_row.get("trigger_doc_id"),
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )

    current_doc_ids = list(
        payload.get("citations_document_ids")
        or payload.get("document_ids")
        or []
    )
    prior_doc_ids = list((prior or {}).get("document_ids") or [])
    new_doc_types = _resolve_new_primary_doc_types(
        sb,
        current_doc_ids=current_doc_ids,
        prior_doc_ids=prior_doc_ids,
    )

    decision = check_tier1_escalation(
        prior, payload,
        new_primary_doc_types=new_doc_types or None,
    )

    escalation_run_id: Optional[str] = None
    if decision.escalate:
        try:
            escalation_run_id = enqueue_tier1_escalation(
                sb, asset_id,
                triggering_assessment_id=assessment_id,
                reasons=decision.reasons,
            )
        except Exception as exc:
            escalation_run_id = None
            decision.reasons.append(
                f"escalation_enqueue_failed: {str(exc)[:200]}"
            )

    sb._rest(
        "PATCH", "orchestrator_runs",
        params={"id": f"eq.{run_id}"},
        json_body={
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "assessment_id": assessment_id,
            "cost_actual_usd": round(cost_usd, 4) if cost_usd else None,
        },
        prefer="return=minimal",
    )

    return {
        "run_id": run_id,
        "asset_id": asset_id,
        "assessment_id": assessment_id,
        "escalated": decision.escalate,
        "escalation_reasons": decision.reasons,
        "escalation_run_id": escalation_run_id,
        "status": "completed",
    }


def fail_tier2_run(
    sb: SupabaseClient,
    run_id: str,
    error_message: str,
) -> Dict[str, Any]:
    """Mark a Tier-2 orchestrator_runs row failed. Idempotent — repeated
    calls overwrite error_message; tier filter prevents accidentally failing
    a Tier-1 row."""
    sb._rest(
        "PATCH", "orchestrator_runs",
        params={"id": f"eq.{run_id}", "tier": "eq.2"},
        json_body={
            "status": "failed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "error_message": (error_message or "tier2 skill error")[:1000],
        },
        prefer="return=minimal",
    )
    return {"run_id": run_id, "status": "failed"}
