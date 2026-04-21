"""
Deterministic legal enrichment for litigation-profile signals.

Adds an additive `extensions.legal_enrichment` block only; never changes
dimensions, score, or band.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from modal_workers.shared.supabase_client import SupabaseClient

NAME = "legal_enrichment_sweep"
SCHEMA_VERSION = "legal_enrichment_v1"
WINDOW_DAYS = 30


def _clamp(v: int, lo: int = 1, hi: int = 5) -> int:
    return max(lo, min(hi, v))


def _risk_color(score: int) -> str:
    if score <= 4:
        return "green"
    if score <= 9:
        return "yellow"
    if score <= 15:
        return "orange"
    return "red"


def _case_family(nos: str, source_feed: str, title: str) -> str:
    if source_feed == "admin":
        return "sec_admin"
    if source_feed == "litrel":
        return "sec_litigation"
    if nos == "850" or "class action" in title:
        return "securities"
    if nos == "410" or "antitrust" in title:
        return "antitrust"
    if nos in {"830", "835"} or "patent" in title:
        return "patent_ip"
    if nos == "190":
        return "contract_mna"
    return "general_civil"


def _procedural_stage(signal_type: str) -> tuple[str, str, str]:
    mapping = {
        "settlement": ("settlement", "high", "1-3m"),
        "summary_judgment": ("summary_judgment", "high", "1-3m"),
        "class_certified": ("class_certification", "high", "3-6m"),
        "mtd_denied": ("post_mtd", "high", "3-6m"),
        "federal_civil_filed": ("complaint_filed", "medium", ">12m"),
        "cease_and_desist": ("cease_and_desist", "high", "≤1m"),
        "administrative_proceeding": ("administrative_proceeding", "high", "1-3m"),
        "litigation_release": ("litigation_release", "high", "1-3m"),
    }
    return mapping.get(signal_type, ("unknown", "low", "unknown"))


def _materiality_hint(case_family: str, signal_type: str) -> str:
    if signal_type in {"settlement", "cease_and_desist", "administrative_proceeding", "litigation_release"}:
        return "potentially_elevated"
    if case_family in {"antitrust", "securities"}:
        return "needs_claim_review"
    return "unknown"


def _merits_hint(signal_type: str) -> str:
    mapping = {
        "settlement": "liability_crystallized",
        "summary_judgment": "late_stage_merits",
        "class_certified": "class_survived_gatekeeping",
        "mtd_denied": "complaint_survived_mtd",
        "federal_civil_filed": "early_stage_only",
        "cease_and_desist": "regulator_action_already_taken",
        "administrative_proceeding": "regulator_action_pending",
        "litigation_release": "sec_litigation_active",
    }
    return mapping.get(signal_type, "unknown")


def enrich_legal_signal(signal: Dict[str, Any]) -> Dict[str, Any]:
    raw = signal.get("raw_payload") or {}
    signal_type = str(signal.get("signal_type") or raw.get("signal_type") or "").lower()
    source_feed = str(raw.get("source_feed") or "").lower()
    title = " ".join(
        str(x or "")
        for x in (
            raw.get("title"),
            raw.get("headline"),
            raw.get("summary"),
            raw.get("case_name"),
            raw.get("nature_of_suit"),
        )
    ).lower()
    nos = str(raw.get("nos") or raw.get("nature_of_suit") or "")
    case_family = str(raw.get("case_family") or _case_family(nos, source_feed, title))
    procedural_stage = str(raw.get("procedural_stage") or "")
    stage_confidence = str(raw.get("procedural_stage_confidence") or "")
    timeline_bucket = str(raw.get("resolution_timeline_bucket") or "")
    if not procedural_stage:
        procedural_stage, stage_confidence, timeline_bucket = _procedural_stage(signal_type)

    severity = {
        "settlement": 4,
        "summary_judgment": 4,
        "class_certified": 3,
        "mtd_denied": 3,
        "federal_civil_filed": 2,
        "cease_and_desist": 4,
        "administrative_proceeding": 4,
        "litigation_release": 4,
    }.get(signal_type, 3)

    likelihood = {
        "settlement": 5,
        "summary_judgment": 4,
        "class_certified": 4,
        "mtd_denied": 4,
        "federal_civil_filed": 2,
        "cease_and_desist": 5,
        "administrative_proceeding": 4,
        "litigation_release": 4,
    }.get(signal_type, 3)

    matched_keywords: List[str] = []
    for keyword, sev_bump, lik_bump in (
        ("fraud", 1, 1),
        ("class action", 1, 0),
        ("antitrust", 1, 1),
        ("patent", 1, 0),
        ("cease-and-desist", 0, 1),
        ("cease and desist", 0, 1),
        ("summary judgment", 0, 1),
        ("settlement", 0, 1),
    ):
        if keyword in title:
            matched_keywords.append(keyword)
            severity += sev_bump
            likelihood += lik_bump

    severity = _clamp(severity)
    likelihood = _clamp(likelihood)
    risk_score = severity * likelihood

    regulations: Set[str] = set()
    if source_feed == "admin" or signal_type in {"administrative_proceeding", "cease_and_desist"}:
        regulations.add("SEC Administrative Proceeding")
    if source_feed == "litrel" or signal_type == "litigation_release":
        regulations.add("SEC Litigation Release")
    if nos == "850":
        regulations.add("Securities Litigation")
    if nos == "410" or "antitrust" in title:
        regulations.add("Antitrust")
    if nos in {"830", "835"} or "patent" in title:
        regulations.add("Patent/IP")
    if nos == "190":
        regulations.add("Contract/M&A")

    return {
        "schema_version": SCHEMA_VERSION,
        "source": "deterministic",
        "case_family": case_family,
        "procedural_stage": procedural_stage,
        "procedural_stage_confidence": stage_confidence,
        "resolution_timeline_bucket": timeline_bucket,
        "merits_hint": _merits_hint(signal_type),
        "materiality_hint": _materiality_hint(case_family, signal_type),
        "ticker_hint_present": bool(raw.get("ticker_hint_present") or raw.get("ticker_hint")),
        "nos_code": nos or None,
        "severity_tier": severity,
        "likelihood_tier": likelihood,
        "risk_score": risk_score,
        "risk_color": _risk_color(risk_score),
        "regulations": sorted(regulations),
        "matched_keywords": matched_keywords,
        "enriched_at": datetime.now(timezone.utc).isoformat(),
    }


def legal_enrichment_sweep(client: Optional[SupabaseClient] = None) -> Dict[str, Any]:
    client = client or SupabaseClient()
    since = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).isoformat()
    rows = client._rest(
        "GET",
        "signals",
        params={
            "select": "signal_id,signal_type,raw_payload,extensions",
            "scoring_profile": "eq.litigation",
            "scan_date": f"gte.{since}",
            "order": "scan_date.desc",
            "limit": "500",
        },
    ) or []

    updated = 0
    colors: Dict[str, int] = {"green": 0, "yellow": 0, "orange": 0, "red": 0}
    for row in rows:
        enrichment = enrich_legal_signal(row)
        colors[enrichment["risk_color"]] += 1
        merged = dict(row.get("extensions") or {})
        merged["legal_enrichment"] = enrichment
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
        "by_color": colors,
        "window_days": WINDOW_DAYS,
    }
