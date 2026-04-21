"""
TDnet (Tokyo Stock Exchange timely disclosure) scanner — Modal port of
tools/tdnet_scanner.py.

Preserved from v1 (byte-equivalent where relevant):
  - Endpoint: https://www.release.tdnet.info/inbs/I_list_{page:03d}_{YYYYMMDD}.html
    (50 rows/page, UTF-8, no auth).
  - Browser-ish User-Agent ("Mozilla/5.0 (compatible; Tool2-NonUS-Discovery/1.0)").
  - TDNET_TITLE_RULES regex table: pattern → (signal_type, signal_category,
    strength, thesis_direction, translation_confidence). Byte-equivalent to v1,
    including the 0.70 default / 0.92 (+ 0.90/0.88/0.85/0.75) confidence tags.
  - _ROW_RE HTML parser + html.unescape on titles/names.
  - kjPlace "東" (Tokyo-listed) filter.
  - JST (UTC+9) → UTC source_date conversion from (ymd, kjTime).
  - 50-row-page / zero-row-page pagination terminator.
  - MAX_PAGES_PER_DAY = 40 cap and 1.5s polite delay between pages/days.
  - Ticker form stays as v1 emitted: we pass the raw 4-digit or 5-char code to
    openfigi_resolver.resolve_ticker_mic; normalize_ticker applies the JP
    5-char-alpha ("469A0" → "469A") fix at the FIGI boundary (registry note —
    already landed in openfigi_resolver; do NOT re-implement here).

Deviations from v1:
  - No OUT_FILE; signals returned via ScannerResult for run_scanner plumbing.
  - Per-day HTML cache routed through Supabase Storage (scanner-caches/tdnet/
    {ymd}_p{page:03d}.html) so re-runs within a day avoid re-hammering TDnet.
    v1 had no persistent HTML cache.
  - source_content_hash now carries the spec.md §3.4 "sha256:<64hex>" prefix for
    convergence classification parity. v1 used a 24-char sha256 prefix without
    the "sha256:" tag.
  - Boilerplate filter (shared.boilerplate_filters.is_boilerplate("TDnet", ..))
    drops the per-exchange drop-list (自己株式の取得状況 / 役員の異動 / 定款の一部
    変更 / コーポレートガバナンスに関する報告書 / 独立役員届出書) BEFORE the
    classifier runs. v1 relied on downstream triage.
  - Best-effort OpenFIGI resolution on (raw_code, XTKS) via resolve_ticker_mic;
    openfigi cache wired through Supabase Storage at scan() entry. v1 deferred
    resolution entirely to pipeline_runner.
  - window_days defaults to 2 (v1 default), overridable via cfg.config.window_days.
  - scoring_profile routing lives in cfg.signal_type_profile_map (registry):
    tender_offer / tender_offer_correction / mbo_tender → merger_arb;
    impairment_loss / special_losses / profit_upgrade / profit_downgrade /
    article_324_filing → activist_governance; litigation_regulatory → litigation.
    scan() does not set scoring_profile explicitly; run_scanner + the map
    resolve it from signal_type (default_scoring_profile=activist_governance).
  - Wall-clock budget from cfg.timeout_soft_s (default 60s). Multi-day / multi-page
    fetch is the expensive leg; partial emits on budget hit.
  - No JPX market-cap attach. v1 Phase 2.1 / jpx_market_cap.py is a separate
    layer and not called from scan().

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
    - No auth required (public endpoint).
    - Budget-guards via cfg.timeout_soft_s (status="partial" on exhaustion).
"""

from __future__ import annotations

import hashlib
import html as _htmllib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from modal_workers.shared.boilerplate_filters import is_boilerplate
from modal_workers.shared.scanner_base import Signal, ScannerResult
from modal_workers.shared.supabase_client import EntityHints, ScannerConfig, SupabaseClient

log = logging.getLogger(__name__)

