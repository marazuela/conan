"""
Pipeline runner for Tool 2 (Non-US Discovery System).

Orchestrates the full pipeline for a given scanner run:
  1. Invoke scanner to get raw signals.
  2. Apply Stage 1 triage (market cap, novelty, date window, boilerplate, language floor).
  3. Entity-resolve via OpenFIGI (D-003).
  4. Dedup + convergence annotate (D-001, D-004).
  5. Score (7-dimension rubric + convergence bonus).
  6. Route to candidate creation or watchlist or discard.

Each scanner module (e.g., `lse_rns_scanner`) provides a
`fetch_raw_signals(window_days: int, ...) -> list[dict]` callable. Optional
kwargs (e.g. max_tickers, throttle_seconds) are forwarded only to scanners
whose signature accepts them.

Usage:
    python tools/pipeline_runner.py --scanner lse_rns --window 7
    python tools/pipeline_runner.py --scanner asx --window 7 --throttle 0.05
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tools import boilerplate_filters, convergence_engine, openfigi_resolver

# Optional per-exchange market-cap enrichers. Keyed by MIC.
_MCAP_ENRICHERS_BY_MIC = {
    "XTKS": "tools.jpx_market_cap",  # Phase 2.1 Japan
}


def _enrich_market_caps(signals):
    by_mic = {}
    for s in signals:
        by_mic.setdefault(s.get("mic") or "", []).append(s)
    for mic, group in by_mic.items():
        mod_path = _MCAP_ENRICHERS_BY_MIC.get(mic)
        if not mod_path:
            continue
        try:
            mod = importlib.import_module(mod_path)
            mod.attach_market_caps(group)
        except Exception as e:
            print(f"[pipeline_runner] mcap enricher {mod_path} failed: {type(e).__name__}: {e}")
    return signals


ROOT = Path(__file__).parent.parent
SIGNALS_DIR = ROOT / "signals"
RAW_DIR = SIGNALS_DIR / "raw"
LOG_PATH = SIGNALS_DIR / "signal_log.json"
CANDIDATES_DIR = ROOT / "candidates"
WATCHLIST_DIR = CANDIDATES_DIR / "watchlist"
WORKING_DIR = ROOT / "working"

for d in (SIGNALS_DIR, RAW_DIR, CANDIDATES_DIR, WATCHLIST_DIR, WORKING_DIR):
    d.mkdir(parents=True, exist_ok=True)

SCANNER_REGISTRY = {
    "lse_rns": ("tools.lse_rns_scanner", "LSE"),
    "tdnet": ("tools.tdnet_scanner", "TDnet"),
    "asx": ("tools.asx_scanner", "ASX"),
    "sedar": ("tools.sedar_scanner", "SEDAR"),
    "sedar_chrome": ("tools.sedar_chrome_supplement", "SEDAR_CHROME"),
    "hkex": ("tools.hkex_scanner", "HKEx"),
    "kind": ("tools.kind_scanner", "KIND"),
    "bse_nse": ("tools.bse_nse_scanner", "BSE_NSE"),
    "cvm": ("tools.cvm_scanner", "CVM"),
    "bmv": ("tools.bmv_scanner", "BMV"),
}

WEIGHTS = {
    "signal_strength": 2.0,
    "catalyst_clarity": 1.0,
    "info_asymmetry": 1.5,
    "risk_reward": 1.0,
    "edge_decay": 1.0,
    "liquidity": 1.0,
    "catalyst_timeline": 1.0,
}

TRANSLATION_CONFIDENCE_DIRECTION = 0.85
TRANSLATION_CONFIDENCE_TRIAGE = 0.70
SIGNAL_STRENGTH_CAP_UNKNOWN = 2
RISK_REWARD_CAP_UNKNOWN = 3


def _load_historical_log():
    if not LOG_PATH.exists():
        return []
    try:
        return json.loads(LOG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _append_log(entries):
    existing = _load_historical_log()
    existing.extend(entries)
    LOG_PATH.write_text(json.dumps(existing, indent=2))


def _load_scanner(scanner_key):
    if scanner_key not in SCANNER_REGISTRY:
        raise ValueError(f"Unknown scanner: {scanner_key}. Registered: {list(SCANNER_REGISTRY)}")
    module_path, exch_key = SCANNER_REGISTRY[scanner_key]
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise RuntimeError(f"Scanner module {module_path} not built yet: {e}")
    if not hasattr(module, "fetch_raw_signals"):
        raise RuntimeError(f"Scanner {module_path} missing fetch_raw_signals()")
    return module.fetch_raw_signals, exch_key


def triage(signal, exchange_key, novelty_set):
    ch = signal.get("source_content_hash")
    if ch and ch in novelty_set:
        return False, "duplicate_content_hash"
    hdl = ""
    raw = signal.get("raw_data")
    if isinstance(raw, dict):
        hdl = raw.get("headline") or raw.get("translated_headline") or ""
    if boilerplate_filters.is_boilerplate(exchange_key, hdl):
        return False, "boilerplate_filter"
    mcap = signal.get("market_cap_usd_mm")
    if mcap is None or mcap < 300:
        return False, "below_market_cap_floor"
    tc = signal.get("translation_confidence")
    if isinstance(tc, (int, float)) and tc < TRANSLATION_CONFIDENCE_TRIAGE:
        return False, "translation_confidence_below_triage"
    return True, ""


def resolve_entity(signal):
    ticker = signal.get("ticker_local")
    mic = signal.get("mic")
    if not ticker or not mic:
        signal["_resolution_error"] = "missing_ticker_or_mic"
        return signal
    res = openfigi_resolver.resolve_ticker_mic(ticker, mic)
    if res.resolved:
        signal["figi"] = res.figi
        signal["issuer_figi"] = res.issuer_figi
        if not signal.get("company_name_en") and res.name:
            signal["company_name_en"] = res.name
    else:
        signal["_resolution_error"] = res.error
    return signal


def _apply_d002_caps(scores, thesis_direction):
    if thesis_direction == "unknown":
        scores = dict(scores)
        scores["signal_strength"] = min(scores.get("signal_strength", 0), SIGNAL_STRENGTH_CAP_UNKNOWN)
        scores["risk_reward"] = min(scores.get("risk_reward", 0), RISK_REWARD_CAP_UNKNOWN)
    return scores


def score_signal(signal):
    scores = signal.get("rubric_scores")
    if not isinstance(scores, dict) or not scores:
        signal["score"] = None
        signal["score_total"] = None
        signal["_score_error"] = "missing_rubric_scores"
        return signal
    thesis_direction = signal.get("thesis_direction", "unknown")
    scores = _apply_d002_caps(scores, thesis_direction)
    total = 0.0
    for dim, weight in WEIGHTS.items():
        val = scores.get(dim, 0)
        try:
            total += float(val) * weight
        except (TypeError, ValueError):
            pass
    signal["rubric_scores_effective"] = scores
    signal["score"] = round(total, 2)
    signal["score_total"] = round(total + signal.get("convergence_bonus", 0), 2)
    return signal


def route(signal):
    total = signal.get("score_total")
    if total is None:
        return "manual_review"
    if total >= 28:
        return "immediate"
    if total >= 22:
        return "watchlist"
    if total >= 14:
        return "archive"
    return "discard"


def _filter_scanner_kwargs(fetch, kwargs):
    try:
        sig = inspect.signature(fetch)
    except (TypeError, ValueError):
        return {}
    allowed = set(sig.parameters.keys())
    return {k: v for k, v in kwargs.items() if k in allowed and v is not None}


def run(scanner_key, window_days=7, **scanner_kwargs):
    scan_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    summary = {
        "scanner": scanner_key,
        "scan_date": scan_date,
        "window_days": window_days,
        "raw_count": 0,
        "post_triage": 0,
        "post_resolve": 0,
        "post_dedup": 0,
        "immediate": 0,
        "watchlist": 0,
        "archive": 0,
        "discard": 0,
        "manual_review": 0,
        "errors": [],
    }

    try:
        fetch, exch_key = _load_scanner(scanner_key)
    except (ValueError, RuntimeError) as e:
        summary["errors"].append(f"scanner_load: {e}")
        return summary

    forwarded = _filter_scanner_kwargs(fetch, scanner_kwargs)
    try:
        raw_signals = fetch(window_days=window_days, **forwarded)
    except Exception as e:
        summary["errors"].append(f"scanner_fetch: {type(e).__name__}: {e}")
        return summary

    summary["raw_count"] = len(raw_signals)
    raw_path = RAW_DIR / f"{scanner_key}_{scan_date}.json"
    raw_path.write_text(json.dumps(raw_signals, indent=2))

    _enrich_market_caps(raw_signals)

    novelty_set = {
        s.get("source_content_hash") for s in _load_historical_log()
        if s.get("source_content_hash")
    }
    triaged = []
    for sig in raw_signals:
        ok, reason = triage(sig, exch_key, novelty_set)
        if ok:
            triaged.append(sig)
        else:
            sig["_triage_dropped"] = reason
    summary["post_triage"] = len(triaged)

    resolved = [resolve_entity(sig) for sig in triaged]
    resolved_ok = [s for s in resolved if s.get("figi")]
    summary["post_resolve"] = len(resolved_ok)

    historical = _load_historical_log()
    processed = convergence_engine.process(resolved_ok, historical)
    survivors = [s for s in processed if not s.get("dedup_dropped")]
    summary["post_dedup"] = len(survivors)

    scored = [score_signal(s) for s in survivors]

    for sig in scored:
        routing = route(sig)
        sig["_routing"] = routing
        summary[routing] = summary.get(routing, 0) + 1

    log_entries = []
    for sig in scored:
        log_entries.append({
            "signal_id": sig.get("signal_id"),
            "issuer_figi": sig.get("issuer_figi"),
            "ticker_plus_mic": sig.get("ticker_plus_mic"),
            "scan_date": scan_date,
            "source_date": sig.get("source_date"),
            "source_content_hash": sig.get("source_content_hash"),
            "scanner": scanner_key,
            "score_total": sig.get("score_total"),
            "routing": sig.get("_routing"),
        })
    _append_log(log_entries)

    out_path = SIGNALS_DIR / f"{scanner_key}_{scan_date}_processed.json"
    out_path.write_text(json.dumps(scored, indent=2))

    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scanner", required=True, choices=list(SCANNER_REGISTRY))
    parser.add_argument("--window", type=int, default=7)
    parser.add_argument("--max-tickers", type=int, default=None,
                        help="Cap universe size (scanner-specific; currently used by asx)")
    parser.add_argument("--throttle", type=float, default=None,
                        help="Per-request throttle seconds (scanner-specific; currently used by asx)")
    args = parser.parse_args()
    summary = run(
        args.scanner,
        args.window,
        max_tickers=args.max_tickers,
        throttle_seconds=args.throttle,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
