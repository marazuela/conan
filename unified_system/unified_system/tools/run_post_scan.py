"""
Unified post-scan — scoring + convergence + signal log update.

Runs AFTER pipeline_runner.py completes. Reads all the scanner output files
(signals/*_scanner_output.json), scores each signal against its matched
profile rubric, runs convergence, and emits:
  - signals/signal_log.json  (appended atomically)
  - working/post_scan_report_YYYY-MM-DD.json (diagnostic)

Scoring is delegated to `score_signal(signal, profile_name)` — the rubric
implementations live in this file as SCORERS dict. Each scorer returns
(raw_score_0_to_5_per_dim, weighted_total, auto_caps_triggered).
"""

from __future__ import annotations

import json
import os
import glob
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

REPO = Path(__file__).parent.parent
WORKSPACE_ROOT = REPO.parent.parent
SIGNAL_LOG = REPO / "signals" / "signal_log.json"
REGISTRY_PATH = REPO / "config" / "scanner_registry.json"
WORKING = REPO / "working"
WORKING.mkdir(exist_ok=True)

# Auto-load credentials from config/secrets.env (no-op if file is missing).
import sys as _sys

_TOOLS_DIR = str(Path(__file__).parent)
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
if str(WORKSPACE_ROOT) not in _sys.path:
    _sys.path.insert(0, str(WORKSPACE_ROOT))
try:
    import env_loader  # noqa: F401
except Exception:
    pass


# --------------------------------------------------------------------
# Profile weight tables (must match framework/profile_*.md)
# --------------------------------------------------------------------

WEIGHTS = {
    "merger_arb": {
        "spread_size": 3.0,
        "deal_certainty": 2.5,
        "annualized_return": 2.0,
        "break_risk": 1.5,
        "liquidity": 1.0,
    },
    "activist_governance": {
        "signal_strength": 2.0,
        "information_asymmetry": 2.0,
        "activist_track_record": 1.5,
        "risk_reward": 1.5,
        "catalyst_clarity": 1.0,
        "edge_decay": 1.0,
        "liquidity": 1.0,
    },
    "binary_catalyst": {
        "approval_probability": 2.5,
        "market_mispricing": 2.5,
        "magnitude": 1.5,
        "competitive_landscape": 1.5,
        "catalyst_timeline": 1.0,
        "liquidity": 1.0,
    },
    "short_positioning": {
        "crowding_intensity": 2.5,
        "trend_direction": 2.0,
        "catalyst_proximity": 2.0,
        "size_vs_float": 1.5,
        "historical_analog": 1.0,
        "liquidity": 1.0,
    },
    "litigation": {
        "financial_materiality": 3.0,
        "legal_outcome_probability": 2.0,
        "market_pricing": 2.0,
        "resolution_timeline": 1.5,
        "liquidity": 1.0,
        "party_resolution_confidence": 0.5,
    },
    "takeover_candidate": {
        "setup_strength": 3.0,
        "edge_freshness": 2.0,
        "valuation_cushion": 2.0,
        "strategic_buyer_clarity": 2.0,
        "liquidity": 1.0,
    },
}


def weighted_total(dims: Dict[str, int], profile: str) -> float:
    weights = WEIGHTS[profile]
    total = 0.0
    for dim, weight in weights.items():
        raw = dims.get(dim, 0)
        total += raw * weight
    return round(total, 2)


def classify_band(score: float) -> str:
    if score >= 30:
        return "immediate"
    if score >= 20:
        return "watchlist"
    if score >= 10:
        return "archive"
    return "discard"


# --------------------------------------------------------------------
# Auto-cap rules
# --------------------------------------------------------------------

RISK_FREE_RATE = 0.043  # 10Y UST as of 2026-04-16; maintenance task may update
EV_FLOOR = 5.0          # percent


