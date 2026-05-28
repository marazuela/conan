"""
emissions_ledger — thin DB adapter for the accuracy feedback loop (Phase 1a).

Three responsibilities:

  1. upsert_catalyst_universe_row(client, ...) — idempotent writes into
     catalyst_universe for the fetchers under modal_workers/fetchers/universe/
     (Phase 1b). Dedupes on (source_feed, catalyst_type, ticker, catalyst_date).

  2. label_outcome(client, candidate_id, ...) — applies an outcome_label +
     realized_move_Xd + catalyst_hit_date to the most recent outcomes row for
     a candidate. Creates the row if none exists (for backfill of known
     post-edge false positives: TVTX, AVNS, GSAT, SEM). Ongoing labels are
     written by the candidate_aging Cowork skill after state transitions.

  3. query_emissions_ledger(client, ...) — SELECT through the emissions_ledger
     view, filtered by profile / scored_at window / gate_decision / ticker.
     Used by coverage_auditor (Phase 1c) and ad-hoc backfill verification.

No writes to signals / thesis_jobs / candidates — those tables remain
authoritative and are populated by their existing owners (scanner_base,
thesis_writer skill, candidate_aging skill). The emissions_ledger view
derives gate_decision from those existing columns.

Contract: all functions take a SupabaseClient instance from
modal_workers/shared/supabase_client.py. They use its _rest() helper directly
so error semantics (SupabaseError on non-2xx) are consistent with the rest of
the shared layer.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from .supabase_client import SupabaseClient, SupabaseError


# --------------------------------------------------------------------
# catalyst_universe
# --------------------------------------------------------------------

VALID_CATALYST_TYPES = frozenset({
    "fda_approval", "fda_crl",
    "mna_announce", "mna_close",
    "activist_13d", "activist_proxy",
    "short_squeeze_resolved",
    "litigation_verdict",
    "take_private_announce", "take_private_close",
    "phase3_readout",
})

VALID_MATERIAL_OUTCOMES = frozenset({"yes", "no", "unclear", "negative"})
VALID_PRICE_MOVE_WINDOWS = frozenset({"t+1", "t+5", "t+30"})


def upsert_catalyst_universe_row(
    client: SupabaseClient,
    *,
    profile: str,
    catalyst_type: str,
    catalyst_date: date | str,
    source_feed: str,
    ticker: Optional[str] = None,
    mic: Optional[str] = None,
    issuer_figi: Optional[str] = None,
    entity_id: Optional[str] = None,
    material_outcome: str = "unclear",
    realized_price_move: Optional[float] = None,
    price_move_window: Optional[str] = None,
    source_url: Optional[str] = None,
    raw_payload: Optional[Dict[str, Any]] = None,
) -> str:
    """Upsert a catalyst_universe row. Idempotent on (source_feed, catalyst_type,
    ticker, catalyst_date) — re-fetching the same catalyst is a no-op update
    that refreshes `fetched_at` + any nullable fields.

    Returns the catalyst_id (uuid).
    """
    if catalyst_type not in VALID_CATALYST_TYPES:
        raise ValueError(
            f"catalyst_type {catalyst_type!r} not in {sorted(VALID_CATALYST_TYPES)}"
        )
    if material_outcome not in VALID_MATERIAL_OUTCOMES:
        raise ValueError(
            f"material_outcome {material_outcome!r} not in {sorted(VALID_MATERIAL_OUTCOMES)}"
        )
    if price_move_window is not None and price_move_window not in VALID_PRICE_MOVE_WINDOWS:
        raise ValueError(
            f"price_move_window {price_move_window!r} not in {sorted(VALID_PRICE_MOVE_WINDOWS)}"
        )

    row: Dict[str, Any] = {
        "profile": profile,
        "catalyst_type": catalyst_type,
        "catalyst_date": _date_str(catalyst_date),
        "source_feed": source_feed,
        "material_outcome": material_outcome,
        "raw_payload": raw_payload or {},
    }
    if ticker is not None:
        row["ticker"] = ticker
    if mic is not None:
        row["mic"] = mic
    if issuer_figi is not None:
        row["issuer_figi"] = issuer_figi
    if entity_id is not None:
        row["entity_id"] = entity_id
    if realized_price_move is not None:
        row["realized_price_move"] = realized_price_move
    if price_move_window is not None:
        row["price_move_window"] = price_move_window
    if source_url is not None:
        row["source_url"] = source_url

    rows = client._rest(
        "POST", "catalyst_universe",
        params={"on_conflict": "source_feed,catalyst_type,ticker,catalyst_date"},
        json_body=row,
        prefer="return=representation,resolution=merge-duplicates",
    )
    return rows[0]["id"]


# --------------------------------------------------------------------
# outcomes labeling
# --------------------------------------------------------------------

VALID_OUTCOME_LABELS = frozenset({
    "pre_edge_hit", "post_edge_miss", "dead_catalyst", "still_pending", "unclear",
})

VALID_OUTCOME_TYPES = frozenset({"delivered", "killed", "expired"})


def label_outcome(
    client: SupabaseClient,
    candidate_id: str,
    *,
    outcome_label: str,
    outcome_type: Optional[str] = None,
    catalyst_hit_date: Optional[date | str] = None,
    realized_move_1d: Optional[float] = None,
    realized_move_7d: Optional[float] = None,
    realized_move_30d: Optional[float] = None,
    realized_return: Optional[float] = None,
    notes: Optional[str] = None,
    labeled_by: Optional[str] = None,
) -> str:
    """Apply an outcome_label (+ optional realized moves / catalyst_hit_date) to
    a candidate.

    Upsert semantics:
      - If an outcomes row already exists for the candidate, PATCH the newest one.
      - If no row exists, INSERT a new row. `outcome_type` is REQUIRED in that
        case (NOT NULL column); for backfill of historical post-edge archives,
        pass 'expired' or 'killed' as appropriate.

    Returns the outcome_id (uuid) that was touched.
    """
    if outcome_label not in VALID_OUTCOME_LABELS:
        raise ValueError(
            f"outcome_label {outcome_label!r} not in {sorted(VALID_OUTCOME_LABELS)}"
        )
    if outcome_type is not None and outcome_type not in VALID_OUTCOME_TYPES:
        raise ValueError(
            f"outcome_type {outcome_type!r} not in {sorted(VALID_OUTCOME_TYPES)}"
        )

    existing = client._rest(
        "GET", "outcomes",
        params={
            "candidate_id": f"eq.{candidate_id}",
            "select": "id,outcome_type",
            "order": "created_at.desc",
            "limit": 1,
        },
    )

    patch: Dict[str, Any] = {
        "outcome_label": outcome_label,
        "labeled_at": _now_iso(),
    }
    if catalyst_hit_date is not None:
        patch["catalyst_hit_date"] = _date_str(catalyst_hit_date)
    if realized_move_1d is not None:
        patch["realized_move_1d"] = realized_move_1d
    if realized_move_7d is not None:
        patch["realized_move_7d"] = realized_move_7d
    if realized_move_30d is not None:
        patch["realized_move_30d"] = realized_move_30d
    if realized_return is not None:
        patch["realized_return"] = realized_return
    if notes is not None:
        patch["notes"] = notes
    if labeled_by is not None:
        patch["labeled_by"] = labeled_by

    if existing:
        outcome_id = existing[0]["id"]
        client._rest(
            "PATCH", "outcomes",
            params={"id": f"eq.{outcome_id}"},
            json_body=patch,
        )
        return outcome_id

    if outcome_type is None:
        raise ValueError(
            f"no existing outcomes row for candidate_id={candidate_id}; "
            "outcome_type is required when creating a new row"
        )
    insert_body = {"candidate_id": candidate_id, "outcome_type": outcome_type, **patch}
    rows = client._rest(
        "POST", "outcomes",
        json_body=insert_body,
        prefer="return=representation",
    )
    return rows[0]["id"]


# --------------------------------------------------------------------
# emissions_ledger view queries
# --------------------------------------------------------------------

DEFAULT_LEDGER_SELECT = (
    "signal_id,scanner_name,profile,ticker,mic,issuer_figi,"
    "signal_type,thesis_direction,scored_at,source_date,"
    "score_total,band,auto_caps_triggered,convergence_bonus,"
    "gate_decision,gate_reason,"
    "thesis_job_id,thesis_job_status,candidate_id,candidate_state,"
    "promoted_at,predicted_catalyst_date,"
    "outcome_id,resolution_type,resolution_date,catalyst_hit_date,"
    "realized_move_1d,realized_move_7d,realized_move_30d,"
    "realized_return,outcome_label"
)


def query_emissions_ledger(
    client: SupabaseClient,
    *,
    profile: Optional[str] = None,
    scanner_name: Optional[str] = None,
    ticker: Optional[str] = None,
    scored_at_start: Optional[datetime | str] = None,
    scored_at_end: Optional[datetime | str] = None,
    gate_decision: Optional[str] = None,
    outcome_label: Optional[str] = None,
    limit: int = 500,
    select: str = DEFAULT_LEDGER_SELECT,
) -> List[Dict[str, Any]]:
    """SELECT from the emissions_ledger view with common filters.

    Paged default 500; callers wanting a full scan should iterate with
    scored_at_start/_end windows. All filters are AND-combined.
    """
    params: Dict[str, Any] = {
        "select": select,
        "limit": str(limit),
        "order": "scored_at.desc",
    }
    if profile is not None:
        params["profile"] = f"eq.{profile}"
    if scanner_name is not None:
        params["scanner_name"] = f"eq.{scanner_name}"
    if ticker is not None:
        params["ticker"] = f"eq.{ticker}"
    if gate_decision is not None:
        params["gate_decision"] = f"eq.{gate_decision}"
    if outcome_label is not None:
        params["outcome_label"] = f"eq.{outcome_label}"
    if scored_at_start is not None and scored_at_end is not None:
        # PostgREST can't AND two filters on the same column via simple params;
        # express the window via an `and=` filter.
        params["and"] = (
            f"(scored_at.gte.{_ts_str(scored_at_start)}"
            f",scored_at.lte.{_ts_str(scored_at_end)})"
        )
    elif scored_at_start is not None:
        params["scored_at"] = f"gte.{_ts_str(scored_at_start)}"
    elif scored_at_end is not None:
        params["scored_at"] = f"lte.{_ts_str(scored_at_end)}"

    rows = client._rest("GET", "emissions_ledger", params=params)
    return rows or []


def count_gate_decisions(
    client: SupabaseClient,
    *,
    profile: Optional[str] = None,
    scored_at_start: Optional[datetime | str] = None,
    scored_at_end: Optional[datetime | str] = None,
) -> Dict[str, int]:
    """Aggregate count of signals by gate_decision in a window.

    Useful for quick sanity checks and the coverage auditor's top-line summary.
    Returns {gate_decision: count, ...}. Uses PostgREST's `group=` when available;
    falls back to a full SELECT + client-side bucketing if the server rejects it.
    """
    rows = query_emissions_ledger(
        client,
        profile=profile,
        scored_at_start=scored_at_start,
        scored_at_end=scored_at_end,
        select="signal_id,gate_decision",
        limit=10000,  # bump for counting; callers can paginate if needed
    )
    counts: Dict[str, int] = {}
    for r in rows:
        gd = r.get("gate_decision") or "unknown"
        counts[gd] = counts.get(gd, 0) + 1
    return counts


# --------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------

def _date_str(d: date | str) -> str:
    if isinstance(d, str):
        return d
    return d.isoformat()


def _ts_str(t: datetime | str) -> str:
    if isinstance(t, str):
        return t
    return t.isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
