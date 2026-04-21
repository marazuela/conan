"""
SEDAR+ (Canada) scanner — Modal port of tools/sedar_plus_scanner.py.

Endpoint strategy (preserved from v1):
  SEDAR+ direct (sedarplus.ca) is blocked by a PerfDrive JavaScript challenge
  on raw HTTP requests; a headless browser is required to hit it. Instead we
  use the same universe-enumeration pattern as v1:

    1. Load the CA universe (TSX + TSXV, market cap >= $300M USD) from
       scanner-caches/sedar/ca_universe.json (25 tickers per registry note).
    2. For each ticker, call yfinance.Ticker('<SYM>.TO' | '<SYM>.V').news
       to get the latest aggregator-syndicated headlines (StockStory, Zacks,
       Reuters, CP, Yahoo Finance).
    3. Classify each headline via SEDAR_TITLE_RULES, emit Signal objects in
       the Modal contract shape.

Preserved from v1 (byte-equivalent where relevant):
  - SEDAR_TITLE_RULES classification table (pattern -> signal_type/strength/direction)
  - _category_for mapping (unused downstream but kept in raw_payload for parity)
  - Per-ticker throttle (0.3s default)
  - window_days cutoff (default 7)

Deviations from v1:
  - No OUT_FILE, no unified-envelope normalizer, no rubric_scores_sedar attachment.
    run_scanner scores via shared rubric_engine on insert.
  - Universe loaded from Supabase Storage via read_cache("sedar", "ca_universe.json"),
    not a local file. Scanner reads on every run (first-run Phase 3 contract); the
    registry note states the file is seeded at working/ca_universe.json — Pedro will
    upload it to scanner-caches/sedar/ca_universe.json before first run.
  - source_content_hash now carries the spec.md §3.4 "sha256:<64hex>" prefix.
  - thesis_direction normalized: v1 emitted "unknown" for ambiguous cases; Signal
    contract accepts only long/short/neutral/None, so "unknown" is mapped to None.
  - sedar_chrome_supplement is NOT ported — v1 scan() does not delegate to it
    (it's a separate once-daily operator-driven tool). If/when Pedro wants the
    Chrome-supplement ingestion path in Modal, it would be a second scanner.

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
    - No auth required (SEDAR+ public; yfinance unauthenticated).
    - Uses cfg.timeout_soft_s (60s default) as wall-clock budget.
    - Raises MissingAuthError if ca_universe.json is absent from the cache
      (graceful-degrade contract — Pedro seeds the cache, then re-runs).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from modal_workers.shared.boilerplate_filters import is_boilerplate
from modal_workers.shared.scanner_base import MissingAuthError, Signal, ScannerResult
from modal_workers.shared.supabase_client import EntityHints, ScannerConfig, SupabaseClient

log = logging.getLogger(__name__)

NAME = "sedar_plus_scanner"

REQUEST_THROTTLE_SECONDS = 0.3
DEFAULT_WINDOW_DAYS = 7

# ---------------------------------------------------------------------------
# Classification table — verbatim from v1 SEDAR_TITLE_RULES.
# Pattern -> (signal_type, strength_estimate, thesis_direction)
# ---------------------------------------------------------------------------

SEDAR_TITLE_RULES: List[Tuple[re.Pattern, str, int, str]] = [
    # M&A
    (re.compile(r"\btake(-?over)?\s+bid\b", re.I), "takeover_bid_circular", 5, "long"),
    (re.compile(r"\bplan of arrangement\b", re.I), "plan_of_arrangement", 5, "long"),
    (re.compile(r"\bproposal\b.*\bacqui(re|sition)\b", re.I), "acquisition_proposal", 4, "long"),
    (re.compile(r"\b(merger|merging|combining)\b.*\b(agreement|deal)\b", re.I),
     "merger_agreement", 4, "unknown"),
    (re.compile(r"\b(definitive|amalgamation)\s+agreement\b", re.I),
     "merger_agreement", 4, "unknown"),
    (re.compile(r"\bdirectors?\s+circular\b", re.I), "directors_circular", 4, "unknown"),

    # Material change
    (re.compile(r"\bmaterial\s+change\s+report\b", re.I), "material_change_report", 4, "unknown"),
    (re.compile(r"\bmaterial\s+change\b", re.I), "material_change_report", 3, "unknown"),

    # Guidance
    (re.compile(r"\b(profit|earnings|revenue)\s+(warning|shortfall)\b", re.I),
     "guidance_downgrade", 5, "short"),
    (re.compile(r"\bguidance\b.*\b(lowered|cut|reduced|withdrawn)\b", re.I),
     "guidance_downgrade", 4, "short"),
    (re.compile(r"\bguidance\b.*\b(raised|upgraded|increased)\b", re.I),
     "guidance_upgrade", 4, "long"),
    (re.compile(r"\b(?:materially\s+)?(?:below|lower\s+than)\s+(?:guidance|consensus|expectations)\b", re.I),
     "guidance_downgrade", 4, "short"),

    # Early-warning / ownership
    (re.compile(r"\bearly\s+warning\s+report\b", re.I), "early_warning_report", 3, "long"),
    (re.compile(r"\b(10|twenty)%?\s+(?:or\s+more\s+)?ownership\b", re.I),
     "early_warning_report", 3, "long"),

    # Technical reports
    (re.compile(r"\bNI\s*43-?101\b|\bmineral\s+resource\s+estimate\b", re.I),
     "ni43101_technical_report", 3, "long"),
    (re.compile(r"\b(maiden|increased?|updated)\s+mineral\s+resource\b", re.I),
     "ni43101_technical_report", 4, "long"),
    (re.compile(r"\bNI\s*51-?101\b|\breserves?\s+(report|evaluation|estimate)\b", re.I),
     "ni51101_reserves", 3, "unknown"),

    # Cease trade / MCTO
    (re.compile(r"\bmanagement\s+cease\s+trade\s+order\b|\bMCTO\b", re.I),
     "mcto_management_cease_trade", 5, "short"),
    (re.compile(r"\bcease\s+trade\s+order\b", re.I), "cease_trade_order", 5, "short"),

    # Impairment / restatement
    (re.compile(r"\b(impair(ment)?|write-?down|write-?off)\b.*\b(charge|loss|asset|goodwill)\b", re.I),
     "impairment_loss", 4, "short"),
    (re.compile(r"\brestat(ement|ed)\b.*\b(accounts|results|financial)\b", re.I),
     "financial_restatement", 5, "short"),

    # Capital / financing
    (re.compile(r"\bbought\s+deal\s+(financing|offering)\b", re.I),
     "bought_deal", 3, "short"),
    (re.compile(r"\bprivate\s+placement\b", re.I), "private_placement", 3, "short"),
    (re.compile(r"\bequity\s+(financing|offering)\b", re.I), "equity_financing", 3, "short"),
    (re.compile(r"\b(normal\s+course|substantial\s+issuer|share)\s+(issuer\s+)?bid\b", re.I),
     "share_buyback", 3, "long"),
    (re.compile(r"\bshare\s+buy-?back\b", re.I), "share_buyback", 3, "long"),

    # Dividend
    (re.compile(r"\bspecial\s+dividend\b", re.I), "special_dividend", 3, "long"),
    (re.compile(r"\bdividend.*\b(suspended|cancell?ed|deferred|cut)\b", re.I),
     "dividend_cut", 4, "short"),

    # Going-concern / distress
    (re.compile(r"\bgoing\s+concern\b", re.I), "going_concern_warning", 5, "short"),
    (re.compile(r"\b(debt|covenant)\s+(breach|default)\b", re.I), "covenant_breach", 5, "short"),
    (re.compile(r"\bCCAA\b|\bcompanies'\s+creditors\s+arrangement\s+act\b", re.I),
     "ccaa_filing", 5, "short"),
    (re.compile(r"\b(receiver|receivership|bankruptc(y|ies))\b", re.I),
     "administration_or_receivership", 5, "short"),

    # Reporting cadence (lower-value but flagged)
    (re.compile(r"\bmanagement('s)?\s+discussion\s+and\s+analysis\b|\bMD&A\b", re.I),
     "interim_mda", 2, "unknown"),
]


# Category mapping (kept from v1 for raw_payload parity; unused downstream).
def _category_for(signal_type: str) -> str:
    if signal_type in ("takeover_bid_circular", "plan_of_arrangement",
                       "acquisition_proposal", "merger_agreement", "directors_circular"):
        return "takeover"
    if signal_type in ("guidance_upgrade", "guidance_downgrade", "guidance_revision",
                       "material_change_report", "impairment_loss", "financial_restatement"):
        return "results"
    if signal_type in ("equity_financing", "bought_deal", "private_placement",
                       "share_buyback"):
        return "capital_structure"
    if signal_type in ("early_warning_report",):
        return "ownership"
    if signal_type in ("ni43101_technical_report", "ni51101_reserves"):
        return "resources"
    if signal_type in ("cease_trade_order", "mcto_management_cease_trade"):
        return "trading_status"
    if signal_type in ("special_dividend", "dividend_cut"):
        return "capital_return"
    if signal_type in ("going_concern_warning", "covenant_breach",
                       "administration_or_receivership", "ccaa_filing"):
        return "distress"
    if signal_type in ("interim_mda", "annual_mda", "proxy_circular"):
        return "reporting"
    return "other"


def _classify(headline: str) -> Optional[Tuple[str, int, str, str]]:
    for pat, stype, strength, direction in SEDAR_TITLE_RULES:
        if pat.search(headline):
            return stype, strength, direction, pat.pattern
    return None


def _normalize_direction(direction: str) -> Optional[str]:
    """v1 emitted 'unknown' for ambiguous cases; Signal contract wants long/short/neutral/None."""
    if direction in ("long", "short", "neutral"):
        return direction
    return None


# ---------------------------------------------------------------------------
# yfinance news fetch + field extraction (verbatim from v1)
# ---------------------------------------------------------------------------

def _fetch_news(yf, ticker: str, suffix: str) -> List[Dict[str, Any]]:
    sym = f"{ticker}{suffix}"
    try:
        t = yf.Ticker(sym)
        news = t.news or []
    except Exception as e:
        log.debug("sedar_plus_scanner: news fetch %s failed: %s", sym, e)
        return []
    return news


def _extract_fields(item: Dict[str, Any]) -> Tuple[str, str, datetime, str, str]:
    """Return (title, uuid, dt_utc, publisher, url) — normalises yfinance news item shape."""
    title = (item.get("title") or "").strip()
    uuid = item.get("uuid") or item.get("id") or ""
    ts = item.get("providerPublishTime") or item.get("pubDate") or 0
    try:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        dt = datetime.now(timezone.utc)
    publisher = (item.get("publisher") or item.get("provider") or "").strip()
    url = (item.get("link") or item.get("url") or "").strip()
    return title, uuid, dt, publisher, url


def _make_signal_id(ticker: str, mic: str, dt: datetime, uuid: str) -> str:
    h = hashlib.sha1(f"{ticker}|{mic}|{dt.strftime('%Y-%m-%dT%H:%M:%SZ')}|{uuid}".encode("utf-8"))
    return h.hexdigest()[:32]


# ---------------------------------------------------------------------------
# Universe loader — reads scanner-caches/sedar/ca_universe.json
# ---------------------------------------------------------------------------

def _load_universe(client: SupabaseClient) -> List[Dict[str, Any]]:
    raw = client.read_cache("sedar", "ca_universe.json")
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return []
    return data.get("tickers", []) or []


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------

def _build_signal(entry: Dict[str, Any], item: Dict[str, Any], cutoff: datetime,
                  scan_date: datetime) -> Optional[Signal]:
    title, uuid, dt, publisher, url = _extract_fields(item)
    if not title or dt < cutoff:
        return None

    # Boilerplate drop (SEDAR bucket in shared filters).
    if is_boilerplate("SEDAR", title):
        return None

    cls = _classify(title)
    if not cls:
        return None
    signal_type, strength, direction, matched_pat = cls

    ticker = entry["ticker"]
    board = entry.get("board", "tsx")
    mic = entry.get("mic", "XTSE")
    suffix = entry.get("suffix", ".TO")
    issuer_name = entry.get("name")

    # Best-effort FIGI resolution (lazy import — scanner emits either way).
    issuer_figi: Optional[str] = None
    try:
        from modal_workers.shared.openfigi_resolver import resolve_ticker
        # yfinance symbols are "<TICKER>.TO" / "<TICKER>.V"; OpenFIGI uses exchCode="CN"
        # for Canadian listings (covers TSX + TSXV). Graceful-degrade on miss.
        res = resolve_ticker(ticker, exch_code="CN")
        if res.resolved:
            issuer_figi = res.issuer_figi
    except Exception:
        pass

    sig_id = _make_signal_id(ticker, mic, dt, uuid)
    source_content_hash = (
        f"sha256:{hashlib.sha256(f'sedar:{ticker}:{uuid}:{title}'.encode()).hexdigest()}"
    )

    raw_payload: Dict[str, Any] = {
        "doc_id": uuid,
        "source_url": url or None,
        "ticker": ticker,
        "ticker_plus_mic": f"{ticker}.{mic}",
        "issuer_name": issuer_name,
        "company_name_local": issuer_name,
        "company_name_en": issuer_name,
        "filing_type": signal_type,
        "filed_at": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "headline": title,
        "publisher": publisher,
        "board": board,
        "suffix": suffix,
        "mic": mic,
        "exchange": "TSX" if board == "tsx" else "TSXV",
        "country": "CA",
        "signal_category": _category_for(signal_type),
        "matched_pattern": matched_pat,
        "market_cap_usd_mm": entry.get("market_cap_usd_mm"),
        "source_type": "yfinance_news_aggregator",
    }

    entity_hints = EntityHints(
        issuer_figi=issuer_figi,
        ticker=ticker,
        mic=mic,
        name=issuer_name,
        country="CA",
    )

    return Signal(
        signal_id=sig_id,
        source_content_hash=source_content_hash,
        source_date=dt,
        scan_date=scan_date,
        signal_type=signal_type,
        raw_payload=raw_payload,
        source_url=url or None,
        issuer_figi=issuer_figi,
        entity_hints=entity_hints,
        thesis_direction=_normalize_direction(direction),
        strength_estimate=strength,
    )


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    client = SupabaseClient()

    # Route OpenFIGI cache backend through Supabase Storage (before resolve_ticker calls).
    try:
        from modal_workers.shared.openfigi_resolver import set_cache_backend
        set_cache_backend(*client.openfigi_cache_backend())
    except Exception:
        pass

    universe = _load_universe(client)
    if not universe:
        # Graceful-degrade: Pedro seeds scanner-caches/sedar/ca_universe.json from
        # v1's working/ca_universe.json before first run. Until then, raise auth_required
        # so run_scanner emits the envelope without error noise.
        raise MissingAuthError(
            "CA universe cache missing — upload v1 working/ca_universe.json to "
            "scanner-caches/sedar/ca_universe.json (25 tickers above $300M USD floor).")

    window_days = int(cfg.config.get("window_days", DEFAULT_WINDOW_DAYS))
    max_tickers = cfg.config.get("max_tickers")
    throttle_seconds = float(cfg.config.get("throttle_seconds", REQUEST_THROTTLE_SECONDS))

    if max_tickers:
        universe = universe[: int(max_tickers)]

    # Lazy-import yfinance so the scanner module loads even if the dep is absent locally.
    try:
        import yfinance as yf  # type: ignore
        import warnings
        warnings.filterwarnings("ignore")
    except ImportError as e:
        raise MissingAuthError(
            "yfinance not installed in Modal image — SEDAR+ scanner relies on it for "
            "aggregator news feed (SEDAR+ direct is WAF-blocked).") from e

    scan_date = datetime.now(timezone.utc)
    cutoff = scan_date - timedelta(days=window_days)

    budget = max(10, cfg.timeout_soft_s - 5)
    scan_start = time.time()
    warnings_list: List[str] = []
    signals: List[Signal] = []
    seen_ids: set[str] = set()
    fetched = 0

    for entry in universe:
        if time.time() - scan_start > budget:
            warnings_list.append(
                f"wall-clock budget ({budget}s) exceeded after {fetched} tickers")
            break

        ticker = entry.get("ticker")
        suffix = entry.get("suffix", ".TO")
        if not ticker:
            continue

        news = _fetch_news(yf, ticker, suffix)
        fetched += 1
        if throttle_seconds:
            time.sleep(throttle_seconds)

        for item in news:
            try:
                sig = _build_signal(entry, item, cutoff, scan_date)
            except Exception as e:  # noqa: BLE001
                warnings_list.append(f"{ticker}: build_signal {type(e).__name__}: {e}")
                continue
            if sig is None:
                continue
            if sig.signal_id in seen_ids:
                continue
            seen_ids.add(sig.signal_id)
            signals.append(sig)

    status = "partial" if warnings_list else "ok"
    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=signals,
        warnings=warnings_list,
        fetched_records=fetched,
    )