def apply_auto_caps(signal: Dict, dims: Dict[str, int], profile: str, band: str) -> Tuple[str, List[str]]:
    """Return (possibly_capped_band, list_of_triggered_rules)."""
    caps: List[str] = []
    if profile == "merger_arb":
        # Rule A: sub-scale annualized return
        annualized = signal.get("raw_data", {}).get("annualized_return_pct")
        if annualized is not None:
            if annualized < (RISK_FREE_RATE * 100) + 3:
                if band == "immediate":
                    band = "watchlist"
                    caps.append("merger_arb.rule_A_sub_scale_return")
        # Rule B: break risk dominance
        if dims.get("break_risk", 5) == 1 and dims.get("deal_certainty", 5) <= 2:
            if band == "immediate":
                band = "watchlist"
                caps.append("merger_arb.rule_B_break_risk_dominance")

    elif profile == "binary_catalyst":
        # EV floor
        p_approval = signal.get("raw_data", {}).get("approval_probability")
        upside = signal.get("raw_data", {}).get("upside_pct")
        downside = signal.get("raw_data", {}).get("downside_pct")
        if p_approval is not None and upside is not None and downside is not None:
            ev = p_approval * upside - (1 - p_approval) * abs(downside)
            if ev < EV_FLOOR and band == "immediate":
                band = "watchlist"
                caps.append(f"binary_catalyst.ev_floor (ev={ev:.2f})")

    elif profile == "litigation":
        # Party confidence cap
        prc = dims.get("party_resolution_confidence", 5)
        if prc < 3:
            if band in ("immediate", "watchlist"):
                band = "archive"
                caps.append("litigation.party_confidence_cap")

    elif profile == "takeover_candidate":
        # Pre-edge disqualifier: definitive merger agreement already announced
        raw = signal.get("raw_data", {})
        if raw.get("definitive_merger_agreement") is True:
            caps.append("takeover_candidate.post_edge_disqualified")
            return "discard", caps
        # Rejected prior offer in trailing 6 months → cap at archive
        if raw.get("rejected_prior_offer_6mo") is True:
            if band in ("immediate", "watchlist"):
                band = "archive"
                caps.append("takeover_candidate.prior_rejection_cap")
        # Going-concern warning → cap at watchlist
        if raw.get("going_concern_warning") is True:
            if band == "immediate":
                band = "watchlist"
                caps.append("takeover_candidate.going_concern_cap")
        # Fewer than 2 of 5 patterns hit → below triage gate, cap at discard
        patterns_hit = raw.get("patterns_hit", 0)
        if isinstance(patterns_hit, int) and patterns_hit < 2:
            caps.append(f"takeover_candidate.below_triage_gate (patterns={patterns_hit})")
            return "discard", caps

    return band, caps


# --------------------------------------------------------------------
# Signal scoring
# --------------------------------------------------------------------

def score_signal(signal: Dict) -> Dict:
    """Apply the matching profile rubric to a raw signal.

    The signal must already have `scoring_profile` set (by the scanner).
    If not, fall back to `activist_governance` as a safe default.

    Input `signal.raw_data.dimensions` is expected to carry the per-dim
    1–5 scores from the scanner OR from a human reviewer. If any required
    dim is missing, the signal is returned unscored (score=None, band=None,
    missing_dimensions=[...]) rather than filled with defaults — a silent
    default produced a fake 30 for every unscored signal, since every
    profile's weights sum to exactly 10.
    """
    profile = signal.get("scoring_profile") or "activist_governance"
    if profile not in WEIGHTS:
        profile = "activist_governance"

    raw_dims = signal.get("raw_data", {}).get("dimensions") or {}
    required = list(WEIGHTS[profile].keys())
    missing = [d for d in required if d not in raw_dims]
    if missing:
        return {
            "scoring_profile": profile,
            "dimensions": {},
            "score": None,
            "band": None,
            "auto_caps_triggered": [],
            "missing_dimensions": missing,
        }

    dims: Dict[str, int] = {}
    for dim in required:
        v = int(raw_dims[dim])
        dims[dim] = max(1, min(5, v))

    score = weighted_total(dims, profile)
    band = classify_band(score)
    band, caps = apply_auto_caps(signal, dims, profile, band)

    return {
        "scoring_profile": profile,
        "dimensions": dims,
        "score": score,
        "band": band,
        "auto_caps_triggered": caps,
    }


# --------------------------------------------------------------------
# Signal log I/O
# --------------------------------------------------------------------

def load_signal_log() -> List[Dict]:
    if not SIGNAL_LOG.exists():
        return []
    try:
        data = json.loads(SIGNAL_LOG.read_text())
        if isinstance(data, list):
            return data
        # support the legacy-wrapped shape
        if isinstance(data, dict) and "signals" in data:
            return data["signals"]
    except (json.JSONDecodeError, OSError):
        # D-052: if malformed, try loading .bak; otherwise start fresh
        bak = SIGNAL_LOG.with_suffix(SIGNAL_LOG.suffix + ".bak")
        if bak.exists():
            try:
                data = json.loads(bak.read_text())
                if isinstance(data, list):
                    return data
            except Exception:
                pass
    return []


