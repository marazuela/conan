"""
FDA signal bridge — turns canonical fda_regulatory_events rows into either
shadow-only feature snapshots (Phase 3) or live signals + features (post-cutover).

Operating modes (read from public.scanners.config.mode, defaulting to 'shadow'):

  shadow            Write shadow_* columns on fda_event_features only. No signals
                    row emission. Existing binary_catalyst flow continues
                    untouched. This is the default while the new pipeline is
                    being calibrated against catalyst_universe ground truth.

  shadow_with_emit  Cutover-interim. Write shadow_* AND canonical score/band on
                    fda_event_features, plus emit signals rows with
                    scoring_profile='fda_event'. Used to confirm zero divergence
                    between feature snapshots and emitted signal rows over a
                    short window before flipping fully operational.

  operational       Write only canonical score/band on fda_event_features and
                    emit signals rows. Phase 6 final state.

Two hard gates are enforced before any signal would be considered for emission:

  1. Resolution events (event_type IN approval/crl/presumed_crl/withdrawal) are
     never new opportunities. They flow through signal_resolver / candidate_aging
     to deliver/kill existing candidates instead. The bridge skips them entirely.

  2. Immediate eligibility requires market_implied_probability NOT NULL. Without
     a market probability, pricing edge is undefined; the rubric cannot certify
     that a position is mispriced enough to warrant Immediate. Bridge demotes
     these from 'immediate' to 'watchlist' before persisting score/band.

Design contract: process_event() is pure given (event, asset, evidence_rows,
providers, mode). Tests can drive it without a database.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from modal_workers.providers.polygon.market_data import MarketDataProvider
from modal_workers.providers.polygon.options_data import OptionsDataProvider
from modal_workers.scanners.fda_event_features import (
    FeatureSnapshot,
    build_features,
)

logger = logging.getLogger(__name__)

NAME = "fda_signal_bridge"

# Event types that resolve a prior milestone — must NOT promote as new
# opportunities. signal_resolver / candidate_aging handle these.
RESOLUTION_EVENT_TYPES = frozenset({"approval", "crl", "presumed_crl", "withdrawal"})

MODE_SHADOW = "shadow"
MODE_SHADOW_WITH_EMIT = "shadow_with_emit"
MODE_OPERATIONAL = "operational"
VALID_MODES = (MODE_SHADOW, MODE_SHADOW_WITH_EMIT, MODE_OPERATIONAL)


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass
class ProcessOutcome:
    """Pure outcome of processing one fda_regulatory_events row.

    Persistence is the caller's concern — the Modal scan() entry point owns DB
    writes and respects the scanner config. Tests check this dataclass directly
    without touching the network or the DB.
    """
    event_id: str
    skipped_reason: Optional[str] = None
    feature_snapshot: Optional[FeatureSnapshot] = None
    write_canonical: bool = False
    write_shadow: bool = False
    emit_signal: bool = False
    immediate_demoted: bool = False


# ---------------------------------------------------------------------------
# Pure pipeline
# ---------------------------------------------------------------------------


def is_resolution_event(event_type: Optional[str]) -> bool:
    return (event_type or "").lower() in RESOLUTION_EVENT_TYPES


def canonical_signal_type(event_type: Optional[str]) -> str:
    """Map FDA event tokens into the v3 signal_type contract.

    Unknown event types pass through so downstream unsupported-type flags can
    surface them with provenance instead of hiding them behind a generic label.
    """
    token = (event_type or "fda_event").lower()
    return {
        "pdufa": "pdufa_watchlist",
        "eop2": "eop2_meeting",
        "phase3_readout": "pre_phase3_readout",
        "date_change": "pdufa_date_advanced",
    }.get(token, token)


def gate_immediate_when_market_p_missing(
    snapshot: FeatureSnapshot,
) -> tuple[FeatureSnapshot, bool]:
    """If band == 'immediate' and market_implied_probability is None, demote
    to 'watchlist'. Returns (possibly-replaced snapshot, demoted_flag).
    """
    if snapshot.band == "immediate" and snapshot.market_implied_probability is None:
        return (
            FeatureSnapshot(
                fair_probability=snapshot.fair_probability,
                market_implied_probability=snapshot.market_implied_probability,
                upside_pct=snapshot.upside_pct,
                downside_pct=snapshot.downside_pct,
                expected_value_pct=snapshot.expected_value_pct,
                pricing_edge=snapshot.pricing_edge,
                evidence_confidence=snapshot.evidence_confidence,
                options_liquidity_score=snapshot.options_liquidity_score,
                market_cap_usd=snapshot.market_cap_usd,
                adv_usd=snapshot.adv_usd,
                implied_move_pct=snapshot.implied_move_pct,
                score=snapshot.score,
                band="watchlist",
                raw_inputs={
                    **snapshot.raw_inputs,
                    "_immediate_demoted_no_market_p": True,
                },
                inputs_hash=snapshot.inputs_hash,
            ),
            True,
        )
    return snapshot, False


def write_flags_for_mode(mode: str) -> tuple[bool, bool, bool]:
    """Return (write_canonical, write_shadow, emit_signal) for a given mode."""
    if mode == MODE_SHADOW:
        return (False, True, False)
    if mode == MODE_SHADOW_WITH_EMIT:
        return (True, True, True)
    if mode == MODE_OPERATIONAL:
        return (True, False, True)
    raise ValueError(f"unknown bridge mode: {mode!r}")


def process_event(
    *,
    event: Mapping[str, Any],
    asset: Mapping[str, Any],
    evidence_rows: Sequence[Mapping[str, Any]],
    base_rates: Mapping[str, float],
    market: Optional[MarketDataProvider],
    options: Optional[OptionsDataProvider],
    mode: str,
    snapshot_at: Optional[datetime] = None,
    designations: Optional[Mapping[str, Any]] = None,
) -> ProcessOutcome:
    """Pure single-event pipeline.

    The caller is responsible for: looking up the asset/evidence rows,
    instantiating providers, and writing the resulting FeatureSnapshot back to
    fda_event_features (canonical + shadow_* per write_* flags) plus emitting a
    signals row when emit_signal=True.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"unknown bridge mode: {mode!r}")

    event_id = str(event.get("id") or "")
    event_type = event.get("event_type")
    event_status = (event.get("event_status") or "pending").lower()

    # Gate 1: resolution events never produce new-opportunity signals.
    if is_resolution_event(event_type):
        return ProcessOutcome(
            event_id=event_id,
            skipped_reason="resolution_event",
        )

    # Gate 0: only pending events are scored. Superseded rows have been replaced
    # by a newer event with the same identity (e.g. PDUFA date moved); the new
    # event is what the bridge sees.
    if event_status != "pending":
        return ProcessOutcome(
            event_id=event_id,
            skipped_reason=f"non_pending_status:{event_status}",
        )

    snapshot = build_features(
        event_id=event_id,
        asset=dict(asset),
        event=dict(event),
        evidence_rows=[dict(r) for r in evidence_rows],
        base_rates=base_rates,
        market=market,
        options=options,
        snapshot_at=snapshot_at,
        designations=dict(designations) if designations else None,
    )

    # Gate 2: no auto-Immediate without market_implied_probability.
    snapshot, demoted = gate_immediate_when_market_p_missing(snapshot)

    write_canonical, write_shadow, emit_signal = write_flags_for_mode(mode)
    return ProcessOutcome(
        event_id=event_id,
        feature_snapshot=snapshot,
        write_canonical=write_canonical,
        write_shadow=write_shadow,
        emit_signal=emit_signal,
        immediate_demoted=demoted,
    )


