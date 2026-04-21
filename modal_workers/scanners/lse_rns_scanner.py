"""
LSE RNS scanner — Modal port of tools/lse_rns_scanner.py.

Preserved from v1 (byte-equivalent where relevant):
  - _HEADLINE_RULES classification regex table — ordered, first-match-wins.
  - Investegate HTML pagination as the primary enumeration source
    (_ROW_RE, _DATE_FMT, POLITE_DELAY_S=2.0, MAX_PAGES=40, PAGE_ROW_THRESHOLD=5).
  - ALLOWED_SUPPLIERS filter (RNS / RNSREACH / PRN / BUSINESSWIRE / REACH).
  - LSE /api/gw/lse/instruments/alldata/{TIDM} metadata fetch, 24h cached.
  - _extract_alldata_fields + GBP→USD market-cap conversion (GBP_TO_USD=1.27).
  - Non-equity category drop (only EQUITY survives when alldata hits).

Deviations from v1:
  - No OUT_FILE; signals returned via ScannerResult list for run_scanner plumbing.
  - No fetch_raw_signals / CLI; only scan(cfg) is public.
  - alldata cache lives in Supabase Storage at scanner-caches/lse/alldata/{TIDM}.json
    instead of working/lse_alldata_cache/{TIDM}.json (24h TTL preserved).
  - source_content_hash now carries the spec.md §3.4 "sha256:<64hex>" prefix.
  - OpenFIGI backend wired through SupabaseClient.openfigi_cache_backend();
    ISIN from alldata feeds resolve_isin as fallback when resolve_ticker(LN) misses.
  - Shared boilerplate_filters.is_boilerplate("LSE", headline) drops voting-rights,
    PDMR shareholding, TR-1, issue-of-equity etc. before classification.
  - scoring_profile resolution is downstream (cfg.signal_type_profile_map); we do
    not set sig.scoring_profile or rubric_scores here (v1 did; v2 reactor handles).
  - No auth. Public endpoints. MissingAuthError not raised.

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
    - Takes days_back from cfg.config.get("days_back", 3).
    - Uses cfg.timeout_soft_s as wall-clock budget; emits status="partial" with
      a warning if Investegate pagination + alldata lookups exceed it.
"""

from __future__ import annotations

import hashlib
import html as _htmllib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from modal_workers.shared.boilerplate_filters import is_boilerplate
from modal_workers.shared.scanner_base import Signal, ScannerResult
from modal_workers.shared.supabase_client import (
    EntityHints,
    ScannerConfig,
    SupabaseClient,
    SupabaseError,
)

NAME = "lse_rns_scanner"

# ---------------------------------------------------------------------------
# Constants (verbatim from v1)
# ---------------------------------------------------------------------------

# Browser User-Agent — v1 used a custom UA; investegate accepts either but a
# browser UA is safer against future anti-bot tightening.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
INVESTEGATE_BASE = "https://www.investegate.co.uk"
LSE_ALLDATA = "https://api.londonstockexchange.com/api/gw/lse/instruments/alldata/{tidm}"

POLITE_DELAY_S = 2.0
MAX_PAGES = 40            # safety bound — ~2000 announcements
PAGE_ROW_THRESHOLD = 5    # if a page yields fewer than this, assume end-of-feed
FETCH_TIMEOUT = 20

# GBP → USD for market-cap conversion. Kept as module constant (v1 parity).
GBP_TO_USD = 1.27

# Cache TTL for LSE alldata (v1 preserved 24h).
ALLDATA_TTL_S = 86400

# Suppliers considered regulatory filings (vs wire news). v1 parity.
ALLOWED_SUPPLIERS = {
    "RNS", "RNSREACH", "RNS_REACH", "PRN",
    "BUSINESSWIRE", "BUSINESS_WIRE", "REACH",
}

