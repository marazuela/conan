"""
TDnet scanner — Tool 2 Phase 2 (Japan).

Data source (verified 2026-04-14):
  - https://www.release.tdnet.info/inbs/I_list_{page:03d}_{YYYYMMDD}.html
  - 50 disclosures per page, UTF-8, no auth.
  - Each row: kjTime (HH:MM), kjCode (4-digit or alphanumeric ticker),
    kjName (Japanese company name), kjTitle (Japanese title + PDF link),
    kjXbrl, kjPlace (listing venue: 東=Tokyo, 名=Nagoya, 福=Fukuoka, 札=Sapporo).
  - Pages end when a GET returns a page with zero rows.

Stage 1 triage here:
  - Filter on kjPlace containing 東 (Tokyo-listed).
  - Match Japanese title against TDNET_TITLE_RULES.
  - Classify signal_type + initial thesis_direction + strength.
  - Mark `translation_confidence=0.70` (conservative — pattern match on title);
    D-002 caps signal_strength at 2 and risk_reward at 3 for `unknown` direction.
    Rules that definitively disambiguate direction (profit warning vs upgrade)
    assign confidence 0.92 so the caps do NOT apply.

Entity resolution: pipeline_runner will hand off ticker + MIC=XTKS to OpenFIGI.
For TDnet tickers, OpenFIGI takes a 4-digit TSE code as idType=TICKER,
micCode=XTKS. Some newer issuers get alphanumeric codes (e.g. 280A0) which
OpenFIGI may not resolve — in that case figi stays None and pipeline_runner
filters on resolution success.
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

USER_AGENT = "Mozilla/5.0 (compatible; Tool2-NonUS-Discovery/1.0)"
TDNET_URL_FMT = "https://www.release.tdnet.info/inbs/I_list_{page:03d}_{ymd}.html"
POLITE_DELAY_S = 1.5
FETCH_TIMEOUT = 20
MAX_PAGES_PER_DAY = 40   # 40*50 = 2000 — more than any real day

# Title-pattern rules — ordered, first match wins.
# (pattern, signal_type, signal_category, strength, thesis_direction, translation_confidence)
TDNET_TITLE_RULES: list[tuple[re.Pattern, str, str, int, str, float]] = [
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


def _http_get(url: str) -> tuple[int, bytes]:
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


def _parse_tdnet_page(text: str) -> list[dict]:
    rows: list[dict] = []
    for m in _ROW_RE.finditer(text):
        d = m.groupdict()
        title = _htmllib.unescape(d["title"] or d.get("title2") or "").strip()
        rows.append({
            "time": d["time"].strip(),
            "code": d["code"].strip(),
            "name": _htmllib.unescape(d["name"]).strip().rstrip("\u3000 "),
            "title": title,
            "pdf": d.get("pdf"),
            "xbrl": (d["xbrl"] or "").strip(),
            "place": (d["place"] or "").strip().replace("\u3000", ""),
        })
    return rows


def _fetch_day(ymd: str) -> list[dict]:
    """Fetch all pages for a given YYYYMMDD date."""
    all_rows: list[dict] = []
    for page in range(1, MAX_PAGES_PER_DAY + 1):
        url = TDNET_URL_FMT.format(page=page, ymd=ymd)
        status, body = _http_get(url)
        if status != 200 or not body:
            log.debug("tdnet %s p%d status=%s — stopping", ymd, page, status)
            break
        text = body.decode("utf-8", errors="replace")
        rows = _parse_tdnet_page(text)
        if not rows:
            break
        for r in rows:
            r["ymd"] = ymd
        all_rows.extend(rows)
        if len(rows) < 50:
            break  # last page
        time.sleep(POLITE_DELAY_S)
    return all_rows


def _tidm_to_ticker(code: str) -> str:
    """TDnet codes are typically 5-char (4-digit + check) like '47550' for 7-11.
    Strip the trailing check digit if present (OpenFIGI uses the 4-digit form)."""
    code = code.strip()
    # Keep full code for alphanumeric (e.g., 469A0) — strip last char only if
    # the result is still all-numeric and 4 digits.
    if len(code) == 5 and code.isdigit():
        return code[:4]
    return code


def _classify_title(title: str) -> Optional[dict]:
    for pat, stype, cat, strength, direction, tconf in TDNET_TITLE_RULES:
        if pat.search(title):
            return {
                "signal_type": stype,
                "signal_category": cat,
                "strength_estimate": strength,
                "thesis_direction": direction,
                "translation_confidence": tconf,
                "_pattern_matched": pat.pattern,
            }
    return None


def _make_signal_id(ticker: str, source_date: str, signal_type: str, title: str) -> str:
    h = hashlib.sha256(f"{ticker}|XTKS|{source_date}|{signal_type}|{title}".encode("utf-8")).hexdigest()
    return h[:32]


def _content_hash(ticker: str, title: str, pdf: Optional[str]) -> str:
    return hashlib.sha256(f"{ticker}|{title}|{pdf or ''}".encode("utf-8")).hexdigest()[:24]


def _rubric_scores(strength: int, signal_type: str) -> dict:
    base = {
        "signal_strength": strength,
        "catalyst_clarity": 3,
        "info_asymmetry": 4,      # Japan — slower English re-dissemination, more asymmetry
        "risk_reward": 3,
        "edge_decay": 3,
        "liquidity": 3,
        "catalyst_timeline": 3,
    }
    if signal_type in ("tender_offer", "mbo_announcement"):
        base["catalyst_clarity"] = 5
        base["catalyst_timeline"] = 5
    elif signal_type == "profit_warning":
        base["catalyst_clarity"] = 4
        base["catalyst_timeline"] = 5
        base["edge_decay"] = 5
    elif signal_type == "impairment_loss":
        base["catalyst_clarity"] = 4
        base["catalyst_timeline"] = 4
    elif signal_type == "restatement":
        base["catalyst_clarity"] = 5
        base["catalyst_timeline"] = 3
    return base


# Rough JPY → USD for the market cap floor. Pipeline expects market_cap_usd_mm;
# TDnet rows don't carry market cap so this is None — pipeline_runner triage
# will reject signals without market cap. This is the right behavior for
# Phase 2: we emit signals with ticker+mic and let the resolver/alldata fetch
# attach market cap in a later pass. For now we pass through and triage drops
# them. Phase 2.1 will add a JPX tick-size/mcap fetcher.
JPY_TO_USD_DEFAULT = 0.0065   # 2026-04 approximate, not load-bearing


def fetch_raw_signals(window_days: int = 7) -> list[dict]:
    """
    Walk TDnet daily pages backwards from today within the window. Classify
    titles via regex rules; emit common-schema signals.

    Tokyo-listed filter (kjPlace contains 東). Non-Tokyo-only listings (e.g. 名
    for Nagoya single-listed) are rare for $300M+ issuers — include them but
    OpenFIGI with MIC=XTKS will fail to resolve for those, and pipeline_runner
    will drop them.
    """
    scan_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).date()
    signals: list[dict] = []
    for d_offset in range(window_days):
        day = today - timedelta(days=d_offset)
        ymd = day.strftime("%Y%m%d")
        try:
            rows = _fetch_day(ymd)
        except Exception as e:
            log.warning("tdnet fetch_day %s failed: %s", ymd, e)
            continue
        log.info("tdnet %s: %d rows", ymd, len(rows))
        for row in rows:
            # Filter: Tokyo listing only
            if "東" not in row.get("place", ""):
                continue
            cls = _classify_title(row["title"])
            if cls is None:
                continue
            ticker = _tidm_to_ticker(row["code"])
            source_dt = datetime.strptime(f"{ymd} {row['time']}", "%Y%m%d %H:%M")
            # TDnet times are JST — offset UTC-9
            source_dt = source_dt.replace(tzinfo=timezone(timedelta(hours=9)))
            source_dt_utc = source_dt.astimezone(timezone.utc)
            pdf_url = row.get("pdf")
            if pdf_url and not pdf_url.startswith("http"):
                pdf_url = f"https://www.release.tdnet.info/inbs/{pdf_url}"
            signal = {
                "upstream_system_id": "tool-2-non-us-primary",
                "signal_id": _make_signal_id(ticker, source_dt_utc.isoformat(), cls["signal_type"], row["title"]),
                "ticker_local": ticker,
                "mic": "XTKS",
                "ticker_plus_mic": f"{ticker}.XTKS",
                "figi": None,
                "issuer_figi": None,
                "company_name_en": None,  # populated downstream by OpenFIGI
                "company_name_local": row["name"],
                "isin": None,
                "exchange": "TDnet",
                "country": "JP",
                "signal_type": cls["signal_type"],
                "signal_category": cls["signal_category"],
                "thesis_direction": cls["thesis_direction"],
                "strength_estimate": cls["strength_estimate"],
                "source_url": pdf_url or TDNET_URL_FMT.format(page=1, ymd=ymd),
                "source_content_hash": _content_hash(ticker, row["title"], pdf_url),
                "source_date": source_dt_utc.isoformat(),
                "scan_date": scan_date,
                "translation_confidence": cls["translation_confidence"],
                "market_cap_usd_mm": None,  # Phase 2.1: attach via JPX fetcher
                "rubric_scores": _rubric_scores(cls["strength_estimate"], cls["signal_type"]),
                "raw_data": {
                    "headline": row["title"],  # Japanese headline — for convergence token hashing
                    "translated_headline": None,  # Phase 2.1: translate title via transformer
                    "code_full": row["code"],
                    "place": row["place"],
                    "time_jst": row["time"],
                    "ymd": ymd,
                    "name_local": row["name"],
                    "xbrl_available": bool(row["xbrl"]),
                    "pattern_matched": cls.get("_pattern_matched"),
                },
                "_scanner": "tdnet",
            }
            signals.append(signal)
        time.sleep(POLITE_DELAY_S)
    log.info("tdnet: %d classified signals (window=%dd)", len(signals), window_days)
    return signals


def _main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, default=2)
    args = parser.parse_args()
    sigs = fetch_raw_signals(window_days=args.window)
    print(json.dumps(sigs, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _main()

# --- END OF FILE ---
