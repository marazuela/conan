"""
scanner_base — the contract every Modal scanner conforms to, plus a run_scanner wrapper
that handles the common plumbing: load config, open/close run rows, score on ingest,
dedup, persist signals, return a status envelope.

This is the one place where IO shape is enforced: every scanner's scan_fn produces
list[Signal] (or a ScannerResult with status=auth_required/timeout/etc.), run_scanner
enriches each Signal with rubric_version_id + scoring fields and persists to Supabase.
It is also the contract boundary where heuristic scoring becomes explicit:
`signals.dimensions` gets `_provenance`, and `signals.extensions.scoring_meta`
records which dims were supported vs neutral fallbacks so reactor/UI can treat
provisional rows differently from final analyst-quality scores.

Contract (matches spec.md §7.1):
  scan_fn(cfg: ScannerConfig) -> ScannerResult

  Each Signal requires: signal_id, source_content_hash, source_url, source_date,
    scan_date, signal_type, raw_payload. Optional: issuer_figi, entity hints, scoring_profile
    (derived from cfg.signal_type_profile_map if omitted), thesis_direction, strength_estimate.

  The scanner does NOT score signals. run_scanner calls rubric_engine.score_signal on each
  Signal just before insert, matching v1's post_scan flow. If the scanner payload
  needs heuristic estimation, the persisted row carries `_provenance='heuristic'`
  plus `extensions.scoring_meta.requires_resolution` when neutral fallback dims
  were used.

Auth-required handling:
  If scan_fn raises MissingAuthError OR returns ScannerResult(status='auth_required'),
  run_scanner writes a scanner_runs row with that status and returns the envelope without
  inserting signals. Matches the v1 graceful behavior for Q-017 CourtListener / Q-019 OpenDART.
"""

from __future__ import annotations

import logging
import os
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Literal, Optional

from modal_workers.shared.dim_estimator import estimate_dimensions
from modal_workers.shared.rubric_engine import (
    RUBRIC_VERSION,
    WEIGHTS,
    build_scoring_meta,
    dimensions_with_provenance,
    score_signal,
    validate_scoring_meta,
)

logger = logging.getLogger(__name__)
from modal_workers.shared.supabase_client import (
    EntityHints,
    ScannerConfig,
    SupabaseClient,
    SupabaseError,
)

ScannerStatus = Literal["ok", "error", "auth_required", "partial", "timeout"]


# ----------------------------------------------------------------------
# Signal dataclass — the universal shape scanners return.
# ----------------------------------------------------------------------

@dataclass
class Signal:
    signal_id: str
    source_content_hash: str
    source_date: datetime
    scan_date: datetime
    signal_type: str
    raw_payload: Dict[str, Any]
    # optional / derived
    source_url: Optional[str] = None
    issuer_figi: Optional[str] = None
    entity_hints: Optional[EntityHints] = None
    scoring_profile: Optional[str] = None  # filled from cfg.signal_type_profile_map if None
    thesis_direction: Optional[Literal["long", "short", "neutral"]] = None
    strength_estimate: Optional[int] = None
    extensions: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ScannerResult:
    scanner: str
    status: ScannerStatus
    signals: List[Signal] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    fetched_records: Optional[int] = None
    error: Optional[str] = None
    run_metrics: Dict[str, Any] = field(default_factory=dict)
    # Optional post-insert hook. run_scanner calls this AFTER signals are
    # successfully persisted to Supabase. Scanners that cache state in Storage
    # (dedup logs, per-day snapshots) should write through this callback instead
    # of inside scan() — otherwise a Modal mid-flight kill between scan() return
    # and insert_signals() leaves cached state out of sync with the signals table
    # (the 2026-04-21 ESMA dedup-poisoning incident, where 2233 dedup entries
    # survived but 0 signals landed).
    # Contract: the hook is called ONLY if `insert_signals()` did NOT raise.
    # Partial inserts (some rows dup-rejected by the unique constraint) are
    # fine — those rejections represent legitimate prior emissions, so cache
    # advance is still correct.
    after_insert: Optional[Callable[[], None]] = None


