"""
Takeover Candidate Scanner — pre-edge M&A target setup detector.

New scanner (2026-04-20). Addresses the AVNS miss (72% premium take-private on
2026-04-14 that this system saw none of pre-announcement).

Data sources (free, no auth beyond SEC User-Agent):
  - SEC EDGAR EFTS full-text search: https://efts.sec.gov/LATEST/search-index
      * 13G / 13D / 13D/A filings from the curated PE-filer allowlist
      * 8-K / 10-K / 10-Q / DEF 14A containing strategic-review language
      * DEFM14A filings (filtered OUT — already post-edge)
  - SEC submissions API: https://data.sec.gov/submissions/CIK{cik}.json
      (ticker + exchange resolution, already used by edgar_filing_monitor)

Design principles (per D-013):
  - The scanner's job is to surface *un-announced* M&A setups — candidates
    currently showing ≥2 of 5 setup patterns before any deal is signed.
  - Candidates where a definitive agreement has already been filed are
    automatically disqualified (post-edge). This is enforced both here
    (filter-out at fetch) AND in run_post_scan.py auto-caps as defence in depth.
  - Output conforms to the unified signal schema so run_post_scan, convergence,
    and the reporting layer require no changes beyond the WEIGHTS entry
    already added for profile `takeover_candidate`.

Emits into signals/takeover_candidate_scanner_output.json. Default profile:
takeover_candidate. Pairs with edgar_filing_monitor (same-direction
convergence on issuer CIK → boosts conviction when both lanes fire).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

sys.path.insert(0, str(Path(__file__).parent))
try:
    from http_client import HttpClient  # type: ignore
except Exception:
    HttpClient = None

NAME = "takeover_candidate_scanner"
REPO = Path(__file__).parent.parent
OUT_FILE = REPO / "signals" / f"{NAME}_output.json"
OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = REPO / "config" / "pe_filer_allowlist.json"

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

USER_AGENT = "InvestmentResearch research@example.com"
SEC_RATE_LIMIT = 8  # req/sec (SEC allows 10; stay conservative)
REQUEST_TIMEOUT = 10
WALL_CLOCK_BUDGET_S = 90  # weekly cadence, larger than daily scanners

# Lookback windows
FILER_LOOKBACK_DAYS = 45    # fresh 13G/D filings from PE allowlist
REVIEW_LOOKBACK_DAYS = 60   # fresh strategic-review language in filings
DEDUP_WINDOW_DAYS = 30

# Pre-edge disqualifier forms — if the target has a DEFM14A or SC TO-T in
# the last 30 days, skip (deal is already public; post-edge per D-013).
POST_EDGE_FORMS = {
    "DEFM14A", "DEFM14A/A",  # definitive merger proxy
    "SC TO-T", "SC TO-T/A",  # third-party tender offer
    "SC TO-I", "SC TO-I/A",  # issuer tender (going-private)
    "SC 13E3", "SC 13E3/A",  # going-private transaction statement
    "425",                    # merger/acquisition communications
}

# Strategic-review keyword phrases (high-precision)
STRATEGIC_REVIEW_KEYWORDS = [
    '"strategic alternatives"',
    '"exploring strategic alternatives"',
    '"review of strategic alternatives"',
    '"financial advisor" AND "Board"',
    '"retained as financial advisor"',
    '"engaged as financial advisor"',
]

# Form types that carry strategic-review language (per CATEGORY_FORM_WHITELIST
# from edgar_filing_monitor — only 8-K and 10-K/10-Q are accepted here).
REVIEW_FORMS = "8-K,10-K,10-Q"

# Shareholder-activist filing forms that we fetch from the PE allowlist.
PE_FILING_FORMS = "SC 13G,SC 13G/A,SC 13D,SC 13D/A"

# Streamlined-for-sale signals (secondary — fetched via keyword on 8-Ks)
STREAMLINED_KEYWORDS = [
    '"divested" AND "non-core"',
    '"portfolio simplification"',
    '"appointed Chief Financial Officer"',  # recent CFO hire
]

logger = logging.getLogger(NAME)


# --------------------------------------------------------------------
# Rate limiter
# --------------------------------------------------------------------

class _SECRateLimiter:
    def __init__(self, max_per_sec: int = SEC_RATE_LIMIT):
        self.max_per_sec = max_per_sec
        self._timestamps: List[float] = []

    def wait(self):
        now = time.time()
        self._timestamps = [t for t in self._timestamps if now - t < 1.0]
        if len(self._timestamps) >= self.max_per_sec:
            sleep_time = 1.0 - (now - self._timestamps[0]) + 0.05
            if sleep_time > 0:
                time.sleep(sleep_time)
        self._timestamps.append(time.time())


_rate_limiter = _SECRateLimiter()


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

def _iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _date_range(days_back: int) -> Tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days_back)
    return start.isoformat(), today.isoformat()


def _sig_id(kind: str, cik: str, adsh: str) -> str:
    key = f"{kind}:{cik}:{adsh}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _content_hash(cik: str, form: str, file_date: str) -> str:
    return hashlib.sha256(f"{cik}|{form}|{file_date}".encode()).hexdigest()[:16]


def _load_pe_allowlist() -> Dict[str, Dict[str, Any]]:
    """Return merged {normalized_name: {cik, type}} from pe_filer_allowlist.json."""
    if not CONFIG_FILE.exists():
        logger.warning(f"PE allowlist not found at {CONFIG_FILE}")
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except Exception as e:
        logger.error(f"Failed to parse PE allowlist: {e}")
        return {}
    merged: Dict[str, Dict[str, Any]] = {}
    for name, info in (data.get("filers") or {}).items():
        merged[name.lower()] = info
    for name, info in ((data.get("activist_crossover") or {}).get("filers") or {}).items():
        merged[name.lower()] = info
    return merged


def _normalize_filer_name(raw: str) -> str:
    """Strip CIK, punctuation, case-normalize for matching."""
    s = re.sub(r"\(CIK\s+\d+\)", "", raw, flags=re.IGNORECASE)
    s = re.sub(r"[,\./]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _match_pe_filer(filer_raw: str, allowlist: Dict[str, Dict[str, Any]]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Substring match of filer against allowlist. Returns (canonical_name, info) or None."""
    norm = _normalize_filer_name(filer_raw)
    if not norm:
        return None
    # Exact match first
    if norm in allowlist:
        return norm, allowlist[norm]
    # Substring match: allowlist key ⊂ normalized filer name
    for key, info in allowlist.items():
        if key in norm:
            return key, info
    return None


