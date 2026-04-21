"""
Kill-Watch DSL + Evaluator (v1.1.1 — 2026-04-20)
===============================================

Structured kill-condition rules evaluated per-candidate. Replaces the purely
prose `kill_watch` field with a machine-checkable list of rules.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).parent.parent
CURATED_PATH = REPO / "candidates" / "_curated_rationales.json"
WORKING = REPO / "working"
WORKING.mkdir(exist_ok=True)
EDGAR_UA = "Conan engine kill_watch/1.2 (pedro javiergorordo13@hotmail.com)"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
    except Exception:
        pass
    os.replace(tmp, path)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _yfinance_history(ticker: str, days: int):
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return None
    try:
        return yf.Ticker(ticker).history(period=f"{days + 3}d", auto_adjust=False)
    except Exception:
        return None


def _edgar_submissions(cik: str) -> Optional[Dict[str, Any]]:
    import urllib.request

    if not cik:
        return None
    cik10 = str(cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": EDGAR_UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _fetch_filing_text(cik: str, accession: str, primary_doc: str) -> str:
    if not (cik and accession and primary_doc):
        return ""
    import urllib.request

    cik_int = str(int(cik))
    nodash = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{nodash}/{primary_doc}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": EDGAR_UA, "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read(40960).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _yahoo_news(ticker: str) -> List[Dict[str, Any]]:
    try:
        import yfinance as yf  # type: ignore

        return yf.Ticker(ticker).news or []
    except Exception:
        return []


def _eval_price_move(rule: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    params = rule.get("params", {}) or {}
    ticker = params.get("ticker") or ctx.get("ticker")
    if not ticker:
        return None
    days = int(params.get("window_days", 5))
    threshold = float(params.get("threshold_pct", 15.0))
    direction = (params.get("direction", "abs") or "abs").lower()
    history = _yfinance_history(ticker, days)
    if history is None or len(history) < 2:
        return None
    closes = history["Close"].tolist()
    dates = [str(value.date()) for value in history.index]
    best_move = 0.0
    best_signed = 0.0
    best_date = None
    for idx in range(1, len(closes)):
        pct = 100.0 * (closes[idx] - closes[idx - 1]) / max(closes[idx - 1], 1e-9)
        if direction == "down" and pct >= 0:
            continue
        if direction == "up" and pct <= 0:
            continue
        if abs(pct) > abs(best_move):
            best_move = abs(pct)
            best_signed = pct
            best_date = dates[idx]
    if best_move >= threshold:
        return {"fired": True, "evidence": {"move_pct": round(best_signed, 2), "date": best_date, "ticker": ticker}}
    return None


def _eval_edgar_form(rule: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    params = rule.get("params", {}) or {}
    cik = params.get("cik") or ctx.get("cik")
    if not cik:
        return None
    forms_wanted = [form.strip().upper() for form in (params.get("forms") or [])]
    if not forms_wanted:
        return None
    max_age_days = int(params.get("max_age_days", 30))
    data = _edgar_submissions(cik)
    if data is None:
        return None
    recent = data.get("filings", {}).get("recent", {}) or {}
    forms = recent.get("form", []) or []
    dates = recent.get("filingDate", []) or []
    accessions = recent.get("accessionNumber", []) or []
    cutoff = _now().date() - timedelta(days=max_age_days)
    hits: List[Dict[str, Any]] = []
    wanted_set = set(forms_wanted)
    for idx, form in enumerate(forms):
        if idx >= len(dates):
            break
        try:
            filing_date = datetime.fromisoformat(dates[idx]).date()
        except Exception:
            continue
        if filing_date < cutoff:
            continue
        if form.strip().upper() in wanted_set:
            hits.append({"form": form, "date": dates[idx], "accession": accessions[idx] if idx < len(accessions) else ""})
    if hits:
        return {"fired": True, "evidence": hits[:5]}
    return None


def _eval_edgar_8k_items(rule: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    params = rule.get("params", {}) or {}
    cik = params.get("cik") or ctx.get("cik")
    if not cik:
        return None
    wanted_items = set(params.get("items") or [])
    keywords_raw = params.get("keywords") or []
    keywords = [re.compile(keyword, re.IGNORECASE) for keyword in keywords_raw]
    max_age_days = int(params.get("max_age_days", 14))
    data = _edgar_submissions(cik)
    if data is None:
        return None
    recent = data.get("filings", {}).get("recent", {}) or {}
    forms = recent.get("form", []) or []
    dates = recent.get("filingDate", []) or []
    items = recent.get("items", []) or []
    accessions = recent.get("accessionNumber", []) or []
    primary_docs = recent.get("primaryDocument", []) or []
    cutoff = _now().date() - timedelta(days=max_age_days)
    hits: List[Dict[str, Any]] = []
    for idx, form in enumerate(forms):
        if idx >= len(dates):
            break
        if form.strip().upper() != "8-K":
            continue
        try:
            filing_date = datetime.fromisoformat(dates[idx]).date()
        except Exception:
            continue
        if filing_date < cutoff:
            continue
        item_str = items[idx] if idx < len(items) else ""
        item_list = {value.strip() for value in item_str.split(",") if value.strip()}
        if wanted_items and not (wanted_items & item_list):
            continue
        if keywords:
            accession = accessions[idx] if idx < len(accessions) else ""
            primary_doc = primary_docs[idx] if idx < len(primary_docs) else ""
            body = _fetch_filing_text(cik, accession, primary_doc)
            if not body:
                continue
            matched = [keyword for regex, keyword in zip(keywords, keywords_raw) if regex.search(body)]
            if not matched:
                continue
            hits.append({"date": dates[idx], "items": item_str, "accession": accession, "keyword_matches": matched})
        else:
            hits.append({"date": dates[idx], "items": item_str})
    if hits:
        return {"fired": True, "evidence": hits[:5]}
    return None


def _eval_news_keyword(rule: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    params = rule.get("params", {}) or {}
    ticker = params.get("ticker") or ctx.get("ticker")
    if not ticker:
        return None
    max_age_days = int(params.get("max_age_days", 10))
    patterns = [re.compile(keyword, re.IGNORECASE) for keyword in (params.get("patterns") or [])]
    if not patterns:
        return None
    items = _yahoo_news(ticker)
    hits: List[Dict[str, Any]] = []
    now_ts = _now().timestamp()
    for item in items:
        title = item.get("title", "") or ""
        published = item.get("providerPublishTime", 0) or 0
        if published and (now_ts - published) > max_age_days * 86400:
            continue
        for regex in patterns:
            if regex.search(title):
                published_iso = None
                try:
                    if published:
                        published_iso = datetime.fromtimestamp(published, tz=timezone.utc).isoformat()
                except Exception:
                    pass
                hits.append({"title": title, "url": item.get("link", ""), "published_utc": published_iso})
                break
        if len(hits) >= 8:
            break
    if hits:
        return {"fired": True, "evidence": hits}
    return None


def _eval_competitor_move(rule: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    params = rule.get("params", {}) or {}
    competitor = params.get("ticker")
    if not competitor:
        return None
    days = int(params.get("window_days", 5))
    threshold = float(params.get("threshold_pct", 15.0))
    direction = (params.get("direction", "up") or "up").lower()
    history = _yfinance_history(competitor, days)
    if history is None or len(history) < 2:
        return None
    closes = history["Close"].tolist()
    dates = [str(value.date()) for value in history.index]
    for idx in range(1, len(closes)):
        pct = 100.0 * (closes[idx] - closes[idx - 1]) / max(closes[idx - 1], 1e-9)
        if direction == "up" and pct < 0:
            continue
        if direction == "down" and pct > 0:
            continue
        if abs(pct) >= threshold:
            return {"fired": True, "evidence": {"competitor": competitor, "move_pct": round(pct, 2), "date": dates[idx]}}
    return None


_EVALUATORS = {
    "price_move": _eval_price_move,
    "edgar_form": _eval_edgar_form,
    "edgar_8k_items": _eval_edgar_8k_items,
    "news_keyword": _eval_news_keyword,
    "yahoo_news": _eval_news_keyword,
    "competitor_move": _eval_competitor_move,
}


def evaluate_rules(entry: Dict[str, Any], ticker: str, ctx: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    rules = entry.get("kill_watch_rules") or []
    if not rules:
        return []
    ctx = dict(ctx or {})
    ctx.setdefault("ticker", ticker)
    firings: List[Dict[str, Any]] = []
    for rule in rules:
        kind = (rule.get("kind") or "").strip()
        evaluator = _EVALUATORS.get(kind)
        if not evaluator:
            firings.append({"rule_id": rule.get("id", "?"), "fired": False, "error": f"unknown kind: {kind}"})
            continue
        try:
            result = evaluator(rule, ctx)
        except Exception as exc:
            firings.append({"rule_id": rule.get("id", "?"), "fired": False, "error": str(exc)})
            continue
        if result and result.get("fired"):
            firings.append(
                {
                    "rule_id": rule.get("id", "?"),
                    "kind": kind,
                    "description": rule.get("description", ""),
                    "severity": rule.get("severity", "medium"),
                    "action": rule.get("action", "review"),
                    "fired": True,
                    "evidence": result.get("evidence"),
                }
            )
    return firings


_TRANSLATE_PATTERNS: List[Tuple[str, str, Dict[str, Any]]] = [
    (r"\bmajor amendment\b", "edgar_8k_items", {"items": ["8.01", "7.01"], "keywords": ["Major Amendment", "PDUFA extended", "three-month extension", "three month extension"], "max_age_days": 14}),
    (r"\bpdufa pushed\b|\bpdufa delayed\b|\bpushes? pdufa\b", "news_keyword", {"patterns": [r"\bPDUFA (?:extend|delay|push)\w*\b", r"\bMajor Amendment\b"], "max_age_days": 21}),
    (r"\bAdCom\b", "news_keyword", {"patterns": [r"\bAdvisory Committee\b", r"\bAdCom\b"], "max_age_days": 30}),
    (r"\bposion pill\b|\bpoison pill\b", "edgar_form", {"forms": ["SC 14D9", "SC 14D9/A", "8-K"], "max_age_days": 14}),
    (r"\bwithdraws? (?:proxy|materials|slate|campaign|13D)\b", "edgar_form", {"forms": ["DEFA14A", "SC 13D/A"], "max_age_days": 30}),
    (r"\bISS\b.*\brecomm", "news_keyword", {"patterns": [r"\bISS\b.*\brecommend", r"\bGlass Lewis\b.*\brecommend", r"\brecommend(?:s|ed)?\s+(?:for|against)\b"], "max_age_days": 30}),
    (r"\bdeal closes?\b|\bacqui(?:sition|rer) closes?\b", "news_keyword", {"patterns": [r"\bclosing of (?:the )?(?:deal|acquisition|merger)\b", r"\bdeal (?:closed|completed)\b"], "max_age_days": 14}),
    (r"\bsafety signal\b|\bblack[- ]box\b", "news_keyword", {"patterns": [r"\bsafety (?:signal|warning|update)\b", r"\bblack[- ]box\b", r"\bFDA label (?:change|update)\b"], "max_age_days": 30}),
    (r"\bCRL\b|\bcomplete response letter\b", "news_keyword", {"patterns": [r"\bcomplete response letter\b", r"\bCRL\b", r"\bFDA rejects\b"], "max_age_days": 14}),
    (r"\b13D/A\b|\breducing stake\b", "edgar_form", {"forms": ["SC 13D/A", "SC 13G/A"], "max_age_days": 21}),
    (r"\bverdict\b|\bliability\b", "news_keyword", {"patterns": [r"\bverdict\b", r"\bliability (?:ruling|verdict|judgment)\b"], "max_age_days": 30}),
    (r"\bREVEAL[- ]?2\b|\belegrobart\b", "news_keyword", {"patterns": [r"\bREVEAL[- ]?2\b", r"\belegrobart\b"], "max_age_days": 30}),
    (r"\biptacopan\b|\batrasentan\b", "news_keyword", {"patterns": [r"\biptacopan\b", r"\batrasentan\b", r"\bPhase 3\b"], "max_age_days": 30}),
]


def translate_kill_watch(ticker: str, entry: Dict[str, Any], cik: Optional[str] = None) -> List[Dict[str, Any]]:
    kill_watch = entry.get("kill_watch") or ""
    if not kill_watch.strip():
        return []
    rules: List[Dict[str, Any]] = []
    seen = set()
    for regex, kind, template in _TRANSLATE_PATTERNS:
        if re.search(regex, kill_watch, re.IGNORECASE):
            key = (kind, json.dumps(template.get("forms", []) + template.get("patterns", []), sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            params = dict(template)
            if kind in ("edgar_form", "edgar_8k_items") and cik:
                params["cik"] = cik
            if kind in ("news_keyword", "yahoo_news"):
                params["ticker"] = ticker
            rules.append(
                {
                    "id": f"{ticker.lower()}_{kind}_{len(rules) + 1}",
                    "description": f"(auto) matched phrase for {kind}",
                    "kind": kind,
                    "params": params,
                    "action": "review",
                    "severity": "medium",
                }
            )
    rules.append(
        {
            "id": f"{ticker.lower()}_price_crash_catchall",
            "description": "Catch-all: >=20% single-day down-move (invalidation signal)",
            "kind": "price_move",
            "params": {"ticker": ticker, "direction": "down", "threshold_pct": 20.0, "window_days": 5},
            "action": "review",
            "severity": "high",
        }
    )
    return rules


def _load_cik_from_md(ticker: str) -> Optional[str]:
    md_paths = sorted((REPO / "candidates").glob(f"{ticker}_*.md"))
    if not md_paths:
        return None
    text = md_paths[0].read_text(encoding="utf-8", errors="ignore")
    for pattern in (r"CIK[^\d]{0,10}(\d{10})", r"CIK[^\d]{0,10}(\d{4,9})", r"/edgar/data/(\d{3,10})/"):
        match = re.search(pattern, text)
        if match:
            return match.group(1).zfill(10)
    return None


def cmd_translate() -> int:
    if not CURATED_PATH.exists():
        print("no curated rationales")
        return 1
    curated = json.loads(CURATED_PATH.read_text(encoding="utf-8"))
    drafts: Dict[str, List[Dict[str, Any]]] = {}
    for ticker, entry in curated.items():
        if ticker.startswith("_") or not isinstance(entry, dict):
            continue
        drafts[ticker] = translate_kill_watch(ticker, entry, cik=_load_cik_from_md(ticker))
    out_path = WORKING / f"kill_watch_rules_draft_{_now().date().isoformat()}.json"
    _atomic_write(out_path, json.dumps(drafts, indent=2, ensure_ascii=False))
    print(json.dumps({"drafted": {ticker: len(value) for ticker, value in drafts.items()}, "output": str(out_path)}, indent=2))
    return 0


def cmd_evaluate(ticker: Optional[str] = None) -> int:
    curated = json.loads(CURATED_PATH.read_text(encoding="utf-8"))
    output: Dict[str, Any] = {}
    for name, entry in curated.items():
        if name.startswith("_") or not isinstance(entry, dict):
            continue
        if ticker and name != ticker.upper():
            continue
        output[name] = evaluate_rules(entry, name, ctx={"cik": _load_cik_from_md(name)})
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Kill-watch DSL translator / evaluator")
    parser.add_argument("--translate", action="store_true", help="Draft kill_watch_rules from each entry's free text")
    parser.add_argument("--evaluate", action="store_true", help="Evaluate existing kill_watch_rules and print firings")
    parser.add_argument("--ticker", help="Filter to a single ticker for evaluate")
    args = parser.parse_args()
    if args.translate:
        sys.exit(cmd_translate())
    if args.evaluate:
        sys.exit(cmd_evaluate(args.ticker))
    parser.print_help()


if __name__ == "__main__":
    main()