# ---------------------------------------------------------------------------
# Persistence helpers (talk to Supabase)
# ---------------------------------------------------------------------------


def feature_payload(snapshot: FeatureSnapshot, *, write_canonical: bool, write_shadow: bool) -> Dict[str, Any]:
    """Build the upsert payload for fda_event_features given the write flags.

    Canonical columns: score, band, expected_value_pct, pricing_edge, etc.
    Shadow columns:    shadow_score, shadow_band, shadow_expected_value_pct,
                       shadow_pricing_edge, shadow_recorded_at.

    Both sides write the same `raw_inputs`/`inputs_hash` and the descriptive
    columns (fair_probability, market_implied_probability, ...) so a shadow-only
    run still leaves a complete feature snapshot for the dashboard event detail.
    """
    payload: Dict[str, Any] = {
        "fair_probability": snapshot.fair_probability,
        "market_implied_probability": snapshot.market_implied_probability,
        "upside_pct": snapshot.upside_pct,
        "downside_pct": snapshot.downside_pct,
        "expected_value_pct": snapshot.expected_value_pct,
        "pricing_edge": snapshot.pricing_edge,
        "evidence_confidence": snapshot.evidence_confidence,
        "options_liquidity_score": snapshot.options_liquidity_score,
        "market_cap_usd": snapshot.market_cap_usd,
        "adv_usd": snapshot.adv_usd,
        "implied_move_pct": snapshot.implied_move_pct,
        "raw_inputs": snapshot.raw_inputs,
        "inputs_hash": snapshot.inputs_hash,
        "snapshot_at": snapshot.raw_inputs.get("snapshot_at"),
    }
    if write_canonical:
        payload["score"] = snapshot.score
        payload["band"] = snapshot.band
    if write_shadow:
        payload["shadow_score"] = snapshot.score
        payload["shadow_band"] = snapshot.band
        payload["shadow_expected_value_pct"] = snapshot.expected_value_pct
        payload["shadow_pricing_edge"] = snapshot.pricing_edge
        payload["shadow_recorded_at"] = snapshot.raw_inputs.get("snapshot_at")
    return payload