# --------------------------------------------------------------------
# EFTS query
# --------------------------------------------------------------------

def _efts_search(query: str, date_from: str, date_to: str,
                 forms: str = "", max_results: int = 50) -> List[dict]:
    params = {
        "q": query,
        "dateRange": "custom",
        "startdt": date_from,
        "enddt": date_to,
    }
    if forms:
        params["forms"] = forms
    headers = {"User-Agent": USER_AGENT}
    _rate_limiter.wait()
    try:
        resp = requests.get(EFTS_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"EFTS query failed for '{query}': {e}")
        return []
    out = []
    for hit in data.get("hits", {}).get("hits", [])[:max_results]:
        src = hit.get("_source", {})
        ciks = src.get("ciks", []) or []
        display_names = src.get("display_names", []) or []
        adsh = src.get("adsh", "")
        # For 13G/D, there are typically TWO CIKs: [subject, filer].
        # We want the subject (target company) — conventionally the first one.
        subject_cik = ciks[0] if ciks else ""
        filer_cik = ciks[1] if len(ciks) > 1 else ""
        subject_name = display_names[0] if display_names else ""
        filer_name = display_names[1] if len(display_names) > 1 else ""
        filing_url = ""
        if subject_cik and adsh:
            cik_stripped = subject_cik.lstrip("0") or "0"
            adsh_clean = adsh.replace("-", "")
            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{adsh_clean}"
        out.append({
            "subject_cik": subject_cik,
            "subject_name_raw": subject_name,
            "subject_name": re.sub(r"\s*\(CIK\s+\d+\)\s*$", "", subject_name).strip(),
            "filer_cik": filer_cik,
            "filer_name_raw": filer_name,
            "adsh": adsh,
            "form": src.get("form", ""),
            "file_date": src.get("file_date", ""),
            "file_description": src.get("file_description", ""),
            "filing_url": filing_url,
            "sics": src.get("sics", []) or [],
            "all_ciks": ciks,
            "all_names": display_names,
        })
    return out


