"""
LSE RNS scanner — Tool 2 canary scanner (Phase 1).

Data pathway (verified 2026-04-14):
  1. investegate.co.uk — paginated HTML feed of every RNS / RNS-Reach / PRN
     announcement in reverse-chronological order. Primary enumeration source.
  2. api.londonstockexchange.com/api/gw/lse/instruments/alldata/{TIDM} —
     issuer metadata (ISIN, SEDOL, name, sector, market_cap). Used for
     sanity-check and ISIN fallback into OpenFIGI.
  3. OpenFIGI — final FIGI / issuer_figi resolution via tools/openfigi_resolver.

This scanner emits raw signals conforming to the common schema in
INSTRUCTIONS.md §2. It does NOT apply triage / scoring — that is done by
pipeline_runner.

Contract:
    fetch_raw_signals(window_days: int) -> list[dict]
"""

from __future__ import annotations

import hashlib
import html as _htmllib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
WORKING = ROOT / "working"
WORKING.mkdir(parents=True, exist_ok=True)
LSE_CACHE = WORKING / "lse_alldata_cache"
LSE_CACHE.mkdir(parents=True, exist_ok=True)

USER_AGENT = "Mozilla/5.0 (compatible; Tool2-NonUS-Discovery/1.0)"
INVESTEGATE_BASE = "https://www.investegate.co.uk"
LSE_ALLDATA = "https://api.londonstockexchange.com/api/gw/lse/instruments/alldata/{tidm}"

POLITE_DELAY_S = 2.0
MAX_PAGES = 40            # safety bound — ~2000 announcements
PAGE_ROW_THRESHOLD = 5    # if a page yields fewer than this, assume end-of-feed
FETCH_TIMEOUT = 20

