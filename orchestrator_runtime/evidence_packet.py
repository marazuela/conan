"""Tier-1 evidence-packet validator.

Extracted from `orchestrator_runtime/tier2.py` in v4 Phase 6b when the
Tier-2 surface was deleted. The validator itself was never Tier-2-specific
— it takes `tier` as a parameter and validates a different bar for tier 1
vs tier 2. Phase 6b only deleted the Tier-2 *enqueue / persist / fail*
flow; the deep-runtime fail-closed evidence gate still runs (as it always
did) at the top of `_run_one_inner` via `require_tier1_evidence_packet`.

When Phase 6c lands and the tier-vs-tier distinction is fully gone, this
function will collapse to a Tier-1-only check.
"""

from __future__ import annotations

from typing import Any, Dict, List


def validate_evidence_packet(
    *,
    asset: Dict[str, Any],
    extracted_facts: List[Dict[str, Any]],
    asset_documents: List[Dict[str, Any]],
    tier: int,
) -> Dict[str, Any]:
    """Validate the minimum evidence packet before deep review.

    Tier 1 (the only remaining tier under v4) requires:
      - asset ticker + drug_name populated
      - at least one material primary or safety_signal document linked
      - at least one extracted_fact rows on the asset

    Tier 2 (sunset in Phase 6b) used a relaxed check that allowed runs
    on a primary linked source document before facts had been extracted.
    The parameter is retained for backward compat — callers that pass
    tier=2 still get the relaxed check until Phase 6c collapses this.
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