def _get_ticker_exchange(cik: str) -> Tuple[Optional[str], Optional[str]]:
    if not cik:
        return None, None
    cik_padded = cik.zfill(10)
    url = SUBMISSIONS_URL.format(cik=cik_padded)
    _rate_limiter.wait()
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            tickers = data.get("tickers", []) or []
            exchanges = data.get("exchanges", []) or []
            return (tickers[0] if tickers else None,
                    exchanges[0] if exchanges else None)
    except Exception as e:
        logger.debug(f"submissions lookup failed for CIK {cik}: {e}")
    return None, None


def _target_has_post_edge_recent(cik: str) -> Tuple[bool, Optional[str]]:
    """Does the target have a DEFM14A / SC TO-T / SC 13E3 / 425 in the last 30 days?
    If so, it's already post-edge — skip.
    Returns (is_post_edge, form_found).
    """
    if not cik:
        return False, None
    # Use the submissions JSON (cheaper than EFTS) — it lists recent forms.
    cik_padded = cik.zfill(10)
    url = SUBMISSIONS_URL.format(cik=cik_padded)
    _rate_limiter.wait()
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return False, None
        data = resp.json()
        recent = data.get("filings", {}).get("recent", {}) or {}
        forms = recent.get("form", []) or []
        dates = recent.get("filingDate", []) or []
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=30)).isoformat()
        for f, d in zip(forms, dates):
            if d >= cutoff and f in POST_EDGE_FORMS:
                return True, f
    except Exception as e:
        logger.debug(f"post-edge check failed for CIK {cik}: {e}")
    return False, None


# --------------------------------------------------------------------
# Signal builders
# --------------------------------------------------------------------

def _build_13g_13d_signal(hit: Dict[str, Any], pe_match: Tuple[str, Dict[str, Any]]) -> Dict[str, Any]:
    pe_name, pe_info = pe_match
    subject_cik = hit["subject_cik"]
    ticker, exchange = _get_ticker_exchange(subject_cik)

    form = hit["form"] or ""
    # 13D = active stake (stronger signal); 13G = passive
    signal_type = "activist_ownership_pe_13d" if "13D" in form else "institutional_pe_13g"

    sid = _sig_id("13gd", subject_cik, hit["adsh"])
    chash = _content_hash(subject_cik, form, hit["file_date"])

    headline = f"{pe_name.title()} files {form}: {hit['subject_name'] or 'Unknown issuer'}"
    summary = (
        f"PE-flagged filer '{pe_name}' ({pe_info.get('type','unknown')}) filed "
        f"{form} on {hit['file_date']} for issuer CIK {subject_cik}"
        f"{' ('+ticker+')' if ticker else ''}. Pattern: institutional/insider accumulation."
    )

    return {
        "signal_id": sid,
        "source_content_hash": chash,
        "scanner_source": NAME,
        "upstream_scanner": NAME,
        "scoring_profile": "takeover_candidate",
        "signal_type": signal_type,
        "thesis_direction": "long",
        "ticker": ticker,
        "figi": None,
        "issuer_figi": None,
        "company_name_en": hit["subject_name"] or "",
        "cik": subject_cik,
        "exchange": exchange,
        "scan_date": _iso(),
        "source_date": hit["file_date"] or _iso(),
        "headline": headline,
        "summary": summary,
        "raw_data": {
            "form": form,
            "adsh": hit["adsh"],
            "filing_url": hit["filing_url"],
            "pe_filer_name": pe_name,
            "pe_filer_type": pe_info.get("type"),
            "pe_filer_cik": pe_info.get("cik"),
            "subject_cik": subject_cik,
            "sics": hit["sics"],
            "patterns_hit": 1,  # This single signal = 1 pattern (institutional accumulation)
            "pattern_names": ["insider_institutional_accumulation"],
            # Auto-cap inputs (downstream):
            "definitive_merger_agreement": False,   # caller's responsibility to verify
            "rejected_prior_offer_6mo": False,
            "going_concern_warning": False,
        },
    }


