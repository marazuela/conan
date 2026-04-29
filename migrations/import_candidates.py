"""Reconcile legacy candidate dossiers into the v2 `public.candidates` table.

The legacy reporting layer still treats the curated top-level candidates as the
authoritative live set. This script makes sure those names exist in Supabase so
the dashboard does not lose them when a row is deleted or moved terminal by
mistake.

Dry-run:
    python3 migrations/import_candidates.py --dry-run

Live:
    SUPABASE_URL=https://... \
    SUPABASE_SERVICE_ROLE_KEY=sbp_... \
    python3 migrations/import_candidates.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
V1_ROOT = REPO_ROOT / "unified_system" / "unified_system"
CANDIDATES_DIR = V1_ROOT / "candidates"
CURATED_RATIONALES = CANDIDATES_DIR / "_curated_rationales.json"
SIGNAL_LOG = V1_ROOT / "signals" / "signal_log.json"
WORKING = V1_ROOT / "working"
WORKING.mkdir(exist_ok=True)

_LEGACY_TOOLS = str(V1_ROOT / "tools")
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if _LEGACY_TOOLS not in sys.path:
    sys.path.insert(0, _LEGACY_TOOLS)

from candidate_gate import extract_existing_thesis  # type: ignore
from candidate_monitor import _classify_archetype  # type: ignore
from modal_workers.shared.rubric_engine import classify_band
from modal_workers.shared.supabase_client import SupabaseClient


ARCHETYPE_TO_PROFILE = {
    "pdufa": "binary_catalyst",
    "activist": "activist_governance",
    "proxy": "activist_governance",
    "merger_arb": "merger_arb",
    "short_positioning": "short_positioning",
    "litigation": "litigation",
}

MIC_ALIASES: Tuple[Tuple[str, str], ...] = (
    ("nasdaq", "XNAS"),
    ("xnas", "XNAS"),
    ("nyse american", "XASE"),
    ("amex", "XASE"),
    ("xase", "XASE"),
    ("new york stock exchange", "XNYS"),
    ("nyse", "XNYS"),
    ("xnys", "XNYS"),
    ("tsx venture", "XTSX"),
    ("xtsx", "XTSX"),
    ("tsx", "XTSE"),
    ("xtse", "XTSE"),
    ("london stock exchange", "XLON"),
    ("lse", "XLON"),
    ("xlon", "XLON"),
    ("australian securities exchange", "XASX"),
    ("asx", "XASX"),
    ("xasx", "XASX"),
    ("hong kong", "XHKG"),
    ("hkex", "XHKG"),
    ("xhkg", "XHKG"),
    ("tokyo stock exchange", "XTKS"),
    ("tdnet", "XTKS"),
    ("jpx", "XTKS"),
    ("xtks", "XTKS"),
    ("india nse", "XNSE"),
    ("national stock exchange", "XNSE"),
    ("xnse", "XNSE"),
    ("bombay stock exchange", "XBOM"),
    ("bse", "XBOM"),
    ("xbom", "XBOM"),
    ("mexico", "XMEX"),
    ("bmv", "XMEX"),
    ("xmex", "XMEX"),
)

MONTH_LOOKUP = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
QUARTER_RE = re.compile(r"\b(Q[1-4])\s+(\d{4})\b", re.IGNORECASE)
HALF_RE = re.compile(r"\b(H[12])\s+(\d{4})\b", re.IGNORECASE)
SEASON_RE = re.compile(r"\b(early|mid|late)\s+(\d{4})\b", re.IGNORECASE)
LONG_DATE_RE = re.compile(
    r"\b("
    r"January|February|March|April|May|June|July|August|"
    r"September|October|November|December|"
    r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    r")\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)
MONTH_RE = re.compile(
    r"\b("
    r"January|February|March|April|May|June|July|August|"
    r"September|October|November|December|"
    r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    r")\s+(\d{4})\b",
    re.IGNORECASE,
)
SCORE_LINE_RE = re.compile(
    r"(?im)^(?:>\s*)?(?:##\s*)?(?:\*\*)?score(?:\*\*)?\s*:\s*(.+)$"
)


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _report_path() -> Path:
    return WORKING / f"legacy_candidate_reconcile_report_{_today_iso()}.json"


def _relpath(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def _load_curated_rationales() -> Dict[str, Any]:
    return json.loads(CURATED_RATIONALES.read_text(encoding="utf-8"))


def _load_signal_log_mics() -> Dict[str, str]:
    if not SIGNAL_LOG.exists():
        return {}
    try:
        payload = json.loads(SIGNAL_LOG.read_text(encoding="utf-8"))
    except Exception:
        return {}

    rows = payload if isinstance(payload, list) else payload.get("signals", [])
    counts: Dict[str, Dict[str, int]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        ticker_plus_mic = str(row.get("ticker_plus_mic") or "")
        ticker = (
            str(row.get("ticker") or row.get("ticker_local") or ticker_plus_mic.split(".")[0] or "")
            .strip()
            .upper()
        )
        mic = str(row.get("mic") or "").strip().upper()
        if not mic and "." in ticker_plus_mic:
            mic = ticker_plus_mic.split(".", 1)[1].strip().upper()
        if not ticker or not mic:
            continue
        counts.setdefault(ticker, {})
        counts[ticker][mic] = counts[ticker].get(mic, 0) + 1

    resolved: Dict[str, str] = {}
    for ticker, per_mic in counts.items():
        resolved[ticker] = sorted(
            per_mic.items(),
            key=lambda item: (item[1], item[0]),
            reverse=True,
        )[0][0]
    return resolved


def _find_md_for_ticker(ticker: str) -> Optional[Path]:
    matches = sorted(CANDIDATES_DIR.glob(f"{ticker}_*.md"))
    return matches[0] if matches else None


def _guess_mic(md_text: str) -> Optional[str]:
    lowered = md_text.lower()
    for needle, mic in MIC_ALIASES:
        if needle in lowered:
            return mic
    return None


def _parse_score(md_text: str) -> Optional[float]:
    payload = ""
    for line in md_text.splitlines():
        stripped = line.strip()
        normalized = stripped.lstrip(">").strip().replace("*", "")
        if normalized.lower().startswith("score:") or normalized.lower().startswith("## score:"):
            payload = normalized.split(":", 1)[1].strip()
            break
    if not payload:
        match = SCORE_LINE_RE.search(md_text)
        if not match:
            return None
        payload = match.group(1).replace("*", "").strip()
    if "→" in payload or "->" in payload:
        tail = re.split(r"→|->", payload, maxsplit=1)[1]
        floats = re.findall(r"\d+(?:\.\d+)?", tail)
        if floats:
            return float(floats[0])
    slash = re.search(r"(\d+(?:\.\d+)?)\s*/", payload)
    if slash:
        return float(slash.group(1))
    floats = re.findall(r"\d+(?:\.\d+)?", payload)
    return float(floats[0]) if floats else None


def _daterange(start: str, end_exclusive: str) -> str:
    return f"[{start},{end_exclusive})"


def _month_window(year: int, month: int) -> Tuple[str, str]:
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{month + 1:02d}-01"
    return start, end


def _parse_catalyst_value(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    text = (value or "").strip()
    if not text:
        return None, None

    iso_match = ISO_DATE_RE.search(text)
    if iso_match:
        return iso_match.group(1), None

    long_date = LONG_DATE_RE.search(text)
    if long_date:
        month_num = MONTH_LOOKUP[long_date.group(1).lower()]
        day = int(long_date.group(2))
        year = int(long_date.group(3))
        return f"{year:04d}-{month_num:02d}-{day:02d}", None

    quarter = QUARTER_RE.search(text)
    if quarter:
        qtr = quarter.group(1).upper()
        year = int(quarter.group(2))
        starts = {"Q1": (1, 1), "Q2": (4, 1), "Q3": (7, 1), "Q4": (10, 1)}
        start_month, start_day = starts[qtr]
        start = f"{year:04d}-{start_month:02d}-{start_day:02d}"
        if qtr == "Q4":
            end = f"{year + 1:04d}-01-01"
        else:
            end_month = start_month + 3
            end = f"{year:04d}-{end_month:02d}-01"
        return None, _daterange(start, end)

    half = HALF_RE.search(text)
    if half:
        half_name = half.group(1).upper()
        year = int(half.group(2))
        if half_name == "H1":
            return None, _daterange(f"{year:04d}-01-01", f"{year:04d}-07-01")
        return None, _daterange(f"{year:04d}-07-01", f"{year + 1:04d}-01-01")

    season = SEASON_RE.search(text)
    if season:
        name = season.group(1).lower()
        year = int(season.group(2))
        if name == "early":
            return None, _daterange(f"{year:04d}-01-01", f"{year:04d}-05-01")
        if name == "mid":
            return None, _daterange(f"{year:04d}-05-01", f"{year:04d}-09-01")
        return None, _daterange(f"{year:04d}-09-01", f"{year + 1:04d}-01-01")

    month = MONTH_RE.search(text)
    if month:
        month_num = MONTH_LOOKUP[month.group(1).lower()]
        year = int(month.group(2))
        start, end = _month_window(year, month_num)
        return None, _daterange(start, end)

    return None, None


def _extract_candidate_timing(
    thesis: Dict[str, str],
    fallback_date_iso: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    catalyst_body = thesis.get("next_catalyst", "")
    next_date, next_window = _parse_catalyst_value(catalyst_body)
    if next_date or next_window:
        return next_date, next_window
    return _parse_catalyst_value(fallback_date_iso)


def _derive_scoring_profile(
    ticker: str,
    rationale: Dict[str, Any],
    md_text: str,
) -> Optional[str]:
    archetype = _classify_archetype(rationale, md_text)
    if archetype in ARCHETYPE_TO_PROFILE:
        return ARCHETYPE_TO_PROFILE[archetype]

    lowered = (md_text + " " + json.dumps(rationale)).lower()
    if "pdufa" in lowered or "fda" in lowered:
        return "binary_catalyst"
    if "13d" in lowered or "proxy" in lowered or "activist" in lowered:
        return "activist_governance"
    if "lawsuit" in lowered or "verdict" in lowered:
        return "litigation"
    if "short" in lowered and "squeeze" in lowered:
        return "short_positioning"
    if "tender offer" in lowered or "take-private" in lowered:
        return "takeover_candidate"
    if ticker.isdigit():
        return "merger_arb"
    return None


def _pick_preferred_existing(rows: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not rows:
        return None
    return sorted(
        rows,
        key=lambda row: (
            bool(row.get("mic")),
            bool(row.get("entity_id")),
            row.get("state") in ("active", "watch"),
            row.get("updated_at") or "",
        ),
        reverse=True,
    )[0]


def _pick_preferred_entity(
    rows: Sequence[Dict[str, Any]],
    desired_mic: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not rows:
        return None
    if desired_mic:
        for row in rows:
            if row.get("primary_mic") == desired_mic:
                return row
    return sorted(rows, key=lambda row: (bool(row.get("primary_mic")), row.get("id") or ""), reverse=True)[0]


def _group_rows(rows: Iterable[Dict[str, Any]], key: str) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        value = str(row.get(key) or "").upper()
        if not value:
            continue
        grouped.setdefault(value, []).append(row)
    return grouped


def _merge_extensions(
    existing_extensions: Any,
    *,
    authoritative_state: str,
    md_path: Path,
) -> Dict[str, Any]:
    base = dict(existing_extensions) if isinstance(existing_extensions, dict) else {}
    base["legacy_import"] = {
        "authoritative_source": "curated_live",
        "dossier_path": _relpath(md_path),
        "rationale_path": _relpath(CURATED_RATIONALES),
        "authoritative_state": authoritative_state,
        "unstructured_kill_conditions": True,
    }
    return base


def _rows_differ(existing: Optional[Dict[str, Any]], desired: Dict[str, Any]) -> bool:
    if not existing:
        return True
    for key in (
        "mic",
        "entity_id",
        "state",
        "scoring_profile",
        "current_score",
        "current_band",
        "next_catalyst_date",
        "next_catalyst_window",
        "dossier_markdown",
        "kill_conditions",
        "extensions",
    ):
        if existing.get(key) != desired.get(key):
            return True
    return False


def load_legacy_candidate_specs(
    tickers: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    curated = _load_curated_rationales()
    signal_log_mics = _load_signal_log_mics()
    wanted = {ticker.upper() for ticker in tickers} if tickers else None
    specs: List[Dict[str, Any]] = []

    for ticker, rationale in sorted(curated.items()):
        if ticker.startswith("_") or not isinstance(rationale, dict):
            continue
        if wanted and ticker.upper() not in wanted:
            continue

        md_path = _find_md_for_ticker(ticker)
        md_text = md_path.read_text(encoding="utf-8", errors="replace") if md_path else ""
        thesis = extract_existing_thesis(md_text) if md_text else {}
        next_catalyst_date, next_catalyst_window = _extract_candidate_timing(
            thesis, rationale.get("catalyst_date_iso")
        )

        specs.append(
            {
                "ticker": ticker.upper(),
                "state": "watch" if bool(rationale.get("_draft")) else "active",
                "md_path": md_path,
                "dossier_markdown": md_text,
                "thesis": thesis,
                "current_score": _parse_score(md_text),
                "next_catalyst_date": next_catalyst_date,
                "next_catalyst_window": next_catalyst_window,
                "signal_log_mic": signal_log_mics.get(ticker.upper()),
                "guessed_mic": _guess_mic(md_text),
                "scoring_profile": _derive_scoring_profile(ticker.upper(), rationale, md_text),
            }
        )

    return specs


def build_candidate_upsert_rows(
    specs: Sequence[Dict[str, Any]],
    existing_by_ticker: Dict[str, List[Dict[str, Any]]],
    entities_by_ticker: Dict[str, List[Dict[str, Any]]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    restored: List[str] = []
    updated: List[str] = []
    unchanged: List[str] = []
    state_resets: List[str] = []
    skipped_missing_md: List[str] = []
    skipped_missing_mic: List[str] = []
    warnings: List[str] = []

    for spec in specs:
        ticker = spec["ticker"]
        md_path = spec.get("md_path")
        if not md_path or not spec.get("dossier_markdown"):
            skipped_missing_md.append(ticker)
            warnings.append(f"{ticker}: skipped because no legacy dossier markdown was found")
            continue

        existing_rows = existing_by_ticker.get(ticker, [])
        entity_rows = entities_by_ticker.get(ticker, [])
        if len(existing_rows) > 1:
            warnings.append(f"{ticker}: multiple existing candidate rows found; using the best-matching row")
        if len(entity_rows) > 1:
            warnings.append(f"{ticker}: multiple entity rows found; using the best-matching row")

        existing = _pick_preferred_existing(existing_rows)
        entity = _pick_preferred_entity(
            entity_rows,
            (existing.get("mic") if existing else None)
            or spec.get("signal_log_mic")
            or spec.get("guessed_mic"),
        )

        mic = (
            (existing.get("mic") if existing else None)
            or (entity.get("primary_mic") if entity else None)
            or spec.get("signal_log_mic")
            or spec.get("guessed_mic")
        )
        if not mic:
            skipped_missing_mic.append(ticker)
            warnings.append(f"{ticker}: skipped because no stable MIC could be resolved")
            continue

        score = (
            spec.get("current_score")
            if spec.get("current_score") is not None
            else (existing.get("current_score") if existing else None)
        )
        score_value = float(score) if score is not None else None
        scoring_profile = spec.get("scoring_profile") or (existing.get("scoring_profile") if existing else None)
        current_band = classify_band(score_value) if score_value is not None else (existing.get("current_band") if existing else None)
        existing_kills = existing.get("kill_conditions") if existing else None
        row = {
            "ticker": ticker,
            "mic": mic,
            "entity_id": (existing.get("entity_id") if existing else None) or (entity.get("id") if entity else None),
            "state": spec["state"],
            "scoring_profile": scoring_profile,
            "current_score": score_value,
            "current_band": current_band,
            "dossier_markdown": spec["dossier_markdown"],
            "kill_conditions": existing_kills if isinstance(existing_kills, list) else [],
            "next_catalyst_date": spec.get("next_catalyst_date"),
            "next_catalyst_window": spec.get("next_catalyst_window"),
            "extensions": _merge_extensions(
                existing.get("extensions") if existing else None,
                authoritative_state=spec["state"],
                md_path=md_path,
            ),
        }

        rows.append(row)
        if existing is None:
            restored.append(ticker)
            continue
        if existing.get("state") != row["state"]:
            state_resets.append(ticker)
        if _rows_differ(existing, row):
            updated.append(ticker)
        else:
            unchanged.append(ticker)

    summary = {
        "tickers_considered": [spec["ticker"] for spec in specs],
        "rows_prepared": len(rows),
        "restored": restored,
        "updated": updated,
        "unchanged": unchanged,
        "state_resets": state_resets,
        "skipped_missing_md": skipped_missing_md,
        "skipped_missing_mic": skipped_missing_mic,
        "warnings": warnings,
    }
    return rows, summary


def _in_filter(values: Sequence[str]) -> Optional[str]:
    cleaned = [value.strip().upper() for value in values if value and value.strip()]
    if not cleaned:
        return None
    return f"in.({','.join(cleaned)})"


def _fetch_existing_candidates(
    client: SupabaseClient,
    tickers: Sequence[str],
) -> Dict[str, List[Dict[str, Any]]]:
    ticker_filter = _in_filter(tickers)
    if not ticker_filter:
        return {}
    rows = client._rest(
        "GET",
        "candidates",
        params={
            "select": "ticker,mic,entity_id,state,scoring_profile,current_score,current_band,kill_conditions,extensions,next_catalyst_date,next_catalyst_window,updated_at",
            "ticker": ticker_filter,
            "limit": "200",
        },
    ) or []
    return _group_rows(rows, "ticker")


def _fetch_entities(
    client: SupabaseClient,
    tickers: Sequence[str],
) -> Dict[str, List[Dict[str, Any]]]:
    ticker_filter = _in_filter(tickers)
    if not ticker_filter:
        return {}
    rows = client._rest(
        "GET",
        "entities",
        params={
            "select": "id,primary_ticker,primary_mic",
            "primary_ticker": ticker_filter,
            "limit": "200",
        },
    ) or []
    return _group_rows(rows, "primary_ticker")


def reconcile_candidates(
    *,
    client: Optional[SupabaseClient] = None,
    dry_run: bool = False,
    tickers: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    # On --dry-run with no caller-supplied client, skip Supabase entirely so the
    # spec parser can be exercised offline without SUPABASE_URL / SERVICE_ROLE_KEY.
    if client is None and not dry_run:
        client = SupabaseClient()
    specs = load_legacy_candidate_specs(tickers)
    tickers_to_load = [spec["ticker"] for spec in specs]
    existing_by_ticker = (
        _fetch_existing_candidates(client, tickers_to_load) if client else {}
    )
    entities_by_ticker = (
        _fetch_entities(client, tickers_to_load) if client else {}
    )
    rows, summary = build_candidate_upsert_rows(specs, existing_by_ticker, entities_by_ticker)

    upserted = 0
    if rows and not dry_run:
        response = client._rest(
            "POST",
            "candidates",
            params={"on_conflict": "ticker,mic"},
            json_body=rows,
            prefer="resolution=merge-duplicates,return=representation",
        ) or []
        upserted = len(response) if isinstance(response, list) else len(rows)

    result: Dict[str, Any] = {
        "ran_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dry_run": dry_run,
        "live_tickers": tickers_to_load,
        "rows_prepared": len(rows),
        "upserted": upserted,
        **summary,
    }
    report_path = _report_path()
    report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["report_path"] = str(report_path)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile legacy curated candidates into Supabase")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--tickers",
        help="Optional comma-separated ticker subset, e.g. AXSM,VERA,VRDN",
    )
    args = parser.parse_args()

    tickers = [value.strip().upper() for value in (args.tickers or "").split(",") if value.strip()]
    result = reconcile_candidates(dry_run=args.dry_run, tickers=tickers or None)
    print(json.dumps(result, indent=2))
    print(f"Full report: {result['report_path']}")


if __name__ == "__main__":
    main()
