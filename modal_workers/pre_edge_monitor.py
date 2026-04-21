"""
Deterministic pre-edge lifecycle monitor for Conan v2.

Purpose:
  - Enforce obvious post-edge / catalyst-resolved transitions between daily
    `candidate_aging` runs without consuming Claude budget.
  - Apply only clear mechanical transitions; ambiguous cases are surfaced as
    operator_flags for human or candidate_aging review.

Current deterministic rules:
  - takeover_candidate -> delivered when a definitive merger is seen OR when a
    same-entity merger_arb signal appears in the recent window.
  - binary_catalyst -> delivered on approval, killed on CRL / rejection.

The monitor intentionally does NOT touch `last_aging_evaluated_at`; that remains
owned by candidate_aging's once-per-day sweep.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from modal_workers.shared.supabase_client import SupabaseClient

NAME = "pre_edge_monitor"

_WINDOW_DAYS_STANDARD = 14
_WINDOW_DAYS_LITIGATION = 30

_POSITIVE_BINARY_STATUSES = {"approved"}
_NEGATIVE_BINARY_STATUSES = {"rejected", "crl", "resolved_crl"}


def _window_days(scoring_profile: Optional[str]) -> int:
    return _WINDOW_DAYS_LITIGATION if scoring_profile == "litigation" else _WINDOW_DAYS_STANDARD


def _upsert_flag(
    client: SupabaseClient,
    *,
    severity: str,
    kind: str,
    title: str,
    evidence: Optional[Dict[str, Any]] = None,
    candidate_id: Optional[str] = None,
) -> None:
    filt = {
        "source": f"eq.{NAME}",
        "kind": f"eq.{kind}",
        "resolved_at": "is.null",
        "candidate_id": f"eq.{candidate_id}" if candidate_id else "is.null",
    }
    existing = client._rest(
        "GET",
        "operator_flags",
        params={**filt, "select": "id", "limit": 1},
    ) or []

    if existing:
        client._rest(
            "PATCH",
            "operator_flags",
            params={"id": f"eq.{existing[0]['id']}"},
            json_body={
                "severity": severity,
                "title": title,
                "evidence": evidence or {},
            },
            prefer="return=minimal",
        )
        return

    client._rest(
        "POST",
        "operator_flags",
        json_body={
            "severity": severity,
            "source": NAME,
            "kind": kind,
            "title": title,
            "evidence": evidence or {},
            "candidate_id": candidate_id,
        },
        prefer="return=representation",
    )


def _resolve_flag(client: SupabaseClient, *, kind: str, candidate_id: Optional[str]) -> None:
    params = {
        "source": f"eq.{NAME}",
        "kind": f"eq.{kind}",
        "resolved_at": "is.null",
        "candidate_id": f"eq.{candidate_id}" if candidate_id else "is.null",
    }
    client._rest(
        "PATCH",
        "operator_flags",
        params=params,
        json_body={
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "resolved_note": "auto-resolved: pre_edge_monitor no longer sees an ambiguous state",
        },
        prefer="return=representation",
    )


def _load_candidates(client: SupabaseClient) -> List[Dict[str, Any]]:
    rows = client._rest(
        "GET",
        "candidates",
        params={
            "select": "id,ticker,mic,entity_id,state,scoring_profile,current_score,current_band",
            "state": "in.(watch,active)",
            "order": "current_score.desc.nullslast,ticker.asc",
            "limit": "500",
        },
    )
    return rows or []


def _load_recent_signals(
    client: SupabaseClient,
    *,
    entity_id: str,
    scoring_profile: Optional[str],
) -> List[Dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(days=_window_days(scoring_profile))).isoformat()
    rows = client._rest(
        "GET",
        "signals",
        params={
            "select": "signal_id,signal_type,scoring_profile,source_url,scan_date,raw_payload",
            "entity_id": f"eq.{entity_id}",
            "scan_date": f"gte.{since}",
            "order": "scan_date.desc",
            "limit": "100",
        },
    )
    return rows or []


def _binary_resolution(signals: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    positives: List[Dict[str, Any]] = []
    negatives: List[Dict[str, Any]] = []

    for signal in signals:
        raw = signal.get("raw_payload") or {}
        status = str(raw.get("status") or "").strip().lower()
        if status in _POSITIVE_BINARY_STATUSES:
            positives.append(signal)
            continue
        if raw.get("crl_date") or status in _NEGATIVE_BINARY_STATUSES:
            negatives.append(signal)

    if positives and negatives:
        return {
            "decision": "ambiguous",
            "reason": "conflicting_binary_resolution_signals",
            "signals": [*(s["signal_id"] for s in positives[:2]), *(s["signal_id"] for s in negatives[:2])],
        }
    if positives:
        sig = positives[0]
        return {
            "decision": "deliver",
            "reason": "binary_catalyst_approved",
            "signal": sig,
            "outcome_type": "delivered",
            "outcome_notes": "Deterministic pre_edge_monitor transition: approval signal observed.",
        }
    if negatives:
        sig = negatives[0]
        return {
            "decision": "kill",
            "reason": "binary_catalyst_negative_resolution",
            "signal": sig,
            "outcome_type": "killed",
            "outcome_notes": "Deterministic pre_edge_monitor transition: CRL/rejection signal observed.",
        }
    return None


def _takeover_resolution(signals: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    definitive: Optional[Dict[str, Any]] = None
    merger_arb: Optional[Dict[str, Any]] = None

    for signal in signals:
        raw = signal.get("raw_payload") or {}
        if raw.get("definitive_merger_agreement") is True:
            definitive = signal
            break
        if signal.get("scoring_profile") == "merger_arb" and merger_arb is None:
            merger_arb = signal

    signal = definitive or merger_arb
    if signal is None:
        return None

    reason = (
        "takeover_candidate_definitive_merger_announced"
        if definitive is not None
        else "takeover_candidate_promoted_to_merger_arb"
    )
    return {
        "decision": "deliver",
        "reason": reason,
        "signal": signal,
        "outcome_type": "delivered",
        "outcome_notes": "Deterministic pre_edge_monitor transition: pre-edge M&A thesis resolved into a public deal signal.",
    }


def _evaluate_candidate(candidate: Dict[str, Any], signals: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    profile = candidate.get("scoring_profile")
    if profile == "binary_catalyst":
        return _binary_resolution(signals)
    if profile == "takeover_candidate":
        return _takeover_resolution(signals)
    return None


def _apply_transition(client: SupabaseClient, candidate: Dict[str, Any], resolution: Dict[str, Any]) -> Dict[str, Any]:
    signal = resolution["signal"]
    new_state = "delivered" if resolution["decision"] == "deliver" else "killed"
    payload = {
        "stage": "deterministic",
        "resolution_signal_id": signal.get("signal_id"),
        "resolution_signal_type": signal.get("signal_type"),
        "resolution_scoring_profile": signal.get("scoring_profile"),
        "resolution_source_url": signal.get("source_url"),
    }
    result = client._rest(
        "POST",
        "rpc/candidate_transition_apply",
        json_body={
            "p_candidate_id": candidate["id"],
            "p_new_state": new_state,
            "p_reason": resolution["reason"],
            "p_source": NAME,
            "p_outcome_type": resolution["outcome_type"],
            "p_outcome_notes": resolution["outcome_notes"],
            "p_payload": payload,
        },
    )
    return {
        "candidate_id": candidate["id"],
        "ticker": candidate.get("ticker"),
        "from_state": candidate.get("state"),
        "to_state": new_state,
        "reason": resolution["reason"],
        "signal_id": signal.get("signal_id"),
        "rpc_result": result,
    }


def pre_edge_monitor(client: Optional[SupabaseClient] = None) -> Dict[str, Any]:
    client = client or SupabaseClient()
    summary: Dict[str, Any] = {
        "function": NAME,
        "candidates_checked": 0,
        "transitions": [],
        "flagged": [],
        "errors": [],
        "skipped": [],
    }

    for candidate in _load_candidates(client):
        summary["candidates_checked"] += 1
        candidate_id = candidate["id"]
        ticker = candidate.get("ticker") or "?"

        entity_id = candidate.get("entity_id")
        if not entity_id:
            _upsert_flag(
                client,
                severity="warn",
                kind="missing_entity",
                candidate_id=candidate_id,
                title=f"{ticker}: pre_edge_monitor skipped candidate with no entity_id",
                evidence={"candidate_id": candidate_id, "ticker": ticker},
            )
            summary["flagged"].append({"candidate_id": candidate_id, "ticker": ticker, "reason": "missing_entity"})
            continue

        try:
            signals = _load_recent_signals(
                client,
                entity_id=entity_id,
                scoring_profile=candidate.get("scoring_profile"),
            )
            resolution = _evaluate_candidate(candidate, signals)
            if resolution is None:
                _resolve_flag(client, kind="review_required", candidate_id=candidate_id)
                summary["skipped"].append({"candidate_id": candidate_id, "ticker": ticker, "reason": "no_clear_resolution"})
                continue

            if resolution["decision"] == "ambiguous":
                _upsert_flag(
                    client,
                    severity="warn",
                    kind="review_required",
                    candidate_id=candidate_id,
                    title=f"{ticker}: pre_edge_monitor found conflicting resolution signals",
                    evidence={
                        "candidate_id": candidate_id,
                        "ticker": ticker,
                        "reason": resolution["reason"],
                        "signals": resolution.get("signals", []),
                    },
                )
                summary["flagged"].append({"candidate_id": candidate_id, "ticker": ticker, "reason": resolution["reason"]})
                continue

            transition = _apply_transition(client, candidate, resolution)
            _resolve_flag(client, kind="review_required", candidate_id=candidate_id)
            _resolve_flag(client, kind="missing_entity", candidate_id=candidate_id)
            summary["transitions"].append(transition)
        except Exception as e:  # noqa: BLE001
            summary["errors"].append(
                {"candidate_id": candidate_id, "ticker": ticker, "error": f"{type(e).__name__}: {e}"}
            )

    summary["transition_count"] = len(summary["transitions"])
    summary["flag_count"] = len(summary["flagged"])
    return summary