def _build_strategic_review_signal(hit: Dict[str, Any]) -> Dict[str, Any]:
    subject_cik = hit["subject_cik"]
    ticker, exchange = _get_ticker_exchange(subject_cik)

    sid = _sig_id("stratrev", subject_cik, hit["adsh"])
    chash = _content_hash(subject_cik, hit["form"], hit["file_date"])

    headline = f"Strategic-review language in {hit['form']}: {hit['subject_name']}"
    summary = (
        f"Filing contains explicit strategic-review / financial-advisor language. "
        f"{hit['form']} filed {hit['file_date']} by {hit['subject_name']}"
        f"{' ('+ticker+')' if ticker else ''}. High-signal pre-edge disclosure."
    )

    return {
        "signal_id": sid,
        "source_content_hash": chash,
        "scanner_source": NAME,
        "upstream_scanner": NAME,
        "scoring_profile": "takeover_candidate",
        "signal_type": "strategic_review_disclosure",
        "thesis_direction": "long",
        "ticker": ticker,
        "figi": None,
        "issuer_figi": None,
        "company_name_en": hit["subject_name"] or "",
        "cik": subject_cik,
        "exchange": exchange,
        "scan_date": _iso(),
        "source_date": hit["file_date"] or _iso(),
        "headline": headline,
        "summary": summary,
        "raw_data": {
            "form": hit["form"],
            "adsh": hit["adsh"],
            "filing_url": hit["filing_url"],
            "subject_cik": subject_cik,
            "sics": hit["sics"],
            "patterns_hit": 1,
            "pattern_names": ["strategic_review_disclosure"],
            "definitive_merger_agreement": False,
            "rejected_prior_offer_6mo": False,
            "going_concern_warning": False,
        },
    }


def _build_streamlined_signal(hit: Dict[str, Any], keyword_hit: str) -> Dict[str, Any]:
    subject_cik = hit["subject_cik"]
    ticker, exchange = _get_ticker_exchange(subject_cik)

    sid = _sig_id("stream", subject_cik, hit["adsh"])
    chash = _content_hash(subject_cik, hit["form"], hit["file_date"])

    headline = f"Streamlined-for-sale pattern: {hit['subject_name']}"
    summary = (
        f"{hit['form']} ({hit['file_date']}) matches streamlined-for-sale keyword "
        f"'{keyword_hit}'. Common setup preceding PE take-private."
    )

    return {
        "signal_id": sid,
        "source_content_hash": chash,
        "scanner_source": NAME,
        "upstream_scanner": NAME,
        "scoring_profile": "takeover_candidate",
        "signal_type": "streamlined_for_sale",
        "thesis_direction": "long",
        "ticker": ticker,
        "figi": None,
        "issuer_figi": None,
        "company_name_en": hit["subject_name"] or "",
        "cik": subject_cik,
        "exchange": exchange,
        "scan_date": _iso(),
        "source_date": hit["file_date"] or _iso(),
        "headline": headline,
        "summary": summary,
        "raw_data": {
            "form": hit["form"],
            "adsh": hit["adsh"],
            "filing_url": hit["filing_url"],
            "subject_cik": subject_cik,
            "sics": hit["sics"],
            "keyword": keyword_hit,
            "patterns_hit": 1,
            "pattern_names": ["streamlined_for_sale"],
            "definitive_merger_agreement": False,
            "rejected_prior_offer_6mo": False,
            "going_concern_warning": False,
        },
    }


# --------------------------------------------------------------------
# Signal merging (multiple patterns on same issuer → boost patterns_hit)
# --------------------------------------------------------------------

