"""
Candidate Monitor (v1.0.1 — 2026-04-20)
=======================================

Polls external data sources for active candidates and classifies them as
AUTO_ARCHIVE / REVIEW / NOOP. In dry-run mode it remains read-mostly and only
writes the summary report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).parent.parent
CANDIDATES_DIR = REPO / "candidates"
ARCHIVED_DIR = CANDIDATES_DIR / "_archived_post_edge"
CURATED_PATH = CANDIDATES_DIR / "_curated_rationales.json"
WORKING = REPO / "working"
AUDIT_LOG = WORKING / "auto_archive_audit.jsonl"
WORKING.mkdir(exist_ok=True)
ARCHIVED_DIR.mkdir(exist_ok=True)
_CIK_CACHE_PATH = WORKING / "_ticker_to_cik_cache.json"

PRICE_MOVE_STRONG_PCT = 15.0
PRICE_MOVE_AUTOARCH_PCT = 20.0
LOOKBACK_DAYS_PRICE = 7
LOOKBACK_DAYS_FILINGS = 14

EDGAR_UA = "Conan engine candidate_monitor/1.1 (pedro javiergorordo13@hotmail.com)"

EDGAR_RESOLUTION_FORMS = {
    "DEFM14A",
    "PREM14A",
    "SC TO-T",
    "SC TO-I",
    "SC 13E3",
    "425",
    "S-4",
    "S-4/A",
    "SC 14D9",
    "SC 14D9/A",
}
EDGAR_ACTIVIST_SETTLEMENT_FORMS = {"SC 13D/A", "SC 13D", "DFAN14A", "DEFA14A", "DEFC14A"}

KEYWORDS = {
    "pdufa": [
        r"\bapprov(?:e|ed|al)\b",
        r"\bcomplete response letter\b",
        r"\bCRL\b",
        r"\bFDA (?:grants?|issues?|denies?)\b",
        r"\bpriority review\b",
        r"\blabel\s+(?:granted|approved|expand)",
        r"\bmajor amendment\b",
        r"\bpushed back\b",
    ],
    "merger_arb": [
        r"\bdefinitive (?:merger )?agreement\b",
        r"\bto be acquired\b",
        r"\bacquires?\b",
        r"\btake[- ]private\b",
        r"\bgoing[- ]private\b",
        r"\ball[- ]cash (?:offer|acquisition|deal)\b",
        r"\bantitrust (?:clearance|approval|blocked)\b",
        r"\bdeal (?:closes?|closed|breaks?|terminated|withdrawn)\b",
    ],
    "activist": [
        r"\bsettlement (?:agreement|reached)\b",
        r"\bboard (?:seats?|representation) (?:granted|awarded|added)\b",
        r"\bpoison pill (?:adopted|approved|extended|waived|rescinded)\b",
        r"\bwithdraws? (?:13D|proxy|slate|campaign)\b",
        r"\baccepts? (?:the )?offer\b",
        r"\brejects? (?:the )?offer\b",
        r"\bconcludes? (?:strategic )?review\b",
    ],
    "proxy": [
        r"\bshareholders? (?:elect|approve|reject)\b",
        r"\bAGM\b",
        r"\bannual meeting\b",
        r"\b(?:ISS|Glass Lewis) recommends?\b",
        r"\bslate (?:elected|defeated|withdrawn)\b",
    ],
    "short_positioning": [
        r"\bshort squeeze\b",
        r"\bshort interest (?:soars?|plunges?)\b",
        r"\bearnings (?:beat|miss)\b",
    ],
    "litigation": [
        r"\bverdict\b",
        r"\bsettle(?:s|ment|d)\b",
        r"\bsummary judgment\b",
        r"\bappeal (?:ruling|denied|granted)\b",
        r"\bdismiss(?:ed|al)\b",
    ],
}


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


def _today_iso() -> str:
    return _now().date().isoformat()


def _append_audit(event: Dict[str, Any]) -> None:
    with open(AUDIT_LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _load_curated() -> Dict[str, Any]:
    if not CURATED_PATH.exists():
        return {}
    return json.loads(CURATED_PATH.read_text(encoding="utf-8"))


def _save_curated(obj: Dict[str, Any]) -> None:
    _atomic_write(CURATED_PATH, json.dumps(obj, indent=2, ensure_ascii=False))


def _active_tickers(curated: Dict[str, Any]) -> List[str]:
    return [key for key in curated.keys() if not key.startswith("_") and isinstance(curated[key], dict)]


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if len(value) == 10:
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _classify_archetype(entry: Dict[str, Any], md_text: str) -> str:
    blob = " ".join(str(value) for value in entry.values() if isinstance(value, str)) + " " + md_text[:2000]
    lowered = blob.lower()
    if "pdufa" in lowered or "fda approval" in lowered or "complete response" in lowered:
        return "pdufa"
    if "poison pill" in lowered or "13d" in lowered or "activist" in lowered:
        return "activist"
    if "proxy fight" in lowered or "agm" in lowered or "annual meeting" in lowered:
        return "proxy"
    if "take-private" in lowered or "merger" in lowered or "acquisition" in lowered or "tender offer" in lowered:
        return "merger_arb"
    if "short" in lowered and "squeeze" in lowered:
        return "short_positioning"
    if "lawsuit" in lowered or "verdict" in lowered or "settlement" in lowered:
        return "litigation"
    return "merger_arb"


def _load_ticker_cik_cache() -> Dict[str, str]:
    if _CIK_CACHE_PATH.exists():
        try:
            return json.loads(_CIK_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_ticker_cik_cache(cache: Dict[str, str]) -> None:
    try:
        _atomic_write(_CIK_CACHE_PATH, json.dumps(cache, indent=2))
    except Exception:
        pass


def _fetch_ticker_to_cik(ticker: str) -> Optional[str]:
    cache = _load_ticker_cik_cache()
    if ticker.upper() in cache:
        return cache[ticker.upper()]
    import urllib.request

    try:
        req = urllib.request.Request("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": EDGAR_UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        fresh: Dict[str, str] = {}
        for _, record in (data or {}).items():
            name = str(record.get("ticker", "")).upper()
            cik = record.get("cik_str")
            if name and cik is not None:
                fresh[name] = str(cik).zfill(10)
        if fresh:
            cache.update(fresh)
            _save_ticker_cik_cache(cache)
            return cache.get(ticker.upper())
    except Exception:
        return None
    return None


def _extract_cik(md_text: str, ticker: Optional[str] = None) -> Optional[str]:
    for pattern in (r"CIK[^\d]{0,10}(\d{10})", r"CIK[^\d]{0,10}(\d{4,9})", r"/edgar/data/(\d{3,10})/", r"data\.sec\.gov/submissions/CIK(\d{3,10})"):
        match = re.search(pattern, md_text)
        if match:
            return match.group(1).zfill(10)
    if ticker:
        return _fetch_ticker_to_cik(ticker)
    return None


def _find_md_for_ticker(ticker: str) -> Optional[Path]:
    matches = sorted(CANDIDATES_DIR.glob(f"{ticker}_*.md"))
    return matches[0] if matches else None


def _probe_price(ticker: str) -> Dict[str, Any]:
    try:
        import yfinance as yf  # type: ignore
    except Exception as exc:
        return {"status": "unavailable", "error": f"yfinance import: {exc}"}
    try:
        history = yf.Ticker(ticker).history(period=f"{LOOKBACK_DAYS_PRICE + 3}d", auto_adjust=False)
        if history is None or len(history) < 2:
            return {"status": "insufficient_history"}
        closes = history["Close"].tolist()
        dates = [str(value.date()) for value in history.index]
        max_move = 0.0
        max_dir = 0
        max_date = None
        for idx in range(1, len(closes)):
            pct = 100.0 * (closes[idx] - closes[idx - 1]) / max(closes[idx - 1], 1e-9)
            if abs(pct) > abs(max_move):
                max_move = pct
                max_dir = 1 if pct > 0 else -1
                max_date = dates[idx]
        return {
            "status": "ok",
            "max_abs_move_pct": round(abs(max_move), 2),
            "max_move_pct_signed": round(max_move, 2),
            "direction": max_dir,
            "max_move_date": max_date,
            "recent_close": round(float(closes[-1]), 2),
            "recent_close_date": dates[-1],
            "closes_tail": [round(float(close), 2) for close in closes[-5:]],
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def _probe_edgar(cik: Optional[str], since_days: int = LOOKBACK_DAYS_FILINGS) -> Dict[str, Any]:
    if not cik:
        return {"status": "no_cik"}
    import urllib.request

    req = urllib.request.Request(f"https://data.sec.gov/submissions/CIK{cik}.json", headers={"User-Agent": EDGAR_UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    recent = data.get("filings", {}).get("recent", {}) or {}
    forms = recent.get("form", []) or []
    dates = recent.get("filingDate", []) or []
    accessions = recent.get("accessionNumber", []) or []
    items = recent.get("items", []) or []
    cutoff = _now().date() - timedelta(days=since_days)
    resolution_hits: List[Dict[str, Any]] = []
    activist_hits: List[Dict[str, Any]] = []
    for idx, form in enumerate(forms):
        if idx >= len(dates):
            break
        try:
            filing_date = datetime.fromisoformat(dates[idx]).date()
        except Exception:
            continue
        if filing_date < cutoff:
            continue
        record = {
            "form": form.strip(),
            "date": dates[idx],
            "accession": accessions[idx] if idx < len(accessions) else "",
            "items": items[idx] if idx < len(items) else "",
        }
        if record["form"] in EDGAR_RESOLUTION_FORMS:
            resolution_hits.append(record)
        elif record["form"] in EDGAR_ACTIVIST_SETTLEMENT_FORMS:
            activist_hits.append(record)
    return {"status": "ok", "resolution_filings": resolution_hits, "activist_filings": activist_hits, "window_days": since_days}


def _probe_news(ticker: str, archetype: str) -> Dict[str, Any]:
    try:
        import yfinance as yf  # type: ignore
    except Exception as exc:
        return {"status": "unavailable", "error": f"yfinance: {exc}"}
    try:
        items = yf.Ticker(ticker).news or []
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    patterns = KEYWORDS.get(archetype, []) + KEYWORDS.get("merger_arb", [])
    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    matches: List[Dict[str, Any]] = []
    now_ts = _now().timestamp()
    for item in items:
        title = item.get("title", "") or ""
        published = item.get("providerPublishTime", 0) or 0
        try:
            published_iso = datetime.fromtimestamp(published, tz=timezone.utc).isoformat()
        except Exception:
            published_iso = None
        if published and (now_ts - published) > 10 * 86400:
            continue
        for regex in compiled:
            if regex.search(title):
                matches.append({"title": title, "published_utc": published_iso, "url": item.get("link", "")})
                break
    return {"status": "ok", "matches": matches[:10], "total_news_items": len(items)}


def _probe_openfda(drug_keywords: List[str]) -> Dict[str, Any]:
    if not drug_keywords:
        return {"status": "skipped", "reason": "no drug keywords"}
    import urllib.request

    since = (_now().date() - timedelta(days=45)).strftime("%Y%m%d")
    hits: List[Dict[str, Any]] = []
    for keyword in drug_keywords[:3]:
        query = f'(products.brand_name:"{keyword}"+OR+products.active_ingredients.name:"{keyword}")+AND+submissions.submission_status_date:[{since}+TO+99991231]'
        try:
            req = urllib.request.Request("https://api.fda.gov/drug/drugsfda.json?search=" + query + "&limit=5", headers={"User-Agent": EDGAR_UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for result in data.get("results", [])[:5]:
                for submission in result.get("submissions", []):
                    status_date = submission.get("submission_status_date")
                    if status_date and status_date >= since:
                        hits.append(
                            {
                                "application_number": result.get("application_number"),
                                "submission_type": submission.get("submission_type"),
                                "submission_status": submission.get("submission_status"),
                                "submission_status_date": status_date,
                                "keyword": keyword,
                            }
                        )
        except Exception:
            continue
    return {"status": "ok", "hits": hits}


def _extract_drug_keywords(entry: Dict[str, Any], md_text: str) -> List[str]:
    tokens = set()
    blob = " ".join(str(value) for value in entry.values() if isinstance(value, str)) + " " + md_text[:3000]
    for match in re.finditer(r"\b([A-Z]{2,5}-?\d{2,5}[A-Z]?)\b", blob):
        tokens.add(match.group(1))
    for match in re.finditer(r"\b([a-z]{6,20})\b", blob.lower()):
        word = match.group(1)
        if re.search(r"(mab|tug|nib|rib|cel|umab|prazole|tinib|setron|gliflozin)$", word):
            tokens.add(word)
    for match in re.finditer(r"\b([A-Z][A-Z]{4,})\b", blob):
        tokens.add(match.group(1))
    return list(tokens)[:8]


def _classify_triggers(price: Dict[str, Any], edgar: Dict[str, Any], news: Dict[str, Any], fda: Optional[Dict[str, Any]], catalyst_date: Optional[datetime], archetype: str) -> Tuple[str, List[str]]:
    triggers: List[str] = []
    if price.get("status") == "ok":
        move = price.get("max_abs_move_pct", 0.0)
        if move >= PRICE_MOVE_AUTOARCH_PCT:
            triggers.append(f"price_strong:{move:.1f}%@{price.get('max_move_date')}")
        elif move >= PRICE_MOVE_STRONG_PCT:
            triggers.append(f"price_elevated:{move:.1f}%@{price.get('max_move_date')}")
    if edgar.get("status") == "ok":
        resolution_hits = edgar.get("resolution_filings", []) or []
        activist_hits = edgar.get("activist_filings", []) or []
        if resolution_hits:
            triggers.append("edgar_resolution:" + ",".join(sorted({hit["form"] for hit in resolution_hits})))
        if activist_hits and archetype == "activist":
            triggers.append("edgar_activist:" + ",".join(sorted({hit["form"] for hit in activist_hits})))
    if news.get("status") == "ok" and news.get("matches"):
        triggers.append(f"news_kw:{len(news['matches'])}")
    if fda and fda.get("status") == "ok" and fda.get("hits"):
        triggers.append(f"fda_submission:{len(fda['hits'])}")

    edgar_resolution = any(trigger.startswith("edgar_resolution:") for trigger in triggers)
    price_strong = any(trigger.startswith("price_strong:") for trigger in triggers)
    news_hit = any(trigger.startswith("news_kw:") for trigger in triggers)
    fda_hit = any(trigger.startswith("fda_submission:") for trigger in triggers)
    future_catalyst = False
    past_catalyst = False
    if catalyst_date is not None:
        future_catalyst = catalyst_date.date() > _now().date() + timedelta(days=7)
        past_catalyst = catalyst_date.date() <= _now().date()
    if edgar_resolution and not future_catalyst:
        return "auto_archive", triggers
    if price_strong and (news_hit or fda_hit):
        return "auto_archive", triggers
    if price_strong and past_catalyst:
        return "auto_archive", triggers
    if triggers:
        return "review", triggers
    return "noop", triggers


def _build_archive_reason(triggers: List[str], price: Dict[str, Any], news: Dict[str, Any], edgar: Dict[str, Any]) -> str:
    parts: List[str] = []
    if price.get("status") == "ok" and price.get("max_abs_move_pct", 0) >= PRICE_MOVE_STRONG_PCT:
        dir_label = "up" if price.get("direction", 0) > 0 else "down"
        parts.append(f"{dir_label} {abs(price.get('max_move_pct_signed', 0)):.1f}% on {price.get('max_move_date')}")
    if edgar.get("status") == "ok" and edgar.get("resolution_filings"):
        forms = sorted({hit["form"] for hit in edgar["resolution_filings"]})
        latest = max(edgar["resolution_filings"], key=lambda hit: hit.get("date", ""))
        parts.append(f"EDGAR {','.join(forms)} filed {latest.get('date')}")
    if news.get("status") == "ok" and news.get("matches"):
        parts.append(f"news keyword: \"{news['matches'][0].get('title', '')[:110]}\"")
    if not parts:
        return "POST-EDGE — monitor detected no single dominant trigger."
    return "POST-EDGE — auto-archived by candidate_monitor. " + "; ".join(parts) + "."


def _archive_candidate(ticker: str, curated: Dict[str, Any], triggers: List[str], price: Dict[str, Any], news: Dict[str, Any], edgar: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    pre_state = json.loads(json.dumps(curated.get(ticker, {})))
    md_path = _find_md_for_ticker(ticker)
    md_from = str(md_path) if md_path else None
    archive_entry = {
        "archived_date": _today_iso(),
        "archive_reason": _build_archive_reason(triggers, price, news, edgar),
        "outcome": "UNDETERMINED — auto-archive by monitor; review for accuracy",
        "former_one_liner": pre_state.get("one_liner", ""),
        "monitor_triggers": triggers,
        "monitor_price_probe": price,
        "monitor_news_probe_hits": (news.get("matches") or [])[:3],
        "monitor_edgar_hits": {"resolution": edgar.get("resolution_filings", [])[:5], "activist": edgar.get("activist_filings", [])[:5]},
    }
    if dry_run:
        return {
            "ticker": ticker,
            "decision": "auto_archive",
            "dry_run": True,
            "triggers": triggers,
            "would_archive_entry": archive_entry,
            "would_move_md": {"from": md_from, "to": str(ARCHIVED_DIR / md_path.name) if md_path else None},
        }

    archived_block = curated.setdefault("_archived", {})
    if ticker in archived_block:
        archived_block[f"{ticker}__monitor_{_today_iso()}"] = archive_entry
    else:
        archived_block[ticker] = archive_entry
    if ticker in curated:
        del curated[ticker]

    md_to: Optional[str] = None
    if md_path and md_path.exists():
        target = ARCHIVED_DIR / md_path.name
        try:
            shutil.move(str(md_path), str(target))
            md_to = str(target)
        except Exception:
            md_to = None
    _save_curated(curated)
    _append_audit(
        {
            "decision_ts": _now().isoformat().replace("+00:00", "Z"),
            "decision": "auto_archive",
            "ticker": ticker,
            "triggers": triggers,
            "archive_entry": archive_entry,
            "pre_state": pre_state,
            "md_moved_from": md_from,
            "md_moved_to": md_to,
        }
    )
    return {"ticker": ticker, "decision": "auto_archive", "archived": True, "triggers": triggers, "md_moved_to": md_to}


def _log_review(ticker: str, triggers: List[str], price: Dict[str, Any], news: Dict[str, Any], edgar: Dict[str, Any], archetype: str, catalyst_date: Optional[datetime]) -> None:
    _append_audit(
        {
            "decision_ts": _now().isoformat().replace("+00:00", "Z"),
            "decision": "review",
            "ticker": ticker,
            "archetype": archetype,
            "triggers": triggers,
            "catalyst_date": catalyst_date.date().isoformat() if catalyst_date else None,
            "price_probe": price,
            "news_hits": (news.get("matches") or [])[:3],
            "edgar_hits": {"resolution": edgar.get("resolution_filings", [])[:3], "activist": edgar.get("activist_filings", [])[:3]},
        }
    )


def undo(ticker: str) -> Dict[str, Any]:
    if not AUDIT_LOG.exists():
        return {"status": "no_audit_log"}
    latest: Optional[Dict[str, Any]] = None
    with open(AUDIT_LOG, "r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("decision") == "auto_archive" and event.get("ticker") == ticker:
                latest = event
    if not latest:
        return {"status": "no_archive_for_ticker", "ticker": ticker}
    curated = _load_curated()
    curated[ticker] = latest["pre_state"]
    if "_archived" in curated and ticker in curated["_archived"]:
        del curated["_archived"][ticker]
    _save_curated(curated)

    md_from = latest.get("md_moved_to")
    md_to = latest.get("md_moved_from")
    moved_back = False
    if md_from and md_to and Path(md_from).exists():
        try:
            shutil.move(md_from, md_to)
            moved_back = True
        except Exception:
            pass
    _append_audit({"decision_ts": _now().isoformat().replace("+00:00", "Z"), "decision": "undo", "ticker": ticker, "undone_of_decision_ts": latest.get("decision_ts"), "md_restored": moved_back})
    return {"status": "ok", "ticker": ticker, "md_restored": moved_back, "undone_of": latest.get("decision_ts")}


def run(dry_run: bool = False, only_ticker: Optional[str] = None, skip_fda: bool = False) -> Dict[str, Any]:
    curated = _load_curated()
    tickers = _active_tickers(curated)
    if only_ticker:
        tickers = [ticker for ticker in tickers if ticker == only_ticker.upper()]
    report: Dict[str, Any] = {
        "ran_at_utc": _now().isoformat().replace("+00:00", "Z"),
        "tickers_checked": tickers,
        "per_ticker": {},
        "archived": [],
        "reviews": [],
        "errors": [],
        "dry_run": dry_run,
    }

    for ticker in tickers:
        entry = curated.get(ticker) or {}
        md_path = _find_md_for_ticker(ticker)
        try:
            md_text = md_path.read_text(encoding="utf-8", errors="ignore") if md_path and md_path.exists() else ""
        except Exception:
            md_text = ""
        archetype = _classify_archetype(entry, md_text)
        catalyst_iso = entry.get("catalyst_date_iso") or entry.get("catalyst_date")
        catalyst_dt = _parse_iso(catalyst_iso)
        cik = _extract_cik(md_text, ticker=ticker)

        price = _probe_price(ticker)
        edgar = _probe_edgar(cik)
        news = _probe_news(ticker, archetype)
        fda: Optional[Dict[str, Any]] = None
        if archetype == "pdufa" and not skip_fda:
            fda = _probe_openfda(_extract_drug_keywords(entry, md_text))

        rule_firings: List[Dict[str, Any]] = []
        try:
            from kill_watch import evaluate_rules  # type: ignore

            rule_firings = evaluate_rules(entry, ticker, ctx={"cik": cik}) or []
        except Exception as exc:
            report["errors"].append({"ticker": ticker, "stage": "rule_eval", "error": str(exc)})

        decision, triggers = _classify_triggers(price, edgar, news, fda, catalyst_dt, archetype)
        for firing in rule_firings:
            triggers.append(f"rule:{firing.get('rule_id')}({firing.get('severity', '?')})")
            if firing.get("action") == "archive" and decision != "auto_archive":
                decision = "auto_archive"
            elif decision == "noop":
                decision = "review"

        report["per_ticker"][ticker] = {
            "archetype": archetype,
            "catalyst_date": catalyst_iso,
            "cik": cik,
            "decision": decision,
            "triggers": triggers,
            "price_snapshot": {
                "max_abs_move_pct": price.get("max_abs_move_pct"),
                "recent_close": price.get("recent_close"),
                "recent_close_date": price.get("recent_close_date"),
            },
            "edgar_status": edgar.get("status"),
            "news_matches": len(news.get("matches", []) or []),
            "rule_firings": rule_firings,
        }

        if decision == "auto_archive":
            try:
                report["archived"].append(_archive_candidate(ticker, curated, triggers, price, news, edgar, dry_run=dry_run))
            except Exception as exc:
                report["errors"].append({"ticker": ticker, "stage": "archive", "error": str(exc)})
        elif decision == "review":
            try:
                if not dry_run:
                    _log_review(ticker, triggers, price, news, edgar, archetype, catalyst_dt)
                report["reviews"].append({"ticker": ticker, "triggers": triggers})
            except Exception as exc:
                report["errors"].append({"ticker": ticker, "stage": "review_log", "error": str(exc)})

    report_path = WORKING / f"candidate_monitor_report_{_today_iso()}.json"
    try:
        _atomic_write(report_path, json.dumps(report, indent=2, ensure_ascii=False))
        report["report_path"] = str(report_path)
    except Exception as exc:
        report["errors"].append({"stage": "report_write", "error": str(exc)})
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Candidate post-edge monitor")
    parser.add_argument("--dry-run", action="store_true", help="Classify and log, but do not archive or move files")
    parser.add_argument("--ticker", help="Run for a single ticker only")
    parser.add_argument("--skip-fda", action="store_true", help="Skip openFDA probe (faster; useful offline)")
    parser.add_argument("--undo", help="Undo the latest AUTO_ARCHIVE for the given ticker")
    args = parser.parse_args()
    if args.undo:
        print(json.dumps(undo(args.undo.upper()), indent=2))
        return
    result = run(dry_run=args.dry_run, only_ticker=args.ticker, skip_fda=args.skip_fda)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps({"tickers_checked": len(result["tickers_checked"]), "archived": len(result["archived"]), "reviews": len(result["reviews"]), "errors": len(result["errors"]), "dry_run": result["dry_run"]}))


if __name__ == "__main__":
    main()
