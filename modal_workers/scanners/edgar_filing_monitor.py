"""
EDGAR filing monitor — Modal port of tools/edgar_filing_monitor.py.

Preservation (PRD §6 + spec.md §2):
  - SIGNAL_KEYWORDS, SIGNAL_FILING_TYPES, KEYWORD_SKIP_FORMS, CATEGORY_FORM_WHITELIST,
    SPAC_IPO_FORM_BLACKLIST, ROTATION_ORDER — byte-equivalent to v1.
  - Rate limiter (8 req/sec) — preserved.
  - EFTS search + data.sec.gov submissions lookup — preserved.
  - Strength heuristics in _compute_strength — ported from v1 _build_signal.
  - 45-day dedup on (cik, keyword, category) — preserved; log lives in
    scanner-caches/edgar/dedup.json instead of signals/edgar_dedup.json.
  - Rotation state (one category per 3h run) — preserved; state in
    scanner-caches/edgar/rotation.json.

Deferred vs v1:
  - Market cap filter (D-003 $215M floor) — not wired here; requires porting
    tools/mcap_cache.py. The Modal scanner emits all signals regardless of
    market cap; downstream auto-caps and the dashboard can gate visually until
    mcap_cache lands. Flagged for Phase 3 completion.
  - Filing type scan (scan_filing_types) — ported below; runs after keyword scan
    if wall-clock budget allows.

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
    - raises MissingAuthError if SEC_USER_AGENT env unset.
    - Uses cfg.timeout_soft_s (default 35s) as wall-clock budget for EFTS calls.
    - Returns up to ~60 signals per run (one rotation category + filing types).
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from modal_workers.shared.scanner_base import MissingAuthError, Signal, ScannerResult
from modal_workers.shared.supabase_client import EntityHints, ScannerConfig, SupabaseClient

# ---------------------------------------------------------------------------
# Constants (verbatim from v1)
# ---------------------------------------------------------------------------

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_RATE_LIMIT = 8              # req/sec (conservative vs SEC's 10)
REQUEST_TIMEOUT = 10            # per-request seconds
DEDUP_WINDOW_DAYS = 45          # signal novelty window
ROTATION_ORDER = ["activist", "mna", "distress", "governance"]

SIGNAL_KEYWORDS: Dict[str, List[str]] = {
    "activist": [
        "strategic alternatives", "board representation", "maximize shareholder value",
        "undervalued", "change in control", "special committee", "proxy contest",
        "consent solicitation",
    ],
    "distress": [
        "going concern", "covenant breach", "waiver", "forbearance agreement",
        "material weakness", "restatement", "liquidity shortfall",
        "substantial doubt", "debtor-in-possession",
    ],
    "mna": [
        "merger agreement", "tender offer", "fairness opinion",
        "change of control", "break-up fee", "definitive agreement",
        "received indication of interest",
    ],
    "governance": [
        "poison pill", "rights plan", "bylaw amendment", "declassify board",
        "auditor resignation", "whistleblower", "internal investigation",
    ],
}

SIGNAL_FILING_TYPES: Dict[str, List[str]] = {
    "activist_ownership": ["SC 13D", "SC 13D/A"],
    "late_filings": ["NT 10-K", "NT 10-K/A", "NT 10-Q", "NT 10-Q/A"],
}

KEYWORD_SKIP_FORMS = {
    "ARS", "DEF 14A", "DEFA14A", "DEFM14A", "PRE 14A",
    "N-CSR", "N-CSRS", "497", "497K", "NPORT-P",
}

CATEGORY_FORM_WHITELIST: Dict[str, set] = {
    "distress":   {"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A"},
    "activist":   {"8-K", "8-K/A", "SC 13D", "SC 13D/A", "SC 14D9", "PRER14A", "DFAN14A"},
    "mna":        {"8-K", "8-K/A", "SC 13D", "SC 13D/A", "SC TO-T", "SC TO-T/A",
                   "SC 13E3", "SC 13E3/A", "PREM14A"},
    "governance": {"8-K", "8-K/A", "10-K", "10-K/A", "10-Q", "10-Q/A"},
}

SPAC_IPO_FORM_BLACKLIST = {
    "S-1", "S-1/A", "S-4", "S-4/A", "F-1", "F-1/A", "F-4", "F-4/A",
    "DRS", "DRS/A", "SB-2", "SB-2/A",
    "425", "SC TO-C", "SC TO-C/A", "424B3", "424B4", "424B5",
}

# Merger-agreement sibling forms used to disqualify activist-category keyword hits
# on 8-K (see `_has_merger_sibling`).
#
# Broadened 2026-04-21 after the QXO/TopBuild DLQ incident (operator_flags
# kind='scanner_miscategorization_activist_vs_mna'): the 2026-04-18 $17B all-cash
# merger 8-K fired activist_keyword on the "board representation" governance
# clause inside the merger agreement, but the narrow form list + 3d window
# missed the companion S-4 / DEFM14A that were filed on related but different
# days. The current list covers the common M&A co-filing ecosystem:
#
#   425     — prospectus communications during business combination
#   PREM14A — preliminary merger proxy
#   DEFM14A — definitive merger proxy (added 2026-04-21)
#   DEFA14A — additional soliciting material during M&A (added 2026-04-21)
#   SC TO-T — third-party tender offer
#   SC TO-I — issuer self-tender (added 2026-04-21)
#   SC 14D9 — target's response to tender (added 2026-04-21)
#   S-4     — registration of securities in business combination (added 2026-04-21)
#
# A real activist campaign does not co-file any of these within a week of its
# 8-K. Window extended 3→7 days to tolerate timing drift.
MERGER_SIBLING_FORMS = (
    "425", "PREM14A", "DEFM14A", "DEFA14A",
    "SC TO-T", "SC TO-I", "SC 14D9", "S-4",
)
MERGER_SIBLING_WINDOW_DAYS = 7

# Category → thesis direction (v2 addition; v1 scanner didn't emit this but the
# reactor + convergence classification need it for contradiction detection).
_CATEGORY_DIRECTION: Dict[str, str] = {
    "activist": "long",      # 13D accumulator bullish on target
    "mna": "long",           # target expected to rise
    "distress": "short",     # going concern / restatement bearish
    "governance": "neutral", # poison pill etc. ambiguous until actor known
}

# Category → signal_type name. Preserved from v1 (`{category}_keyword`).
_CATEGORY_SIGNAL_TYPE: Dict[str, str] = {
    "activist":   "activist_keyword",
    "distress":   "distress_keyword",
    "mna":        "mna_keyword",
    "governance": "governance_keyword",
}

# ---------------------------------------------------------------------------
# Rate limiter (verbatim from v1)
# ---------------------------------------------------------------------------

class _SECRateLimiter:
    def __init__(self, max_per_sec: int = SEC_RATE_LIMIT):
        self.max_per_sec = max_per_sec
        self._timestamps: List[float] = []

    def wait(self) -> None:
        now = time.time()
        self._timestamps = [t for t in self._timestamps if now - t < 1.0]
        if len(self._timestamps) >= self.max_per_sec:
            sleep_time = 1.0 - (now - self._timestamps[0]) + 0.05
            if sleep_time > 0:
                time.sleep(sleep_time)
        self._timestamps.append(time.time())


_rate_limiter = _SECRateLimiter()


# ---------------------------------------------------------------------------
# EFTS + submissions
# ---------------------------------------------------------------------------

def _efts_search(query: str, date_from: str, date_to: str,
                 form_type: str = "", max_results: int = 50,
                 *, user_agent: str) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "q": query, "dateRange": "custom",
        "startdt": date_from, "enddt": date_to,
    }
    if form_type:
        params["forms"] = form_type

    _rate_limiter.wait()
    try:
        resp = requests.get(EFTS_URL, params=params,
                            headers={"User-Agent": user_agent},
                            timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException:
        return []

    results: List[Dict[str, Any]] = []
    for hit in data.get("hits", {}).get("hits", [])[:max_results]:
        src = hit.get("_source", {})
        cik = src.get("ciks", [""])[0] if src.get("ciks") else ""
        adsh = src.get("adsh", "")
        raw_name = src.get("display_names", [""])[0] if src.get("display_names") else ""
        company_name = re.sub(r"\s*\(CIK\s+\d+\)\s*$", "", raw_name).strip()

        filing_url = ""
        if cik and adsh:
            cik_stripped = cik.lstrip("0") or "0"
            adsh_clean = adsh.replace("-", "")
            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{adsh_clean}"

        results.append({
            "cik": cik,
            "adsh": adsh,
            "company_name": company_name,
            "company_raw": raw_name,
            "form": src.get("form", ""),
            "file_date": src.get("file_date", ""),
            "file_description": src.get("file_description", ""),
            "filing_url": filing_url,
            "sics": src.get("sics", []),
        })
    return results


def _get_company_tickers(cik: str, *, user_agent: str) -> Tuple[List[str], Optional[str]]:
    if not cik:
        return [], None
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
            return tickers, exchanges[0] if exchanges else None
    except Exception:
        pass
    return [], None


def _has_merger_sibling(cik: str, file_date_str: str, *, user_agent: str,
                        cache: Dict[Tuple[str, str], bool]) -> bool:
    """Return True if the same CIK has a merger-agreement sibling filing
    (425 / PREM14A / SC TO-T) within ±MERGER_SIBLING_WINDOW_DAYS of file_date.

    Used to suppress activist-category keyword hits on 8-K that are really
    mechanical governance clauses inside a merger announcement (see
    QXO-TopBuild 2026-04-18 DLQ incident). Per-run cache is keyed on
    (cik, file_date) so multiple activist keywords hitting the same filing
    only trigger one sibling lookup.
    """
    if not cik or not file_date_str:
        return False
    key = (cik, file_date_str)
    if key in cache:
        return cache[key]
    try:
        anchor = datetime.strptime(file_date_str, "%Y-%m-%d")
    except ValueError:
        cache[key] = False
        return False
    date_from = (anchor - timedelta(days=MERGER_SIBLING_WINDOW_DAYS)).strftime("%Y-%m-%d")
    date_to = (anchor + timedelta(days=MERGER_SIBLING_WINDOW_DAYS)).strftime("%Y-%m-%d")

    params: Dict[str, Any] = {
        "q": "",
        "dateRange": "custom",
        "startdt": date_from,
        "enddt": date_to,
        "forms": ",".join(MERGER_SIBLING_FORMS),
        "ciks": cik.zfill(10),
    }
    _rate_limiter.wait()
    try:
        resp = requests.get(EFTS_URL, params=params,
                            headers={"User-Agent": user_agent},
                            timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException:
        cache[key] = False  # fail open: do not suppress on network error
        return False

    hit = bool(data.get("hits", {}).get("hits", []))
    cache[key] = hit
    return hit


# ---------------------------------------------------------------------------
# Dedup + rotation (Storage-backed)
# ---------------------------------------------------------------------------

def _signal_hash(cik: str, keyword: str, category: str) -> str:
    return hashlib.md5(f"{cik}|{keyword}|{category}".encode()).hexdigest()


def _is_novel(cik: str, keyword: str, category: str,
              dedup_log: Dict[str, str],
              window_days: int = DEDUP_WINDOW_DAYS) -> bool:
    h = _signal_hash(cik, keyword, category)
    if h in dedup_log:
        try:
            first_date = datetime.strptime(dedup_log[h], "%Y-%m-%d")
            if (datetime.utcnow() - first_date).days < window_days:
                return False
        except ValueError:
            pass
    return True


def _load_dedup(client: SupabaseClient) -> Dict[str, str]:
    raw = client.read_cache("edgar", "dedup.json")
    if raw is None:
        return {}
    try:
        import json
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}


def _save_dedup(client: SupabaseClient, log: Dict[str, str]) -> None:
    import json
    client.write_cache("edgar", "dedup.json", json.dumps(log).encode("utf-8"),
                       content_type="application/json")


def _load_rotation(client: SupabaseClient) -> Dict[str, Any]:
    raw = client.read_cache("edgar", "rotation.json")
    if raw is None:
        return {"rotation_index": -1, "scan_history": {}}
    try:
        import json
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {"rotation_index": -1, "scan_history": {}}


def _save_rotation(client: SupabaseClient, state: Dict[str, Any]) -> None:
    import json
    client.write_cache("edgar", "rotation.json", json.dumps(state).encode("utf-8"),
                       content_type="application/json")


# ---------------------------------------------------------------------------
# Strength heuristics (verbatim from v1 _build_signal)
# ---------------------------------------------------------------------------

def _compute_strength(category: str, keyword: str, form: str) -> int:
    strength = 2
    if category == "activist" and "13D" in form:
        strength = 4
    elif category == "distress" and keyword in ("going concern", "substantial doubt"):
        strength = 4
    elif category == "mna":
        if keyword in ("definitive agreement", "merger agreement"):
            ongoing_forms = (
                "SC TO-T", "SC TO-C", "PREM14A", "DEFM14A",
                "S-4", "SC 13E3", "SC TO-I", "SC TO-T/A",
                "SC TO-C/A", "S-4/A", "SC 13E3/A", "SC TO-I/A",
                "DFAN14A", "DEFA14A",
            )
            if any(form.upper().startswith(f) for f in ongoing_forms):
                strength = 2
            else:
                strength = 5
        elif keyword == "tender offer":
            if "SC TO-T" in form.upper() and "/A" not in form.upper():
                strength = 4
            else:
                strength = 2
        else:
            strength = 3
    elif category == "governance" and keyword in ("poison pill", "rights plan"):
        strength = 3
    return strength


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------

def _build_signal(hit: Dict[str, Any], keyword: str, category: str,
                  scan_date: datetime, *, user_agent: str) -> Optional[Signal]:
    cik = hit.get("cik", "")
    adsh = hit.get("adsh", "")
    if not adsh:
        return None

    tickers, exchange = _get_company_tickers(cik, user_agent=user_agent)
    ticker = tickers[0] if tickers else None

    # Resolve FIGI (best-effort; no failure path — scanner emits signal either way
    # and the reactor/entity-resolver cascade handles the miss).
    issuer_figi: Optional[str] = None
    if ticker:
        try:
            from modal_workers.shared.openfigi_resolver import resolve_ticker
            res = resolve_ticker(ticker, exch_code="US")
            if res.resolved:
                issuer_figi = res.issuer_figi
        except Exception:
            pass

    signal_type = _CATEGORY_SIGNAL_TYPE.get(category, f"{category}_keyword")
    direction = _CATEGORY_DIRECTION.get(category)
    form = hit.get("form", "")

    source_content_hash = f"sha256:{hashlib.sha256(f'{adsh}|{keyword}|{category}'.encode()).hexdigest()}"
    signal_id = f"edgar_{adsh.replace('-', '')}_{category}_{hashlib.md5(keyword.encode()).hexdigest()[:8]}"

    source_date_str = hit.get("file_date", "")
    try:
        source_date = datetime.strptime(source_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        source_date = scan_date

    raw_payload: Dict[str, Any] = {
        "keyword": keyword,
        "filing_type": form,
        "cik": cik,
        "adsh": adsh,
        "file_description": hit.get("file_description", ""),
        "company_raw": hit.get("company_raw", ""),
        "company_name": hit.get("company_name", ""),
        "tickers": tickers,
        "exchange": exchange,
    }

    entity_hints = EntityHints(
        issuer_figi=issuer_figi,
        ticker=ticker,
        mic=None,  # US MIC not known without exchange→MIC lookup; left for entity_identifiers cascade
        cik=cik or None,
        name=hit.get("company_name") or None,
        country="US",
    )

    return Signal(
        signal_id=signal_id,
        source_content_hash=source_content_hash,
        source_date=source_date,
        scan_date=scan_date,
        signal_type=signal_type,
        raw_payload=raw_payload,
        source_url=hit.get("filing_url") or None,
        issuer_figi=issuer_figi,
        entity_hints=entity_hints,
        thesis_direction=direction,
        strength_estimate=_compute_strength(category, keyword, form),
    )


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

    # Route openfigi cache reads/writes through Supabase Storage.
    from modal_workers.shared.openfigi_resolver import set_cache_backend
    set_cache_backend(*client.openfigi_cache_backend())

    # Rotation: one keyword category per 3h run (v1 parity).
    rotation_state = _load_rotation(client)
    next_idx = (rotation_state.get("rotation_index", -1) + 1) % len(ROTATION_ORDER)
    category = ROTATION_ORDER[next_idx]
    rotation_state["rotation_index"] = next_idx
    rotation_state["last_category"] = category
    rotation_state["last_scan_ts"] = datetime.now(timezone.utc).isoformat()
    rotation_state.setdefault("scan_history", {})[category] = rotation_state["last_scan_ts"]

    days_back = int(cfg.config.get("days_back", 2))
    scan_date = datetime.now(timezone.utc)
    date_from = (scan_date - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = scan_date.strftime("%Y-%m-%d")

    dedup_log = _load_dedup(client)
    budget = max(10, cfg.timeout_soft_s - 5)  # leave headroom for final ops
    scan_start = time.time()
    warnings: List[str] = []
    signals: List[Signal] = []
    seen_adsh_keyword: set[str] = set()
    hits_processed = 0

    activist_merger_suppression = bool(
        cfg.config.get("activist_merger_sibling_suppression", True))
    merger_sibling_cache: Dict[Tuple[str, str], bool] = {}
    merger_suppressed_count = 0

    # --- Keyword scan (current rotation category) ---
    for keyword in SIGNAL_KEYWORDS.get(category, []):
        if time.time() - scan_start > budget:
            warnings.append(f"wall-clock budget ({budget}s) exceeded during keyword scan")
            break

        hits = _efts_search(f'"{keyword}"', date_from, date_to,
                            max_results=30, user_agent=user_agent)
        for hit in hits:
            hits_processed += 1
            adsh = hit.get("adsh", "")
            cik = hit.get("cik", "")
            dedup_key = f"{adsh}|{keyword}"
            if dedup_key in seen_adsh_keyword:
                continue
            seen_adsh_keyword.add(dedup_key)

            form = hit.get("form", "").strip()
            if form in KEYWORD_SKIP_FORMS:
                continue
            if any(form.upper().startswith(bl) for bl in SPAC_IPO_FORM_BLACKLIST):
                continue
            if category in CATEGORY_FORM_WHITELIST:
                whitelist = CATEGORY_FORM_WHITELIST[category]
                if not any(form.upper().startswith(wl) for wl in whitelist):
                    continue

            # Merger-clause defense: activist keyword in 8-K is ambiguous. If the
            # same CIK filed a 425 / PREM14A / SC TO-T within ±3 days, treat the
            # activist hit as a merger-governance clause and suppress.
            if (activist_merger_suppression
                    and category == "activist"
                    and form.upper().startswith("8-K")):
                if _has_merger_sibling(cik, hit.get("file_date", ""),
                                       user_agent=user_agent,
                                       cache=merger_sibling_cache):
                    merger_suppressed_count += 1
                    continue

            if not _is_novel(cik, keyword, category, dedup_log):
                continue

            sig = _build_signal(hit, keyword, category, scan_date, user_agent=user_agent)
            if sig is None:
                continue
            signals.append(sig)
            dedup_log[_signal_hash(cik, keyword, category)] = date_to

    if merger_suppressed_count:
        warnings.append(
            f"suppressed {merger_suppressed_count} activist 8-K hit(s) with "
            f"merger-agreement sibling filing (425/PREM14A/SC TO-T) within "
            f"\u00b1{MERGER_SIBLING_WINDOW_DAYS}d")

    # --- Filing type scan (SC 13D, NT 10-K variants) — cheap, always-on. ---
    seen_adsh_filing: set[str] = set()
    for signal_type_key, form_types in SIGNAL_FILING_TYPES.items():
        if time.time() - scan_start > budget:
            warnings.append(f"wall-clock budget ({budget}s) exceeded during filing-type scan")
            break
        for form_type in form_types:
            if time.time() - scan_start > budget:
                break
            hits = _efts_search("*", date_from, date_to, form_type=form_type,
                                max_results=50, user_agent=user_agent)
            for hit in hits:
                adsh = hit.get("adsh", "")
                if adsh in seen_adsh_filing:
                    continue
                seen_adsh_filing.add(adsh)
                hits_processed += 1

                # Filing-type scan uses signal_type_key as the category for strength calc.
                sig = _build_signal(hit, keyword=form_type, category=signal_type_key,
                                    scan_date=scan_date, user_agent=user_agent)
                if sig is None:
                    continue
                # Strength boost for 13D and NT 10-K filings (v1 parity).
                if "13D" in form_type:
                    sig.strength_estimate = max(sig.strength_estimate, 4)
                elif "NT 10" in form_type:
                    sig.strength_estimate = max(sig.strength_estimate, 3)
                sig.signal_type = signal_type_key   # override generic category fallback
                signals.append(sig)

    _save_dedup(client, dedup_log)
    _save_rotation(client, rotation_state)

    return ScannerResult(
        scanner="edgar_filing_monitor",
        status="partial" if warnings else "ok",
        signals=signals,
        warnings=warnings,
        fetched_records=hits_processed,
    )
