"""
Deterministic biotech enrichment for binary-catalyst signals.

Adds an additive `extensions.biotech_enrichment` block only; never changes
dimensions, score, or band.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from modal_workers.shared.supabase_client import SupabaseClient

NAME = "biotech_enrichment_sweep"
SCHEMA_VERSION = "biotech_enrichment_v1"
WINDOW_DAYS = 30


def _clamp(v: int, lo: int = 1, hi: int = 5) -> int:
    return max(lo, min(hi, v))


def _parse_adcom_vote(vote: Any) -> Optional[float]:
    if isinstance(vote, dict):
        yes = vote.get("yes") or 0
        no = vote.get("no") or 0
        total = yes + no
        if total:
            return yes / total
        return None
    if isinstance(vote, str) and "-" in vote:
        left, right = vote.split("-", 1)
        try:
            yes = int(left)
            no = int(right)
        except ValueError:
            return None
        total = yes + no
        if total:
            return yes / total
    return None


def _approval_count(history: Any) -> int:
    if not isinstance(history, list):
        return 0
    count = 0
    for item in history:
        if not isinstance(item, dict):
            continue
        for sub in item.get("submissions") or []:
            if not isinstance(sub, dict):
                continue
            if (sub.get("status") or sub.get("submission_status")) == "AP":
                count += 1
                break
    return count


_HARD_ENDPOINT_RE = re.compile(r"\b(os|overall survival|mortality|mace)\b", re.IGNORECASE)
_SURROGATE_ENDPOINT_RE = re.compile(r"\b(pfs|orr|response rate|biomarker|surrogate)\b", re.IGNORECASE)


def _readout_timeline_bucket(raw: Dict[str, Any]) -> str:
    days = raw.get("days_until_pdufa")
    if days is None:
        days = raw.get("days_until_readout")
    if not isinstance(days, (int, float)):
        return "unknown"
    if days <= 14:
        return "≤14d"
    if days <= 30:
        return "15-30d"
    if days <= 90:
        return "31-90d"
    return ">90d"


def enrich_biotech_signal(signal: Dict[str, Any]) -> Dict[str, Any]:
    raw = signal.get("raw_payload") or {}
    enrichment = raw.get("enrichment") or {}
    trial = enrichment.get("trial") or {}
    trial_status = str(trial.get("status") or raw.get("status") or "")
    primary_outcomes = raw.get("primary_outcomes") or trial.get("primary_outcomes") or []
    if not isinstance(primary_outcomes, list):
        primary_outcomes = []
    outcome_text = " ".join(str(x or "") for x in primary_outcomes)
    hard_endpoint_present = bool(raw.get("hard_endpoint_present")) or bool(_HARD_ENDPOINT_RE.search(outcome_text))
    surrogate_endpoint_present = bool(raw.get("surrogate_endpoint_present")) or bool(_SURROGATE_ENDPOINT_RE.search(outcome_text))
    single_primary_endpoint = bool(raw.get("single_primary_endpoint")) if "single_primary_endpoint" in raw else len(primary_outcomes) == 1

    endpoint_strength = 3
    if single_primary_endpoint:
        endpoint_strength += 1
    if trial_status in {"COMPLETED", "ACTIVE_NOT_RECRUITING"}:
        endpoint_strength += 1
    adcom_ratio = _parse_adcom_vote(raw.get("adcom_vote"))
    if adcom_ratio is not None:
        if adcom_ratio >= 0.75:
            endpoint_strength += 1
        elif adcom_ratio <= 0.25:
            endpoint_strength -= 1
    endpoint_strength = _clamp(endpoint_strength)

    sponsor_track_record = 3
    approval_count = _approval_count(enrichment.get("fda_history"))
    sponsor_class = str(raw.get("sponsor_class") or "")
    industry_sponsored = bool(raw.get("industry_sponsored")) if "industry_sponsored" in raw else sponsor_class == "INDUSTRY"
    if approval_count >= 3:
        sponsor_track_record = 5
    elif approval_count >= 1:
        sponsor_track_record = 4
    elif industry_sponsored:
        sponsor_track_record = 3
    else:
        sponsor_track_record = 2

    adcom_ratio = _parse_adcom_vote(raw.get("adcom_vote"))
    enrollment = raw.get("enrollment")
    meaningful_enrollment = (
        bool(raw.get("meaningful_enrollment"))
        if "meaningful_enrollment" in raw
        else isinstance(enrollment, int) and enrollment >= 200
    )
    ev_inputs_complete = all(
        isinstance(raw.get(k), (int, float))
        for k in ("approval_probability", "upside_pct", "downside_pct")
    )
    expected_value_pct = None
    if ev_inputs_complete:
        approval_probability = float(raw["approval_probability"])
        upside_pct = float(raw["upside_pct"])
        downside_pct = float(raw["downside_pct"])
        expected_value_pct = round(
            approval_probability * upside_pct
            - (1 - approval_probability) * abs(downside_pct),
            2,
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "source": "deterministic",
        "endpoint_strength_tier": endpoint_strength,
        "single_primary_endpoint": single_primary_endpoint,
        "hard_endpoint_present": hard_endpoint_present,
        "surrogate_endpoint_present": surrogate_endpoint_present,
        "sponsor_track_record_tier": sponsor_track_record,
        "approval_history_count": approval_count,
        "trial_status": trial_status or None,
        "approval_probability": raw.get("approval_probability"),
        "indication_key": raw.get("base_rate_key") or raw.get("indication"),
        "patterns_hit": raw.get("patterns_hit"),
        "enrollment": enrollment,
        "meaningful_enrollment": meaningful_enrollment,
        "sponsor_class": sponsor_class or None,
        "industry_sponsored": industry_sponsored,
        "adcom_support_ratio": round(adcom_ratio, 3) if adcom_ratio is not None else None,
        "readout_timeline_bucket": _readout_timeline_bucket(raw),
        "ev_inputs_complete": ev_inputs_complete,
        "expected_value_pct": expected_value_pct,
        "enriched_at": datetime.now(timezone.utc).isoformat(),
    }


def biotech_enrichment_sweep(client: Optional[SupabaseClient] = None) -> Dict[str, Any]:
    client = client or SupabaseClient()
    since = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).isoformat()
    rows = client._rest(
        "GET",
        "signals",
        params={
            "select": "signal_id,signal_type,raw_payload,extensions",
            "scoring_profile": "eq.binary_catalyst",
            "scan_date": f"gte.{since}",
            "order": "scan_date.desc",
            "limit": "500",
        },
    ) or []

    updated = 0
    endpoint_histogram: Dict[str, int] = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    for row in rows:
        enrichment = enrich_biotech_signal(row)
        endpoint_histogram[str(enrichment["endpoint_strength_tier"])] += 1
        merged = dict(row.get("extensions") or {})
        merged["biotech_enrichment"] = enrichment
        client._rest(
            "PATCH",
            "signals",
            params={"signal_id": f"eq.{row['signal_id']}"},
            json_body={"extensions": merged},
        )
        updated += 1

    return {
        "function": NAME,
        "updated": updated,
        "endpoint_strength_histogram": endpoint_histogram,
        "window_days": WINDOW_DAYS,
    }
