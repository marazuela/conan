"""
Forward Catalyst Calendar (v1.0 — 2026-04-20)
=============================================

Aggregates every known forward-dated catalyst across the system into a
single lookahead calendar.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).parent.parent
SIGNALS_DIR = REPO / "signals"
CANDIDATES_DIR = REPO / "candidates"
ARCHIVED_DIR = CANDIDATES_DIR / "_archived_post_edge"
CURATED_PATH = CANDIDATES_DIR / "_curated_rationales.json"
WORKING = REPO / "working"
WORKING.mkdir(exist_ok=True)

DEFAULT_WINDOW_DAYS = 180
EVENT_TYPE_RULES = [
    (re.compile(r"pdufa|fda|approval|cr[l]|drug application", re.I), "PDUFA"),
    (re.compile(r"shareholder meeting|annual meeting|proxy vote|AGM", re.I), "shareholder_meeting"),
    (re.compile(r"merger close|close date|effective date|tender.*(expir|deadline)", re.I), "merger_close"),
    (re.compile(r"phase.?3|readout|topline|clinical", re.I), "trial_readout"),
    (re.compile(r"13d|activist|proxy", re.I), "activist_proxy"),
]
CATALYST_DATE_KEYS = [
    "pdufa_date",
    "catalyst_date",
    "meeting_date",
    "shareholder_meeting_date",
    "close_date",
    "effective_date",
    "offer_expiration",
    "tender_close_date",
    "primary_completion_date",
    "readout_date",
    "trial_completion_date",
]


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except Exception:
            pass
    match = re.match(r"^(\d{4})/(\d{2})/(\d{2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except Exception:
            pass
    return None


def _classify_event(text: str, fallback: str = "other") -> str:
    if not text:
        return fallback
    for pattern, tag in EVENT_TYPE_RULES:
        if pattern.search(text):
            return tag
    return fallback


def _priority_from_time_sensitivity(value: Any) -> str:
    if not value:
        return "—"
    text = str(value).upper()
    if text.startswith("HIGH") or "URGENT" in text or "IMMEDIATE" in text:
        return "HIGH"
    if "MEDIUM" in text:
        return "MEDIUM"
    if text.startswith("LOW"):
        return "LOW"
    return "—"


def _existing_md(ticker: str) -> Optional[Path]:
    for hit in sorted(CANDIDATES_DIR.glob(f"{ticker.upper()}_*.md")):
        return hit
    if ARCHIVED_DIR.exists():
        for hit in sorted(ARCHIVED_DIR.glob(f"{ticker.upper()}_*.md")):
            return hit
    return None


def _load_curated() -> Dict[str, Any]:
    if not CURATED_PATH.exists():
        return {}
    try:
        return json.loads(CURATED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
    except Exception:
        pass
    os.replace(tmp, path)


def _event(
    date_iso: str,
    ticker: Optional[str],
    company: str,
    event_type: str,
    source: str,
    status: str,
    priority: str,
    notes: str,
    md: Optional[Path],
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    parsed = _parse_date(date_iso)
    if parsed is None:
        return {}
    event = {
        "date_iso": parsed.isoformat(),
        "days_until": (parsed - _today()).days,
        "ticker": (ticker or "").upper() or None,
        "company_name": company,
        "event_type": event_type,
        "source": source,
        "candidate_status": status,
        "priority": priority,
        "notes": (notes or "")[:200],
        "md_path": str(md) if md else None,
    }
    if extra:
        event["extra"] = extra
    return event


def _events_from_curated(curated: Dict[str, Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for ticker, value in curated.items():
        if ticker.startswith("_") or not isinstance(value, dict):
            continue
        catalyst_date = value.get("catalyst_date_iso")
        if not catalyst_date:
            continue
        priority = _priority_from_time_sensitivity(value.get("time_sensitivity"))
        notes = value.get("one_liner") or value.get("time_sensitivity") or ""
        event_type = _classify_event(f"{value.get('time_sensitivity') or ''} {value.get('one_liner') or ''}", fallback="other")
        event = _event(
            date_iso=catalyst_date,
            ticker=ticker,
            company=value.get("company_name") or "",
            event_type=event_type,
            source="curated",
            status=("draft" if bool(value.get("_draft")) else "active"),
            priority=priority,
            notes=notes,
            md=_existing_md(ticker),
        )
        if event:
            events.append(event)

    archived = curated.get("_archived") or {}
    for ticker, value in archived.items() if isinstance(archived, dict) else []:
        if not isinstance(value, dict):
            continue
        catalyst_date = value.get("catalyst_date_iso") or value.get("resolution_date")
        if not catalyst_date:
            continue
        parsed = _parse_date(catalyst_date)
        if not parsed or parsed < _today():
            continue
        event = _event(
            date_iso=catalyst_date,
            ticker=ticker,
            company=value.get("company_name") or "",
            event_type="other",
            source="curated_archived",
            status="archived",
            priority="—",
            notes=(value.get("outcome") or "")[:140],
            md=_existing_md(ticker),
        )
        if event:
            events.append(event)
    return events


def _events_from_pre_phase3() -> List[Dict[str, Any]]:
    path = SIGNALS_DIR / "pre_phase3_readout_scanner_output.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    records = data.get("signals") or data.get("records") or (data if isinstance(data, list) else [])
    events = []
    for record in records:
        if not isinstance(record, dict):
            continue
        raw = record.get("raw_data") or {}
        primary_date = raw.get("primary_completion_date") or raw.get("readout_date")
        if not primary_date:
            continue
        ticker = record.get("ticker")
        notes = f"{raw.get('trial_title') or ''} | approval_p={raw.get('approval_probability')}, +{raw.get('upside_pct')}% / -{raw.get('downside_pct')}%"
        event = _event(
            date_iso=primary_date,
            ticker=ticker,
            company=record.get("company_name_en") or "",
            event_type="trial_readout",
            source="pre_phase3_readout",
            status="untracked",
            priority="—",
            notes=notes,
            md=_existing_md(ticker) if ticker else None,
            extra={"nct_id": raw.get("nct_id"), "approval_probability": raw.get("approval_probability")},
        )
        if event:
            events.append(event)
    return events


def _events_from_generic_scanners() -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for path in SIGNALS_DIR.glob("*_scanner_output.json"):
        if path.name == "pre_phase3_readout_scanner_output.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict):
            records = data.get("signals") or data.get("records") or []
        else:
            records = []
        scanner_name = path.name.replace("_scanner_output.json", "")
        for record in records:
            if not isinstance(record, dict):
                continue
            raw = record.get("raw_data") or {}
            catalyst_date = None
            matched_key = None
            for key in CATALYST_DATE_KEYS:
                if raw.get(key):
                    catalyst_date = raw[key]
                    matched_key = key
                    break
                if record.get(key):
                    catalyst_date = record[key]
                    matched_key = key
                    break
            if not catalyst_date:
                continue
            if not _parse_date(catalyst_date):
                continue
            ticker = record.get("ticker") or record.get("ticker_plus_mic", "").split(".")[0] or None
            event_type = _classify_event(f"{matched_key} {record.get('signal_type', '')} {record.get('headline', '')}", fallback="other")
            event = _event(
                date_iso=catalyst_date,
                ticker=ticker,
                company=record.get("company_name_en") or record.get("company_name_local") or "",
                event_type=event_type,
                source=scanner_name,
                status="untracked",
                priority="—",
                notes=(record.get("headline") or "")[:160],
                md=_existing_md(ticker) if ticker else None,
                extra={"matched_key": matched_key, "signal_id": record.get("signal_id")},
            )
            if event:
                events.append(event)
    return events


def _dedupe(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    priority = {"curated": 3, "curated_archived": 2, "pre_phase3_readout": 1}
    best: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for event in events:
        key = ((event.get("ticker") or "?"), event["date_iso"], event["event_type"])
        current = best.get(key)
        if current is None or priority.get(event["source"], 0) > priority.get(current["source"], 0):
            best[key] = event
    return list(best.values())


def _group_events(events: List[Dict[str, Any]], window_days: int) -> Dict[str, List[Dict[str, Any]]]:
    today = _today()
    horizon = today + timedelta(days=window_days)
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "overdue": [],
        "this_week": [],
        "next_30_days": [],
        "30_to_90_days": [],
        "90_plus_days": [],
    }
    for event in events:
        parsed = _parse_date(event["date_iso"])
        if not parsed:
            continue
        days = (parsed - today).days
        if parsed > horizon:
            continue
        if days < 0:
            if event["candidate_status"] in ("active", "draft"):
                buckets["overdue"].append(event)
            continue
        if days <= 7:
            buckets["this_week"].append(event)
        elif days <= 30:
            buckets["next_30_days"].append(event)
        elif days <= 90:
            buckets["30_to_90_days"].append(event)
        else:
            buckets["90_plus_days"].append(event)
    for key in buckets:
        buckets[key].sort(key=lambda event: (event["date_iso"], event.get("ticker") or ""))
    return buckets


def run(window_days: int = DEFAULT_WINDOW_DAYS, ticker_filter: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
    curated = _load_curated()
    events = _events_from_curated(curated) + _events_from_pre_phase3() + _events_from_generic_scanners()
    if ticker_filter:
        ticker = ticker_filter.upper()
        events = [event for event in events if (event.get("ticker") or "") == ticker]
    events = _dedupe(events)

    active_tickers = {key.upper() for key in curated.keys() if not key.startswith("_")}
    for event in events:
        ticker = event.get("ticker")
        if ticker and event["candidate_status"] == "untracked" and ticker in active_tickers:
            event["candidate_status"] = "active_uncorrelated"

    buckets = _group_events(events, window_days)
    summary = {
        "n_total": len(events),
        "per_bucket": {key: len(value) for key, value in buckets.items()},
        "per_source": dict(Counter(event["source"] for event in events)),
        "per_event_type": dict(Counter(event["event_type"] for event in events)),
    }
    result = {
        "ran_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "today": _today().isoformat(),
        "window_days": window_days,
        "summary": summary,
        "buckets": buckets,
    }
    if not dry_run:
        _atomic_write(WORKING / "catalyst_calendar.json", json.dumps(result, indent=2, ensure_ascii=False))
        _atomic_write(WORKING / "catalyst_calendar.md", _render_md(result))
        result["_out_json"] = str(WORKING / "catalyst_calendar.json")
        result["_out_md"] = str(WORKING / "catalyst_calendar.md")
    return result


def _fmt_row(event: Dict[str, Any]) -> str:
    ticker = event.get("ticker") or "—"
    date_text = f"{event['date_iso']} (T+{event['days_until']})"
    notes = (event.get("notes") or "").replace("|", "/").replace("\n", " ")
    md = event.get("md_path") or ""
    md_link = ""
    if md:
        try:
            rel = os.path.relpath(md, start=str(REPO))
        except Exception:
            rel = md
        md_link = f" [MD]({rel})"
    return f"| {date_text} | **{ticker}** | {event['event_type']} | {event['candidate_status']} | {event.get('priority') or '—'} | {event['source']} | {notes[:90]}{md_link} |"


def _render_md(result: Dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [
        f"# Forward Catalyst Calendar — as of {result['today']}",
        "",
        f"**Window**: next {result['window_days']} days. **Total events**: {summary['n_total']}",
        "",
        f"**Buckets**: overdue={summary['per_bucket']['overdue']}, this-week={summary['per_bucket']['this_week']}, next-30d={summary['per_bucket']['next_30_days']}, 30-90d={summary['per_bucket']['30_to_90_days']}, 90+d={summary['per_bucket']['90_plus_days']}",
        "",
        f"**By source**: {summary['per_source']}",
        "",
        f"**By event type**: {summary['per_event_type']}",
        "",
    ]
    bucket_titles = [
        ("overdue", "Overdue — active candidates whose catalyst has passed"),
        ("this_week", "This week (0-7 days)"),
        ("next_30_days", "Next 30 days (8-30)"),
        ("30_to_90_days", "30-90 days"),
        ("90_plus_days", "90+ days"),
    ]
    for key, title in bucket_titles:
        events = result["buckets"].get(key, [])
        if not events:
            continue
        lines.extend([f"## {title}", "", "| Date | Ticker | Type | Status | Priority | Source | Notes |", "|------|--------|------|--------|----------|--------|-------|"])
        for event in events:
            lines.append(_fmt_row(event))
        lines.append("")
    lines.extend(
        [
            "---",
            "",
            "_Sources: `_curated_rationales.json` (active + draft + archived), `pre_phase3_readout_scanner_output.json`, all `signals/*_scanner_output.json` with recognized date fields._",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Forward catalyst calendar aggregator")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW_DAYS, help=f"Window length in days (default {DEFAULT_WINDOW_DAYS})")
    parser.add_argument("--ticker", help="Filter to a single ticker")
    parser.add_argument("--dry-run", action="store_true", help="Do not write files")
    args = parser.parse_args()
    result = run(window_days=args.window, ticker_filter=args.ticker, dry_run=args.dry_run)
    compact = {
        "ran_at_utc": result["ran_at_utc"],
        "today": result["today"],
        "window_days": result["window_days"],
        "summary": result["summary"],
        "out_json": result.get("_out_json"),
        "out_md": result.get("_out_md"),
    }
    print(json.dumps(compact, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