class MissingAuthError(RuntimeError):
    """Raise from scan_fn when a required secret is missing. run_scanner catches this and
    produces status='auth_required' — the v1 graceful-degradation contract."""


# ----------------------------------------------------------------------
# run_scanner — the wrapper every Modal scanner calls.
# ----------------------------------------------------------------------

def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _resolve_profile(sig: Signal, cfg: ScannerConfig) -> str:
    if sig.scoring_profile:
        return sig.scoring_profile
    mapped = cfg.signal_type_profile_map.get(sig.signal_type)
    return mapped or cfg.default_scoring_profile


# Profiles intentionally NOT scored by the legacy v1 rubric. Per DECISIONS.md
# C1, fda_event signals are routed to the v3 orchestrator (ingestion →
# documents → asset_documents → orchestrator_runs); their score/band live on
# fda_event_features + the orchestrator, not WEIGHTS (a frozen preservation
# covenant — adding fda_event there would violate the rubric_engine covenant).
# Emit the signals row unscored rather than raising UnknownProfile.
UNSCORED_PROFILES = {"fda_event"}


_VALID_DIRECTIONS = {"long", "short", "neutral"}


def _normalise_direction(d: Any) -> Optional[str]:
    """Defence-in-depth: coerce any direction value to the DB CHECK domain.

    The `signals.thesis_direction` column enforces `long|short|neutral|NULL`.
    v1 scanners sometimes emitted "unknown" for ambiguous cases. Rather than
    rely on each scanner remembering to normalise, we enforce the contract at
    the row-build boundary: unknown / empty / non-string → NULL."""
    if isinstance(d, str) and d in _VALID_DIRECTIONS:
        return d
    return None