# RNS headline classification. Ordered — first match wins.
# (regex, signal_type, signal_category, strength_estimate, thesis_direction)
# PRESERVED VERBATIM from v1 (byte-equivalent patterns + metadata).
_HEADLINE_RULES: List[Tuple[str, str, str, int, str]] = [
    # Takeovers — highest priority
    (r"\brule\s*2\.7\b|\brecommended\s+(cash\s+)?offer\b|\bfirm\s+intention\s+to\s+(make\s+an\s+)?offer\b",
     "rule_2_7_firm_offer", "takeover", 5, "long"),
    (r"\brule\s*2\.4\b|\bpossible\s+offer\b|\bpotential\s+offer\b|\bapproach\s+regarding\s+a\s+possible\s+offer\b",
     "rule_2_4_possible_offer", "takeover", 4, "long"),
    (r"\bscheme\s+of\s+arrangement\b.*\b(sanction|court|effective)\b|\bscheme\s+effective\b",
     "scheme_of_arrangement", "takeover", 5, "long"),
    (r"\bstrategic\s+review\b",
     "strategic_review", "takeover", 3, "neutral"),
    # Profit / trading
    (r"\bprofit\s+warning\b|\btrading\s+alert\b|\bmaterial(ly)?\s+below\s+(market\s+)?expectations\b",
     "profit_warning", "results", 5, "short"),
    (r"\bprofit\s+upgrade\b|\bahead\s+of\s+(market\s+)?expectations\b|\bmaterially\s+ahead\b",
     "profit_upgrade", "results", 4, "long"),
    (r"\btrading\s+update\b|\bpre[-\s]?close\s+(trading\s+)?update\b|\btrading\s+statement\b",
     "trading_update", "results", 3, "neutral"),
    (r"\bfinal\s+results\b|\bpreliminary\s+results\b|\bannual\s+results\b",
     "final_results", "results", 3, "neutral"),
    (r"\binterim\s+results\b|\bhalf(\s|-)year(ly)?\s+(report|results)\b|\bq[1-4]\s+(trading\s+)?(update|results)\b",
     "interim_results", "results", 3, "neutral"),
    # Governance / insider
    (r"\bceo\s+(resign|step\s*down|departure|appoint)|\bchief\s+executive\s+(resign|step\s*down|appoint)|\bchairman\s+(resign|step\s*down|appoint)",
     "board_change", "governance", 4, "neutral"),
    # Shareholder notifications (TR-1)
    (r"\btr[\s-]?1\b|\bnotification\s+of\s+major\s+holding(s)?\b|\bnotification\s+of\s+major\s+interest\s+in\s+shares\b",
     "major_shareholder_change", "shareholder", 3, "neutral"),
    # Mining
    (r"\bjorc\b|\bmineral\s+resource\s+(estimate|update)\b|\bresource\s+upgrade\b|\breserves?\s+update\b|\bdrilling\s+results\b",
     "jorc_resource_update", "mining", 4, "neutral"),
    # Suspensions
    (r"\bsuspension\s+of\s+(trading|listing)\b|\btrading\s+suspended\b|\bcancellation\s+of\s+admission\b",
     "aim_suspension", "governance", 4, "short"),
    # Fundraise / placing (usually short-term dilutive)
    (r"\bplacing\b.*\b(announcement|results?)\b|\bproposed\s+placing\b|\bretail\s+offer\b|\brights\s+issue\b",
     "equity_fundraise", "shareholder", 3, "short"),
    # Buybacks — initiation only (execution filtered as boilerplate)
    (r"\bshare\s+(buy[\s-]?back|repurchase)\s+(programme|authorisation|announcement)\b|\bcommencement\s+of\s+share\s+buy[\s-]?back\b",
     "buyback_initiation", "shareholder", 3, "long"),
]

# Investegate row regex — v1 parity.
_ROW_RE = re.compile(
    r'<tr>\s*'
    r'<td>\s*(?P<date>[^<]+?)\s*</td>\s*'
    r'<td>.*?<a\s+class="[^"]*source-(?P<supplier>[A-Za-z0-9_]+)[^"]*"[^>]*>(?P<supplier_label>[^<]+)</a>.*?</td>\s*'
    r'<td>.*?<a\s+href="https://www\.investegate\.co\.uk/company/(?P<tidm>[^"]+)">\s*(?P<company>[^<]+?)\s*\((?P=tidm)\)\s*</a>.*?</td>\s*'
    r'<td>\s*<a\s+class="announcement-link"\s+href="(?P<url>https://www\.investegate\.co\.uk/announcement/[^"]+)">(?P<headline>[^<]+)</a>\s*</td>\s*'
    r'</tr>',
    re.DOTALL | re.IGNORECASE,
)