def upsert_feature_snapshot(
    client: Any,  # SupabaseClient
    event_id: str,
    snapshot: FeatureSnapshot,
    *,
    write_canonical: bool,
    write_shadow: bool,
) -> None:
    body = feature_payload(snapshot, write_canonical=write_canonical, write_shadow=write_shadow)
    body["event_id"] = event_id
    client._rest_with_retry(
        "POST",
        "fda_event_features?on_conflict=event_id",
        json_body=[body],
        prefer="resolution=merge-duplicates,return=minimal",
    )


# ---------------------------------------------------------------------------
# Scanner entrypoint — drives the bridge over pending fda_regulatory_events.
# ---------------------------------------------------------------------------


def _build_polygon_providers() -> tuple[Optional[MarketDataProvider], Optional[OptionsDataProvider], Optional[str]]:
    """Construct Polygon market+options providers if POLYGON_API_KEY is set.
    Returns (market, options, warning). Missing key is not fatal — build_features
    degrades gracefully when providers are None (market_cap/adv/straddle stay
    None, market_implied_probability ends up None, gate 2 demotes Immediate).
    """
    try:
        from modal_workers.providers.polygon.base import PolygonClient
        from modal_workers.providers.polygon.market_data import PolygonMarketData
        from modal_workers.providers.polygon.options_data import PolygonOptionsData
        polygon_client = PolygonClient()
    except RuntimeError as e:
        return None, None, f"polygon disabled: {e}"
    except Exception as e:  # noqa: BLE001
        return None, None, f"polygon init failed: {type(e).__name__}: {e}"
    return PolygonMarketData(polygon_client), PolygonOptionsData(client=polygon_client), None


