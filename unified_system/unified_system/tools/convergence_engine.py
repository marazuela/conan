"""
Multi-Profile Convergence Engine (v2.0 — 2026-04-16)

Runs downstream of all scanners. Groups signals in the rolling log by
`issuer_figi` and detects cross-scanner / cross-profile convergence.

Per D-006:
- Window = 14 days for most profiles; 30 days when any signal in the group
  has profile "litigation".
- Dedup within a group: if two signals share `source_content_hash`, they are
  echoes of the same event (common for cross-listed names) and count as ONE.
- Classify convergence type:
    same_direction: all thesis_directions agree (long+long OR short+short)
    orthogonal: mix of event-driven + positioning signals, same direction
    contradiction: at least one long and one short — flag, do NOT bonus
- Bonus: +5 for 2 independent signals; +10 for 3+; 0 for contradictions.

Output: writes working/convergence_report_YYYY-MM-DD.json with all groups.
Also updates the scoring on each signal in the signal log by adding
`scoring.convergence_bonus` and recomputing `scoring.score_with_bonus`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Tuple
from collections import defaultdict

REPO = Path(__file__).parent.parent
SIGNAL_LOG = REPO / "signals" / "signal_log.json"
WORKING = REPO / "working"
WORKING.mkdir(exist_ok=True)


def _atomic_write(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def _within_window(signal: Dict, now: datetime, window_days: int) -> bool:
    scan_s = signal.get("scan_date") or signal.get("source_date")
    if not scan_s:
        return False
    try:
        scan_dt = datetime.fromisoformat(scan_s.replace("Z", "+00:00"))
    except Exception:
        return False
    # Make timezone aware if naive
    if scan_dt.tzinfo is None:
        scan_dt = scan_dt.replace(tzinfo=timezone.utc)
    return (now - scan_dt) <= timedelta(days=window_days)


def _classify(signals: List[Dict]) -> Tuple[str, int]:
    """Return (convergence_type, bonus)."""
    directions = {s.get("thesis_direction") for s in signals if s.get("thesis_direction") in ("long", "short")}
    if len(directions) == 0:
        return "unknown_direction", 0
    if "long" in directions and "short" in directions:
        return "contradiction", 0

    n_independent = len(signals)
    scanners = {s.get("upstream_scanner") for s in signals}
    profiles = {s.get("scoring_profile") for s in signals}

    if n_independent >= 3:
        bonus = 10
    elif n_independent >= 2:
        bonus = 5
    else:
        return "single", 0

    # Orthogonal = different profiles (positioning + event-driven)
    if len(profiles) >= 2:
        return "orthogonal", bonus
    return "same_direction", bonus


def run_convergence() -> Dict:
    if not SIGNAL_LOG.exists():
        return {"groups": [], "note": "no signal log"}
    try:
        data = json.loads(SIGNAL_LOG.read_text())
    except Exception as e:
        return {"error": f"signal log malformed: {e}"}
    signals: List[Dict] = data if isinstance(data, list) else data.get("signals", [])

    now = datetime.now(timezone.utc)

    # Group by strongest identifier available. Priority:
    #   1. issuer_figi (global canonical)
    #   2. ticker + mic (exchange-local canonical)
    #   3. venue-specific id (codigo_cvm for BR, id_empresa_biva for MX, stock_code for HK/KR)
    #   4. normalized company_name_en (last resort; litigation defendants, misc)
    # If NONE of the above resolves, skip grouping — emit the signal into
    # a synthetic key `unidentified:<signal_id>` so it never collides with
    # an unrelated signal. (Previously all null-id signals bucketed into
    # `None_<MIC>` producing false "convergences".)
    groups: Dict[str, List[Dict]] = defaultdict(list)
    for s in signals:
        profile = s.get("scoring_profile") or "activist_governance"
        window = 30 if profile == "litigation" else 14
        if not _within_window(s, now, window):
            continue

        figi = s.get("issuer_figi")
        ticker = s.get("ticker")
        mic = s.get("mic")
        if figi:
            key = f"figi:{figi}"
        elif ticker and mic:
            key = f"tkr:{ticker}:{mic}"
        elif ticker:
            key = f"tkr:{ticker}"
        elif s.get("codigo_cvm"):
            key = f"cvm:{s['codigo_cvm']}"
        elif s.get("id_empresa_biva"):
            key = f"biva:{s['id_empresa_biva']}"
        elif s.get("stock_code"):
            key = f"sc:{s['stock_code']}:{mic or '?'}"
        elif s.get("company_name_en"):
            # Normalize: lowercase, strip corp suffixes, collapse whitespace
            name = s["company_name_en"].lower()
            import re as _re
            name = _re.sub(r"[,.]", " ", name)
            name = _re.sub(r"\b(inc|corp|llc|ltd|sa|s\.a|s\.a\.|plc|nv|ag|gmbh|kk|co|company)\b", "", name)
            name = _re.sub(r"\s+", " ", name).strip()
            if name:
                key = f"name:{name}"
            else:
                key = f"unidentified:{s.get('signal_id', id(s))}"
        else:
            key = f"unidentified:{s.get('signal_id', id(s))}"
        groups[key].append(s)

    report_groups = []
    for issuer_key, group_signals in groups.items():
        if len(group_signals) < 2:
            continue

        # Dedup by source_content_hash (cross-listing echo protection)
        seen_hashes = set()
        unique_signals: List[Dict] = []
        for s in group_signals:
            h = s.get("source_content_hash")
            if h and h in seen_hashes:
                continue
            if h:
                seen_hashes.add(h)
            unique_signals.append(s)

        if len(unique_signals) < 2:
            continue

        conv_type, bonus = _classify(unique_signals)

        report_groups.append({
            "issuer_key": issuer_key,
            "signal_count": len(unique_signals),
            "raw_signal_count_pre_dedup": len(group_signals),
            "scanners": sorted({s.get("upstream_scanner") for s in unique_signals if s.get("upstream_scanner")}),
            "profiles": sorted({s.get("scoring_profile") for s in unique_signals if s.get("scoring_profile")}),
            "directions": sorted({s.get("thesis_direction") for s in unique_signals if s.get("thesis_direction")}),
            "convergence_type": conv_type,
            "bonus": bonus,
            "signal_ids": [s.get("signal_id") for s in unique_signals if s.get("signal_id")],
            "tickers_seen": sorted({s.get("ticker") for s in unique_signals if s.get("ticker")}),
        })

        # Apply bonus to the highest-scoring signal in the group
        if bonus > 0:
            top = max(unique_signals, key=lambda x: (x.get("scoring", {}) or {}).get("score", 0))
            scoring = top.get("scoring", {})
            scoring["convergence_bonus"] = bonus
            scoring["score_with_bonus"] = round((scoring.get("score", 0) or 0) + bonus, 2)
            # Re-classify band based on boosted score
            new_band = scoring["score_with_bonus"]
            if new_band >= 35:
                scoring["band_with_bonus"] = "immediate"
            elif new_band >= 25:
                scoring["band_with_bonus"] = "watchlist"
            elif new_band >= 15:
                scoring["band_with_bonus"] = "archive"
            else:
                scoring["band_with_bonus"] = "discard"
            top["scoring"] = scoring

    # Write updated signal log with any bonuses
    _atomic_write(SIGNAL_LOG, signals)

    # Write convergence report
    today = datetime.now(timezone.utc).date().isoformat()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "groups": report_groups,
        "n_groups": len(report_groups),
    }
    _atomic_write(WORKING / f"convergence_report_{today}.json", report)
    return report


def main():
    r = run_convergence()
    print(json.dumps(r, indent=2))


if __name__ == "__main__":
    main()

# --- END OF FILE ---