def save_signal_log(signals: List[Dict]) -> None:
    # Keep 90-day rolling window for litigation + 14-day for everything else.
    cutoff_std = datetime.now(timezone.utc) - timedelta(days=14)
    cutoff_lit = datetime.now(timezone.utc) - timedelta(days=90)
    keep: List[Dict] = []
    for s in signals:
        profile = s.get("scoring_profile") or "activist_governance"
        scan_s = s.get("scan_date")
        if not scan_s:
            keep.append(s)
            continue
        try:
            scan_dt = datetime.fromisoformat(scan_s.replace("Z", "+00:00"))
        except Exception:
            keep.append(s)
            continue
        # Ensure tz-aware for comparison with cutoff_std/cutoff_lit
        if scan_dt.tzinfo is None:
            scan_dt = scan_dt.replace(tzinfo=timezone.utc)
        cutoff = cutoff_lit if profile == "litigation" else cutoff_std
        if scan_dt >= cutoff:
            keep.append(s)
    tmp = SIGNAL_LOG.with_suffix(SIGNAL_LOG.suffix + ".tmp")
    tmp.write_text(json.dumps(keep, indent=2))
    # keep a backup of the prior log before replacing
    if SIGNAL_LOG.exists():
        bak = SIGNAL_LOG.with_suffix(SIGNAL_LOG.suffix + ".bak")
        try:
            os.replace(SIGNAL_LOG, bak)
        except OSError:
            pass
    os.replace(tmp, SIGNAL_LOG)


def append_signals(new_signals: List[Dict]) -> Dict:
    existing = load_signal_log()
    # Dedup on (signal_id) OR (source_content_hash + scoring_profile)
    seen_ids = {s.get("signal_id") for s in existing if s.get("signal_id")}
    seen_hashes = {(s.get("source_content_hash"), s.get("scoring_profile")) for s in existing if s.get("source_content_hash")}

    added = 0
    skipped = 0
    for s in new_signals:
        sid = s.get("signal_id")
        ch = s.get("source_content_hash")
        if sid and sid in seen_ids:
            skipped += 1
            continue
        if ch and (ch, s.get("scoring_profile")) in seen_hashes:
            skipped += 1
            continue
        # Score on ingest
        scoring = score_signal(s)
        s["scoring"] = scoring
        existing.append(s)
        added += 1
        if sid:
            seen_ids.add(sid)
        if ch:
            seen_hashes.add((ch, s.get("scoring_profile")))

    save_signal_log(existing)
    return {"added": added, "skipped_duplicates": skipped, "total": len(existing)}

# --------------------------------------------------------------------
# Main — ingest all scanner output files in signals/*_scanner_output.json
# --------------------------------------------------------------------

def _collect_scanner_outputs():
    collected = []
    per_scanner = {}
    for path in sorted(glob.glob(str(REPO / "signals" / "*_scanner_output.json"))):
        try:
            data = json.loads(Path(path).read_text())
        except Exception:
            continue
        # Accept either {"signals": [...]} (modern) or bare [...] (legacy tdnet)
        if isinstance(data, list):
            sigs = data
        elif isinstance(data, dict):
            sigs = data.get("signals") or []
        else:
            continue
        if not isinstance(sigs, list):
            continue
        per_scanner[Path(path).name] = len(sigs)
        for s in sigs:
            if isinstance(s, dict):
                collected.append(s)
    return collected, per_scanner