def _merge_by_issuer(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """When the same issuer CIK appears across multiple pattern types, merge
    them into a single signal with patterns_hit = count and pattern_names = union.

    The triage gate in run_post_scan requires patterns_hit >= 2 to score above
    discard, so this merging step is load-bearing.
    """
    by_cik: Dict[str, List[Dict[str, Any]]] = {}
    without_cik: List[Dict[str, Any]] = []
    for s in signals:
        cik = s.get("cik") or ""
        if not cik:
            without_cik.append(s)
            continue
        by_cik.setdefault(cik, []).append(s)

    merged: List[Dict[str, Any]] = []
    for cik, group in by_cik.items():
        if len(group) == 1:
            merged.append(group[0])
            continue
        # Multiple patterns on same issuer — merge into primary (highest signal)
        # Priority order: strategic_review > 13D > 13G > streamlined
        priority = {
            "strategic_review_disclosure": 4,
            "activist_ownership_pe_13d": 3,
            "institutional_pe_13g": 2,
            "streamlined_for_sale": 1,
        }
        group.sort(key=lambda g: -priority.get(g["signal_type"], 0))
        primary = dict(group[0])
        raw = dict(primary.get("raw_data") or {})
        names = set(raw.get("pattern_names", []))
        contributing: List[Dict[str, Any]] = []
        for g in group:
            names.update((g.get("raw_data") or {}).get("pattern_names", []))
            contributing.append({
                "signal_type": g["signal_type"],
                "form": (g.get("raw_data") or {}).get("form"),
                "adsh": (g.get("raw_data") or {}).get("adsh"),
                "file_date": g.get("source_date"),
            })
        raw["patterns_hit"] = len(names)
        raw["pattern_names"] = sorted(names)
        raw["contributing_filings"] = contributing
        primary["raw_data"] = raw
        primary["summary"] = (
            f"{primary['summary']} | Additional patterns on same issuer: "
            f"{', '.join(sorted(names))} ({len(names)}/5 of takeover setup)."
        )
        merged.append(primary)

    merged.extend(without_cik)
    return merged


# --------------------------------------------------------------------
# Main scan
# --------------------------------------------------------------------

def scan(force: bool = False, max_per_query: int = 50) -> Dict[str, Any]:
    started = time.time()
    allowlist = _load_pe_allowlist()
    if not allowlist:
        return {
            "scanner": NAME,
            "ran_at_utc": _iso(),
            "status": "error",
            "signals": [],
            "error": "pe_filer_allowlist.json missing or unreadable",
        }

    errors: List[str] = []
    raw_signals: List[Dict[str, Any]] = []
    fetch_stats: Dict[str, Any] = {}

    # --- Pattern A: PE-filer 13G/D filings --------------------------------
    # Rather than query by every filer name (40+ queries), we query by form
    # type across the window and then filter client-side by filer match.
    d_from, d_to = _date_range(FILER_LOOKBACK_DAYS)
    try:
        # EFTS requires a non-empty query term. Use a very common word to
        # capture all filings of the target forms across the window; actual
        # filter is by `forms` + client-side filer allowlist match.
        hits = _efts_search(
            query='the',
            date_from=d_from, date_to=d_to,
            forms=PE_FILING_FORMS, max_results=max_per_query * 4,
        )
        fetch_stats["pe_13gd_raw"] = len(hits)
        pe_matched = 0
        pe_post_edge_skipped = 0
        for hit in hits:
            # Match filer name (typically display_names[1])
            candidate_names = [hit.get("filer_name_raw", "")]
            # Some 13Gs have the filer as display_names[0]; check both
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
            # Skip if target has post-edge form already filed
            if hit["subject_cik"]:
                is_post, form_found = _target_has_post_edge_recent(hit["subject_cik"])
                if is_post:
                    pe_post_edge_skipped += 1
                    continue
            pe_matched += 1
            raw_signals.append(_build_13g_13d_signal(hit, matched))
            if time.time() - started > WALL_CLOCK_BUDGET_S * 0.4:
                break
        fetch_stats["pe_13gd_matched"] = pe_matched
        fetch_stats["pe_13gd_post_edge_skipped"] = pe_post_edge_skipped
    except Exception as e:
        errors.append(f"pe_13gd: {type(e).__name__}: {e}")

    # --- Pattern B: Strategic-review language -----------------------------
    d_from_r, d_to_r = _date_range(REVIEW_LOOKBACK_DAYS)
    review_hits_seen = set()
    stratrev_count = 0
    stratrev_post_edge_skipped = 0
    for kw in STRATEGIC_REVIEW_KEYWORDS:
        if time.time() - started > WALL_CLOCK_BUDGET_S * 0.75:
            errors.append("strategic_review: wall-clock budget exhausted")
            break
        try:
            hits = _efts_search(
                query=kw, date_from=d_from_r, date_to=d_to_r,
                forms=REVIEW_FORMS, max_results=max_per_query,
            )
            for hit in hits:
                key = (hit["subject_cik"], hit["adsh"])
                if key in review_hits_seen:
                    continue
                review_hits_seen.add(key)
                if hit["subject_cik"]:
                    is_post, _ = _target_has_post_edge_recent(hit["subject_cik"])
                    if is_post:
                        stratrev_post_edge_skipped += 1
                        continue
                raw_signals.append(_build_strategic_review_signal(hit))
                stratrev_count += 1
        except Exception as e:
            errors.append(f"stratrev[{kw}]: {type(e).__name__}: {e}")
    fetch_stats["strategic_review_found"] = stratrev_count
    fetch_stats["strategic_review_post_edge_skipped"] = stratrev_post_edge_skipped

    # --- Pattern C: Streamlined-for-sale language (lightweight — 1 kw for now) ---
    stream_count = 0
    for kw in STREAMLINED_KEYWORDS[:1]:  # only primary keyword to stay in budget
        if time.time() - started > WALL_CLOCK_BUDGET_S * 0.9:
            errors.append("streamlined: wall-clock budget exhausted")
            break
        try:
            hits = _efts_search(
                query=kw, date_from=d_from_r, date_to=d_to_r,
                forms="8-K", max_results=max_per_query,
            )
            for hit in hits:
                key = (hit["subject_cik"], hit["adsh"])
                if key in review_hits_seen:
                    continue
                review_hits_seen.add(key)
                if hit["subject_cik"]:
                    is_post, _ = _target_has_post_edge_recent(hit["subject_cik"])
                    if is_post:
                        continue
                raw_signals.append(_build_streamlined_signal(hit, kw))
                stream_count += 1
        except Exception as e:
            errors.append(f"streamlined[{kw}]: {type(e).__name__}: {e}")
    fetch_stats["streamlined_found"] = stream_count

    # --- Merge by issuer --------------------------------------------------
    merged = _merge_by_issuer(raw_signals)
    # Gate: require patterns_hit >= 2 or the single-pattern exceptions
    # (13D standalone is acceptable at pattern=1 with activist 13D flag per rubric).
    kept: List[Dict[str, Any]] = []
    gated_out = 0
    for s in merged:
        raw = s.get("raw_data") or {}
        patterns = raw.get("patterns_hit", 0)
        stype = s.get("signal_type", "")
        # Per profile rubric dim 1: a single pattern can pass only if unusually
        # strong — strategic_review or activist 13D.
        strong_single = stype in ("strategic_review_disclosure", "activist_ownership_pe_13d")
        if patterns >= 2 or strong_single:
            kept.append(s)
        else:
            gated_out += 1

    fetch_stats["merged_issuers"] = len(merged)
    fetch_stats["gated_out_below_2_patterns"] = gated_out

    status = "ok"
    if errors and not kept:
        status = "error"
    elif errors:
        status = "partial"

    return {
        "scanner": NAME,
        "ran_at_utc": _iso(),
        "status": status,
        "signals": kept,
        "fetched_items": fetch_stats,
        "unique_signals": len(kept),
        "errors": errors,
        "elapsed_s": round(time.time() - started, 2),
    }


def main():
    result = scan()
    tmp = OUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=2))
    os.replace(tmp, OUT_FILE)
    print(json.dumps({
        "signals": len(result["signals"]),
        "scanner": NAME,
        "status": result["status"],
        "fetched": result.get("fetched_items", {}),
        "elapsed_s": result.get("elapsed_s"),
        "errors": result.get("errors", [])[:3],
    }))


if __name__ == "__main__":
    main()

# --- END OF FILE ---