# RNS category classification from headline keywords. Ordered: first match wins.
# Each entry is (regex, signal_type, signal_category, strength_estimate, thesis_direction)
# thesis_direction: 'long' | 'short' | 'unknown'
_HEADLINE_RULES = [
    # Takeovers — highest priority
    (r"\brule\s*2\.7\b|\brecommended\s+(cash\s+)?offer\b|\bfirm\s+intention\s+to\s+(make\s+an\s+)?offer\b",
     "takeover_firm_offer", "takeover", 5, "long"),
    (r"\brule\s*2\.4\b|\bpossible\s+offer\b|\bpotential\s+offer\b|\bapproach\s+regarding\s+a\s+possible\s+offer\b",
     "takeover_possible_offer", "takeover", 4, "long"),
    (r"\bscheme\s+of\s+arrangement\b.*\b(sanction|court|effective)\b|\bscheme\s+effective\b",
     "scheme_sanction", "takeover", 5, "long"),
    (r"\bstrategic\s+review\b",
     "strategic_review", "takeover", 3, "unknown"),
    # Profit / trading
    (r"\bprofit\s+warning\b|\btrading\s+alert\b|\bmaterial(ly)?\s+below\s+(market\s+)?expectations\b",
     "profit_warning", "results", 5, "short"),
    (r"\bprofit\s+upgrade\b|\bahead\s+of\s+(market\s+)?expectations\b|\bmaterially\s+ahead\b",
     "profit_upgrade", "results", 4, "long"),
    (r"\btrading\s+update\b|\bpre[-\s]?close\s+(trading\s+)?update\b|\btrading\s+statement\b",
     "trading_update", "results", 3, "unknown"),
    (r"\bfinal\s+results\b|\bpreliminary\s+results\b|\bannual\s+results\b",
     "final_results", "results", 3, "unknown"),
    (r"\binterim\s+results\b|\bhalf(\s|-)year(ly)?\s+(report|results)\b|\bq[1-4]\s+(trading\s+)?(update|results)\b",
     "interim_results", "results", 3, "unknown"),
    # Governance / insider
    (r"\bceo\s+(resign|step\s*down|departure|appoint)|\bchief\s+executive\s+(resign|step\s*down|appoint)|\bchairman\s+(resign|step\s*down|appoint)",
     "senior_governance_change", "governance", 4, "unknown"),
    # Shareholder notifications (TR-1)
    (r"\btr[\s-]?1\b|\bnotification\s+of\s+major\s+holding(s)?\b|\bnotification\s+of\s+major\s+interest\s+in\s+shares\b",
     "major_shareholder_change", "shareholder", 3, "unknown"),
    # Mining
    (r"\bjorc\b|\bmineral\s+resource\s+(estimate|update)\b|\bresource\s+upgrade\b|\breserves?\s+update\b|\bdrilling\s+results\b",
     "jorc_resource_update", "mining", 4, "unknown"),
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


# -------- HTTP helpers --------

def _http_get(url: str, accept: str = "*/*") -> tuple[int, bytes]:
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
    except URLError as e:
        log.warning("URL error for %s: %s", url, e)
        return 0, b""


# -------- investegate row parsing --------

_ROW_RE = re.compile(
    r'<tr>\s*'
    r'<td>\s*(?P<date>[^<]+?)\s*</td>\s*'
    r'<td>.*?<a\s+class="[^"]*source-(?P<supplier>[A-Za-z0-9_]+)[^"]*"[^>]*>(?P<supplier_label>[^<]+)</a>.*?</td>\s*'
    r'<td>.*?<a\s+href="https://www\.investegate\.co\.uk/company/(?P<tidm>[^"]+)">\s*(?P<company>[^<]+?)\s*\((?P=tidm)\)\s*</a>.*?</td>\s*'
    r'<td>\s*<a\s+class="announcement-link"\s+href="(?P<url>https://www\.investegate\.co\.uk/announcement/[^"]+)">(?P<headline>[^<]+)</a>\s*</td>\s*'
    r'</tr>',
    re.DOTALL | re.IGNORECASE,
)

# Date format on investegate: "14 Apr 2026 03:41 PM"
_DATE_FMT = "%d %b %Y %I:%M %p"


def _parse_investegate_date(s: str) -> Optional[datetime]:
    s = s.strip()
    # Normalize weird whitespace
    s = re.sub(r"\s+", " ", s)
    try:
        # Naive parse, then attach UTC (investegate runs UK time; RNS itself is UK;
        # we treat as UTC-equivalent for window math — 1hr BST drift is acceptable).
        return datetime.strptime(s, _DATE_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_investegate_page(html_body: str) -> list[dict]:
    rows: list[dict] = []
    for m in _ROW_RE.finditer(html_body):
        d = m.groupdict()
        dt = _parse_investegate_date(d["date"])
        rows.append({
            "date_str": d["date"].strip(),
            "date": dt.isoformat() if dt else None,
            "_dt": dt,
            "supplier": d["supplier"].strip().upper(),  # RNS, RNSREACH, PRN, etc.
            "supplier_label": _htmllib.unescape(d["supplier_label"]).strip(),
            "tidm": d["tidm"].strip().upper(),
            "company_name": _htmllib.unescape(d["company"]).strip(),
            "headline": _htmllib.unescape(d["headline"]).strip(),
            "url": d["url"].strip(),
        })
    return rows


def _fetch_investegate_pages(window_days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    all_rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        url = INVESTEGATE_BASE + "/" if page == 1 else f"{INVESTEGATE_BASE}/?page={page}"
        status, body = _http_get(url, accept="text/html")
        if status != 200 or not body:
            log.warning("investegate page %d returned status=%s", page, status)
            break
        text = body.decode("utf-8", errors="replace")
        rows = _parse_investegate_page(text)
        if len(rows) < PAGE_ROW_THRESHOLD:
            log.info("investegate page %d yielded %d rows — stopping", page, len(rows))
            all_rows.extend(rows)
            break
        all_rows.extend(rows)
        # check if oldest row on page is past the cutoff
        oldest = next((r["_dt"] for r in reversed(rows) if r["_dt"]), None)
        if oldest and oldest < cutoff:
            log.info("investegate page %d's oldest row (%s) past cutoff — stopping", page, oldest)
            break
        time.sleep(POLITE_DELAY_S)
    # drop anything outside the window
    in_window = [r for r in all_rows if r["_dt"] is not None and r["_dt"] >= cutoff]
    return in_window


# -------- LSE alldata metadata --------

def _alldata(tidm: str) -> Optional[dict]:
    """Hit LSE's /instruments/alldata for TIDM metadata, cached 24h."""
    safe = re.sub(r"[^A-Z0-9]", "_", tidm.upper())
    cache_path = LSE_CACHE / f"{safe}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            ts = cached.get("_fetched_at")
            if ts:
                fetched = datetime.fromisoformat(ts)
                if (datetime.now(timezone.utc) - fetched).total_seconds() < 86400:
                    return cached.get("data")
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    url = LSE_ALLDATA.format(tidm=tidm)
    status, body = _http_get(url, accept="application/json")
    data: Optional[dict] = None
    if status == 200 and body:
        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = None
    try:
        cache_path.write_text(json.dumps({
            "_fetched_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "data": data,
        }))
    except OSError:
        pass
    return data


def _extract_alldata_fields(data: Optional[dict]) -> dict:
    """Extract the fields we care about from LSE alldata.

    Verified response shape (2026-04-14 against TIDM=GSK):
      - flat object with keys: isin, sedol, issuername, description, sectorcode,
        subsectorcode, currency, marketcapitalization (full GBX pennies ×1),
        market (MAINMARKET/AIM), tradingstatus, category (EQUITY/ETF/...).
      - marketcapitalization is in GBX-pennies × shares (i.e. full pence value),
        so dividing by 1e8 converts to GBP millions (e.g. 88,067,211,298 -> 880.67 GBP mm? No —
        actually 88,067,211,298 pence == £880.67m -> but GSK market cap ≈ £88bn,
        so the field is already full-pounds, not pence). Empirically confirmed:
        the number matches the company's market cap in GBP, not GBX. Divide by 1e6.
    """
    out = {"isin": None, "sedol": None, "issuer_name": None,
           "sector_code": None, "market_cap_gbp_mm": None,
           "currency": None, "market_tier": None, "category": None,
           "trading_status": None}
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
    out["trading_status"] = data.get("tradingstatus") or data.get("tradingstatuscode")
    mc = data.get("marketcapitalization") or data.get("marketCapitalization") or data.get("marketCap")
    if mc:
        try:
            v = float(mc)
            # LSE returns market cap in full GBP units (verified against GSK).
            out["market_cap_gbp_mm"] = round(v / 1e6, 2)
        except (TypeError, ValueError):
            pass
    return out


# -------- signal assembly --------

def _classify_headline(headline: str) -> Optional[dict]:
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


def _make_signal_id(tidm: str, source_date: str, signal_type: str, headline: str) -> str:
    h = hashlib.sha256(f"{tidm}|XLON|{source_date}|{signal_type}|{headline}".encode("utf-8")).hexdigest()
    return h[:32]


def _content_hash(tidm: str, headline: str, url: str) -> str:
    return hashlib.sha256(f"{tidm}|{headline}|{url}".encode("utf-8")).hexdigest()[:24]


# GBP -> USD rough conversion for the market cap floor.
# Kept as a module constant; pipeline_runner or a daily FX-update job can
# overwrite this. Triage uses usd market cap; getting this roughly right
# (1.25 ± 10%) is enough for a $300M floor.
GBP_TO_USD = 1.27


def _rubric_scores_from_strength(strength: int, signal_type: str) -> dict:
    """
    Seed rubric scores from headline classification. pipeline_runner applies
    D-002 caps; deep-dives skill refines these.

    7 dimensions, each 0-5:
      signal_strength, catalyst_clarity, info_asymmetry, risk_reward,
      edge_decay, liquidity, catalyst_timeline
    """
    # Defaults tuned to category
    base = {
        "signal_strength": strength,
        "catalyst_clarity": 3,
        "info_asymmetry": 2,    # LSE is widely watched — asymmetry is modest
        "risk_reward": 3,
        "edge_decay": 3,
        "liquidity": 4,         # LSE main-market is liquid
        "catalyst_timeline": 3,
    }
    if signal_type in ("takeover_firm_offer", "scheme_sanction"):
        base["catalyst_clarity"] = 5
        base["catalyst_timeline"] = 5
    elif signal_type == "takeover_possible_offer":
        base["catalyst_clarity"] = 4
        base["catalyst_timeline"] = 3
    elif signal_type == "profit_warning":
        base["catalyst_clarity"] = 4
        base["catalyst_timeline"] = 5   # same-day reaction
        base["edge_decay"] = 5
    elif signal_type == "jorc_resource_update":
        base["info_asymmetry"] = 3
        base["catalyst_timeline"] = 2
    elif signal_type == "aim_suspension":
        base["catalyst_clarity"] = 5
        base["catalyst_timeline"] = 5
        base["liquidity"] = 1   # suspended! override liquidity
    return base


def _build_signal(row: dict, cls: dict, meta: dict, scan_date: str) -> dict:
    tidm = row["tidm"]
    source_date = row["date"]
    headline = row["headline"]
    url = row["url"]

    market_cap_usd_mm = None
    mc_gbp = meta.get("market_cap_gbp_mm")
    if mc_gbp:
        market_cap_usd_mm = round(mc_gbp * GBP_TO_USD, 2)

    signal = {
        "upstream_system_id": "tool-2-non-us-primary",
        "signal_id": _make_signal_id(tidm, source_date or "", cls["signal_type"], headline),
        "ticker_local": tidm,
        "mic": "XLON",
        "ticker_plus_mic": f"{tidm}.XLON",
        "figi": None,
        "issuer_figi": None,
        "company_name_en": meta.get("issuer_name") or row["company_name"],
        "isin": meta.get("isin"),
        "sedol": meta.get("sedol"),
        "sector_code": meta.get("sector_code"),
        "market_tier": meta.get("market_tier"),      # MAINMARKET | AIM
        "instrument_category": meta.get("category"), # EQUITY only survives triage
        "trading_status": meta.get("trading_status"),
        "market_cap_usd_mm": market_cap_usd_mm,
        "market_cap_gbp_mm": mc_gbp,
        "exchange": "LSE",
        "country": "GB",
        "signal_type": cls["signal_type"],
        "signal_category": cls["signal_category"],
        "thesis_direction": cls["thesis_direction"],
        "strength_estimate": cls["strength_estimate"],
        "source_url": url,
        "source_content_hash": _content_hash(tidm, headline, url),
        "source_date": source_date,
        "scan_date": scan_date,
        "translation_confidence": "n/a",
        "rubric_scores": _rubric_scores_from_strength(cls["strength_estimate"], cls["signal_type"]),
        "raw_data": {
            "headline": headline,
            "supplier": row.get("supplier"),
            "supplier_label": row.get("supplier_label"),
            "company_name_raw": row["company_name"],
            "date_str": row["date_str"],
            "pattern_matched": cls.get("_pattern_matched"),
        },
        "_scanner": "lse_rns",
    }
    return signal


# -------- public API --------

def fetch_raw_signals(window_days: int = 7) -> list[dict]:
    """
    Enumerate RNS announcements from investegate within `window_days`, classify
    by headline, attach LSE issuer metadata, and return signals in common schema.

    Does NOT call OpenFIGI (pipeline_runner.resolve_entity does).
    Does NOT apply triage (pipeline_runner.triage does).
    """
    scan_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = _fetch_investegate_pages(window_days)
    log.info("investegate: %d rows in %d-day window", len(rows), window_days)

    # Keep only RNS + RNSREACH + PRN (actual regulatory filings; drop news wires unless explicitly RNS)
    ALLOWED_SUPPLIERS = {"RNS", "RNSREACH", "RNS_REACH", "PRN", "BUSINESSWIRE", "BUSINESS_WIRE", "REACH"}
    rns_rows = [r for r in rows if r.get("supplier", "").replace("-", "").replace("_", "") in
                {s.replace("-", "").replace("_", "") for s in ALLOWED_SUPPLIERS}]

    signals: list[dict] = []
    tidms_seen: set[str] = set()
    for row in rns_rows:
        cls = _classify_headline(row["headline"])
        if cls is None:
            # Uninteresting RNS filing — pipeline triage would reject anyway,
            # but dropping here saves work and keeps raw output focused.
            continue
        tidm = row["tidm"]
        # Alldata lookup — 1 per TIDM per day (cached)
        if tidm not in tidms_seen:
            tidms_seen.add(tidm)
        meta = _extract_alldata_fields(_alldata(tidm))
        # Drop non-equity instrument categories (bonds, ETFs, gilts). If alldata
        # returned nothing we keep the signal — pipeline_runner's OpenFIGI
        # resolution will filter via security_type.
        cat = (meta.get("category") or "").upper()
        if cat and cat != "EQUITY":
            log.debug("skip non-equity tidm=%s cat=%s", tidm, cat)
            continue
        signal = _build_signal(row, cls, meta, scan_date)
        signals.append(signal)
    log.info("lse_rns: %d classified signals emitted", len(signals))
    return signals


# -------- CLI for manual testing --------

def _main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip LSE alldata lookup (faster — skip issuer metadata)")
    args = parser.parse_args()
    if args.dry_run:
        # Monkeypatch _alldata to skip network
        global _alldata
        def _noop(tidm):
            return None
        _alldata = _noop  # type: ignore
    signals = fetch_raw_signals(window_days=args.window)
    print(json.dumps(signals, indent=2))


if __name__ == "__main__":
    _main()