def _run_post_scan_hooks() -> Dict[str, Any]:
    """Run low-risk post-scan checks after ingestion completes.

    The first pass keeps the live scoring policy intact while adding
    observability, enrichment, and read-mostly monitoring surfaces.
    """
    hooks: Dict[str, Any] = {}

    try:
        import catalyst_calendar  # type: ignore

        result = catalyst_calendar.run(window_days=180, ticker_filter=None, dry_run=False)
        hooks["catalyst_calendar"] = {
            "status": "ok",
            "summary": result.get("summary", {}),
            "out_json": result.get("_out_json"),
            "out_md": result.get("_out_md"),
        }
    except Exception as exc:
        hooks["catalyst_calendar"] = {"status": "error", "error": repr(exc)}

    try:
        import candidate_monitor  # type: ignore

        result = candidate_monitor.run(dry_run=True)
        hooks["candidate_monitor"] = {
            "status": "ok",
            "dry_run": True,
            "tickers_checked": result.get("tickers_checked", []),
            "n_archived": len(result.get("archived", []) or []),
            "n_reviews": len(result.get("reviews", []) or []),
            "errors": (result.get("errors") or [])[:5],
            "report_path": result.get("report_path"),
        }
    except Exception as exc:
        hooks["candidate_monitor"] = {"status": "error", "error": repr(exc)}

    try:
        from migrations import import_candidates  # type: ignore

        result = import_candidates.reconcile_candidates(dry_run=False)
        hooks["legacy_candidate_reconcile"] = {
            "status": "ok",
            "dry_run": False,
            "rows_prepared": result.get("rows_prepared", 0),
            "upserted": result.get("upserted", 0),
            "restored": result.get("restored", []),
            "updated": result.get("updated", []),
            "state_resets": result.get("state_resets", []),
            "skipped_missing_md": result.get("skipped_missing_md", []),
            "skipped_missing_mic": result.get("skipped_missing_mic", []),
            "warnings": (result.get("warnings") or [])[:5],
            "report_path": result.get("report_path"),
        }
    except Exception as exc:
        hooks["legacy_candidate_reconcile"] = {"status": "error", "error": repr(exc)}

    try:
        import validate_signal_log  # type: ignore

        result = validate_signal_log.run(dry_run=False)
        hooks["validate_signal_log"] = {
            "status": "ok",
            "signal_log_size": result.get("signal_log_size"),
            "max_severity": result.get("max_severity"),
            "counts": result.get("counts"),
            "out": result.get("_out"),
        }
    except Exception as exc:
        hooks["validate_signal_log"] = {"status": "error", "error": repr(exc)}

    try:
        import legal_enricher  # type: ignore

        result = legal_enricher.enrich_signal_log()
        hooks["legal_enricher"] = {
            "status": result.get("status", "ok"),
            "enriched": result.get("enriched"),
            "by_color": result.get("by_color"),
            "report_path": result.get("report_path"),
        }
    except Exception as exc:
        hooks["legal_enricher"] = {"status": "error", "error": repr(exc)}

    try:
        import biotech_enricher  # type: ignore

        result = biotech_enricher.enrich_signal_log(online=False)
        hooks["biotech_enricher"] = {
            "status": result.get("status", "ok"),
            "enriched": result.get("enriched"),
            "by_color": result.get("by_color"),
            "tier_histogram": result.get("tier_histogram"),
            "report_path": result.get("report_path"),
        }
    except Exception as exc:
        hooks["biotech_enricher"] = {"status": "error", "error": repr(exc)}

    try:
        import health_check  # type: ignore

        result = health_check.run(dry_run=False, quiet=True)
        hooks["health_check"] = {
            "status": "ok",
            "max_severity": result.get("max_severity"),
            "counts": result.get("counts", {}),
            "out_json": result.get("_out_json"),
            "out_md": result.get("_out_md"),
        }
    except Exception as exc:
        hooks["health_check"] = {"status": "error", "error": repr(exc)}

    try:
        import build_dashboard  # type: ignore

        state = build_dashboard.gather_state()
        html = build_dashboard.render_html(state)
        build_dashboard.DASHBOARD.parent.mkdir(parents=True, exist_ok=True)
        build_dashboard.DASHBOARD.write_text(html, encoding="utf-8")
        hooks["build_dashboard"] = {
            "status": "ok",
            "dashboard": str(build_dashboard.DASHBOARD),
            "signal_count": state.get("signal_count"),
            "health": (state.get("health") or {}).get("max_severity"),
            "live_candidates": len((state.get("candidates") or {}).get("per_ticker_summary", [])),
            "drafts_pending": (state.get("drafts") or {}).get("count", 0),
        }
    except Exception as exc:
        hooks["build_dashboard"] = {"status": "error", "error": repr(exc)}

    return hooks


def main():
    signals, per_scanner = _collect_scanner_outputs()
    result = append_signals(signals)
    report = {
        "ran_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "per_scanner_signals_seen": per_scanner,
        "ingestion": result,
    }
    try:
        report["hooks"] = _run_post_scan_hooks()
    except Exception as exc:
        report["hooks"] = {"status": "error", "error": repr(exc)}
    today = datetime.now(timezone.utc).date().isoformat()
    report_path = WORKING / f"post_scan_report_{today}.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