# Investegate date format: "14 Apr 2026 03:41 PM". Naive UK-local, we attach UTC
# (RNS runs UK time; ≤1h BST drift is acceptable for window math + dedup).
_DATE_FMT = "%d %b %Y %I:%M %p"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _http_get(url: str, accept: str = "*/*") -> Tuple[int, bytes]:
    """Raw urlopen — mirrors v1's dependency-free fetch. stdlib-only keeps the
    scanner cheap to cold-start under Modal without requests-session warmup."""
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            return resp.status, resp.read()
    except HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body
    except URLError:
        return 0, b""
    except Exception:
        return 0, b""


# ---------------------------------------------------------------------------
# Investegate parsing + pagination
# ---------------------------------------------------------------------------

def _parse_investegate_date(s: str) -> Optional[datetime]:
    s = re.sub(r"\s+", " ", s.strip())
    try:
        return datetime.strptime(s, _DATE_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_investegate_page(html_body: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for m in _ROW_RE.finditer(html_body):
        d = m.groupdict()
        dt = _parse_investegate_date(d["date"])
        rows.append({
            "date_str": d["date"].strip(),
            "date": dt.isoformat() if dt else None,
            "_dt": dt,
            "supplier": d["supplier"].strip().upper(),
            "supplier_label": _htmllib.unescape(d["supplier_label"]).strip(),
            "tidm": d["tidm"].strip().upper(),
            "company_name": _htmllib.unescape(d["company"]).strip(),
            "headline": _htmllib.unescape(d["headline"]).strip(),
            "url": d["url"].strip(),
        })
    return rows


def _fetch_investegate_pages(
    window_days: int,
    *,
    scan_start: float,
    budget_s: float,
    warnings: List[str],
) -> List[Dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    all_rows: List[Dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        if time.time() - scan_start > budget_s:
            warnings.append(
                f"wall-clock budget ({budget_s}s) exceeded during investegate pagination "
                f"after page {page - 1}"
            )
            break
        url = INVESTEGATE_BASE + "/" if page == 1 else f"{INVESTEGATE_BASE}/?page={page}"
        status, body = _http_get(url, accept="text/html")
        if status != 200 or not body:
            warnings.append(f"investegate page {page} returned status={status}")
            break
        text = body.decode("utf-8", errors="replace")
        rows = _parse_investegate_page(text)
        if len(rows) < PAGE_ROW_THRESHOLD:
            all_rows.extend(rows)
            break
        all_rows.extend(rows)
        # stop if the oldest row on this page is past the cutoff
        oldest = next((r["_dt"] for r in reversed(rows) if r["_dt"]), None)
        if oldest and oldest < cutoff:
            break
        time.sleep(POLITE_DELAY_S)
    return [r for r in all_rows if r["_dt"] is not None and r["_dt"] >= cutoff]


# ---------------------------------------------------------------------------
# LSE alldata (Supabase-Storage-cached, 24h TTL)
# ---------------------------------------------------------------------------

def _alldata_cache_key(tidm: str) -> str:
    safe = re.sub(r"[^A-Z0-9]", "_", tidm.upper())
    return f"alldata/{safe}.json"


def _load_alldata_cached(client: SupabaseClient, tidm: str) -> Optional[Dict[str, Any]]:
    """Fetch alldata for TIDM from LSE, with 24h Supabase-Storage cache.

    Cache envelope matches v1 shape:
        {"_fetched_at": "<isoformat utc>", "status": <int>, "data": <dict|None>}
    Returns the inner `data` dict (or None if the API had nothing / failed).
    """
    key = _alldata_cache_key(tidm)
    try:
        raw = client.read_cache("lse", key)
    except SupabaseError:
        raw = None
    if raw is not None:
        try:
            cached = json.loads(raw)
            ts = cached.get("_fetched_at")
            if ts:
                fetched = datetime.fromisoformat(ts)
                if fetched.tzinfo is None:
                    fetched = fetched.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - fetched).total_seconds() < ALLDATA_TTL_S:
                    return cached.get("data")
        except (ValueError, UnicodeDecodeError, TypeError):
            pass

    url = LSE_ALLDATA.format(tidm=tidm)
    status, body = _http_get(url, accept="application/json")
    data: Optional[Dict[str, Any]] = None
    if status == 200 and body:
        try:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict):
                data = parsed
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = None

    try:
        envelope = {
            "_fetched_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "data": data,
        }
        client.write_cache(
            "lse", key,
            json.dumps(envelope).encode("utf-8"),
            content_type="application/json",
        )
    except SupabaseError:
        pass  # best effort
    return data


def _extract_alldata_fields(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract the subset of alldata we care about.

    LSE's marketcapitalization is verified (2026-04-14) to be full GBP units,
    so /1e6 → GBP millions. v1 parity."""
    out: Dict[str, Any] = {
        "isin": None, "sedol": None, "issuer_name": None,
        "sector_code": None, "market_cap_gbp_mm": None,
        "currency": None, "market_tier": None, "category": None,
        "trading_status": None,
    }
    if not isinstance(data, dict):
        return out
    out["isin"] = data.get("isin") or data.get("ISIN")
    out["sedol"] = data.get("sedol") or data.get("SEDOL")
    out["issuer_name"] = (
        data.get("issuername") or data.get("issuerName") or data.get("description")
    )
    out["sector_code"] = data.get("sectorcode") or data.get("subsectorcode")
    out["currency"] = data.get("currency")
    out["market_tier"] = data.get("market")       # MAINMARKET | AIM
    out["category"] = data.get("category")        # EQUITY | ETF | BOND | ...
    out["trading_status"] = (
        data.get("tradingstatus") or data.get("tradingstatuscode")
    )
    mc = (
        data.get("marketcapitalization")
        or data.get("marketCapitalization")
        or data.get("marketCap")
    )
    if mc:
        try:
            out["market_cap_gbp_mm"] = round(float(mc) / 1e6, 2)
        except (TypeError, ValueError):
            pass
    return out


# ---------------------------------------------------------------------------
# Classification + hashing
# ---------------------------------------------------------------------------

def _classify_headline(headline: str) -> Optional[Dict[str, Any]]:
    h = headline.strip()
    for pat, stype, cat, strength, direction in _HEADLINE_RULES:
        if re.search(pat, h, flags=re.IGNORECASE):
            return {
                "signal_type": stype,
                "signal_category": cat,
                "strength_estimate": strength,
                "thesis_direction": direction,
                "_pattern_matched": pat,
            }
    return None


def _signal_id(tidm: str, source_date: str, signal_type: str, headline: str) -> str:
    return hashlib.sha256(
        f"{tidm}|XLON|{source_date}|{signal_type}|{headline}".encode("utf-8")
    ).hexdigest()[:32]


def _content_hash(tidm: str, headline: str, url: str) -> str:
    return (
        "sha256:"
        + hashlib.sha256(f"{tidm}|{headline}|{url}".encode("utf-8")).hexdigest()
    )


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------

def _build_signal(
    row: Dict[str, Any],
    cls: Dict[str, Any],
    meta: Dict[str, Any],
    scan_date: datetime,
) -> Optional[Signal]:
    tidm = row["tidm"]
    headline = row["headline"]
    url = row["url"]
    source_dt: datetime = row["_dt"] or scan_date

    # OpenFIGI resolution: ticker → issuer_figi, with ISIN fallback.
    issuer_figi: Optional[str] = None
    isin_val = meta.get("isin")
    try:
        from modal_workers.shared.openfigi_resolver import resolve_ticker, resolve_isin
        res = resolve_ticker(tidm, exch_code="LN")
        if res.resolved:
            issuer_figi = res.issuer_figi
        elif isin_val:
            res2 = resolve_isin(isin_val)
            if res2.resolved:
                issuer_figi = res2.issuer_figi
    except Exception:
        pass

    # GBP → USD market cap (best-effort, module-constant FX).
    market_cap_usd_mm: Optional[float] = None
    mc_gbp = meta.get("market_cap_gbp_mm")
    if mc_gbp is not None:
        try:
            market_cap_usd_mm = round(float(mc_gbp) * GBP_TO_USD, 2)
        except (TypeError, ValueError):
            market_cap_usd_mm = None

    raw_payload: Dict[str, Any] = {
        "tidm": tidm,
        "headline": headline,
        "story_id": url.rsplit("/", 1)[-1] if url else None,
        "published_at": source_dt.isoformat(),
        "category": cls.get("signal_category"),
        "thesis_direction": cls.get("thesis_direction"),
        "isin": isin_val,
        "sedol": meta.get("sedol"),
        "market_cap_gbp_mm": mc_gbp,
        "market_cap_usd_mm": market_cap_usd_mm,
        "sector_code": meta.get("sector_code"),
        "market_tier": meta.get("market_tier"),
        "instrument_category": meta.get("category"),
        "trading_status": meta.get("trading_status"),
        "currency": meta.get("currency"),
        "company_name_en": meta.get("issuer_name") or row.get("company_name"),
        "supplier": row.get("supplier"),
        "supplier_label": row.get("supplier_label"),
        "date_str": row.get("date_str"),
        "pattern_matched": cls.get("_pattern_matched"),
        "source_url": url,
    }

    entity_hints = EntityHints(
        issuer_figi=issuer_figi,
        ticker=tidm,
        mic="XLON",
        isin=isin_val,
        name=meta.get("issuer_name") or row.get("company_name") or None,
        country="GB",
    )

    source_date_str = source_dt.isoformat()
    return Signal(
        signal_id=_signal_id(tidm, source_date_str, cls["signal_type"], headline),
        source_content_hash=_content_hash(tidm, headline, url),
        source_date=source_dt,
        scan_date=scan_date,
        signal_type=cls["signal_type"],
        raw_payload=raw_payload,
        source_url=url or None,
        issuer_figi=issuer_figi,
        entity_hints=entity_hints,
        thesis_direction=cls["thesis_direction"],
        strength_estimate=cls["strength_estimate"],
    )


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    # No auth — public endpoints. SupabaseClient handles SUPABASE_URL /
    # SUPABASE_SERVICE_ROLE_KEY env lookup and raises implicitly if missing.
    client = SupabaseClient()

    # Route openfigi cache reads/writes through Supabase Storage.
    try:
        from modal_workers.shared.openfigi_resolver import set_cache_backend
        set_cache_backend(*client.openfigi_cache_backend())
    except Exception:
        pass  # best effort — resolver falls back to file cache

    scan_date = datetime.now(timezone.utc)
    scan_start = time.time()
    budget_s = max(10.0, float(cfg.timeout_soft_s) - 5.0)

    days_back = int(cfg.config.get("days_back", 3))

    warnings: List[str] = []
    signals: List[Signal] = []
    seen_hashes: set[str] = set()

    # --- Investegate pagination (primary enumeration) ---
    rows = _fetch_investegate_pages(
        days_back, scan_start=scan_start, budget_s=budget_s, warnings=warnings,
    )
    fetched_records = len(rows)

    # Keep only regulatory suppliers (parity with v1; matched by
    # hyphen/underscore-normalised comparison).
    def _supplier_norm(s: str) -> str:
        return s.replace("-", "").replace("_", "")
    allowed_norm = {_supplier_norm(s) for s in ALLOWED_SUPPLIERS}
    rns_rows = [
        r for r in rows
        if _supplier_norm((r.get("supplier") or "")) in allowed_norm
    ]

    # --- Walk rows: boilerplate drop → classify → alldata → build signal ---
    alldata_by_tidm: Dict[str, Dict[str, Any]] = {}
    for row in rns_rows:
        if time.time() - scan_start > budget_s:
            warnings.append(
                f"wall-clock budget ({budget_s}s) exceeded during classification/alldata"
            )
            break

        headline = row.get("headline") or ""
        if not headline:
            continue

        # Shared LSE boilerplate drop (TR-1, PDMR, total voting rights, etc.).
        if is_boilerplate("LSE", headline):
            continue

        cls = _classify_headline(headline)
        if cls is None:
            continue  # not in our rule table — drop (v1 parity)

        tidm = row["tidm"]
        if not tidm:
            continue

        # One alldata lookup per TIDM per run (24h cached in Storage across runs).
        if tidm not in alldata_by_tidm:
            try:
                meta_raw = _load_alldata_cached(client, tidm)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"alldata {tidm}: {type(e).__name__}: {e}")
                meta_raw = None
            alldata_by_tidm[tidm] = _extract_alldata_fields(meta_raw)
        meta = alldata_by_tidm[tidm]

        # Drop non-equity instruments when alldata resolved; unresolved → keep
        # and let downstream entity-resolver / security_type cascade filter.
        cat = (meta.get("category") or "").upper()
        if cat and cat != "EQUITY":
            continue

        try:
            sig = _build_signal(row, cls, meta, scan_date)
        except Exception as e:  # noqa: BLE001
            warnings.append(
                f"build_signal {tidm} {row.get('headline', '')[:60]}: "
                f"{type(e).__name__}: {e}"
            )
            continue
        if sig is None:
            continue
        if sig.source_content_hash in seen_hashes:
            continue
        seen_hashes.add(sig.source_content_hash)
        signals.append(sig)

    status = "partial" if warnings else "ok"
    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=signals,
        warnings=warnings,
        fetched_records=fetched_records,
    )