def _designations_from(asset: Mapping[str, Any], evidence_rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Pull designation flags from asset.extensions, with fallback to any
    evidence row of evidence_type='designations'. Mirrors the pdufa pipeline's
    raw_payload designation block layout."""
    extensions = asset.get("extensions") or {}
    flags = dict(extensions.get("designations") or {})
    for row in evidence_rows or []:
        if (row.get("evidence_type") or "") != "designations":
            continue
        if (row.get("evidence_status") or "active") != "active":
            continue
        payload = row.get("payload") or {}
        for k, v in payload.items():
            flags.setdefault(k, v)
    return flags


def scan(cfg) -> "ScannerResult":  # noqa: F821 — runtime import to avoid circulars
    """Drive the bridge over pending `fda_regulatory_events` rows.

    Mode is read from `cfg.config.mode` and defaults to 'shadow'. Per
    write_flags_for_mode:
      - shadow            → upsert shadow_* columns only, no signal emission.
      - shadow_with_emit  → upsert canonical+shadow, emit signals.
      - operational       → upsert canonical, emit signals.

    Even in shadow mode the run is idempotent — fda_event_features is keyed on
    event_id with on_conflict=merge-duplicates, so re-runs just refresh the
    snapshot from the latest provider data + evidence.
    """
    from modal_workers.shared.biotech_base_rates import load_base_rates
    from modal_workers.shared.scanner_base import ScannerResult, Signal
    from modal_workers.shared.supabase_client import EntityHints, SupabaseClient

    client = SupabaseClient()
    started = time.time()
    soft_budget_s = max(int(cfg.timeout_soft_s or 60), 30)
    deadline = started + soft_budget_s

    mode = (cfg.config or {}).get("mode") or MODE_SHADOW
    if mode not in VALID_MODES:
        return ScannerResult(
            scanner=NAME, status="error",
            error=f"invalid bridge mode: {mode!r} (expected one of {VALID_MODES})",
        )

    warnings: List[str] = []
    market, options, polygon_warning = _build_polygon_providers()
    if polygon_warning:
        warnings.append(polygon_warning)

    base_rates = load_base_rates(client)

    # Pending events first — ordered by event_date ASC so near-term decisions
    # get scored even when the budget runs out before the tail.
    events = client._rest(
        "GET", "fda_regulatory_events",
        params={
            "event_status": "eq.pending",
            "asset_id": "not.is.null",
            "select": "id,asset_id,event_type,event_date,event_status,source_content_hash,extensions",
            "order": "event_date.asc.nullslast",
        },
    ) or []

    fetched_records = len(events)
    snapshot_at = datetime.now(timezone.utc)
    signals: List[Signal] = []
    processed = 0
    skipped = 0

    # Pre-fetch assets in one round-trip; events without an asset row get skipped.
    asset_ids = sorted({e["asset_id"] for e in events if e.get("asset_id")})
    assets_by_id: Dict[str, Dict[str, Any]] = {}
    if asset_ids:
        in_clause = ",".join(asset_ids)
        asset_rows = client._rest(
            "GET", "fda_assets",
            params={
                "id": f"in.({in_clause})",
                "select": "id,ticker,mic,entity_id,drug_name,generic_name,application_number,indication,sponsor_name,extensions",
            },
        ) or []
        assets_by_id = {row["id"]: row for row in asset_rows}

    # Pre-fetch active evidence for all pending events in one round-trip
    # rather than N+1 GETs inside the loop. Saves ~56 Supabase round-trips
    # when 57 events are pending and is the cheapest way to reclaim
    # wall-clock budget headroom.
    event_ids_for_evidence = [e["id"] for e in events if e.get("id") and e.get("asset_id")]
    evidence_by_event: Dict[str, List[Dict[str, Any]]] = {}
    if event_ids_for_evidence:
        evid_in_clause = ",".join(event_ids_for_evidence)
        evidence_rows_all = client._rest(
            "GET", "fda_event_evidence",
            params={
                "event_id": f"in.({evid_in_clause})",
                "evidence_status": "eq.active",
                "select": "event_id,source,evidence_type,payload,evidence_status",
            },
        ) or []
        for row in evidence_rows_all:
            evid = row.get("event_id")
            if evid:
                evidence_by_event.setdefault(evid, []).append(row)

    for event in events:
        if time.time() > deadline:
            warnings.append("wall-clock budget exceeded during signal build")
            break

        event_id = event.get("id")
        asset_id = event.get("asset_id")
        if not event_id or not asset_id:
            skipped += 1
            continue
        asset = assets_by_id.get(asset_id)
        if not asset:
            skipped += 1
            continue

        evidence_rows = evidence_by_event.get(event_id, [])

        designations = _designations_from(asset, evidence_rows)

        try:
            outcome = process_event(
                event=event,
                asset=asset,
                evidence_rows=evidence_rows,
                base_rates=base_rates,
                market=market,
                options=options,
                mode=mode,
                snapshot_at=snapshot_at,
                designations=designations,
            )
        except Exception as e:  # noqa: BLE001 — one bad event must not poison the run
            warnings.append(f"process_event failed for {event_id}: {type(e).__name__}: {e}")
            continue

        if outcome.skipped_reason or outcome.feature_snapshot is None:
            skipped += 1
            continue

        try:
            upsert_feature_snapshot(
                client, event_id, outcome.feature_snapshot,
                write_canonical=outcome.write_canonical,
                write_shadow=outcome.write_shadow,
            )
        except Exception as e:  # noqa: BLE001
            warnings.append(f"upsert_feature_snapshot failed for {event_id}: {type(e).__name__}: {e}")
            continue

        processed += 1

        if not outcome.emit_signal:
            continue

        # Emit signal — only reached in shadow_with_emit / operational modes.
        snapshot = outcome.feature_snapshot
        ticker = asset.get("ticker") or ""
        drug = asset.get("drug_name") or asset.get("generic_name") or ""
        event_type = event.get("event_type") or "fda_event"
        signal_type = canonical_signal_type(event_type)
        event_date = event.get("event_date") or ""
        try:
            source_date = datetime.strptime(str(event_date)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            source_date = snapshot_at
        source_content_hash = event.get("source_content_hash") or f"sha256:{snapshot.inputs_hash}"
        signal_id = f"fda_event:{event_id}"[:128]

        raw_payload: Dict[str, Any] = {
            "event_id": event_id,
            "asset_id": asset_id,
            "ticker": ticker,
            "drug_name": drug,
            "indication": asset.get("indication"),
            "event_type": event_type,
            "event_date": str(event_date) if event_date else None,
            "fair_probability": snapshot.fair_probability,
            "market_implied_probability": snapshot.market_implied_probability,
            "expected_value_pct": snapshot.expected_value_pct,
            "pricing_edge": snapshot.pricing_edge,
            "implied_move_pct": snapshot.implied_move_pct,
            "evidence_confidence": snapshot.evidence_confidence,
            "options_liquidity_score": snapshot.options_liquidity_score,
            "market_cap_usd": snapshot.market_cap_usd,
            "adv_usd": snapshot.adv_usd,
            "score": snapshot.score,
            "band": snapshot.band,
            "immediate_demoted": outcome.immediate_demoted,
        }

        signals.append(Signal(
            signal_id=signal_id,
            source_content_hash=source_content_hash,
            source_date=source_date,
            scan_date=snapshot_at,
            signal_type=signal_type,
            raw_payload=raw_payload,
            entity_hints=EntityHints(
                ticker=ticker or None,
                mic=asset.get("mic") or None,
                country="US",
            ),
        ))

    status = "ok"
    if warnings:
        status = "partial"

    run_metrics = {
        "mode": mode,
        "events_pending": fetched_records,
        "events_processed": processed,
        "events_skipped": skipped,
        "polygon_market": market is not None,
        "polygon_options": options is not None,
    }

    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=signals,
        warnings=warnings,
        fetched_records=fetched_records,
        run_metrics=run_metrics,
    )