NAME = "tdnet_scanner"

# ---------------------------------------------------------------------------
# Constants (verbatim from v1)
# ---------------------------------------------------------------------------

USER_AGENT = "Mozilla/5.0 (compatible; Tool2-NonUS-Discovery/1.0)"
TDNET_URL_FMT = "https://www.release.tdnet.info/inbs/I_list_{page:03d}_{ymd}.html"
POLITE_DELAY_S = 1.5
FETCH_TIMEOUT = 20
MAX_PAGES_PER_DAY = 40      # 40*50 = 2000 — more than any real day
DEFAULT_WINDOW_DAYS = 2

# Title-pattern rules — ordered, first match wins.
# (pattern, signal_type, signal_category, strength, thesis_direction, translation_confidence)
# BYTE-EQUIVALENT TO v1 tools/tdnet_scanner.py. Do not modify without a PRD/DECISIONS
# update — strength + translation_confidence feed rubric_engine D-002 caps directly.
TDNET_TITLE_RULES: List[Tuple[re.Pattern, str, str, int, str, float]] = [
    # Guidance revision — direction disambiguated if the title contains 下方/下落 (down) or 上方/上振れ (up).
    (re.compile(r"業績予想.*(下方修正|下振れ)"), "profit_warning", "results", 5, "short", 0.92),
    (re.compile(r"業績予想.*(上方修正|上振れ)"), "profit_upgrade", "results", 4, "long", 0.92),
    # Generic guidance revision (direction not in title — D-002 caps apply)
    (re.compile(r"業績予想.*(修正|見直し|変更)"), "guidance_revision", "results", 4, "unknown", 0.70),
    # Variance between forecast and actual (通期予想と実績値との差異)
    (re.compile(r"予想と実績.*差異"), "forecast_variance", "results", 4, "unknown", 0.70),
    # Dividend changes
    (re.compile(r"配当予想.*(増配|上方修正)"), "dividend_increase", "shareholder", 3, "long", 0.90),
    (re.compile(r"配当予想.*(減配|無配|下方修正)"), "dividend_cut", "shareholder", 4, "short", 0.90),
    # TOB (tender offer) / MBO — takeover signals
    (re.compile(r"公開買付"), "tender_offer", "takeover", 5, "long", 0.92),
    (re.compile(r"ＭＢＯ|マネジメント・バイアウト|MBO"), "mbo_announcement", "takeover", 5, "long", 0.92),
    # Strategic M&A
    (re.compile(r"株式交換.*契約|合併契約|経営統合"), "merger_agreement", "takeover", 5, "unknown", 0.75),
    # Special losses / impairment (usually bearish, short-tag confidently)
    (re.compile(r"特別損失.*計上"), "impairment_loss", "results", 4, "short", 0.88),
    (re.compile(r"減損損失"), "impairment_loss", "results", 4, "short", 0.88),
    # Restatements / audit issues
    (re.compile(r"決算訂正|過年度.*訂正"), "restatement", "governance", 5, "short", 0.90),
    (re.compile(r"内部統制.*開示すべき重要な不備"), "internal_control_weakness", "governance", 5, "short", 0.90),
    # Share buybacks initiation (exec filings are boilerplate-filtered)
    (re.compile(r"自己株式.*取得.*(決議|取締役会)"), "buyback_initiation", "shareholder", 3, "long", 0.85),
    # Large secondary offering / dilutive capital raise
    (re.compile(r"新株発行|公募増資|第三者割当"), "equity_fundraise", "shareholder", 3, "short", 0.85),
    # Tanshin (quarterly / annual results brief) — widely watched; keep as watchlist
    (re.compile(r"決算短信"), "tanshin_results", "results", 3, "unknown", 0.70),
    # Stockholder lawsuit / enforcement action
    (re.compile(r"訴訟|課徴金|行政処分"), "litigation_regulatory", "governance", 4, "short", 0.85),
]


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