def _scanner_scoring_meta(
    profile: str,
    raw_dims: Dict[str, Any],
    *,
    data_freshness: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    required = list(WEIGHTS[profile].keys())
    supported = [dim for dim in required if dim in raw_dims]
    defaulted = [dim for dim in required if dim not in raw_dims]
    return build_scoring_meta(
        provenance="scanner",
        supported_dims=supported,
        defaulted_dims=defaulted,
        requires_resolution=bool(defaulted),
        missing_dimensions=defaulted or None,
        data_freshness=data_freshness,
    )


_LIVENESS_TO_STATUS = {
    "live": "live",
    "stale_served": "stale_served",
    "unavailable": "missing",
}


def _extract_data_freshness(raw_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pull market_snapshot liveness metadata out of the merged raw_payload.

    Scanners that call `load_market_snapshot` merge its return dict (including
    `source_liveness` + `age_seconds`) into raw_payload. We surface that here
    as a compact `data_freshness` block for scoring_meta. Returns None when
    no snapshot was attempted, so profiles that never touch market data don't
    grow an empty key.
    """
    liveness = raw_payload.get("source_liveness")
    if not liveness:
        return None
    status = _LIVENESS_TO_STATUS.get(liveness, liveness)
    return {
        "market_snapshot": {
            "status": status,
            "age_seconds": raw_payload.get("age_seconds"),
            "source": raw_payload.get("market_snapshot_source") or "unknown",
        }
    }


def _signal_to_row(
    sig: Signal,
    cfg: ScannerConfig,
    entity_id: Optional[str],
    scanner_run_id: str,
    client: SupabaseClient,
) -> Dict[str, Any]:
    profile = _resolve_profile(sig, cfg)
    rubric_version_id = client.load_rubric_version_id(
        profile,
        rubric_version=RUBRIC_VERSION,
    )

    # Estimate dims from raw_payload if the scanner didn't pre-populate them.
    # Profiles whose scanner output lacks the data to estimate honestly
    # (activist_governance, merger_arb, litigation) get None → unscored.
    scoring_payload: Dict[str, Any] = dict(sig.raw_payload)
    raw_dims = scoring_payload.get("dimensions") if isinstance(scoring_payload.get("dimensions"), dict) else None
    estimate = None
    if "dimensions" not in scoring_payload:
        estimate = estimate_dimensions(profile, sig.raw_payload)
        if estimate is not None:
            scoring_payload["dimensions"] = estimate.dimensions

    if profile in UNSCORED_PROFILES:
        # Bypass the legacy rubric (DECISIONS.md C1). Emit unscored; the v3
        # orchestrator + fda_event_features carry the real score/band.
        estimate = None
        raw_dims = None
        scored = {
            "dimensions": {},
            "score": None,
            "band": None,
            "auto_caps_triggered": [],
            "demotion_reason": None,
            "missing_dimensions": [],
        }
    else:
        scoring_input: Dict[str, Any] = {
            "scoring_profile": profile,
            "raw_data": scoring_payload,
        }
        scoring_provenance = "heuristic" if estimate is not None else "scanner"
        scored = score_signal(scoring_input, provenance=scoring_provenance)

    persisted_dimensions: Dict[str, Any]
    extensions: Dict[str, Any] = dict(sig.extensions or {})
    data_freshness = _extract_data_freshness(scoring_payload)
    if estimate is not None:
        persisted_dimensions = dimensions_with_provenance(
            scored["dimensions"],
            "heuristic",
        )
        extensions["scoring_meta"] = estimate.scoring_meta(
            "heuristic",
            data_freshness=data_freshness,
        )
    elif raw_dims is not None:
        persisted_dimensions = dimensions_with_provenance(
            scored["dimensions"],
            "scanner",
        )
        extensions["scoring_meta"] = _scanner_scoring_meta(
            profile,
            raw_dims,
            data_freshness=data_freshness,
        )
    else:
        persisted_dimensions = scored["dimensions"]
        if scored.get("missing_dimensions"):
            extensions["scoring_meta"] = build_scoring_meta(
                provenance="unscored",
                supported_dims=[],
                defaulted_dims=[],
                requires_resolution=True,
                missing_dimensions=list(scored["missing_dimensions"]),
                data_freshness=data_freshness,
            )

    if profile in UNSCORED_PROFILES and "scoring_meta" not in extensions:
        extensions["scoring_meta"] = build_scoring_meta(
            provenance="unscored",
            supported_dims=[],
            defaulted_dims=[],
            requires_resolution=True,
            missing_dimensions=None,
            data_freshness=data_freshness,
        )

    if "scoring_meta" in extensions:
        meta_errors = validate_scoring_meta(extensions["scoring_meta"])
        if meta_errors:
            logger.warning(
                "scoring_meta shape invalid for signal_id=%s profile=%s: %s",
                sig.signal_id, profile, "; ".join(meta_errors),
            )

    return {
        "signal_id": sig.signal_id,
        "entity_id": entity_id,
        "issuer_figi": sig.issuer_figi,
        "scanner_id": cfg.scanner_id,
        "scanner_run_id": scanner_run_id,
        "scoring_profile": profile,
        "rubric_version_id": rubric_version_id,
        "source_content_hash": sig.source_content_hash,
        "source_url": sig.source_url,
        "source_date": _iso_utc(sig.source_date),
        "scan_date": _iso_utc(sig.scan_date),
        "signal_type": sig.signal_type,
        "thesis_direction": _normalise_direction(sig.thesis_direction),
        "strength_estimate": sig.strength_estimate,
        "imported": False,
        "dimensions": persisted_dimensions,
        "score": scored["score"],
        "band": scored["band"],
        "auto_caps_triggered": scored["auto_caps_triggered"],
        "demotion_reason": scored.get("demotion_reason"),
        "raw_payload": sig.raw_payload,
        "extensions": extensions,
    }


def _warning_payloads(warnings: List[str]) -> List[Dict[str, Any]]:
    return [{"warnings": warnings}] if warnings else []


def _metrics_payload(metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"metrics": metrics}] if metrics else []


def run_scanner(
    scanner_name: str,
    scan_fn: Callable[[ScannerConfig], ScannerResult],
    *,
    client: Optional[SupabaseClient] = None,
) -> ScannerResult:
    """Orchestrate one scanner invocation end-to-end.

    1. Load config from `scanners` table.
    2. Open `scanner_runs` row with status='running'.
    3. Call scan_fn(cfg). Catch MissingAuthError → auth_required envelope.
    4. For each emitted Signal: resolve entity (if hints given), score, build row.
    5. Bulk insert into `signals` with ON CONFLICT DO NOTHING (dedup on
       (source_content_hash, scoring_profile)).
    6. Close `scanner_runs` row with final status + counts.
    7. Patch `scanners.last_run_*`.
    8. Return ScannerResult envelope.
    """
    client = client or SupabaseClient()
    cfg = client.load_scanner_config(scanner_name)
    modal_inv = os.environ.get("MODAL_TASK_ID")  # Modal sets this at runtime
    run_id = client.open_scanner_run(cfg.scanner_id, modal_invocation_id=modal_inv)
    result = ScannerResult(scanner=scanner_name, status="error", signals=[])
    final_status: ScannerStatus = "error"
    final_signals_emitted = 0
    final_errors: List[Dict[str, Any]] = []
    final_warnings: List[str] = []
    final_run_metrics: Dict[str, Any] = {}
    final_fetched_records: Optional[int] = None

    try:
        try:
            result = scan_fn(cfg)
        except MissingAuthError as e:
            result = ScannerResult(
                scanner=scanner_name,
                status="auth_required",
                signals=[],
                warnings=[str(e)],
            )

        final_fetched_records = result.fetched_records

        if result.status == "auth_required":
            final_status = "auth_required"
            final_warnings = list(result.warnings or [])
            final_run_metrics = dict(result.run_metrics or {})
            return result

        # Build signal rows, resolving entities as we go.
        # Pre-pass: bulk-fetch entities whose FIGI we already know. For cold-start
        # runs with thousands of signals (ESMA), this collapses thousands of per-
        # signal priority-1 GETs into one `issuer_figi IN (...)` query. Signals
        # whose hints miss the map still fall through to resolve_or_create_entity's
        # full fallback chain.
        figis_needed: List[str] = []
        for sig in result.signals:
            f = (sig.entity_hints.issuer_figi if sig.entity_hints else None) or sig.issuer_figi
            if f:
                figis_needed.append(f)
        prefetched = client.prefetch_entities_by_figi(figis_needed) if figis_needed else {}

        rows: List[Dict[str, Any]] = []
        per_signal_errors: List[Dict[str, Any]] = []
        for sig in result.signals:
            try:
                entity_id: Optional[str] = None
                if sig.entity_hints is not None:
                    entity_id = client.resolve_or_create_entity(sig.entity_hints, prefetched=prefetched)
                elif sig.issuer_figi:
                    entity_id = client.resolve_or_create_entity(
                        EntityHints(issuer_figi=sig.issuer_figi), prefetched=prefetched)
                rows.append(_signal_to_row(sig, cfg, entity_id, run_id, client))
            except Exception as e:  # noqa: BLE001
                per_signal_errors.append({"signal_id": sig.signal_id, "error": str(e)})

        inserted: List[str] = []
        insert_succeeded = False
        bulk_insert_failed = False
        try:
            inserted = client.insert_signals(rows)
            insert_succeeded = True
        except SupabaseError as e:
            per_signal_errors.append({"phase": "bulk_insert", "status": e.status, "error": e.body[:400]})
            bulk_insert_failed = True

        # Post-insert cache persistence. See ScannerResult.after_insert docstring —
        # this is the hook that prevents dedup/snapshot poisoning when a Modal
        # container is killed mid-flight. Only runs if insert_signals didn't raise;
        # partial dup-rejections are still a successful insert from this hook's POV.
        if insert_succeeded and result.after_insert is not None:
            try:
                result.after_insert()
            except Exception as e:  # noqa: BLE001
                per_signal_errors.append({"phase": "after_insert", "error": f"{type(e).__name__}: {e}"})

        # Final status resolution:
        #   error   — the bulk insert itself raised (nothing landed, upstream broken)
        #   partial — insert landed but some per-signal errors happened (dup rejections
        #             from ON CONFLICT, or per-signal entity-resolution failures), OR
        #             the scanner pulled records from upstream but emitted zero signals
        #             with no declared error (silent-filter failure, observed
        #             2026-05-26 for fda_pdufa_pipeline + edgar_8k_pdufa).
        #   ok      — clean run, no errors anywhere
        # Previously a failed bulk insert with zero per_signal_errors left status='ok'
        # with signals_emitted=0, so dashboards showed a green scanner that was
        # silently down.
        final_status = result.status
        final_signals_emitted = len(inserted)
        if bulk_insert_failed:
            final_status = "error"
        elif per_signal_errors and final_status == "ok":
            final_status = "partial"
        elif (
            final_status == "ok"
            and final_signals_emitted == 0
            and result.fetched_records is not None
            and result.fetched_records > 0
        ):
            # Fetched upstream rows but emitted nothing — a silent filter failure
            # (matching gate too tight, scoring rejected everything, dedup ate
            # everything, etc.). Surface as 'partial' so the scanner_liveness
            # watchdog catches it instead of letting dashboards show green.
            final_status = "partial"
            per_signal_errors.append({
                "phase": "post_emit",
                "kind": "zero_signal_with_fetched_records",
                "fetched_records": result.fetched_records,
                "note": "scanner pulled records but emitted zero signals — investigate filters",
            })
            logger.warning(
                "scanner %s status flipped to 'partial': fetched=%d signals_emitted=0",
                scanner_name, result.fetched_records,
            )

        final_errors = per_signal_errors
        final_warnings = list(result.warnings or [])
        final_run_metrics = dict(result.run_metrics or {})
        result.status = final_status
        return result
    except Exception as e:  # noqa: BLE001 — catch post-scan pipeline failures too
        tb = traceback.format_exc()
        existing_warnings = list(result.warnings or [])
        final_status = "error"
        final_signals_emitted = 0
        final_fetched_records = result.fetched_records
        final_errors = [{"type": e.__class__.__name__, "message": str(e), "trace": tb}]
        final_warnings = existing_warnings
        final_run_metrics = dict(result.run_metrics or {})
        result = ScannerResult(
            scanner=scanner_name,
            status="error",
            signals=[],
            warnings=existing_warnings,
            fetched_records=final_fetched_records,
            error=str(e),
            run_metrics=result.run_metrics,
        )
        return result
    finally:
        finalization_warnings: List[str] = []
        finished_at = _iso_utc(datetime.now(timezone.utc))
        try:
            client.close_scanner_run(
                run_id,
                status=final_status,
                signals_emitted=final_signals_emitted,
                fetched_records=final_fetched_records,
                errors=final_errors,
                warnings=final_warnings,
                run_metrics=final_run_metrics,
            )
        except Exception as e:  # noqa: BLE001
            finalization_warnings.append(
                f"close_scanner_run failed after retries: {type(e).__name__}: {e}"
            )
        try:
            client.update_scanner_last_run(
                cfg.scanner_id,
                last_run_utc=finished_at,
                last_run_status=final_status,
                last_run_signals=final_signals_emitted,
            )
        except Exception as e:  # noqa: BLE001
            finalization_warnings.append(
                f"update_scanner_last_run failed after retries: {type(e).__name__}: {e}"
            )

        if finalization_warnings:
            result.warnings.extend(
                warning for warning in finalization_warnings if warning not in result.warnings
            )
            if result.status == "ok":
                result.status = "partial"


# ----------------------------------------------------------------------
# Convenience: JSON-serialisable dict of a ScannerResult (for logging).
# ----------------------------------------------------------------------

def result_to_dict(result: ScannerResult) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "scanner": result.scanner,
        "status": result.status,
        "signals_count": len(result.signals),
        "warnings": result.warnings,
        "fetched_records": result.fetched_records,
    }
    if result.error:
        d["error"] = result.error
    if result.run_metrics:
        d["run_metrics"] = result.run_metrics
    return d
