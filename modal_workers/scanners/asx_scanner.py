"""
ASX (Australia) announcement scanner -- Modal port of tools/asx_scanner.py.

Preserved from v1 (byte-equivalent where relevant):
  - Per-ticker Markit Digital announcements API
    (https://asx.api.markitdigital.com/asx-research/1.0/companies/{ticker}/announcements).
  - Browser User-Agent header ("Mozilla/5.0") -- endpoint is public, no auth.
  - ASX_TITLE_RULES classifier: 30+ regexes mapping headlines to (signal_type,
    strength, thesis_direction). Price-sensitive flag bumps strength by 1 (cap 5).
  - Concurrent fetch via ThreadPoolExecutor (MAX_CONCURRENT_REQUESTS=10).
  - Rotation checkpoint: resume from last-scanned index across runs when budget
    exhausted. Preserves universe-size change detection (reset if size changed).
  - Window cutoff: 7 days by default (overridable via cfg.config.window_days).
  - Universe CSV source: asx.com.au ASXListedCompanies.csv (cached).

Deviations from v1:
  - No on-disk working/signals paths; universe + rotation state live in Supabase
    Storage (scanner-caches/asx/universe.json + rotation.json).
  - No yfinance market-cap floor on the universe. v1 filtered to >=$300M USD via
    per-ticker yfinance.info lookups -- that dependency + latency is incompatible
    with a 60s Modal budget. All listed tickers are in scope; downstream auto-caps
    + the convergence layer gate mcap-visible. Flagged for Phase 3 mcap_cache
    port (mirrors the edgar deviation).
  - Universe refresh cadence: weekly (7d TTL). Fetched inline on cache miss via
    urllib; no yfinance imports.
  - source_content_hash now carries the spec.md sha256:<64hex> prefix (v1 used a
    sha1 string without prefix) -- required for reactor convergence keying.
  - Scanner emits unified Signal dataclass; no direct row-shape concerns (rubric +
    scoring_profile resolution handled by run_scanner from cfg.signal_type_profile_map).
  - Boilerplate filter (is_boilerplate("ASX", headline)) drops v1-classified
    substantial_holder_* and director's interest notices before a Signal is built
    -- the v1 scanner emitted them and relied on downstream filtering.
  - Best-effort OpenFIGI resolution on (ticker, XASX); cache routed through
    Supabase Storage.
  - Wall-clock budget from cfg.timeout_soft_s (default 60s) with 8s headroom for
    cache writes + entity resolution; partial emits on budget hit.

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
    - No auth required (public endpoint).
    - Budget-guards via cfg.timeout_soft_s (partial status if exhausted).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from modal_workers.shared.boilerplate_filters import is_boilerplate
from modal_workers.shared.scanner_base import Signal, ScannerResult
from modal_workers.shared.supabase_client import EntityHints, ScannerConfig, SupabaseClient

log = logging.getLogger(__name__)

NAME = "asx_scanner"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ANNOUNCEMENTS_API = "https://asx.api.markitdigital.com/asx-research/1.0/companies/{ticker}/announcements"
UNIVERSE_CSV_URL = "https://www.asx.com.au/asx/research/ASXListedCompanies.csv"
USER_AGENT = "Mozilla/5.0"

REQUEST_TIMEOUT_SECONDS = 10
MAX_RETRIES = 2
MAX_CONCURRENT_REQUESTS = 10
UNIVERSE_TTL_SECONDS = 7 * 24 * 3600  # weekly refresh
DEFAULT_WINDOW_DAYS = 7

# ---------------------------------------------------------------------------
# Classification rules (verbatim from v1 ASX_TITLE_RULES)
# ---------------------------------------------------------------------------

ASX_TITLE_RULES: List[Tuple[re.Pattern, str, int, str]] = [
    (re.compile(r"\btakeover\b.*\b(offer|bid)\b", re.I), "takeover_offer", 5, "long"),
    (re.compile(r"\bscheme of arrangement\b", re.I), "scheme_of_arrangement", 5, "long"),
    (re.compile(r"\bproposal\b.*\bacqui(re|sition)\b", re.I), "acquisition_proposal", 4, "long"),
    (re.compile(r"\b(merger|merging)\b", re.I), "merger_agreement", 4, "neutral"),
    (re.compile(r"\b(profit|earnings)\s+(upgrade|guidance).*\b(above|beat|exceed|strong|materially higher)\b", re.I),
     "guidance_upgrade", 4, "long"),
    (re.compile(r"\b(profit|earnings)\s+(downgrade|warning)\b", re.I),
     "profit_warning", 4, "short"),
    (re.compile(r"\b(?:materially\s+)?(?:below|lower\s+than)\s+(?:guidance|consensus|expectations)", re.I),
     "profit_warning", 4, "short"),
    (re.compile(r"\brevised\s+(?:guidance|outlook)\b", re.I), "guidance_revision", 3, "neutral"),
    (re.compile(r"\bitems impacting\b", re.I), "impact_on_results", 4, "short"),
    (re.compile(r"\bimpairment\b.*\b(charge|loss|write-?down)\b", re.I),
     "impairment_loss", 4, "short"),
    (re.compile(r"\b(goodwill|asset)\s+(impairment|write-?down)\b", re.I),
     "impairment_loss", 4, "short"),
    (re.compile(r"\brestat(ement|ed)\b.*\b(accounts|results|financial)\b", re.I),
     "financial_restatement", 5, "short"),
    (re.compile(r"\b(preliminary final report|appendix\s*4e)\b", re.I),
     "preliminary_final_report", 3, "neutral"),
    (re.compile(r"\b(half year|half-year|appendix\s*4d)\b.*\bresults?\b", re.I),
     "half_year_report", 3, "neutral"),
    (re.compile(r"\b(placement|institutional placement)\b", re.I),
     "institutional_placement", 3, "short"),
    (re.compile(r"\bentitlement offer\b|\brights issue\b", re.I),
     "rights_issue", 3, "short"),
    (re.compile(r"\bshare purchase plan\b|\bspp\b", re.I),
     "share_purchase_plan", 2, "neutral"),
    (re.compile(r"\bcapital raising\b", re.I), "capital_raising", 3, "short"),
    (re.compile(r"\bon-?market\s+buy-?back\b|\bshare\s+buy-?back\b", re.I),
     "share_buyback", 3, "long"),
    (re.compile(r"\bbecoming\s+a\s+substantial\s+holder\b|\bform\s*603\b", re.I),
     "substantial_holder_initial", 3, "long"),
    (re.compile(r"\bceasing\s+to\s+be\s+a\s+substantial\s+holder\b|\bform\s*605\b", re.I),
     "substantial_holder_ceasing", 3, "short"),
    (re.compile(r"\bchange\s+(?:in|of)\s+substantial\s+holder\b|\bform\s*604\b", re.I),
     "substantial_holder_change", 2, "neutral"),
    (re.compile(r"\btrading\s+halt\b", re.I), "trading_halt", 3, "neutral"),
    (re.compile(r"\btrading\s+suspension\b|\bsuspended\s+from\s+quotation\b", re.I),
     "trading_suspension", 4, "short"),
    (re.compile(r"\b(jorc|drill(?:ing)?\s+results)\b", re.I),
     "jorc_drilling_results", 2, "neutral"),
    (re.compile(r"\bresource\s+(?:upgrade|update|estimate)\b", re.I),
     "jorc_resource_update", 3, "long"),
    (re.compile(r"\bappendix\s*4c\b", re.I), "appendix_4c_cashflow", 2, "neutral"),
    (re.compile(r"\bspecial\s+dividend\b", re.I), "special_dividend", 3, "long"),
    (re.compile(r"\bdividend\s+(cut|reduction|suspension)\b", re.I),
     "dividend_cut", 4, "short"),
    (re.compile(r"\bgoing concern\b", re.I), "going_concern_warning", 5, "short"),
    (re.compile(r"\bcovenant\s+(breach|waiver)\b", re.I), "covenant_breach", 5, "short"),
    (re.compile(r"\b(administration|receivership|voluntary administrator)\b", re.I),
     "administration_or_receivership", 5, "short"),
    (re.compile(r"\bmaterial\s+contract\b", re.I), "material_contract", 3, "long"),
]


def _classify(headline: str, is_price_sensitive: bool
              ) -> Optional[Tuple[str, int, str, str]]:
    """Match headline against ASX_TITLE_RULES. Price-sensitive bumps strength by 1
    (cap 5). Returns (signal_type, strength, direction, matched_pattern) or None."""
    if not headline:
        return None
    for pat, stype, strength, direction in ASX_TITLE_RULES:
        if pat.search(headline):
            if is_price_sensitive and strength < 5:
                strength += 1
            return stype, strength, direction, pat.pattern
    return None


# ---------------------------------------------------------------------------
# Universe fetch (weekly-cached via Supabase Storage)
# ---------------------------------------------------------------------------

def _fetch_universe_csv() -> List[Dict[str, str]]:
    """Fetch ASX listed-companies CSV and parse into [{ticker, name, gics}, ...]."""
    req = urllib.request.Request(UNIVERSE_CSV_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read().decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(data))
    rows: List[Dict[str, str]] = []
    for r in reader:
        if len(r) < 3:
            continue
        name, ticker, gics = r[0], r[1], r[2]
        if not ticker or not ticker.strip():
            continue
        t_up = ticker.strip().upper()
        if t_up in ("ASX CODE", "ASX_CODE") or t_up.startswith("ASX LISTED"):
            continue
        rows.append({"ticker": t_up, "name": name.strip(), "gics": gics.strip()})
    return rows


def _load_universe(client: SupabaseClient) -> List[Dict[str, str]]:
    """Load ASX universe from scanner-caches/asx/universe.json; refresh if stale
    or missing. Returns [] on failure (scan will emit error warning)."""
    raw = client.read_cache("asx", "universe.json")
    if raw is not None:
        try:
            cached = json.loads(raw)
            asof_str = cached.get("as_of", "")
            asof = datetime.fromisoformat(asof_str.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - asof).total_seconds()
            if age < UNIVERSE_TTL_SECONDS:
                return cached.get("tickers", []) or []
        except (ValueError, KeyError):
            pass

    try:
        tickers = _fetch_universe_csv()
    except Exception as e:  # noqa: BLE001
        log.warning("asx_scanner: universe CSV fetch failed: %s", e)
        # Try to use stale cache as last resort.
        if raw is not None:
            try:
                cached = json.loads(raw)
                return cached.get("tickers", []) or []
            except (ValueError, UnicodeDecodeError):
                pass
        return []

    payload = {
        "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": UNIVERSE_CSV_URL,
        "tickers": tickers,
    }
    try:
        client.write_cache("asx", "universe.json",
                           json.dumps(payload).encode("utf-8"),
                           content_type="application/json")
    except Exception as e:  # noqa: BLE001
        log.warning("asx_scanner: universe cache write failed: %s", e)
    return tickers


# ---------------------------------------------------------------------------
# Rotation state (Supabase Storage-backed)
# ---------------------------------------------------------------------------

def _load_rotation(client: SupabaseClient) -> Dict[str, Any]:
    raw = client.read_cache("asx", "rotation.json")
    if raw is None:
        return {"last_index": 0, "universe_size": 0}
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {"last_index": 0, "universe_size": 0}


def _save_rotation(client: SupabaseClient, last_index: int, universe_size: int) -> None:
    payload = {
        "last_index": last_index,
        "universe_size": universe_size,
        "updated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        client.write_cache("asx", "rotation.json",
                           json.dumps(payload).encode("utf-8"),
                           content_type="application/json")
    except Exception as e:  # noqa: BLE001
        log.warning("asx_scanner: rotation cache write failed: %s", e)


# ---------------------------------------------------------------------------
# Announcements fetch (per-ticker, with retry)
# ---------------------------------------------------------------------------

def _fetch_announcements(ticker: str) -> Optional[Dict[str, Any]]:
    """Fetch announcements JSON for one ticker; None on failure. Retries 429/503."""
    url = ANNOUNCEMENTS_API.format(ticker=ticker)
    for attempt in range(MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                data = resp.read()
            return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            return None
        except Exception:
            if attempt < MAX_RETRIES:
                time.sleep(0.5)
                continue
            return None
    return None


def _parse_iso_datetime(iso: str) -> Optional[datetime]:
    """Parse the Markit Digital announcement date string to UTC datetime.
    The feed returns ISO-8601 with a trailing Z or an offset; convert to UTC."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------

