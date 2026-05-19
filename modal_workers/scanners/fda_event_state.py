"""
FDA event-state helpers — transforms the legacy pdufa_watchlist.json payload into
the canonical fda_assets / fda_regulatory_events / fda_event_evidence shape.

The watchlist JSON is a preserved v1 export (see
data/legacy/pdufa_watchlist.json). v2 makes Postgres authoritative and keeps the
JSON as a rollback-only export.

This module is pure: transform_watchlist_payload takes a list of dicts and
returns the shape that the backfill script will INSERT. No DB I/O. The
acceptance invariant for Phase 1 is unit-testable here:

  - exactly one asset row per (ticker, drug_name, application_number)
  - 0 or 1 event row per (ticker, drug_name, status) per the rules below
  - one evidence row per emitted event (provenance = 'manual')

Status rules (six values seen in production JSON):
  active           -> 1 pending PDUFA event on pdufa_date
  approved         -> 1 resolved approval event on resolution_date (fallback pdufa_date)
  resolved_crl     -> 1 resolved CRL event on crl_date
  linked_to_*      -> 0 events (derivative ticker; asset retained for cross-link)
  non_tradeable    -> 0 events (private / foreign listing)

Additional rule: if adcom_date is set on an active row, emit a parallel pending
adcom event so the bridge can surface both milestones.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, TypedDict


class AssetRow(TypedDict, total=False):
    ticker: str
    mic: Optional[str]
    drug_name: str
    application_number: str
    application_type: Optional[str]
    indication: Optional[str]
    sponsor_name: Optional[str]
    extensions: Dict[str, Any]


class EventRow(TypedDict, total=False):
    asset_key: str  # (ticker, drug_name, application_number) joined
    event_type: str
    event_date: Optional[str]
    event_status: str
    source_content_hash: str
    notes: Optional[str]
    extensions: Dict[str, Any]


class EvidenceRow(TypedDict, total=False):
    asset_key: str
    event_key: str  # (asset_key, event_type, event_date)
    source: str
    evidence_type: str
    payload: Dict[str, Any]
    hash: str


class TransformResult(TypedDict):
    assets: List[AssetRow]
    events: List[EventRow]
    evidence: List[EvidenceRow]


SKIP_STATUSES_FOR_EVENTS = ("non_tradeable",)


def _asset_key(ticker: str, drug_name: str, application_number: str) -> str:
    return f"{ticker}|{drug_name}|{application_number}"


def _event_key(asset_key: str, event_type: str, event_date: Optional[str]) -> str:
    return f"{asset_key}|{event_type}|{event_date or ''}"


def _hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def _is_linked_status(status: str) -> bool:
    return status.startswith("linked_to_")


def _coerce_app_number(raw: Any) -> str:
    if raw is None:
        return ""
    return str(raw).strip()


def _make_asset(row: Dict[str, Any]) -> AssetRow:
    ticker = str(row["ticker"]).strip()
    drug_name = str(row.get("drug_name") or "").strip()
    application_number = _coerce_app_number(row.get("application_number"))
    extensions: Dict[str, Any] = {
        "company_name": row.get("company_name"),
        "added_date": row.get("added_date"),
        "phase3_nctid": row.get("phase3_nctid"),
        "watchlist_status": row.get("status"),
    }
    if row.get("status") and _is_linked_status(row["status"]):
        extensions["linked_to"] = row["status"].replace("linked_to_", "")
    return AssetRow(
        ticker=ticker,
        mic=None,  # MIC not known at watchlist time — resolved downstream via entity_identifiers
        drug_name=drug_name,
        application_number=application_number,
        application_type=row.get("nda_type"),
        indication=row.get("indication"),
        sponsor_name=row.get("company_name"),
        extensions=extensions,
    )


def _build_event(
    asset_key: str,
    *,
    ticker: str,
    drug_name: str,
    application_number: str,
    event_type: str,
    event_date: Optional[str],
    event_status: str,
    notes: Optional[str],
) -> EventRow:
    sch = _hash(ticker, drug_name, application_number, event_type, event_date or "")
    return EventRow(
        asset_key=asset_key,
        event_type=event_type,
        event_date=event_date,
        event_status=event_status,
        source_content_hash=sch,
        notes=notes,
        extensions={},
    )


def _build_evidence(
    asset_key: str, event: EventRow, raw_row: Dict[str, Any]
) -> EvidenceRow:
    event_date = event.get("event_date") or ""
    ev_key = _event_key(asset_key, event["event_type"], event.get("event_date"))
    payload = {
        "source": "watchlist_backfill",
        "ticker": raw_row.get("ticker"),
        "drug_name": raw_row.get("drug_name"),
        "indication": raw_row.get("indication"),
        "nda_type": raw_row.get("nda_type"),
        "is_resubmission": raw_row.get("is_resubmission"),
        "watchlist_status": raw_row.get("status"),
        "added_date": raw_row.get("added_date"),
        "resolution_date": raw_row.get("resolution_date"),
        "resolution_note": raw_row.get("resolution_note"),
        "notes": raw_row.get("notes"),
    }
    h = _hash(asset_key, event["event_type"], event_date, "watchlist_backfill")
    return EvidenceRow(
        asset_key=asset_key,
        event_key=ev_key,
        source="manual",
        evidence_type="watchlist_backfill",
        payload=payload,
        hash=h,
    )


def transform_watchlist_payload(rows: List[Dict[str, Any]]) -> TransformResult:
    """Map raw watchlist JSON rows to canonical asset/event/evidence rows.

    Invariants asserted by tests:
      - Exactly one AssetRow per (ticker, drug_name, application_number).
      - EventRow count per row is determined by status:
          active        -> 1 (pdufa) + optional 1 (adcom) when adcom_date set
          approved      -> 1 (approval, resolved)
          resolved_crl  -> 1 (crl, resolved)
          linked_to_*   -> 0
          non_tradeable -> 0
      - One EvidenceRow per EventRow (provenance='manual').
    """
    assets_by_key: Dict[str, AssetRow] = {}
    events: List[EventRow] = []
    evidence: List[EvidenceRow] = []

    for row in rows:
        if not row.get("ticker") or not row.get("drug_name"):
            continue
        ticker = str(row["ticker"]).strip()
        drug_name = str(row["drug_name"]).strip()
        application_number = _coerce_app_number(row.get("application_number"))
        key = _asset_key(ticker, drug_name, application_number)

        if key not in assets_by_key:
            assets_by_key[key] = _make_asset(row)

        status = (row.get("status") or "").strip()
        if status in SKIP_STATUSES_FOR_EVENTS or _is_linked_status(status):
            continue

        row_events: List[EventRow] = []
        if status == "active":
            pdufa_date = row.get("pdufa_date")
            if pdufa_date:
                row_events.append(
                    _build_event(
                        key,
                        ticker=ticker,
                        drug_name=drug_name,
                        application_number=application_number,
                        event_type="pdufa",
                        event_date=pdufa_date,
                        event_status="pending",
                        notes=row.get("notes"),
                    )
                )
            if row.get("adcom_date"):
                row_events.append(
                    _build_event(
                        key,
                        ticker=ticker,
                        drug_name=drug_name,
                        application_number=application_number,
                        event_type="adcom",
                        event_date=row["adcom_date"],
                        event_status="pending",
                        notes=row.get("notes"),
                    )
                )
        elif status == "approved":
            event_date = row.get("resolution_date") or row.get("pdufa_date")
            row_events.append(
                _build_event(
                    key,
                    ticker=ticker,
                    drug_name=drug_name,
                    application_number=application_number,
                    event_type="approval",
                    event_date=event_date,
                    event_status="resolved",
                    notes=row.get("resolution_note") or row.get("notes"),
                )
            )
        elif status == "resolved_crl":
            event_date = row.get("crl_date") or row.get("resolution_date")
            row_events.append(
                _build_event(
                    key,
                    ticker=ticker,
                    drug_name=drug_name,
                    application_number=application_number,
                    event_type="crl",
                    event_date=event_date,
                    event_status="resolved",
                    notes=row.get("resolution_note") or row.get("notes"),
                )
            )

        for ev in row_events:
            events.append(ev)
            evidence.append(_build_evidence(key, ev, row))

    return TransformResult(
        assets=list(assets_by_key.values()),
        events=events,
        evidence=evidence,
    )