def _http_get(url: str) -> Tuple[int, bytes]:
    req = Request(url, headers={"User-Agent": USER_AGENT})
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
        log.warning("tdnet URL error %s: %s", url, e)
        return 0, b""


# ---------------------------------------------------------------------------
# HTML parse (verbatim regex from v1)
# ---------------------------------------------------------------------------

_ROW_RE = re.compile(
    r'<tr>\s*'
    r'<td[^>]*class="(?:odd|even)new-L kjTime"[^>]*>(?P<time>[^<]+?)</td>\s*'
    r'<td[^>]*class="(?:odd|even)new-M kjCode"[^>]*>(?P<code>[^<]+?)</td>\s*'
    r'<td[^>]*class="(?:odd|even)new-M kjName"[^>]*>(?P<name>[^<]+?)</td>\s*'
    r'<td[^>]*class="(?:odd|even)new-M kjTitle"[^>]*>\s*'
    r'(?:<a[^>]*href="(?P<pdf>[^"]+)"[^>]*>(?P<title>[^<]+)</a>|(?P<title2>[^<]+))'
    r'\s*</td>\s*'
    r'<td[^>]*class="(?:odd|even)new-M kjXbrl"[^>]*>(?P<xbrl>[^<]*)</td>\s*'
    r'<td[^>]*class="(?:odd|even)new-M kjPlace"[^>]*>(?P<place>[^<]*?)</td>',
    re.DOTALL,
)


