"""
Convergence engine for Tool 2 (Non-US Discovery System).

Dedup + cross-scanner convergence annotation.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

JACCARD_THRESHOLD = 0.80
CONVERGENCE_WINDOW_DAYS = 14


def _normalize_text(text: str) -> list[str]:
    if not text:
        return []
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return text.split()[:500]


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def _parse_date(s: str) -> datetime:
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _signal_text(signal: dict) -> str:
    raw = signal.get("raw_data")
    if not isinstance(raw, dict):
        return ""
    parts = [raw.get("headline", ""), raw.get("snippet", ""), raw.get("translated_headline", "")]
    return " ".join(str(p) for p in parts if p)


def _content_token_set(signal: dict) -> set[str]:
    return set(_normalize_text(_signal_text(signal)))


def dedup_by_issuer(signals: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    no_figi: list[dict] = []
    for sig in signals:
        figi = sig.get("issuer_figi")
        if not figi:
            no_figi.append(sig)
            continue
        groups.setdefault(figi, []).append(sig)

    all_out: list[dict] = []

    for figi, group in groups.items():
        group.sort(key=lambda s: _parse_date(s.get("source_date", "")))
        token_sets = [_content_token_set(s) for s in group]
        parent: list[int] = list(range(len(group)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            ri, rj = find(i), find(j)
            if ri == rj:
                return
            if ri < rj:
                parent[rj] = ri
            else:
                parent[ri] = rj

        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                si, sj = group[i], group[j]
                di = (si.get("source_date") or "")[:10]
                dj = (sj.get("source_date") or "")[:10]
                ti = si.get("signal_type")
                tj = sj.get("signal_type")
                if di and di == dj and ti and ti == tj:
                    union(i, j)
                    continue
                if _jaccard(token_sets[i], token_sets[j]) >= JACCARD_THRESHOLD:
                    union(i, j)

        clusters: dict[int, list[int]] = {}
        for i in range(len(group)):
            clusters.setdefault(find(i), []).append(i)

        for _root, members in clusters.items():
            members.sort()
            survivor = dict(group[members[0]])
            related = [group[i].get("signal_id") for i in members[1:]]
            if related:
                existing = survivor.get("related_signal_ids") or []
                survivor["related_signal_ids"] = sorted(set(existing) | set(r for r in related if r))
            survivor["dedup_dropped"] = False
            all_out.append(survivor)
            for i in members[1:]:
                d = dict(group[i])
                d["dedup_dropped"] = True
                d["dedup_merged_into"] = survivor.get("signal_id")
                all_out.append(d)

    for sig in no_figi:
        sig = dict(sig)
        sig["dedup_dropped"] = False
        sig["dedup_note"] = "no_issuer_figi_skipped_dedup"
        all_out.append(sig)

    return all_out


def annotate_convergence(
    surviving_signals: list[dict],
    historical_signals: Iterable[dict] = (),
    now: datetime | None = None,
) -> list[dict]:
    if now is None:
        now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=CONVERGENCE_WINDOW_DAYS)

    all_sigs = [s for s in list(surviving_signals) + list(historical_signals) if not s.get("dedup_dropped")]

    strat_by_figi: dict[str, set[str]] = {}
    for sig in all_sigs:
        if _parse_date(sig.get("source_date", "")) < window_start:
            continue
        figi = sig.get("issuer_figi")
        if not figi:
            continue
        strat = None
        raw = sig.get("raw_data")
        if isinstance(raw, dict):
            strat = raw.get("scanner_name")
        strat = strat or sig.get("_scanner") or sig.get("exchange")
        if not strat:
            continue
        strat_by_figi.setdefault(figi, set()).add(strat)

    out: list[dict] = []
    for sig in surviving_signals:
        sig = dict(sig)
        if sig.get("dedup_dropped"):
            sig["convergence_strategy_count"] = 0
            sig["convergence_bonus"] = 0
            out.append(sig)
            continue
        figi = sig.get("issuer_figi")
        distinct = len(strat_by_figi.get(figi, set())) if figi else 0
        sig["convergence_strategy_count"] = distinct
        if distinct >= 3:
            sig["convergence_bonus"] = 8
        elif distinct == 2:
            sig["convergence_bonus"] = 4
        else:
            sig["convergence_bonus"] = 0
        out.append(sig)
    return out


def process(new_signals: list[dict], historical_signals: Iterable[dict] = ()) -> list[dict]:
    combined = list(new_signals) + list(historical_signals)
    deduped = dedup_by_issuer(combined)
    new_ids = {s.get("signal_id") for s in new_signals}
    new_deduped = [s for s in deduped if s.get("signal_id") in new_ids]
    historical_deduped = [s for s in deduped if s.get("signal_id") not in new_ids]
    annotated = annotate_convergence(new_deduped, historical_deduped)
    for s in new_deduped:
        if not any(a.get("signal_id") == s.get("signal_id") for a in annotated):
            annotated.append(s)
    return annotated


if __name__ == "__main__":
    import json, sys
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            sigs = json.load(f)
    else:
        sigs = [
            {"signal_id": "a", "issuer_figi": "BBG001", "source_date": "2026-04-10",
             "exchange": "LSE", "signal_type": "takeover_firm_offer",
             "raw_data": {"headline": "Acme Rule 2.7 firm offer"}},
            {"signal_id": "b", "issuer_figi": "BBG001", "source_date": "2026-04-10",
             "exchange": "LSE", "signal_type": "takeover_firm_offer",
             "raw_data": {"headline": "Recommended Cash Offer for Acme"}},
        ]
    print(json.dumps(process(sigs), indent=2))