def _build_signal(entry: Dict[str, str], ann: Dict[str, Any],
                  display_name: str, scan_date: datetime,
                  ) -> Optional[Signal]:
    ticker = entry["ticker"]
    headline = (ann.get("headline") or "").strip()
    if not headline:
        return None

    # Drop boilerplate (substantial holder, director interest, etc.) BEFORE
    # running the classifier so those patterns don't produce noisy signals.
    if is_boilerplate("ASX", headline):
        return None

    is_price_sensitive = bool(ann.get("isPriceSensitive"))
    cls = _classify(headline, is_price_sensitive)
    if cls is None:
        return None
    signal_type, strength, direction, matched_pattern = cls

    date_str = ann.get("date", "")
    source_date = _parse_iso_datetime(date_str) or scan_date

    document_key = ann.get("documentKey") or ""
    announcement_id = ann.get("announcementId") or document_key
    file_url = ann.get("url") or ann.get("fileUrl")

    # signal_id: stable on (ticker, MIC, date, document_key).
    signal_id = hashlib.sha1(
        f"{ticker}|XASX|{date_str}|{document_key}".encode("utf-8")
    ).hexdigest()[:32]

    # source_content_hash: spec.md §3.4 format sha256:<64hex>.
    source_content_hash = (
        f"sha256:{hashlib.sha256(f'asx:{ticker}:{document_key}'.encode()).hexdigest()}"
    )

    raw_payload: Dict[str, Any] = {
        "ticker": ticker,
        "announcement_id": announcement_id,
        "headline": headline,
        "release_type": ann.get("announcementType") or "",
        "file_url": file_url,
        "announcement_time": date_str,
        "is_price_sensitive": is_price_sensitive,
        "document_key": document_key,
        "file_size": ann.get("fileSize"),
        "num_pages": ann.get("numPages"),
        "company_name": display_name,
        "gics": entry.get("gics"),
        "matched_pattern": matched_pattern,
    }

    # Best-effort FIGI resolution on (ticker, XASX).
    issuer_figi: Optional[str] = None
    try:
        from modal_workers.shared.openfigi_resolver import resolve_ticker_mic
        res = resolve_ticker_mic(ticker, "XASX")
        if res.resolved:
            issuer_figi = res.issuer_figi
    except Exception:
        pass

    entity_hints = EntityHints(
        issuer_figi=issuer_figi,
        ticker=ticker,
        mic="XASX",
        name=display_name or entry.get("name") or ticker,
        country="AU",
    )

    return Signal(
        signal_id=signal_id,
        source_content_hash=source_content_hash,
        source_date=source_date,
        scan_date=scan_date,
        signal_type=signal_type,
        raw_payload=raw_payload,
        source_url=file_url,
        issuer_figi=issuer_figi,
        entity_hints=entity_hints,
        thesis_direction=direction,
        strength_estimate=strength,
    )


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    client = SupabaseClient()

    # Route openfigi cache reads/writes through Supabase Storage.
    try:
        from modal_workers.shared.openfigi_resolver import set_cache_backend
        set_cache_backend(*client.openfigi_cache_backend())
    except Exception as e:  # noqa: BLE001
        log.warning("asx_scanner: openfigi cache wiring failed: %s", e)

    scan_date = datetime.now(timezone.utc)
    window_days = int(cfg.config.get("window_days", DEFAULT_WINDOW_DAYS))
    cutoff = scan_date - timedelta(days=window_days)

    warnings: List[str] = []
    signals: List[Signal] = []
    seen_hashes: set[str] = set()

    # --- Universe load ---
    universe = _load_universe(client)
    if not universe:
        return ScannerResult(
            scanner=NAME, status="error", signals=[],
            warnings=["asx universe unavailable (CSV fetch failed, no cached copy)"],
            fetched_records=0,
        )

    # --- Rotation: resume from last-scanned index. ---
    rotation = _load_rotation(client)
    start_idx = 0
    if rotation.get("universe_size") == len(universe):
        last_idx = rotation.get("last_index")
        if isinstance(last_idx, int):
            start_idx = last_idx % len(universe)
    rotated = universe[start_idx:] + universe[:start_idx]

    # --- Wall-clock budget from cfg.timeout_soft_s with headroom. ---
    budget = max(10, cfg.timeout_soft_s - 8)
    t0 = time.time()
    fetched = 0
    last_completed_offset = 0
    fetch_results: List[Tuple[int, Dict[str, str], Dict[str, Any]]] = []

    # --- Concurrent per-ticker fetch ---
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS) as ex:
        future_to_idx: Dict[Any, Tuple[int, Dict[str, str]]] = {}
        for offset, entry in enumerate(rotated):
            if time.time() - t0 > budget - 5:
                break
            fut = ex.submit(_fetch_announcements, entry["ticker"])
            future_to_idx[fut] = (offset, entry)

        for fut in as_completed(future_to_idx):
            offset, entry = future_to_idx[fut]
            try:
                doc = fut.result(timeout=REQUEST_TIMEOUT_SECONDS + 2)
            except Exception:
                doc = None
            fetched += 1
            last_completed_offset = max(last_completed_offset, offset)
            if doc is not None:
                fetch_results.append((offset, entry, doc))
            if time.time() - t0 > budget:
                warnings.append(
                    f"wall-clock budget ({budget}s) exceeded at offset {offset}"
                )
                break

    # Checkpoint rotation BEFORE building signals so a later exception doesn't
    # strand the cursor. Next run picks up one past the highest-completed offset.
    next_index = (start_idx + last_completed_offset + 1) % len(universe)
    _save_rotation(client, next_index, len(universe))

    # --- Classify + build signals ---
    classified = 0
    for _, entry, doc in fetch_results:
        data = doc.get("data") or {}
        items = data.get("items") or []
        display_name = data.get("displayName") or entry.get("name") or ""

        for ann in items:
            date_str = ann.get("date", "")
            source_dt = _parse_iso_datetime(date_str)
            if source_dt is None or source_dt < cutoff:
                continue

            sig = _build_signal(entry, ann, display_name, scan_date)
            if sig is None:
                continue
            if sig.source_content_hash in seen_hashes:
                continue
            seen_hashes.add(sig.source_content_hash)
            signals.append(sig)
            classified += 1

    elapsed = time.time() - t0
    log.info("asx_scanner: fetched=%d classified=%d elapsed=%.1fs", fetched, classified, elapsed)

    status = "partial" if warnings else "ok"
    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=signals,
        warnings=warnings,
        fetched_records=fetched,
    )