def _parse_tdnet_page(text: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for m in _ROW_RE.finditer(text):
        d = m.groupdict()
        title = _htmllib.unescape(d["title"] or d.get("title2") or "").strip()
        rows.append({
            "time": d["time"].strip(),
            "code": d["code"].strip(),
            "name": _htmllib.unescape(d["name"]).strip().rstrip("\u3000 "),
            "title": title,
            "pdf": d.get("pdf") or "",
            "xbrl": (d["xbrl"] or "").strip(),
            "place": (d["place"] or "").strip().replace("\u3000", ""),
        })
    return rows


# ---------------------------------------------------------------------------
# Per-day fetch (paginated), with Supabase-backed HTML cache
# ---------------------------------------------------------------------------

def _fetch_page_with_cache(
    client: SupabaseClient, ymd: str, page: int
) -> Tuple[int, Optional[str]]:
    """Fetch one TDnet page; cache HTML in Supabase Storage keyed by (ymd, page).

    Returns (http_status, utf-8 text or None). HTTP status 200 with cached body
    reuses the cached copy. Any other status clears cache entry implicitly (not
    written). Cache lets re-runs within a day skip the network round-trip.
    """
    cache_key = f"{ymd}_p{page:03d}.html"
    cached = client.read_cache("tdnet", cache_key)
    if cached is not None:
        try:
            return 200, cached.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass  # fall through to live fetch

    url = TDNET_URL_FMT.format(page=page, ymd=ymd)
    status, body = _http_get(url)
    if status != 200 or not body:
        return status, None
    text = body.decode("utf-8", errors="replace")
    try:
        client.write_cache("tdnet", cache_key, body, content_type="text/html; charset=utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("tdnet: cache write failed for %s: %s", cache_key, e)
    return 200, text


def _fetch_day(
    client: SupabaseClient, ymd: str, *, budget_deadline: float
) -> Tuple[List[Dict[str, str]], bool]:
    """Fetch all pages for YYYYMMDD. Returns (rows, budget_hit).

    Stops on first non-200 or empty page (v1 termination semantics). Each row
    carries its page index for raw_payload."""
    all_rows: List[Dict[str, str]] = []
    budget_hit = False
    for page in range(1, MAX_PAGES_PER_DAY + 1):
        if time.time() > budget_deadline:
            budget_hit = True
            break
        status, text = _fetch_page_with_cache(client, ymd, page)
        if status != 200 or text is None:
            log.debug("tdnet %s p%d status=%s — stopping", ymd, page, status)
            break
        rows = _parse_tdnet_page(text)
        if not rows:
            break
        for r in rows:
            r["ymd"] = ymd
            r["page"] = str(page)
        all_rows.extend(rows)
        if len(rows) < 50:
            break  # last page
        # Polite only between live (non-cached) pages — cached pages are free.
        time.sleep(POLITE_DELAY_S)
    return all_rows, budget_hit


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _classify_title(title: str) -> Optional[Dict[str, Any]]:
    for pat, stype, cat, strength, direction, tconf in TDNET_TITLE_RULES:
        if pat.search(title):
            return {
                "signal_type": stype,
                "signal_category": cat,
                "strength_estimate": strength,
                "thesis_direction": direction,
                "translation_confidence": tconf,
                "pattern_matched": pat.pattern,
            }
    return None


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------

def _build_signal(
    row: Dict[str, str], scan_date: datetime
) -> Optional[Signal]:
    title = row.get("title") or ""
    if not title:
        return None

    # Drop TDnet boilerplate (自己株式の取得状況, 役員の異動, 定款の一部変更,
    # コーポレートガバナンスに関する報告書, 独立役員届出書) BEFORE classification.
    if is_boilerplate("TDnet", title):
        return None

    cls = _classify_title(title)
    if cls is None:
        return None

    ticker = row["code"].strip()  # keep raw form; FIGI boundary applies JP 5-char fix
    ymd = row["ymd"]
    t_str = row.get("time") or ""

    # JST → UTC. kjTime is "HH:MM" (24h).
    try:
        source_dt_jst = datetime.strptime(f"{ymd} {t_str}", "%Y%m%d %H:%M")
        source_dt_jst = source_dt_jst.replace(tzinfo=timezone(timedelta(hours=9)))
        source_date = source_dt_jst.astimezone(timezone.utc)
    except ValueError:
        source_date = scan_date

    pdf_url = row.get("pdf") or None
    if pdf_url and not pdf_url.startswith("http"):
        pdf_url = f"https://www.release.tdnet.info/inbs/{pdf_url}"

    xbrl_raw = row.get("xbrl") or ""
    xbrl_url: Optional[str] = None
    if xbrl_raw:
        # xbrl cell is typically a <a href="..."> fragment that survived the
        # regex with just its inner text if any. Treat any non-empty value as
        # "xbrl available" and best-effort build an absolute URL if it looks
        # like a relative path.
        if xbrl_raw.startswith("http"):
            xbrl_url = xbrl_raw
        elif xbrl_raw.lower().endswith(".zip") or "/" in xbrl_raw:
            xbrl_url = f"https://www.release.tdnet.info/inbs/{xbrl_raw}"

    try:
        page_idx = int(row.get("page") or "1")
    except ValueError:
        page_idx = 1

    # Best-effort OpenFIGI. normalize_ticker handles the 5-char alphanumeric
    # quirk ("469A0" → "469A") inside resolve_ticker_mic — do not pre-strip here.
    issuer_figi: Optional[str] = None
    try:
        from modal_workers.shared.openfigi_resolver import resolve_ticker_mic
        res = resolve_ticker_mic(ticker, "XTKS")
        if res.resolved:
            issuer_figi = res.issuer_figi
    except Exception:
        pass

    raw_payload: Dict[str, Any] = {
        "kjCode": ticker,
        "kjTitle": title,
        "kjName": row.get("name") or "",
        "kjPlace": row.get("place") or "",
        "kjTime": t_str,
        "kjXbrl_url": xbrl_url,
        "xbrl_available": bool(xbrl_raw),
        "pdf_url": pdf_url,
        "page_index": page_idx,
        "ymd": ymd,
        "headline": title,  # for convergence / hashing downstream
        "translation_confidence": cls["translation_confidence"],
        "signal_category": cls["signal_category"],
        "pattern_matched": cls["pattern_matched"],
    }

    # signal_id — stable on (ticker, mic, source_date, signal_type, title).
    signal_id = hashlib.sha256(
        f"{ticker}|XTKS|{source_date.isoformat()}|{cls['signal_type']}|{title}".encode("utf-8")
    ).hexdigest()[:32]

    # source_content_hash — spec.md §3.4 "sha256:<64hex>" prefix.
    _pdf_component = pdf_url or ""
    _hash_input = f"tdnet|{ticker}|{title}|{_pdf_component}".encode("utf-8")
    source_content_hash = f"sha256:{hashlib.sha256(_hash_input).hexdigest()}"

    entity_hints = EntityHints(
        issuer_figi=issuer_figi,
        ticker=ticker,
        mic="XTKS",
        name=row.get("name") or ticker,
        country="JP",
    )

    return Signal(
        signal_id=signal_id,
        source_content_hash=source_content_hash,
        source_date=source_date,
        scan_date=scan_date,
        signal_type=cls["signal_type"],
        raw_payload=raw_payload,
        source_url=pdf_url or TDNET_URL_FMT.format(page=page_idx, ymd=ymd),
        issuer_figi=issuer_figi,
        entity_hints=entity_hints,
        thesis_direction=cls["thesis_direction"],
        strength_estimate=cls["strength_estimate"],
    )


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    client = SupabaseClient()

    # Route openfigi cache reads/writes through Supabase Storage. Do this once
    # at entry — normalize_ticker's JP 5-char alphanumeric fix runs inside
    # resolve_ticker_mic and needs the backend wired.
    try:
        from modal_workers.shared.openfigi_resolver import set_cache_backend
        set_cache_backend(*client.openfigi_cache_backend())
    except Exception as e:  # noqa: BLE001
        log.warning("tdnet_scanner: openfigi cache wiring failed: %s", e)

    scan_date = datetime.now(timezone.utc)
    window_days = int(cfg.config.get("window_days", DEFAULT_WINDOW_DAYS))
    budget = max(10, cfg.timeout_soft_s - 5)  # 5s headroom for post-processing
    t0 = time.time()
    budget_deadline = t0 + budget

    warnings: List[str] = []
    signals: List[Signal] = []
    seen_hashes: set[str] = set()
    fetched_rows = 0

    today = scan_date.date()
    for d_offset in range(window_days):
        if time.time() > budget_deadline:
            warnings.append(
                f"wall-clock budget ({budget}s) exceeded before day offset {d_offset}"
            )
            break
        day = today - timedelta(days=d_offset)
        ymd = day.strftime("%Y%m%d")
        try:
            rows, day_budget_hit = _fetch_day(client, ymd, budget_deadline=budget_deadline)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"tdnet fetch_day {ymd} failed: {type(e).__name__}: {e}")
            continue
        fetched_rows += len(rows)
        log.info("tdnet %s: %d rows", ymd, len(rows))

        for row in rows:
            # Tokyo-listed filter (preserve v1 kjPlace 東 gate).
            if "東" not in (row.get("place") or ""):
                continue
            sig = _build_signal(row, scan_date)
            if sig is None:
                continue
            if sig.source_content_hash in seen_hashes:
                continue
            seen_hashes.add(sig.source_content_hash)
            signals.append(sig)

        if day_budget_hit:
            warnings.append(
                f"wall-clock budget ({budget}s) exceeded during {ymd} fetch"
            )
            break
        # Polite delay between days (matches v1).
        if time.time() < budget_deadline:
            time.sleep(POLITE_DELAY_S)

    elapsed = time.time() - t0
    log.info("tdnet_scanner: fetched=%d classified=%d elapsed=%.1fs",
             fetched_rows, len(signals), elapsed)

    status = "partial" if warnings else "ok"
    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=signals,
        warnings=warnings,
        fetched_records=fetched_rows,
    )
