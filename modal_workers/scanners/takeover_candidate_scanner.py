"""
Takeover Candidate Scanner — Modal port of v1 tools/takeover_candidate_scanner.py.

Pre-edge M&A target setup detector. Addresses the AVNS-class miss — issuers
that trade at a discount while exhibiting multiple take-private setup patterns
but have not yet filed a definitive agreement.

Preservation (v1 parity):
  - 5 setup patterns: institutional_pe_13g / activist_ownership_pe_13d /
    strategic_review_disclosure / streamlined_for_sale (plus per-issuer merge
    counting each contributing pattern toward the ≥2 triage gate).
  - Multi-pattern per-CIK merge with priority ordering
    (strategic_review > 13D > 13G > streamlined).
  - Triage gate: emit only if patterns_hit ≥ 2.
    (Previously had a strong-single carveout for strategic_review_disclosure or
    activist_ownership_pe_13d, but the rubric engine's below_triage_gate cap
    from D-014 discards any single-pattern signal unconditionally — the
    carveout produced 112 guaranteed-discard signals/month. Removed
    2026-04-21 to align scanner emissions with D-014.)
  - Post-edge disqualification: drop any CIK with DEFM14A / SC TO / SC 13E3 /
    425 in the last 30 days. Enforced in-scanner as defence-in-depth.
  - Weekly cadence, 45d PE-filer lookback, 60d review/streamlined lookback.

Deviations vs v1:
  - PE filer allowlist loaded from `pe_filer_allowlist` Supabase table (v1 read
    config/pe_filer_allowlist.json). Cached per-process.
  - Post-edge filter list cached in scanner-caches/takeover_candidate/
    post_edge_filter.json with a timestamp; refreshed weekly. Reduces
    submissions-API calls from per-hit to per-unique-CIK.
  - Dedup + candidate state persisted via SupabaseClient().read_cache /
    write_cache (scanner-caches/takeover_candidate/state.json).
  - EFTS rate limiter shared with edgar_filing_monitor (imported, not
    re-instantiated) so the 8 req/sec ceiling covers both scanners.
  - Emits Signal objects via scanner_base contract; scoring happens in
    run_scanner (not here). OUT_FILE side-channel removed.
  - strength_estimate: 4 for strong single pattern, 5 for ≥3 merged patterns,
    3 otherwise (2-pattern merged case).

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
    - raises MissingAuthError if SEC_USER_AGENT env unset.
    - Uses cfg.timeout_soft_s (default 90s) as wall-clock budget.
    - Emits partial status if budget exhausted mid-scan.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from modal_workers.scanners.edgar_filing_monitor import (
    SUBMISSIONS_URL,
    _efts_search,
    _rate_limiter,
)
from modal_workers.shared.scanner_base import MissingAuthError, ScannerResult, Signal
from modal_workers.shared.supabase_client import (
    EntityHints,
    ScannerConfig,
    SupabaseClient,
    SupabaseError,
)

NAME = "takeover_candidate_scanner"

REQUEST_TIMEOUT = 10
DEFAULT_WALL_CLOCK_S = 90

FILER_LOOKBACK_DAYS = 45
REVIEW_LOOKBACK_DAYS = 60
POST_EDGE_LOOKBACK_DAYS = 30
POST_EDGE_CACHE_TTL_DAYS = 7  # weekly refresh (matches scanner cadence)

POST_EDGE_FORMS = {
    "DEFM14A", "DEFM14A/A",
    "SC TO-T", "SC TO-T/A",
    "SC TO-I", "SC TO-I/A",
    "SC 13E3", "SC 13E3/A",
    "425",
}

STRATEGIC_REVIEW_KEYWORDS = [
    '"strategic alternatives"',
    '"exploring strategic alternatives"',
    '"review of strategic alternatives"',
    '"financial advisor" AND "Board"',
    '"retained as financial advisor"',
    '"engaged as financial advisor"',
]

STREAMLINED_KEYWORDS = [
    '"divested" AND "non-core"',
    '"portfolio simplification"',
    '"appointed Chief Financial Officer"',
]

REVIEW_FORMS = "8-K,10-K,10-Q,DEF 14A"
PE_FILING_FORMS = "SC 13G,SC 13G/A,SC 13D,SC 13D/A"

# Signal-type → pattern name used in the merge/gate.
_PATTERN_NAMES = {
    "institutional_pe_13g": "institutional_pe_13g",
    "activist_ownership_pe_13d": "activist_ownership_pe_13d",
    "strategic_review_disclosure": "strategic_review_disclosure",
    "streamlined_for_sale": "streamlined_for_sale",
}

_PATTERN_PRIORITY = {
    "strategic_review_disclosure": 4,
    "activist_ownership_pe_13d": 3,
    "institutional_pe_13g": 2,
    "streamlined_for_sale": 1,
}


# ---------------------------------------------------------------------------
# PE filer allowlist (Supabase table; cached per-process)
# ---------------------------------------------------------------------------

_allowlist_cache: Optional[Dict[str, Dict[str, Any]]] = None


def _load_pe_allowlist(client: SupabaseClient) -> Dict[str, Dict[str, Any]]:
    """Return {normalized_filer_name: row} from `pe_filer_allowlist` table.

    v2 spec.md §3.1 — each row has filer_name + optional cik + type. Cached
    per-process (the allowlist changes infrequently, reloaded on cold start).
    """
    global _allowlist_cache
    if _allowlist_cache is not None:
        return _allowlist_cache
    try:
        rows = client._rest("GET", "pe_filer_allowlist", params={"select": "*"})
    except SupabaseError:
        _allowlist_cache = {}
        return _allowlist_cache
    merged: Dict[str, Dict[str, Any]] = {}
    for r in rows or []:
        name = (r.get("filer_name") or "").strip().lower()
        if name:
            merged[name] = r
    _allowlist_cache = merged
    return merged


def _normalize_filer_name(raw: str) -> str:
    s = re.sub(r"\(CIK\s+\d+\)", "", raw, flags=re.IGNORECASE)
    s = re.sub(r"[,\./]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _match_pe_filer(filer_raw: str,
                    allowlist: Dict[str, Dict[str, Any]]
                    ) -> Optional[Tuple[str, Dict[str, Any]]]:
    norm = _normalize_filer_name(filer_raw)
    if not norm:
        return None
    if norm in allowlist:
        return norm, allowlist[norm]
    for key, info in allowlist.items():
        if key and key in norm:
            return key, info
    return None


# ---------------------------------------------------------------------------
# EFTS 13G/D fetch — uses the shared edgar _efts_search, adapts to the
# two-CIK issuer/filer convention for SC 13G/D filings.
# ---------------------------------------------------------------------------

def _efts_pe_filings(date_from: str, date_to: str, max_results: int,
                     *, user_agent: str) -> List[Dict[str, Any]]:
    """Fetch 13G/D filings and extract subject-vs-filer CIKs.

    edgar_filing_monitor._efts_search returns the FIRST CIK only. For 13G/D
    we need both CIKs (subject=ciks[0], filer=ciks[1]). We re-query the raw
    EFTS endpoint directly here to preserve that distinction. Rate limiter
    is the shared module-level instance.
    """
    params = {
        "q": "the",  # EFTS requires non-empty query; form filter does the work
        "dateRange": "custom",
        "startdt": date_from,
        "enddt": date_to,
        "forms": PE_FILING_FORMS,
    }
    _rate_limiter.wait()
    try:
        resp = requests.get(
            "https://efts.sec.gov/LATEST/search-index",
            params=params,
            headers={"User-Agent": user_agent},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException:
        return []

    out: List[Dict[str, Any]] = []
    for hit in data.get("hits", {}).get("hits", [])[:max_results]:
        src = hit.get("_source", {})
        ciks = src.get("ciks", []) or []
        names = src.get("display_names", []) or []
        adsh = src.get("adsh", "")
        subject_cik = ciks[0] if ciks else ""
        filer_cik = ciks[1] if len(ciks) > 1 else ""
        subject_name_raw = names[0] if names else ""
        filer_name_raw = names[1] if len(names) > 1 else ""
        filing_url = ""
        if subject_cik and adsh:
            cs = subject_cik.lstrip("0") or "0"
            ac = adsh.replace("-", "")
            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cs}/{ac}"
        out.append({
            "subject_cik": subject_cik,
            "subject_name": re.sub(r"\s*\(CIK\s+\d+\)\s*$", "", subject_name_raw).strip(),
            "filer_cik": filer_cik,
            "filer_name_raw": filer_name_raw,
            "all_names": names,
            "adsh": adsh,
            "form": src.get("form", ""),
            "file_date": src.get("file_date", ""),
            "file_description": src.get("file_description", ""),
            "filing_url": filing_url,
            "sics": src.get("sics", []) or [],
        })
    return out


# ---------------------------------------------------------------------------
# Post-edge disqualification (cached weekly)
# ---------------------------------------------------------------------------

def _load_post_edge_filter(client: SupabaseClient) -> Dict[str, Any]:
    raw = client.read_cache("takeover_candidate", "post_edge_filter.json")
    if raw is None:
        return {"ts": None, "ciks": {}}
    try:
        data = json.loads(raw)
        return {"ts": data.get("ts"), "ciks": data.get("ciks", {})}
    except (ValueError, UnicodeDecodeError):
        return {"ts": None, "ciks": {}}


def _save_post_edge_filter(client: SupabaseClient, state: Dict[str, Any]) -> None:
    try:
        client.write_cache(
            "takeover_candidate", "post_edge_filter.json",
            json.dumps(state).encode("utf-8"),
            content_type="application/json",
        )
    except SupabaseError:
        pass  # best effort


def _is_post_edge_cache_fresh(ts: Optional[str]) -> bool:
    if not ts:
        return False
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - when).days < POST_EDGE_CACHE_TTL_DAYS


def _check_post_edge(cik: str, *, user_agent: str) -> Tuple[bool, Optional[str]]:
    """Live check of data.sec.gov submissions for recent post-edge forms."""
    if not cik:
        return False, None
    cik_padded = cik.zfill(10)
    url = SUBMISSIONS_URL.format(cik=cik_padded)
    _rate_limiter.wait()
    try:
        resp = requests.get(url, headers={"User-Agent": user_agent},
                            timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return False, None
        data = resp.json()
        recent = data.get("filings", {}).get("recent", {}) or {}
        forms = recent.get("form", []) or []
        dates = recent.get("filingDate", []) or []
        cutoff = (datetime.now(timezone.utc).date()
                  - timedelta(days=POST_EDGE_LOOKBACK_DAYS)).isoformat()
        for f, d in zip(forms, dates):
            if d >= cutoff and f in POST_EDGE_FORMS:
                return True, f
    except Exception:
        pass
    return False, None


def _get_ticker_exchange(cik: str, *, user_agent: str
                         ) -> Tuple[Optional[str], Optional[str]]:
    if not cik:
        return None, None
    cik_padded = cik.zfill(10)
    url = SUBMISSIONS_URL.format(cik=cik_padded)
    _rate_limiter.wait()
    try:
        resp = requests.get(url, headers={"User-Agent": user_agent},
                            timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            tickers = data.get("tickers", []) or []
            exchanges = data.get("exchanges", []) or []
            return (tickers[0] if tickers else None,
                    exchanges[0] if exchanges else None)
    except Exception:
        pass
    return None, None


# ---------------------------------------------------------------------------
# Raw-candidate container — one hit worth of evidence before merge.
# ---------------------------------------------------------------------------

def _content_hash(cik: str, pattern_type: str, key: str) -> str:
    h = hashlib.sha256(f"{cik}|{pattern_type}|{key}".encode()).hexdigest()
    return f"sha256:{h}"


def _signal_id(pattern_type: str, cik: str, key: str) -> str:
    return (
        "takeover_"
        + hashlib.sha256(f"{pattern_type}|{cik}|{key}".encode()).hexdigest()[:24]
    )


def _iso_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        raise MissingAuthError(
            "SEC_USER_AGENT env var missing — SEC requires a valid contact email "
            "in the User-Agent header. Set via Modal secret `scanner-secrets`.")

    client = SupabaseClient()

    from modal_workers.shared.openfigi_resolver import set_cache_backend
    set_cache_backend(*client.openfigi_cache_backend())

    allowlist = _load_pe_allowlist(client)
    warnings: List[str] = []
    if not allowlist:
        warnings.append("pe_filer_allowlist empty or unreadable — PE pattern skipped")

    post_edge_state = _load_post_edge_filter(client)
    post_edge_map: Dict[str, str] = dict(post_edge_state.get("ciks") or {})
    post_edge_fresh = _is_post_edge_cache_fresh(post_edge_state.get("ts"))

    budget_s = max(10, (cfg.timeout_soft_s or DEFAULT_WALL_CLOCK_S) - 5)
    scan_start = time.time()
    scan_date = datetime.now(timezone.utc)
    date_from_45 = (scan_date - timedelta(days=FILER_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_from_60 = (scan_date - timedelta(days=REVIEW_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to = scan_date.strftime("%Y-%m-%d")

    hits_processed = 0
    budget_exhausted = False

    # ---- post-edge filter helper (live call + cache) ---------------------
    def is_post_edge(cik: str) -> bool:
        if not cik:
            return False
        # Fresh cache: trust it.
        if post_edge_fresh and cik in post_edge_map:
            return bool(post_edge_map[cik])
        is_post, form_found = _check_post_edge(cik, user_agent=user_agent)
        post_edge_map[cik] = form_found or "" if is_post else ""
        return is_post

    # Raw candidates keyed by (cik, pattern_type, evidence_key) → one per filing hit.
    # Each entry is a tuple (signal_type, hit_dict, extra_meta).
    raw_candidates: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []

    # -------- Pattern A+B: PE-filer 13G/D (institutional + activist) ------
    if allowlist:
        try:
            pe_hits = _efts_pe_filings(
                date_from=date_from_45, date_to=date_to,
                max_results=200, user_agent=user_agent,
            )
            hits_processed += len(pe_hits)
            for hit in pe_hits:
                if time.time() - scan_start > budget_s * 0.4:
                    budget_exhausted = True
                    warnings.append("pe_13gd: soft budget reached")
                    break
                candidate_names = [hit.get("filer_name_raw", "")]
                for n in hit.get("all_names", [])[:3]:
                    candidate_names.append(n)
                matched = None
                for cname in candidate_names:
                    if not cname:
                        continue
                    m = _match_pe_filer(cname, allowlist)
                    if m:
                        matched = m
                        break
                if not matched:
                    continue
                subject_cik = hit["subject_cik"]
                if not subject_cik:
                    continue
                if is_post_edge(subject_cik):
                    continue
                form = hit["form"] or ""
                sigtype = ("activist_ownership_pe_13d" if "13D" in form
                           else "institutional_pe_13g")
                raw_candidates.append((sigtype, hit, {
                    "pe_match_name": matched[0],
                    "pe_match_info": matched[1],
                }))
        except Exception as e:  # noqa: BLE001
            warnings.append(f"pe_13gd: {type(e).__name__}: {e}")

    # -------- Pattern C: Strategic-review language -------------------------
    if not budget_exhausted:
        review_seen: set[Tuple[str, str]] = set()
        for kw in STRATEGIC_REVIEW_KEYWORDS:
            if time.time() - scan_start > budget_s * 0.75:
                budget_exhausted = True
                warnings.append("strategic_review: soft budget reached")
                break
            try:
                hits = _efts_search(
                    query=kw,
                    date_from=date_from_60, date_to=date_to,
                    form_type=REVIEW_FORMS, max_results=50,
                    user_agent=user_agent,
                )
                hits_processed += len(hits)
                for hit in hits:
                    key = (hit.get("cik", ""), hit.get("adsh", ""))
                    if key in review_seen:
                        continue
                    review_seen.add(key)
                    subject_cik = hit.get("cik", "")
                    if subject_cik and is_post_edge(subject_cik):
                        continue
                    # Normalise to the pe_filings shape for unified downstream use.
                    shaped = {
                        "subject_cik": subject_cik,
                        "subject_name": hit.get("company_name", ""),
                        "adsh": hit.get("adsh", ""),
                        "form": hit.get("form", ""),
                        "file_date": hit.get("file_date", ""),
                        "file_description": hit.get("file_description", ""),
                        "filing_url": hit.get("filing_url", ""),
                        "sics": hit.get("sics", []),
                    }
                    raw_candidates.append(
                        ("strategic_review_disclosure", shaped, {"keyword": kw})
                    )
            except Exception as e:  # noqa: BLE001
                warnings.append(f"stratrev[{kw}]: {type(e).__name__}: {e}")

    # -------- Pattern D: Streamlined-for-sale (8-K only, primary kw) -------
    if not budget_exhausted:
        stream_seen: set[Tuple[str, str]] = set()
        for kw in STREAMLINED_KEYWORDS[:1]:
            if time.time() - scan_start > budget_s * 0.9:
                budget_exhausted = True
                warnings.append("streamlined: soft budget reached")
                break
            try:
                hits = _efts_search(
                    query=kw,
                    date_from=date_from_60, date_to=date_to,
                    form_type="8-K", max_results=50,
                    user_agent=user_agent,
                )
                hits_processed += len(hits)
                for hit in hits:
                    key = (hit.get("cik", ""), hit.get("adsh", ""))
                    if key in stream_seen:
                        continue
                    stream_seen.add(key)
                    subject_cik = hit.get("cik", "")
                    if subject_cik and is_post_edge(subject_cik):
                        continue
                    shaped = {
                        "subject_cik": subject_cik,
                        "subject_name": hit.get("company_name", ""),
                        "adsh": hit.get("adsh", ""),
                        "form": hit.get("form", ""),
                        "file_date": hit.get("file_date", ""),
                        "file_description": hit.get("file_description", ""),
                        "filing_url": hit.get("filing_url", ""),
                        "sics": hit.get("sics", []),
                    }
                    raw_candidates.append(
                        ("streamlined_for_sale", shaped, {"keyword": kw})
                    )
            except Exception as e:  # noqa: BLE001
                warnings.append(f"streamlined[{kw}]: {type(e).__name__}: {e}")

    # ---- Save refreshed post-edge cache (always — prune nothing; entries
    # accumulate across weekly runs but are consulted only until TTL expires).
    post_edge_state_new = {
        "ts": scan_date.isoformat().replace("+00:00", "Z"),
        "ciks": post_edge_map,
    }
    _save_post_edge_filter(client, post_edge_state_new)

    # -------- Merge by issuer CIK ------------------------------------------
    by_cik: Dict[str, List[Tuple[str, Dict[str, Any], Dict[str, Any]]]] = {}
    for item in raw_candidates:
        sigtype, hit, _ = item
        cik = hit.get("subject_cik") or ""
        if not cik:
            continue
        by_cik.setdefault(cik, []).append(item)

    # Resolve ticker/exchange once per CIK (shared across the merged signal).
    ticker_cache: Dict[str, Tuple[Optional[str], Optional[str]]] = {}

    def resolve_ticker_cached(cik: str):
        if cik not in ticker_cache:
            ticker_cache[cik] = _get_ticker_exchange(cik, user_agent=user_agent)
        return ticker_cache[cik]

    signals: List[Signal] = []
    for cik, items in by_cik.items():
        # Sort by priority — primary evidence drives the emitted signal_type.
        items.sort(key=lambda it: -_PATTERN_PRIORITY.get(it[0], 0))
        pattern_set = sorted({it[0] for it in items})
        patterns_hit = len(pattern_set)
        primary_type = items[0][0]
        primary_hit = items[0][1]
        primary_extra = items[0][2]

        # Triage gate: ≥2 patterns required. The rubric engine's
        # below_triage_gate cap (D-014) discards patterns_hit<2 unconditionally,
        # so emitting single-pattern signals is pure write-amplification — all
        # 112/month would land in signals, auto-cap to discard, never clear.
        # `strong_single` is still computed below for the strength_estimate
        # branch (2-pattern signals with a strong primary → strength=4).
        strong_single = primary_type in (
            "strategic_review_disclosure",
            "activist_ownership_pe_13d",
        )
        if patterns_hit < 2:
            continue

        adsh = primary_hit.get("adsh", "")
        key_for_hash = (adsh if primary_type in (
            "institutional_pe_13g", "activist_ownership_pe_13d")
            else primary_extra.get("keyword") or adsh)
        src_content_hash = _content_hash(cik, primary_type, key_for_hash or adsh)
        sig_id = _signal_id(primary_type, cik, key_for_hash or adsh)

        source_date_str = primary_hit.get("file_date", "")
        try:
            source_date = datetime.strptime(source_date_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
        except ValueError:
            source_date = scan_date

        ticker, exchange = resolve_ticker_cached(cik)
        issuer_figi: Optional[str] = None
        if ticker:
            try:
                from modal_workers.shared.openfigi_resolver import resolve_ticker
                res = resolve_ticker(ticker, exch_code="US")
                if res.resolved:
                    issuer_figi = res.issuer_figi
            except Exception:
                pass

        # strength_estimate: 5 multi-pattern(≥3), 4 strong single or 2-pattern
        # with strong driver, 3 otherwise.
        if patterns_hit >= 3:
            strength = 5
        elif strong_single:
            strength = 4
        else:
            strength = 3

        contributing = [{
            "signal_type": it[0],
            "form": it[1].get("form"),
            "adsh": it[1].get("adsh"),
            "file_date": it[1].get("file_date"),
            "filing_url": it[1].get("filing_url"),
            "meta": it[2],
        } for it in items]

        pe_info = primary_extra.get("pe_match_info") or {}
        raw_payload: Dict[str, Any] = {
            "cik": cik,
            "subject_name": primary_hit.get("subject_name", ""),
            "primary_pattern": primary_type,
            "patterns_hit": patterns_hit,
            "pattern_names": pattern_set,
            "contributing_filings": contributing,
            "primary_filing": {
                "form": primary_hit.get("form"),
                "adsh": primary_hit.get("adsh"),
                "file_date": primary_hit.get("file_date"),
                "file_description": primary_hit.get("file_description"),
                "filing_url": primary_hit.get("filing_url"),
                "sics": primary_hit.get("sics"),
            },
            "tickers": [ticker] if ticker else [],
            "exchange": exchange,
            "pe_filer_name": primary_extra.get("pe_match_name"),
            "pe_filer_type": pe_info.get("type"),
            "pe_filer_cik": pe_info.get("cik"),
            "keyword": primary_extra.get("keyword"),
            # Auto-cap inputs (downstream rubric expects these flags).
            "definitive_merger_agreement": False,
            "rejected_prior_offer_6mo": False,
            "going_concern_warning": False,
        }

        entity_hints = EntityHints(
            issuer_figi=issuer_figi,
            ticker=ticker,
            mic=None,
            cik=cik or None,
            name=primary_hit.get("subject_name") or None,
            country="US",
        )

        signals.append(Signal(
            signal_id=sig_id,
            source_content_hash=src_content_hash,
            source_date=source_date,
            scan_date=scan_date,
            signal_type=primary_type,
            raw_payload=raw_payload,
            source_url=primary_hit.get("filing_url") or None,
            issuer_figi=issuer_figi,
            entity_hints=entity_hints,
            thesis_direction="long",
            strength_estimate=strength,
        ))

    status = "partial" if (budget_exhausted or warnings) else "ok"
    if not signals and warnings and not allowlist:
        # Allowlist empty + no signals = hard error.
        status = "error"

    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=signals,
        warnings=warnings,
        fetched_records=hits_processed,
    )
