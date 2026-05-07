"""
Observability functions for Conan v2 — spec §7.6.

Four scheduled Modal functions that replace v1's `maintenance` skill's mechanical
sweeps. All write to the `operator_flags` table (spec §3.4) as a common structured
surface. No Claude routine calls; these are deterministic sanity sweeps.

  7.6.1 translation_health          — daily 02:00 UTC
  7.6.2 scanner_probe               — every 6h at :15
  7.6.3 convergence_qa              — daily 03:00 UTC
  7.6.4 litigation_baselines_refresh — weekly Sun 04:00 UTC

Scheduled via Modal decorators in `app.py`; the implementations live here to keep
app.py a thin declaration file.

operator_flags upsert semantics:
  Partial unique on (source, kind, subject-tuple) WHERE resolved_at IS NULL.
  Producers INSERT … ON CONFLICT DO UPDATE to bump `evidence` on re-occurrence.
  When the condition clears, producers PATCH `resolved_at` + `resolved_note`.
"""

from __future__ import annotations

import json
import random
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError


# ===========================================================================
# operator_flags helpers
# ===========================================================================

def _upsert_flag(
    client: SupabaseClient,
    *,
    severity: str,
    source: str,
    kind: str,
    title: str,
    body: Optional[str] = None,
    evidence: Optional[Dict[str, Any]] = None,
    scanner_id: Optional[str] = None,
    entity_id: Optional[str] = None,
    signal_id: Optional[str] = None,
    candidate_id: Optional[str] = None,
) -> Dict[str, Any]:
    """UPSERT an operator_flag via explicit GET-then-PATCH-or-INSERT.

    The table's uniqueness is enforced by the partial expression index
    `operator_flags_open_uniq` (COALESCE-normalised subject tuple, WHERE
    resolved_at IS NULL). PostgREST's `on_conflict` query param can't target
    partial/expression indexes, so we do the upsert manually: look for an
    existing open flag with the same (source, kind, subject-tuple), PATCH it
    if found (bump evidence/title/body), else INSERT a new row.
    """
    filt = {
        "source": f"eq.{source}",
        "kind": f"eq.{kind}",
        "resolved_at": "is.null",
    }
    for col, val in (
        ("scanner_id", scanner_id),
        ("entity_id", entity_id),
        ("signal_id", signal_id),
        ("candidate_id", candidate_id),
    ):
        filt[col] = f"eq.{val}" if val else "is.null"

    existing = client._rest(
        "GET", "operator_flags",
        params={**filt, "select": "id", "limit": 1},
    ) or []

    if existing:
        flag_id = existing[0]["id"]
        client._rest(
            "PATCH", "operator_flags",
            params={"id": f"eq.{flag_id}"},
            json_body={
                "title": title,
                "body": body,
                "evidence": evidence or {},
                "severity": severity,
            },
            prefer="return=minimal",
        )
        return {"id": flag_id, "action": "updated"}

    row = {
        "severity": severity,
        "source": source,
        "kind": kind,
        "title": title,
        "body": body,
        "evidence": evidence or {},
        "scanner_id": scanner_id,
        "entity_id": entity_id,
        "signal_id": signal_id,
        "candidate_id": candidate_id,
    }
    inserted = client._rest(
        "POST", "operator_flags",
        json_body=row,
        prefer="return=representation",
    )
    return inserted[0] if inserted else {"action": "inserted"}


def _resolve_flag(
    client: SupabaseClient,
    *,
    source: str,
    kind: str,
    note: str,
    scanner_id: Optional[str] = None,
) -> int:
    """PATCH any open flag matching (source, kind[, scanner_id]) → resolved_at=now()."""
    filt = {
        "source": f"eq.{source}",
        "kind": f"eq.{kind}",
        "resolved_at": "is.null",
    }
    if scanner_id:
        filt["scanner_id"] = f"eq.{scanner_id}"
    resp_rows = client._rest(
        "PATCH",
        "operator_flags",
        params=filt,
        json_body={"resolved_at": datetime.now(timezone.utc).isoformat(), "resolved_note": note},
        prefer="return=representation",
    )
    return len(resp_rows or [])


def record_snapshot_fetch_failure(
    client: SupabaseClient,
    *,
    scanner_name: str,
    ticker: str,
    exc: BaseException,
) -> None:
    """Log a market_snapshot fetch failure to operator_flags.

    Replaces the bare `except Exception: continue/pass` at scanner caller
    sites (esma / takeover / fda) so provider outages become visible via
    open flags instead of silent-drop. The partial unique index on
    (source, kind, open) collapses repeated upserts to one row per scanner,
    with evidence carrying the most recent failure. Never raises — flag
    writing must not break the scanner loop.
    """
    try:
        _upsert_flag(
            client,
            severity="info",
            source=f"scanner:{scanner_name}",
            kind="market_snapshot_fetch_failed",
            title=f"market_snapshot fetch failed for {ticker}",
            body=f"{type(exc).__name__}: {exc}",
            evidence={
                "ticker": ticker,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:400],
            },
        )
    except Exception:  # noqa: BLE001 — observability MUST NOT break scanners
        pass


# ===========================================================================
# 7.6.1 translation_health
# ===========================================================================

# Scanners that emit translation_confidence in signals.raw_payload.
_NON_ENGLISH_SCANNERS = (
    "tdnet_scanner", "kind_scanner", "cvm_scanner",
    "bmv_scanner", "sedar_plus_scanner", "hkex_scanner", "bse_nse_scanner",
)

_TRANSLATION_MEDIAN_WARN = 0.75
_TRANSLATION_MEDIAN_CRITICAL_DAY = 0.70
_TRANSLATION_CRITICAL_STREAK_DAYS = 7


def translation_health(client: Optional[SupabaseClient] = None) -> Dict[str, Any]:
    """Daily: compute rolling 30d median translation_confidence per non-English
    scanner; flag warn <0.75; critical if 7-day streak of day-median <0.70."""
    client = client or SupabaseClient()
    summary: Dict[str, Any] = {"function": "translation_health", "per_scanner": {}}

    # One query per scanner to avoid huge join; small enough table.
    thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    scanners = client._rest(
        "GET", "scanners",
        params={"select": "id,name", "name": f"in.({','.join(_NON_ENGLISH_SCANNERS)})"},
    ) or []

    for sc in scanners:
        sc_id, sc_name = sc["id"], sc["name"]
        # 30-day sample
        rows30 = client._rest(
            "GET", "signals",
            params={
                "select": "raw_payload,scan_date",
                "scanner_id": f"eq.{sc_id}",
                "scan_date": f"gte.{thirty_days_ago}",
            },
        ) or []
        confs = [
            float(r["raw_payload"].get("translation_confidence"))
            for r in rows30
            if isinstance(r.get("raw_payload"), dict)
            and r["raw_payload"].get("translation_confidence") is not None
        ]
        if not confs:
            summary["per_scanner"][sc_name] = {"n": 0, "note": "no translation_confidence in 30d"}
            continue

        median30 = statistics.median(confs)
        p25 = statistics.quantiles(confs, n=4)[0] if len(confs) >= 4 else min(confs)

        # 7-day streak: day-by-day median < 0.70?
        streak_breaking = False
        streak_day_count = 0
        for days_back in range(_TRANSLATION_CRITICAL_STREAK_DAYS):
            day_start = datetime.now(timezone.utc) - timedelta(days=days_back + 1)
            day_end = datetime.now(timezone.utc) - timedelta(days=days_back)
            day_vals = [
                float(r["raw_payload"].get("translation_confidence"))
                for r in rows30
                if isinstance(r.get("raw_payload"), dict)
                and r["raw_payload"].get("translation_confidence") is not None
                and day_start.isoformat() <= r["scan_date"] < day_end.isoformat()
            ]
            if day_vals:
                day_median = statistics.median(day_vals)
                if day_median < _TRANSLATION_MEDIAN_CRITICAL_DAY:
                    streak_day_count += 1
                else:
                    streak_breaking = True
                    break
            # empty days don't break the streak; they just don't count toward it
        critical_streak = (
            streak_day_count >= _TRANSLATION_CRITICAL_STREAK_DAYS and not streak_breaking
        )

        per = {"n": len(confs), "median_30d": round(median30, 3), "p25_30d": round(p25, 3)}
        summary["per_scanner"][sc_name] = per

        if critical_streak:
            _upsert_flag(
                client,
                severity="critical",
                source="translation_health",
                kind="translation_confidence_trend",
                scanner_id=sc_id,
                title=f"{sc_name}: translation day-median <0.70 for {streak_day_count} consecutive days",
                evidence={**per, "streak_days": streak_day_count, "threshold": _TRANSLATION_MEDIAN_CRITICAL_DAY},
            )
        elif median30 < _TRANSLATION_MEDIAN_WARN:
            _upsert_flag(
                client,
                severity="warn",
                source="translation_health",
                kind="translation_confidence_trend",
                scanner_id=sc_id,
                title=f"{sc_name}: translation median {median30:.3f} over 30d (threshold {_TRANSLATION_MEDIAN_WARN})",
                evidence=per,
            )
        elif median30 >= 0.80:
            # Auto-resolve if the flag was open.
            _resolve_flag(
                client,
                source="translation_health",
                kind="translation_confidence_trend",
                scanner_id=sc_id,
                note=f"auto-resolved: median_30d={median30:.3f} recovered",
            )

    return summary


# ===========================================================================
# 7.6.2 scanner_probe
# ===========================================================================

_PROBE_TIMEOUT_S = 15.0
_BODY_PEEK_BYTES = 256


def _substitute_url_template(url: str, today: Optional[datetime] = None) -> str:
    """Replace common date-placeholder tokens in a registry URL so we probe the
    real resource, not a literal `{YYYYMMDD}` string. Scanners like tdnet store
    `.../I_list_001_{YYYYMMDD}.html` in `endpoints.primary`; the scanner
    itself interpolates at fetch time. The probe must do the same."""
    t = today or datetime.now(timezone.utc)
    replacements = (
        ("{YYYY-MM-DD}", t.strftime("%Y-%m-%d")),
        ("{YYYYMMDD}", t.strftime("%Y%m%d")),
        ("{YYYY}", t.strftime("%Y")),
        ("{MM}", t.strftime("%m")),
        ("{DD}", t.strftime("%d")),
    )
    for placeholder, value in replacements:
        url = url.replace(placeholder, value)
    return url


def _should_skip_probe(scanner: Dict[str, Any]) -> Optional[str]:
    """Return a reason string if this scanner should be skipped entirely, else None.

    Skip conditions:
    - `config.probe_skip_reason` set (e.g. bse_nse geo-block — deferred per v2.1).
    - `config.requires_auth=true` (e.g. courtlistener, kind — scanner runs with
      graceful `auth_required` envelope when the token is missing; probing
      without the token would 401/403 and produce noise, not signal).
    - `last_run_status='auth_required'` as a defensive fallback once the scanner
      has reported its own auth state.
    """
    cfg = scanner.get("config") or {}
    if cfg.get("probe_skip_reason"):
        return str(cfg["probe_skip_reason"])
    if cfg.get("requires_auth"):
        return "requires_auth (token not provisioned)"
    if scanner.get("last_run_status") == "auth_required":
        return "last_run_status=auth_required"
    return None


def scanner_probe(client: Optional[SupabaseClient] = None) -> Dict[str, Any]:
    """Every 6h at :15: probe each operational scanner's primary endpoint.
    Updates scanners.last_probe_* columns; flags drift via operator_flags.
    Does NOT auto-repair (v1 behavior explicitly dropped in v2 spec)."""
    client = client or SupabaseClient()
    summary: Dict[str, Any] = {"function": "scanner_probe", "results": [], "skipped": []}

    scanners = client._rest(
        "GET", "scanners",
        params={"select": "id,name,endpoints,config,last_run_status", "status": "eq.operational"},
    ) or []

    for sc in scanners:
        sc_id, sc_name = sc["id"], sc["name"]

        skip_reason = _should_skip_probe(sc)
        if skip_reason:
            summary["skipped"].append({"scanner": sc_name, "reason": skip_reason})
            # Record the check timestamp but leave last_probe_status/latency NULL
            # so the dashboard can distinguish "recently evaluated, intentionally
            # skipped" from genuine drift.
            client._rest(
                "PATCH", "scanners",
                params={"id": f"eq.{sc_id}"},
                json_body={
                    "last_probe_at": datetime.now(timezone.utc).isoformat(),
                    "last_probe_status": None,
                    "last_probe_latency_ms": None,
                },
            )
            # Auto-resolve any pre-existing drift flag — the scanner is
            # intentionally unprobed, not broken.
            _resolve_flag(
                client,
                source="scanner_probe",
                kind="endpoint_drift",
                scanner_id=sc_id,
                note=f"skipped: {skip_reason}",
            )
            continue

        endpoints = sc.get("endpoints") or {}
        primary = endpoints.get("primary") or endpoints.get("endpoint_primary")
        fallbacks = endpoints.get("fallbacks") or []
        if not primary:
            continue

        primary = _substitute_url_template(primary)
        fallbacks = [_substitute_url_template(fb) for fb in fallbacks]

        status, latency_ms, body_size = _probe_url(primary)
        probe_status = "ok" if 200 <= status < 300 else "error"
        fallback_used = None

        if probe_status == "error":
            for fb in fallbacks:
                fb_status, fb_latency, fb_body = _probe_url(fb)
                if 200 <= fb_status < 300:
                    fallback_used = fb
                    probe_status = "fallback"
                    break
            if probe_status == "error":
                probe_status = "drift"

        # Update scanner row with probe result.
        client._rest(
            "PATCH", "scanners",
            params={"id": f"eq.{sc_id}"},
            json_body={
                "last_probe_at": datetime.now(timezone.utc).isoformat(),
                "last_probe_status": probe_status,
                "last_probe_latency_ms": latency_ms,
            },
        )

        result = {"scanner": sc_name, "status": probe_status, "latency_ms": latency_ms}
        summary["results"].append(result)

        if probe_status == "drift":
            _upsert_flag(
                client,
                severity="critical",
                source="scanner_probe",
                kind="endpoint_drift",
                scanner_id=sc_id,
                title=f"{sc_name}: endpoint drift — primary + fallbacks all non-2xx",
                evidence={"primary_status": status, "primary_url": primary, "latency_ms": latency_ms},
            )
        elif probe_status == "fallback":
            _upsert_flag(
                client,
                severity="warn",
                source="scanner_probe",
                kind="endpoint_fallback_active",
                scanner_id=sc_id,
                title=f"{sc_name}: using fallback endpoint",
                evidence={"primary_status": status, "fallback": fallback_used},
            )
        else:
            _resolve_flag(
                client,
                source="scanner_probe",
                kind="endpoint_drift",
                scanner_id=sc_id,
                note=f"auto-resolved: probe ok ({latency_ms}ms)",
            )
            _resolve_flag(
                client,
                source="scanner_probe",
                kind="endpoint_fallback_active",
                scanner_id=sc_id,
                note=f"auto-resolved: primary probe ok ({latency_ms}ms)",
            )

    return summary


def _probe_url(url: str) -> tuple[int, int, int]:
    t0 = time.time()
    try:
        r = requests.get(
            url, timeout=_PROBE_TIMEOUT_S, allow_redirects=True,
            headers={"User-Agent": "conan-v2 scanner_probe (ops@solutz.com)"},
            stream=True,
        )
        chunk = r.raw.read(_BODY_PEEK_BYTES, decode_content=False) or b""
        latency_ms = int((time.time() - t0) * 1000)
        return r.status_code, latency_ms, len(chunk)
    except requests.RequestException:
        latency_ms = int((time.time() - t0) * 1000)
        return 0, latency_ms, 0


# ===========================================================================
# 7.6.3 convergence_qa
# ===========================================================================

_QA_SAMPLE_SIZE = 20


def convergence_qa(client: Optional[SupabaseClient] = None) -> Dict[str, Any]:
    """Daily 03:00 UTC: sample recent convergence decisions and verify the
    reactor's output against the pure-Python rubric_engine.convergence_reference().
    Critical flag on any mismatch."""
    client = client or SupabaseClient()
    summary: Dict[str, Any] = {"function": "convergence_qa", "sampled": 0, "mismatches": 0}

    from modal_workers.shared.rubric_engine import convergence_reference  # type: ignore

    yesterday = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = client._rest(
        "GET", "signals",
        params={
            "select": "signal_id,entity_id,issuer_figi,scoring_profile,thesis_direction,"
                      "score,score_with_bonus,band_with_bonus,convergence_key,convergence_bonus,"
                      "source_content_hash,scan_date,convergence_evaluated_at",
            "convergence_evaluated_at": f"gte.{yesterday}",
            "convergence_bonus": "gt.0",
            "order": "convergence_evaluated_at.desc",
            "limit": 200,
        },
    ) or []

    if not rows:
        return summary

    sample = random.sample(rows, min(_QA_SAMPLE_SIZE, len(rows)))
    summary["sampled"] = len(sample)

    for row in sample:
        # Reconstruct the group the reactor actually saw — three fidelity rules:
        # (1) Window must be anchored to when the reactor evaluated, not "now".
        #     Otherwise the window slides forward by hours and aged-out siblings
        #     produce spurious bonus=0 mismatches.
        # (2) Litigation-expansion is decided by whether ANY group member is
        #     litigation, not by the sampled row's own profile (parity with
        #     reactor/index.ts:211-212 which checks the whole group).
        # (3) Unscored siblings (score IS NULL) are dropped before classification
        #     to match the reactor's filter at reactor/index.ts:219.
        evaluated_at_iso = row.get("convergence_evaluated_at") or row.get("scan_date")
        evaluated_at = datetime.fromisoformat(evaluated_at_iso.replace("Z", "+00:00"))

        def _fetch_window(win_days: int) -> List[Dict[str, Any]]:
            since = (evaluated_at - timedelta(days=win_days)).isoformat()
            grp = client._rest(
                "GET", "signals",
                params={
                    "select": "signal_id,scoring_profile,thesis_direction,score,source_content_hash",
                    "convergence_key": f"eq.{row['convergence_key']}",
                    "scan_date": f"gte.{since}",
                },
            ) or []
            if row["convergence_key"] and row["convergence_key"].startswith("figi:") and row.get("issuer_figi"):
                extra = client._rest(
                    "GET", "signals",
                    params={
                        "select": "signal_id,scoring_profile,thesis_direction,score,source_content_hash",
                        "issuer_figi": f"eq.{row['issuer_figi']}",
                        "convergence_key": "is.null",
                        "scan_date": f"gte.{since}",
                    },
                ) or []
                seen_ids = {g["signal_id"] for g in grp}
                for e in extra:
                    if e["signal_id"] not in seen_ids:
                        grp.append(e)
            return grp

        group = _fetch_window(14)
        if any(s.get("scoring_profile") == "litigation" for s in group):
            group = _fetch_window(30)
            win_days = 30
        else:
            win_days = 14
        group = [s for s in group if s.get("score") is not None]

        ref = convergence_reference(group)

        # Tolerances: bonus exact; winner_signal_id exact. convergence_key is
        # resolved upstream by the reactor from entity_identifiers and is not
        # recomputed here — the audit only verifies rubric outputs the reference
        # implementation actually produces (bonus, winner).
        reactor_bonus = int(row.get("convergence_bonus") or 0)
        mismatch_keys: List[str] = []
        if int(ref.get("bonus", 0)) != reactor_bonus:
            mismatch_keys.append(
                f"bonus: reactor={reactor_bonus} ref={ref.get('bonus')}"
            )
        ref_winner = ref.get("winner_signal_id")
        if ref_winner is not None and ref_winner != row["signal_id"] and reactor_bonus > 0:
            # Reactor stamps bonus only on the winner row, so if we sampled a
            # bonus>0 row the reference must pick the same winner.
            mismatch_keys.append(
                f"winner: reactor={row['signal_id']} ref={ref_winner}"
            )

        if mismatch_keys:
            summary["mismatches"] += 1
            _upsert_flag(
                client,
                severity="critical",
                source="convergence_qa",
                kind="convergence_disagreement",
                signal_id=row["signal_id"],
                title=f"convergence_qa mismatch on {row['signal_id']}",
                evidence={
                    "reactor": {
                        "bonus": reactor_bonus,
                        "key": row["convergence_key"],
                        "winner_signal_id": row["signal_id"],
                    },
                    "reference": ref,
                    "delta": mismatch_keys,
                    "window_days": win_days,
                },
            )

    # Orphan-alert sweep: alerts whose signal no longer has band_with_bonus='immediate'.
    orphans = client._rest(
        "GET", "alerts",
        params={
            "select": "id,signal_id,signals(signal_id,band_with_bonus)",
            "created_at": f"gte.{yesterday}",
        },
    ) or []
    orphan_count = 0
    for a in orphans:
        sig = (a.get("signals") or {})
        if sig and sig.get("band_with_bonus") != "immediate":
            orphan_count += 1
    if orphan_count > 0:
        _upsert_flag(
            client,
            severity="warn",
            source="convergence_qa",
            kind="orphan_alert",
            title=f"convergence_qa: {orphan_count} alert(s) whose signal is no longer immediate",
            evidence={"count_24h": orphan_count},
        )
    summary["orphan_alerts"] = orphan_count

    return summary


# ===========================================================================
# 7.6.4 litigation_baselines_refresh
# ===========================================================================

_PARTY_RESOLUTION_TTL_DAYS = 180
_PARTY_REVERIFY_BUDGET_PER_PASS = 50
_DEF14A_TTL_DAYS = 90
_EXHIBIT21_TTL_DAYS = 90

_LITIGATION_SCANNERS = ("courtlistener_scanner", "sec_enforcement_scanner")


def litigation_baselines_refresh(client: Optional[SupabaseClient] = None) -> Dict[str, Any]:
    """Weekly Sun 04:00 UTC. Short-circuits if both litigation scanners are
    auth_required/deprecated. Otherwise:
      1. Re-verify up to 50 party-resolution cache entries older than 180d.
      2. Flag staleness on DEF 14A + Exhibit 21 baselines (no auto-refresh — v1
         policy preserved; those refreshes are too expensive to run autonomously).
    """
    client = client or SupabaseClient()
    summary: Dict[str, Any] = {"function": "litigation_baselines_refresh"}

    # Short-circuit if litigation is dormant.
    scs = client._rest(
        "GET", "scanners",
        params={"select": "name,status", "name": f"in.({','.join(_LITIGATION_SCANNERS)})"},
    ) or []
    active = [s for s in scs if s["status"] == "operational"]
    if not active:
        summary["skipped"] = "all litigation scanners auth_required/deprecated"
        return summary

    # (1) Party-resolution cache re-verification.
    cache_blob = client.read_cache("litigation", "party_resolution_cache.json")
    if cache_blob:
        try:
            cache = json.loads(cache_blob.decode("utf-8"))
        except Exception:
            cache = {}
        stale: List[str] = []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_PARTY_RESOLUTION_TTL_DAYS)).isoformat()
        for party, rec in list(cache.items()):
            if not isinstance(rec, dict):
                continue
            last_verified = rec.get("last_verified", "1970-01-01")
            if last_verified < cutoff:
                stale.append(party)
            if len(stale) >= _PARTY_REVERIFY_BUDGET_PER_PASS:
                break
        summary["party_reverify_queued"] = len(stale)
        summary["party_cache_total"] = len(cache)
        # The actual re-verification is out-of-scope for v2 scanners-only Modal
        # scope; emit a flag listing the stale entries so an operator or future
        # specialist job can drain them.
        if stale:
            _upsert_flag(
                client,
                severity="info",
                source="litigation_baselines",
                kind="party_cache_reverify_due",
                title=f"{len(stale)} party-resolution entries past 180d",
                evidence={"sample": stale[:10], "total_due": len(stale)},
            )
    else:
        summary["party_cache_total"] = 0
        _upsert_flag(
            client,
            severity="info",
            source="litigation_baselines",
            kind="party_cache_missing",
            title="party_resolution_cache.json not present in scanner-caches/litigation/",
            evidence={},
        )

    # (2) DEF 14A + Exhibit 21 staleness.
    for baseline_key, ttl_days, flag_kind in (
        ("executive_lookup.json", _DEF14A_TTL_DAYS, "baseline_stale_def14a"),
        ("exhibit21_subsidiary_table.json", _EXHIBIT21_TTL_DAYS, "baseline_stale_exhibit21"),
    ):
        baseline_blob = client.read_cache("litigation", baseline_key)
        stale_flag = False
        detail: Dict[str, Any] = {"key": baseline_key}
        if baseline_blob is None:
            stale_flag = True
            detail["reason"] = "not present"
        else:
            try:
                payload = json.loads(baseline_blob.decode("utf-8"))
                last_refreshed = (payload.get("_meta") or {}).get("last_refreshed")
                if last_refreshed:
                    detail["last_refreshed"] = last_refreshed
                    if last_refreshed < (
                        datetime.now(timezone.utc) - timedelta(days=ttl_days)
                    ).isoformat():
                        stale_flag = True
                        detail["reason"] = f"last_refreshed > {ttl_days}d ago"
                else:
                    detail["reason"] = "no _meta.last_refreshed in payload"
                    stale_flag = True
            except Exception as e:
                detail["reason"] = f"parse_error: {e}"
                stale_flag = True

        if stale_flag:
            _upsert_flag(
                client,
                severity="warn",
                source="litigation_baselines",
                kind=flag_kind,
                title=f"{baseline_key} refresh due",
                body=(
                    "No auto-refresh — v1 policy preserved (too expensive for autonomous "
                    "run). Operator should schedule a manual refresh pass."
                ),
                evidence=detail,
            )
        else:
            _resolve_flag(
                client,
                source="litigation_baselines",
                kind=flag_kind,
                note=f"auto-resolved: {baseline_key} fresh ({detail.get('last_refreshed')})",
            )
        summary[flag_kind] = detail

    return summary


# ===========================================================================
# orphan_convergence_sweeper — heal signals dropped by webhook burst
# ===========================================================================
#
# Symptom (observed 2026-04-21): when a scanner bulk-INSERTs 100+ signals in
# the same minute (ESMA dumped 236, takeover_candidate 115), ~7% of the
# per-row reactor webhook invocations silently time out before `stampRow`
# commits — the signal gets scored (band='immediate' etc.) but never
# convergence-evaluated (`band_with_bonus IS NULL`). Failures never reach
# `failed_reactor_events` because they're timeouts, not 5xx responses.
# One such orphan was band='immediate' → a silently-missed SLA alert.
#
# Fix: sweep signals that are scored but stale-orphaned and re-invoke the
# reactor edge function serially. The reactor path is idempotent — alerts
# + thesis_jobs have ON CONFLICT DO NOTHING, stampRow is a plain UPDATE —
# so re-invocation is safe.

import os as _os  # avoid shadowing observability module's `os` if any

_ORPHAN_MIN_AGE_SECONDS = 300       # 5 min — give the live reactor time to land
_ORPHAN_BATCH_LIMIT = 250           # per-sweep cap
_ORPHAN_REACTOR_TIMEOUT_S = 30.0    # per-call wall clock


def orphan_convergence_sweeper(client: Optional[SupabaseClient] = None) -> Dict[str, Any]:
    """Find signals scored but never convergence-evaluated and re-invoke the
    reactor for each. Runs as part of dispatch_observability every 6h."""
    client = client or SupabaseClient()
    summary: Dict[str, Any] = {"function": "orphan_convergence_sweeper"}

    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=_ORPHAN_MIN_AGE_SECONDS)).isoformat()
    orphans = client._rest(
        "GET", "signals",
        params={
            "select": "*",
            "score": "not.is.null",
            "band_with_bonus": "is.null",
            "scan_date": f"lt.{cutoff}",
            "order": "scan_date.asc",
            "limit": str(_ORPHAN_BATCH_LIMIT),
        },
    ) or []
    summary["orphans_found"] = len(orphans)

    if not orphans:
        # Clear any prior flag — we were healed.
        _resolve_flag(
            client,
            source="reactor",
            kind="orphan_convergence_stuck",
            note="auto-resolved: no orphans past cutoff",
        )
        return summary

    reactor_url = _os.environ.get(
        "REACTOR_URL",
        f"{client.url}/functions/v1/reactor",
    )
    webhook_secret = _os.environ.get("WEBHOOK_SECRET", "")
    headers = {"Content-Type": "application/json"}
    if webhook_secret:
        headers["x-supabase-webhook-secret"] = webhook_secret
    # Fallback auth for edge functions that require Authorization: most Supabase
    # projects accept the service key as a bearer for function invocation even
    # when the webhook path is bypassed.
    headers["Authorization"] = f"Bearer {client.service_key}"

    healed, failed = 0, 0
    error_samples: List[str] = []
    immediate_healed: List[str] = []

    for sig in orphans:
        payload = {
            "type": "INSERT",
            "table": "signals",
            "schema": "public",
            "record": sig,
            "old_record": None,
        }
        try:
            r = requests.post(
                reactor_url, json=payload, headers=headers,
                timeout=_ORPHAN_REACTOR_TIMEOUT_S,
            )
            if 200 <= r.status_code < 300:
                healed += 1
                if sig.get("band") == "immediate":
                    immediate_healed.append(sig["signal_id"])
            else:
                failed += 1
                if len(error_samples) < 3:
                    error_samples.append(
                        f"{sig['signal_id']}: HTTP {r.status_code} {r.text[:120]}"
                    )
        except requests.RequestException as e:
            failed += 1
            if len(error_samples) < 3:
                error_samples.append(f"{sig['signal_id']}: {type(e).__name__}: {e}")

    summary.update({
        "healed": healed,
        "failed": failed,
        "immediate_healed": immediate_healed,
        "errors_sample": error_samples,
    })

    # Flag only when failures persist. A healthy sweep (100% success OR empty)
    # auto-resolves any prior flag.
    if failed > 0:
        _upsert_flag(
            client,
            severity="warn",
            source="reactor",
            kind="orphan_convergence_stuck",
            title=f"orphan_convergence_sweeper: {failed}/{len(orphans)} reactor replays failed",
            evidence={
                "orphans_found": len(orphans),
                "healed": healed,
                "failed": failed,
                "immediate_healed": immediate_healed,
                "errors_sample": error_samples,
            },
        )
    else:
        _resolve_flag(
            client,
            source="reactor",
            kind="orphan_convergence_stuck",
            note=f"auto-resolved: healed {healed}/{len(orphans)} orphans",
        )

    return summary


# ===========================================================================
# thesis_jobs_sla_sweeper — detect + unblock stuck queue rows
#
# Pattern-copy from orphan_convergence_sweeper. Each queue status has its own
# age threshold. We stamp one flag per breaching status (kind=f"sla_breach_{status}"),
# auto-resolving when that status clears. The only status we auto-reset is
# `scoring` — a crashed signal_resolver worker is the most common cause, and
# the transition `scoring → needs_scoring` is idempotent. Drafting is human-
# in-the-loop (Claude skill); we flag but don't touch it.
#
# Age uses `updated_at` today. That's imprecise (re-bumps on unrelated patches)
# but the plan defers adding a dedicated `status_entered_at` column — ship the
# flag sweeper first, see if noise is a problem, then upgrade.
# ===========================================================================

_SLA_THRESHOLDS_S: Dict[str, int] = {
    "needs_scoring": 30 * 60,   # 30min — signal_resolver claim latency budget
    "scoring": 15 * 60,         # 15min — dim estimation should be fast
    "queued": 60 * 60,          # 60min — thesis_writer poll + draft
    "drafting": 45 * 60,        # 45min — Claude skill wall-clock
}
_SLA_BATCH_LIMIT = 200
_SLA_SAMPLE_SIZE = 5
_SCORING_AUTO_RESET_MAX_ATTEMPTS = 3
# F-216: long-horizon "really stuck" threshold for needs_scoring keyed on
# created_at. Catches rows the regular SLA sweep can hide when updated_at
# re-bumps on unrelated patches.
_NEEDS_SCORING_AGED_THRESHOLD_DAYS = 7


def _age_seconds(updated_at: Any, now: datetime) -> Optional[int]:
    if not isinstance(updated_at, str):
        return None
    try:
        ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int((now - ts).total_seconds())


def thesis_jobs_sla_sweeper(client: Optional[SupabaseClient] = None) -> Dict[str, Any]:
    """Detect stuck thesis_jobs rows and — for `scoring` specifically —
    auto-reset to `needs_scoring` (3.2) until attempt_count exceeds the
    retry budget, at which point the row is promoted to `dlq`."""
    client = client or SupabaseClient()
    now = datetime.now(timezone.utc)
    summary: Dict[str, Any] = {
        "function": "thesis_jobs_sla_sweeper",
        "breaches_by_status": {},
        "scoring_resets": 0,
        "scoring_dlqs": 0,
    }

    for status, threshold_s in _SLA_THRESHOLDS_S.items():
        cutoff = (now - timedelta(seconds=threshold_s)).isoformat()
        rows = client._rest(
            "GET", "thesis_jobs",
            params={
                # Embedded select pulls scoring_profile from the signals FK in one roundtrip.
                "select": "id,signal_id,status,updated_at,attempt_count,gate_reasons,signals(scoring_profile)",
                "status": f"eq.{status}",
                "updated_at": f"lt.{cutoff}",
                "order": "updated_at.asc",
                "limit": str(_SLA_BATCH_LIMIT),
            },
        ) or []
        summary["breaches_by_status"][status] = len(rows)

        if not rows:
            _resolve_flag(
                client,
                source="thesis_jobs",
                kind=f"sla_breach_{status}",
                note=f"auto-resolved: 0 jobs stuck in {status}",
            )
            continue

        sample: List[Dict[str, Any]] = []
        for row in rows[:_SLA_SAMPLE_SIZE]:
            signals_inner = row.get("signals")
            profile = None
            if isinstance(signals_inner, dict):
                profile = signals_inner.get("scoring_profile")
            elif isinstance(signals_inner, list) and signals_inner:
                # PostgREST may return embedded resources as a list in some schemas.
                inner = signals_inner[0]
                profile = inner.get("scoring_profile") if isinstance(inner, dict) else None
            sample.append({
                "job_id": row.get("id"),
                "signal_id": row.get("signal_id"),
                "age_seconds": _age_seconds(row.get("updated_at"), now),
                "attempt_count": row.get("attempt_count"),
                "scoring_profile": profile,
            })

        # 3.2 — auto-reset for `scoring` status only. Crashed worker → row
        # stuck pre-commit → safe to reset.
        resets = 0
        dlqs = 0
        severity = "warn"
        if status == "scoring":
            for row in rows:
                attempt = int(row.get("attempt_count") or 0)
                next_attempt = attempt + 1
                existing_reasons = row.get("gate_reasons") or []
                if next_attempt >= _SCORING_AUTO_RESET_MAX_ATTEMPTS:
                    client._rest(
                        "PATCH", "thesis_jobs",
                        params={"id": f"eq.{row['id']}"},
                        json_body={
                            "status": "dlq",
                            "attempt_count": next_attempt,
                            "gate_reasons": existing_reasons + ["stuck_scoring_sla_dlq"],
                        },
                        prefer="return=minimal",
                    )
                    dlqs += 1
                else:
                    client._rest(
                        "PATCH", "thesis_jobs",
                        params={"id": f"eq.{row['id']}"},
                        json_body={
                            "status": "needs_scoring",
                            "attempt_count": next_attempt,
                            "gate_reasons": existing_reasons + ["stuck_scoring_sla_reset"],
                        },
                        prefer="return=minimal",
                    )
                    resets += 1
            summary["scoring_resets"] = resets
            summary["scoring_dlqs"] = dlqs
            if dlqs > 0:
                severity = "error"

        _upsert_flag(
            client,
            severity=severity,
            source="thesis_jobs",
            kind=f"sla_breach_{status}",
            title=f"{len(rows)} thesis_jobs stuck in {status} past {threshold_s}s",
            evidence={
                "status": status,
                "threshold_seconds": threshold_s,
                "breach_count": len(rows),
                "sample": sample,
                "scoring_resets": resets if status == "scoring" else None,
                "scoring_dlqs": dlqs if status == "scoring" else None,
            },
        )

    # F-216: long-horizon "really stuck" sweep for needs_scoring, keyed on
    # `created_at` instead of `updated_at`. The regular per-status loop above
    # uses updated_at, which can re-bump on unrelated patches and hide rows
    # that were born stuck. We do NOT auto-reset — at this age the right
    # response is to check signal_resolver health, not retry a backlog.
    aged_cutoff = (
        now - timedelta(days=_NEEDS_SCORING_AGED_THRESHOLD_DAYS)
    ).isoformat()
    aged_rows = client._rest(
        "GET", "thesis_jobs",
        params={
            "select": "id,signal_id,status,created_at,updated_at,attempt_count,signals(scoring_profile)",
            "status": "eq.needs_scoring",
            "created_at": f"lt.{aged_cutoff}",
            "order": "created_at.asc",
            "limit": str(_SLA_BATCH_LIMIT),
        },
    ) or []
    summary["needs_scoring_aged_count"] = len(aged_rows)

    if not aged_rows:
        _resolve_flag(
            client,
            source="thesis_jobs",
            kind="thesis_jobs_needs_scoring_aged",
            note=(
                f"auto-resolved: 0 jobs stuck in needs_scoring past "
                f"{_NEEDS_SCORING_AGED_THRESHOLD_DAYS}d"
            ),
        )
    else:
        aged_sample: List[Dict[str, Any]] = []
        for row in aged_rows[:_SLA_SAMPLE_SIZE]:
            signals_inner = row.get("signals")
            profile = None
            if isinstance(signals_inner, dict):
                profile = signals_inner.get("scoring_profile")
            elif isinstance(signals_inner, list) and signals_inner:
                inner = signals_inner[0]
                profile = inner.get("scoring_profile") if isinstance(inner, dict) else None
            aged_sample.append({
                "job_id": row.get("id"),
                "signal_id": row.get("signal_id"),
                "age_seconds": _age_seconds(row.get("created_at"), now),
                "attempt_count": row.get("attempt_count"),
                "scoring_profile": profile,
            })
        _upsert_flag(
            client,
            severity="warn",
            source="thesis_jobs",
            kind="thesis_jobs_needs_scoring_aged",
            title=(
                f"{len(aged_rows)} thesis_jobs stuck in needs_scoring "
                f"past {_NEEDS_SCORING_AGED_THRESHOLD_DAYS}d "
                f"(check signal_resolver health)"
            ),
            evidence={
                "status": "needs_scoring",
                "threshold_days": _NEEDS_SCORING_AGED_THRESHOLD_DAYS,
                "aged_count": len(aged_rows),
                "sample": aged_sample,
            },
        )

    return summary


# ===========================================================================
# provisional_convergence_audit — detect invariant-violating rows
#
# Invariant: a row with `extensions.scoring_meta.requires_resolution=true` must
# NEVER carry `band_with_bonus` (convergence stamp). The reactor's
# `classifyProvisionalHeuristic` gate enforces this — rows flagged provisional
# are routed to signal_resolver BEFORE convergence can stamp them. If this
# query ever returns a row, the reactor gate has regressed.
#
# This sweeper does NOT auto-fix: stamping a convergence result is not safe to
# silently undo (the row may have driven alerts / thesis drafts). It just
# surfaces a severity=error flag so an operator can investigate.
# ===========================================================================

_PROVISIONAL_AUDIT_LIMIT = 100


def provisional_convergence_audit(client: Optional[SupabaseClient] = None) -> Dict[str, Any]:
    """Flag any signals that hold a provisional scoring_meta AND convergence
    stamps. In steady state the count is 0; a non-zero result means the
    reactor's provisional guard leaked. Runs every 6h."""
    client = client or SupabaseClient()
    summary: Dict[str, Any] = {"function": "provisional_convergence_audit"}

    violators = client._rest(
        "GET", "signals",
        params={
            "select": "signal_id,scoring_profile,band_with_bonus,score_with_bonus",
            "extensions->scoring_meta->>requires_resolution": "eq.true",
            "band_with_bonus": "not.is.null",
            "limit": str(_PROVISIONAL_AUDIT_LIMIT),
        },
    ) or []
    summary["violators_found"] = len(violators)

    if not violators:
        _resolve_flag(
            client,
            source="reactor",
            kind="provisional_converged_invariant_violated",
            note="auto-resolved: 0 provisional rows carry convergence stamps",
        )
        return summary

    sample_ids = [row.get("signal_id") for row in violators[:10]]
    profiles_breakdown: Dict[str, int] = {}
    for row in violators:
        profile = row.get("scoring_profile") or "unknown"
        profiles_breakdown[profile] = profiles_breakdown.get(profile, 0) + 1
    summary["sample_signal_ids"] = sample_ids
    summary["per_profile"] = profiles_breakdown

    _upsert_flag(
        client,
        severity="error",
        source="reactor",
        kind="provisional_converged_invariant_violated",
        title=f"{len(violators)} provisional rows carry convergence stamps",
        evidence={
            "violators_found": len(violators),
            "sample_signal_ids": sample_ids,
            "per_profile": profiles_breakdown,
            "note": (
                "classifyProvisionalHeuristic should route these to signal_resolver "
                "before convergence — investigate reactor gate regression or direct "
                "SQL writes bypassing the edge function"
            ),
        },
    )
    return summary


# ===========================================================================
# Health sub-entry (for ad-hoc invocation)
# ===========================================================================

def summarize_open_flags(client: Optional[SupabaseClient] = None) -> Dict[str, Any]:
    """Utility: return counts of open operator_flags by severity + source.
    Useful for the scanner-health card read endpoint (spec §11 task 12)."""
    client = client or SupabaseClient()
    rows = client._rest(
        "GET", "operator_flags",
        params={"select": "severity,source", "resolved_at": "is.null"},
    ) or []
    out: Dict[str, Dict[str, int]] = {}
    for r in rows:
        sev = r["severity"]
        src = r["source"]
        out.setdefault(sev, {}).setdefault(src, 0)
        out[sev][src] += 1
    return {"open_flags": out, "total_open": len(rows)}


def summarize_provisional_backlog(client: Optional[SupabaseClient] = None) -> Dict[str, Any]:
    """How many heuristic signals are still flagged provisional by profile.

    Surface for answering "is the signal_resolver keeping up?" without ad-hoc
    SQL. A growing number per profile = backlog; sustained growth = resolver
    capacity problem. Dashboard panels consume this via the scanner-health
    endpoint. Paginated because provisional counts can exceed PAGE_SIZE during
    burst periods — we don't want a silent cap on the backlog number.
    """
    client = client or SupabaseClient()
    counts: Dict[str, int] = {}
    page_size = 500
    offset = 0
    while True:
        rows = client._rest(
            "GET", "signals",
            params={
                "select": "scoring_profile",
                "extensions->scoring_meta->>requires_resolution": "eq.true",
                "band_with_bonus": "is.null",
                "order": "scan_date.asc",
                "limit": str(page_size),
                "offset": str(offset),
            },
        ) or []
        for row in rows:
            profile = row.get("scoring_profile") or "unknown"
            counts[profile] = counts.get(profile, 0) + 1
        if len(rows) < page_size:
            break
        offset += page_size
    return {
        "provisional_by_profile": counts,
        "total_provisional": sum(counts.values()),
    }


# ===========================================================================
# EDGAR runtime health
# ===========================================================================

_EDGAR_RUNTIME_WINDOW_RUNS = 4
_EDGAR_DEGRADED_RUNS_WARN = 2


def _extract_run_metrics(errors: Any) -> Dict[str, Any]:
    if not isinstance(errors, list):
        return {}
    for entry in errors:
        if isinstance(entry, dict) and isinstance(entry.get("metrics"), dict):
            return entry["metrics"]
    return {}


def edgar_runtime_health(client: Optional[SupabaseClient] = None) -> Dict[str, Any]:
    """Flag repeated degraded EDGAR runs so operators can distinguish
    upstream quietness from a scanner that is alive but under-covering."""
    client = client or SupabaseClient()
    summary: Dict[str, Any] = {
        "function": "edgar_runtime_health",
        "window_runs": _EDGAR_RUNTIME_WINDOW_RUNS,
        "runs_considered": 0,
        "budget_exhausted_runs": 0,
        "zero_signal_degraded_runs": 0,
        "flagged": False,
    }

    scanner_rows = client._rest(
        "GET", "scanners",
        params={"select": "id,name", "name": "eq.edgar_filing_monitor", "limit": 1},
    ) or []
    if not scanner_rows:
        summary["status"] = "missing_scanner"
        return summary

    scanner_id = scanner_rows[0]["id"]
    runs = client._rest(
        "GET", "scanner_runs",
        params={
            "select": "status,signals_emitted,started_at,completed_at,errors",
            "scanner_id": f"eq.{scanner_id}",
            "order": "started_at.desc",
            "limit": str(_EDGAR_RUNTIME_WINDOW_RUNS),
        },
    ) or []
    summary["runs_considered"] = len(runs)
    if not runs:
        return summary

    degraded_samples: List[Dict[str, Any]] = []
    budget_exhausted_runs = 0
    zero_signal_degraded_runs = 0

    for run in runs:
        metrics = _extract_run_metrics(run.get("errors"))
        partial_reasons = metrics.get("partial_reasons") or []
        budget_exhausted = bool(metrics.get("budget_exhausted")) or any(
            isinstance(reason, str) and reason.startswith("budget_exhausted")
            for reason in partial_reasons
        )
        degraded = run.get("status") in ("partial", "error", "timeout") or bool(metrics.get("degraded"))
        if budget_exhausted:
            budget_exhausted_runs += 1
        if degraded and int(run.get("signals_emitted") or 0) == 0:
            zero_signal_degraded_runs += 1
        if degraded:
            degraded_samples.append({
                "status": run.get("status"),
                "signals_emitted": run.get("signals_emitted"),
                "started_at": run.get("started_at"),
                "completed_at": run.get("completed_at"),
                "partial_reasons": partial_reasons,
                "budget_exhausted": budget_exhausted,
            })

    summary["budget_exhausted_runs"] = budget_exhausted_runs
    summary["zero_signal_degraded_runs"] = zero_signal_degraded_runs
    summary["degraded_samples"] = degraded_samples

    if (
        budget_exhausted_runs >= _EDGAR_DEGRADED_RUNS_WARN
        or zero_signal_degraded_runs >= _EDGAR_DEGRADED_RUNS_WARN
    ):
        _upsert_flag(
            client,
            severity="warn",
            source="edgar_runtime_health",
            kind="degraded_run_streak",
            scanner_id=scanner_id,
            title=(
                f"edgar_filing_monitor degraded in {len(degraded_samples)}/{len(runs)} recent runs"
            ),
            evidence={
                "window_runs": len(runs),
                "budget_exhausted_runs": budget_exhausted_runs,
                "zero_signal_degraded_runs": zero_signal_degraded_runs,
                "samples": degraded_samples[:3],
            },
        )
        summary["flagged"] = True
    else:
        _resolve_flag(
            client,
            source="edgar_runtime_health",
            kind="degraded_run_streak",
            scanner_id=scanner_id,
            note="auto-resolved: recent EDGAR runs are no longer repeatedly degraded",
        )

    return summary


# ===========================================================================
# 7.6.5 precision_auditor (Phase 1d) — v2 (bulk-insert normalization)
# ===========================================================================
#
# Aggregates emissions_ledger over a 90d window by (profile × gate_decision ×
# confidence × outcome_label) and writes sparse-column rows into
# accuracy_metrics. Raises operator_flags on drift / inversion / collapse.
#
# Complements coverage_auditor (recall, Phase 1c Cowork) — this is the
# precision / calibration half.

_PRECISION_WINDOW_DAYS = 90
_PRECISION_MIN_SAMPLE_N = 20
_PRECISION_MIN_CONFIDENCE_SAMPLE_N = 30
_PRECISION_MIN_BAND_SAMPLE_N = 30
_PRECISION_DRIFT_WARN_PP = 0.20       # 20 percentage points
_PRECISION_DRIFT_CRITICAL_PP = 0.40   # 40 pp
_PRECISION_POST_EDGE_MISS_WARN = 0.30
_PRECISION_POST_EDGE_MISS_CRITICAL = 0.50
_PRECISION_DEAD_CATALYST_WARN = 0.40
_PRECISION_CONFIDENCE_NOISE_PP = 0.05
_PRECISION_BAND_COLLAPSE_PP = 0.10

_EMISSIONS_LEDGER_COLUMNS = (
    "signal_id,profile,ticker,mic,scored_at,band,auto_caps_triggered,"
    "gate_decision,thesis_job_id,thesis_job_status,candidate_id,"
    "candidate_state,promoted_at,predicted_catalyst_date,"
    "outcome_id,resolution_type,resolution_date,catalyst_hit_date,"
    "realized_move_1d,realized_move_7d,realized_move_30d,"
    "realized_return,outcome_label"
)


def _fetch_emissions_window(
    client: SupabaseClient, window_days: int
) -> List[Dict[str, Any]]:
    """GET emissions_ledger for the rolling window. Paginates via `Range` offsets
    if the result exceeds PostgREST's default cap."""
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    all_rows: List[Dict[str, Any]] = []
    offset = 0
    page_size = 1000
    while True:
        page = client._rest(
            "GET", "emissions_ledger",
            params={
                "select": _EMISSIONS_LEDGER_COLUMNS,
                "scored_at": f"gte.{since}",
                "order": "scored_at.desc",
                "limit": str(page_size),
                "offset": str(offset),
            },
        ) or []
        if not page:
            break
        all_rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return all_rows


def _fetch_thesis_confidences(
    client: SupabaseClient, candidate_ids: List[str]
) -> Dict[str, Optional[str]]:
    """Pull thesis.confidence from the 'created' (or latest 'thesis_drafted_by_claude')
    event per candidate. Returns {candidate_id: confidence_string_or_None}."""
    if not candidate_ids:
        return {}
    out: Dict[str, Optional[str]] = {}
    # Batch in chunks to avoid very long in.() clauses.
    chunk = 100
    for i in range(0, len(candidate_ids), chunk):
        batch = candidate_ids[i : i + chunk]
        rows = client._rest(
            "GET", "candidate_events",
            params={
                "select": "candidate_id,event_type,payload,created_at",
                "candidate_id": f"in.({','.join(batch)})",
                "event_type": "in.(created,thesis_drafted_by_claude)",
                "order": "created_at.desc",
            },
        ) or []
        for r in rows:
            cid = r["candidate_id"]
            if cid in out:
                continue  # already have the most recent (DESC order)
            thesis = (r.get("payload") or {}).get("thesis") or {}
            out[cid] = thesis.get("confidence")
    return out


def _fetch_prior_baseline(
    client: SupabaseClient, auditor: str, window_days: int
) -> Dict[tuple, float]:
    """For drift detection: fetch the most recent prior accuracy_metrics.delivery_rate
    keyed by (profile, gate_decision, confidence). Cells with NULL delivery_rate
    or insufficient_sample=true are excluded."""
    rows = client._rest(
        "GET", "accuracy_metrics",
        params={
            "select": "profile,gate_decision,confidence,delivery_rate,measured_at",
            "auditor": f"eq.{auditor}",
            "insufficient_sample": "eq.false",
            "delivery_rate": "not.is.null",
            "order": "measured_at.desc",
            "limit": "500",
        },
    ) or []
    # Keep only the most-recent value per (profile, gate_decision, confidence)
    out: Dict[tuple, float] = {}
    for r in rows:
        key = (r.get("profile"), r.get("gate_decision"), r.get("confidence"))
        if key not in out and r.get("delivery_rate") is not None:
            out[key] = float(r["delivery_rate"])
    return out


def _delivery_rate(delivered: int, killed: int, expired: int) -> Optional[float]:
    denom = delivered + killed + expired
    if denom == 0:
        return None
    return round(delivered / denom, 4)


def _label_rate(count: int, labeled_n: int) -> Optional[float]:
    if labeled_n == 0:
        return None
    return round(count / labeled_n, 4)


# All nullable columns in accuracy_metrics (outside the fixed dims). Bulk
# inserts via PostgREST require every row to share identical keys (PGRST102),
# so we normalize each row to the full schema with explicit NULLs.
_ACCURACY_METRICS_NULLABLE_KEYS = (
    "profile", "gate_decision", "confidence", "outcome_label",
    "labeled_n",
    "delivered_n", "killed_n", "expired_n",
    "pre_edge_hit_n", "post_edge_miss_n", "dead_catalyst_n",
    "delivery_rate", "pre_edge_hit_rate", "post_edge_miss_rate",
    "dead_catalyst_rate",
    "band_discrimination", "confidence_discrimination", "auto_cap_inversion",
    "timing_error_median_days", "timing_error_abs_p50", "timing_error_abs_p90",
    "emission_lead_days", "decay_ratio_30d_over_1d",
    "mean_realized_move_1d", "mean_realized_move_7d",
    "mean_realized_move_30d", "mean_realized_return",
    "sampled_total",
    "calibrated_hit_n", "ambiguous_hit_n", "miss_n", "save_n",
    "partial_save_n", "pass_through_n", "timing_catch_n", "timing_miss_n",
    "miss_rate", "pass_through_rate", "save_rate", "calibrated_hit_rate",
    "evidence",
)


def _normalize_metrics_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Fill missing nullable columns with None so bulk-insert row shapes match."""
    normalized = dict(row)
    for k in _ACCURACY_METRICS_NULLABLE_KEYS:
        normalized.setdefault(k, None)
    return normalized


def _insert_accuracy_metrics(
    client: SupabaseClient, rows: List[Dict[str, Any]]
) -> None:
    if not rows:
        return
    normalized = [_normalize_metrics_row(r) for r in rows]
    client._rest(
        "POST", "accuracy_metrics",
        json_body=normalized,
        prefer="return=minimal",
    )


def precision_auditor(client: Optional[SupabaseClient] = None) -> Dict[str, Any]:
    """Phase 1d weekly: compute precision / calibration metrics over the 90d
    emissions_ledger window and raise operator_flags on drift / inversion.

    Writes one accuracy_metrics row per (profile × gate_decision) cell, plus
    per-profile summary rows carrying confidence_discrimination, band_discrimination,
    and auto_cap_inversion signals.
    """
    client = client or SupabaseClient()
    summary: Dict[str, Any] = {
        "function": "precision_auditor",
        "window_days": _PRECISION_WINDOW_DAYS,
        "cells_measured": 0,
        "cells_insufficient": 0,
        "flags_raised": 0,
        "flags_resolved": 0,
    }

    emissions = _fetch_emissions_window(client, _PRECISION_WINDOW_DAYS)
    summary["emissions_in_window"] = len(emissions)

    if not emissions:
        return summary

    # Pull thesis.confidence for promoted candidates only (only gate_decision
    # where confidence is meaningful).
    promoted_candidate_ids = sorted({
        e["candidate_id"] for e in emissions
        if e.get("gate_decision") == "promoted" and e.get("candidate_id")
    })
    confidences = _fetch_thesis_confidences(client, promoted_candidate_ids)

    # Attach confidence onto each emission row (None for non-promoted).
    for e in emissions:
        cid = e.get("candidate_id")
        e["_confidence"] = confidences.get(cid) if cid else None

    prior_baseline = _fetch_prior_baseline(
        client, auditor="precision", window_days=_PRECISION_WINDOW_DAYS
    )

    # ----- Cell aggregation -----
    # Primary: (profile, gate_decision). Also record per-profile splits by
    # confidence and band for discrimination metrics.
    from collections import defaultdict

    cells: Dict[tuple, Dict[str, int]] = defaultdict(lambda: {
        "sample_n": 0, "labeled_n": 0,
        "delivered_n": 0, "killed_n": 0, "expired_n": 0,
        "pre_edge_hit_n": 0, "post_edge_miss_n": 0, "dead_catalyst_n": 0,
    })

    def _bump(key: tuple, e: Dict[str, Any]) -> None:
        c = cells[key]
        c["sample_n"] += 1
        if e.get("outcome_label"):
            c["labeled_n"] += 1
            lbl = e["outcome_label"]
            if lbl == "pre_edge_hit":
                c["pre_edge_hit_n"] += 1
            elif lbl == "post_edge_miss":
                c["post_edge_miss_n"] += 1
            elif lbl == "dead_catalyst":
                c["dead_catalyst_n"] += 1
        rt = e.get("resolution_type")
        if rt == "delivered":
            c["delivered_n"] += 1
        elif rt == "killed":
            c["killed_n"] += 1
        elif rt == "expired":
            c["expired_n"] += 1

    # Primary cells — (profile, gate_decision)
    for e in emissions:
        profile = e.get("profile")
        gd = e.get("gate_decision")
        if not profile or not gd:
            continue
        _bump(("primary", profile, gd, None, None), e)

    # Confidence cells — (profile, 'promoted', confidence). Used for discrimination.
    for e in emissions:
        if e.get("gate_decision") != "promoted":
            continue
        profile = e.get("profile")
        conf = e.get("_confidence")
        if not profile or not conf:
            continue
        _bump(("confidence", profile, "promoted", conf, None), e)

    # Band cells — (profile, band=immediate|watchlist) — proxied via gate_decision.
    # In emissions_ledger, 'promoted' rows ran through immediate band; 'below_band'
    # rows with band=watchlist are the counterfactual. Simpler: split by band field.
    for e in emissions:
        profile = e.get("profile")
        band = e.get("band")
        if not profile or band not in ("immediate", "watchlist"):
            continue
        _bump(("band", profile, None, None, band), e)

    # ----- Write accuracy_metrics rows + raise flags -----
    now_iso = datetime.now(timezone.utc).isoformat()
    rows_to_insert: List[Dict[str, Any]] = []
    profiles_seen: set = set()

    for key, c in cells.items():
        cell_type, profile, gd, conf, band_or_label = key
        insufficient = c["sample_n"] < _PRECISION_MIN_SAMPLE_N

        dr = None if insufficient else _delivery_rate(
            c["delivered_n"], c["killed_n"], c["expired_n"]
        )
        labeled_n = c["labeled_n"]
        peh_rate = _label_rate(c["pre_edge_hit_n"], labeled_n)
        pem_rate = _label_rate(c["post_edge_miss_n"], labeled_n)
        dc_rate = _label_rate(c["dead_catalyst_n"], labeled_n)

        row: Dict[str, Any] = {
            "measured_at": now_iso,
            "window_days": _PRECISION_WINDOW_DAYS,
            "auditor": "precision",
            "profile": profile,
            "gate_decision": gd if cell_type != "band" else None,
            "confidence": conf,
            "outcome_label": None,
            "sample_n": c["sample_n"],
            "labeled_n": labeled_n,
            "insufficient_sample": insufficient,
            "delivered_n": c["delivered_n"],
            "killed_n": c["killed_n"],
            "expired_n": c["expired_n"],
            "pre_edge_hit_n": c["pre_edge_hit_n"],
            "post_edge_miss_n": c["post_edge_miss_n"],
            "dead_catalyst_n": c["dead_catalyst_n"],
            "delivery_rate": dr,
            "pre_edge_hit_rate": peh_rate,
            "post_edge_miss_rate": pem_rate,
            "dead_catalyst_rate": dc_rate,
            "evidence": {
                "cell_type": cell_type,
                "band_or_label": band_or_label,
            },
        }
        rows_to_insert.append(row)
        profiles_seen.add(profile)

        if insufficient:
            summary["cells_insufficient"] += 1
            continue
        summary["cells_measured"] += 1

        # ---- Flag: precision_drift
        baseline = prior_baseline.get((profile, gd, conf))
        if dr is not None and baseline is not None:
            drop = baseline - dr
            if drop >= _PRECISION_DRIFT_CRITICAL_PP:
                _upsert_flag(
                    client,
                    severity="critical",
                    source="precision_auditor",
                    kind="precision_drift",
                    title=f"{profile}/{gd or '-'}: delivery_rate dropped {drop:.2f} (now {dr:.2f}, baseline {baseline:.2f})",
                    evidence={"profile": profile, "gate_decision": gd, "confidence": conf,
                              "current": dr, "baseline": baseline, "drop": round(drop, 4)},
                )
                summary["flags_raised"] += 1
            elif drop >= _PRECISION_DRIFT_WARN_PP:
                _upsert_flag(
                    client,
                    severity="warn",
                    source="precision_auditor",
                    kind="precision_drift",
                    title=f"{profile}/{gd or '-'}: delivery_rate dropped {drop:.2f} (now {dr:.2f}, baseline {baseline:.2f})",
                    evidence={"profile": profile, "gate_decision": gd, "confidence": conf,
                              "current": dr, "baseline": baseline, "drop": round(drop, 4)},
                )
                summary["flags_raised"] += 1

        # ---- Flag: post_edge_miss_spike + dead_catalyst_spike
        if labeled_n >= _PRECISION_MIN_SAMPLE_N:
            if pem_rate is not None and pem_rate >= _PRECISION_POST_EDGE_MISS_CRITICAL:
                _upsert_flag(
                    client,
                    severity="critical",
                    source="precision_auditor",
                    kind="post_edge_miss_spike",
                    title=f"{profile}/{gd or '-'}: post_edge_miss_rate {pem_rate:.2f} over {labeled_n} labeled",
                    evidence={"profile": profile, "gate_decision": gd,
                              "post_edge_miss_rate": pem_rate, "labeled_n": labeled_n},
                )
                summary["flags_raised"] += 1
            elif pem_rate is not None and pem_rate >= _PRECISION_POST_EDGE_MISS_WARN:
                _upsert_flag(
                    client,
                    severity="warn",
                    source="precision_auditor",
                    kind="post_edge_miss_spike",
                    title=f"{profile}/{gd or '-'}: post_edge_miss_rate {pem_rate:.2f} over {labeled_n} labeled",
                    evidence={"profile": profile, "gate_decision": gd,
                              "post_edge_miss_rate": pem_rate, "labeled_n": labeled_n},
                )
                summary["flags_raised"] += 1

            if dc_rate is not None and dc_rate >= _PRECISION_DEAD_CATALYST_WARN:
                _upsert_flag(
                    client,
                    severity="warn",
                    source="precision_auditor",
                    kind="dead_catalyst_spike",
                    title=f"{profile}/{gd or '-'}: dead_catalyst_rate {dc_rate:.2f} over {labeled_n} labeled",
                    evidence={"profile": profile, "gate_decision": gd,
                              "dead_catalyst_rate": dc_rate, "labeled_n": labeled_n},
                )
                summary["flags_raised"] += 1

    # ---- Per-profile discrimination + inversion summary rows + flags
    for profile in profiles_seen:
        hi_c = cells.get(("confidence", profile, "promoted", "high", None))
        md_c = cells.get(("confidence", profile, "promoted", "medium", None))
        hi_rate = None
        md_rate = None
        if hi_c and hi_c["sample_n"] >= _PRECISION_MIN_CONFIDENCE_SAMPLE_N:
            hi_rate = _delivery_rate(hi_c["delivered_n"], hi_c["killed_n"], hi_c["expired_n"])
        if md_c and md_c["sample_n"] >= _PRECISION_MIN_CONFIDENCE_SAMPLE_N:
            md_rate = _delivery_rate(md_c["delivered_n"], md_c["killed_n"], md_c["expired_n"])
        conf_disc = None
        if hi_rate is not None and md_rate is not None:
            conf_disc = round(hi_rate - md_rate, 4)

        imm_c = cells.get(("band", profile, None, None, "immediate"))
        wl_c = cells.get(("band", profile, None, None, "watchlist"))
        imm_rate = None
        wl_rate = None
        if imm_c and imm_c["sample_n"] >= _PRECISION_MIN_BAND_SAMPLE_N:
            imm_rate = _delivery_rate(imm_c["delivered_n"], imm_c["killed_n"], imm_c["expired_n"])
        if wl_c and wl_c["sample_n"] >= _PRECISION_MIN_BAND_SAMPLE_N:
            wl_rate = _delivery_rate(wl_c["delivered_n"], wl_c["killed_n"], wl_c["expired_n"])
        band_disc = None
        if imm_rate is not None and wl_rate is not None:
            band_disc = round(imm_rate - wl_rate, 4)

        promoted_c = cells.get(("primary", profile, "promoted", None, None))
        capped_c = cells.get(("primary", profile, "auto_capped", None, None))
        prom_rate = None
        cap_rate = None
        if promoted_c and promoted_c["sample_n"] >= _PRECISION_MIN_SAMPLE_N:
            prom_rate = _delivery_rate(
                promoted_c["delivered_n"], promoted_c["killed_n"], promoted_c["expired_n"]
            )
        if capped_c and capped_c["sample_n"] >= _PRECISION_MIN_SAMPLE_N:
            cap_rate = _delivery_rate(
                capped_c["delivered_n"], capped_c["killed_n"], capped_c["expired_n"]
            )
        auto_cap_inv = None
        if prom_rate is not None and cap_rate is not None:
            auto_cap_inv = round(cap_rate - prom_rate, 4)

        # Write summary row even if discrimination metrics are NULL — preserves
        # the time series and shows "we ran for this profile."
        rows_to_insert.append({
            "measured_at": now_iso,
            "window_days": _PRECISION_WINDOW_DAYS,
            "auditor": "precision",
            "profile": profile,
            "gate_decision": None,
            "confidence": None,
            "outcome_label": None,
            "sample_n": (promoted_c["sample_n"] if promoted_c else 0),
            "labeled_n": (promoted_c["labeled_n"] if promoted_c else 0),
            "insufficient_sample": False,
            "band_discrimination": band_disc,
            "confidence_discrimination": conf_disc,
            "auto_cap_inversion": auto_cap_inv,
            "evidence": {
                "cell_type": "summary",
                "hi_confidence_rate": hi_rate, "md_confidence_rate": md_rate,
                "immediate_band_rate": imm_rate, "watchlist_band_rate": wl_rate,
                "promoted_rate": prom_rate, "auto_capped_rate": cap_rate,
            },
        })

        if conf_disc is not None and abs(conf_disc) < _PRECISION_CONFIDENCE_NOISE_PP:
            _upsert_flag(
                client,
                severity="warn",
                source="precision_auditor",
                kind="confidence_noise",
                title=f"{profile}: confidence label doesn't discriminate (Δ={conf_disc:+.2f})",
                evidence={"profile": profile, "hi_rate": hi_rate, "md_rate": md_rate,
                          "discrimination": conf_disc},
            )
            summary["flags_raised"] += 1

        if band_disc is not None and band_disc < _PRECISION_BAND_COLLAPSE_PP:
            _upsert_flag(
                client,
                severity="warn",
                source="precision_auditor",
                kind="band_collapse",
                title=f"{profile}: band collapsed (immediate−watchlist = {band_disc:+.2f})",
                evidence={"profile": profile, "immediate_rate": imm_rate,
                          "watchlist_rate": wl_rate, "discrimination": band_disc},
            )
            summary["flags_raised"] += 1

        if auto_cap_inv is not None and auto_cap_inv > 0:
            _upsert_flag(
                client,
                severity="critical",
                source="precision_auditor",
                kind="auto_cap_inverted",
                title=f"{profile}: auto-capped delivery rate EXCEEDS promoted (Δ={auto_cap_inv:+.2f})",
                evidence={"profile": profile, "promoted_rate": prom_rate,
                          "auto_capped_rate": cap_rate, "inversion": auto_cap_inv},
            )
            summary["flags_raised"] += 1

    _insert_accuracy_metrics(client, rows_to_insert)
    summary["rows_written"] = len(rows_to_insert)
    return summary


# ===========================================================================
# 7.6.6 timing_auditor (Phase 1d)
# ===========================================================================
#
# Per-profile catalyst-date forecast accuracy + return-decay profile. Only
# inspects the promoted+outcome-labeled subset; all other emissions are
# irrelevant to timing.

_TIMING_WINDOW_DAYS = 90
_TIMING_MIN_SAMPLE_N = 10
_TIMING_DRIFT_DAYS = 60
_TIMING_EMISSION_LEAD_MIN_DAYS = 3
_TIMING_DECAY_ANOMALY_RATIO = 3.0


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Dates come back as 'YYYY-MM-DD' strings from PostgREST.
        return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def timing_auditor(client: Optional[SupabaseClient] = None) -> Dict[str, Any]:
    """Phase 1d weekly: compute per-profile timing-error + emission-lead +
    return-decay from the labeled promoted subset; flag drift / too-late /
    decay-anomaly."""
    client = client or SupabaseClient()
    summary: Dict[str, Any] = {
        "function": "timing_auditor",
        "window_days": _TIMING_WINDOW_DAYS,
        "profiles_measured": 0,
        "profiles_insufficient": 0,
        "flags_raised": 0,
    }

    emissions = _fetch_emissions_window(client, _TIMING_WINDOW_DAYS)
    # Only the auditable subset: promoted + both dates + outcome present.
    auditable = [
        e for e in emissions
        if e.get("gate_decision") == "promoted"
        and e.get("predicted_catalyst_date")
        and e.get("catalyst_hit_date")
        and e.get("promoted_at")
    ]
    summary["auditable_n"] = len(auditable)

    from collections import defaultdict
    by_profile: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for e in auditable:
        profile = e.get("profile")
        if profile:
            by_profile[profile].append(e)

    now_iso = datetime.now(timezone.utc).isoformat()
    rows_to_insert: List[Dict[str, Any]] = []

    for profile, rows in by_profile.items():
        n = len(rows)
        insufficient = n < _TIMING_MIN_SAMPLE_N

        # Compute stats.
        timing_errors_signed: List[int] = []
        timing_errors_abs: List[int] = []
        emission_leads: List[int] = []
        decay_ratios: List[float] = []
        moves_1d: List[float] = []
        moves_7d: List[float] = []
        moves_30d: List[float] = []
        realized_rets: List[float] = []

        for r in rows:
            pred = _parse_date(r.get("predicted_catalyst_date"))
            hit = _parse_date(r.get("catalyst_hit_date"))
            promoted_at = _parse_date(r.get("promoted_at"))
            if pred and hit:
                delta_days = (hit - pred).days
                timing_errors_signed.append(delta_days)
                timing_errors_abs.append(abs(delta_days))
            if hit and promoted_at:
                lead_days = (hit - promoted_at).days
                emission_leads.append(lead_days)

            m1 = r.get("realized_move_1d")
            m7 = r.get("realized_move_7d")
            m30 = r.get("realized_move_30d")
            rr = r.get("realized_return")
            if m1 is not None:
                moves_1d.append(float(m1))
            if m7 is not None:
                moves_7d.append(float(m7))
            if m30 is not None:
                moves_30d.append(float(m30))
            if rr is not None:
                realized_rets.append(float(rr))
            if m1 is not None and m30 is not None and abs(float(m1)) > 0.001:
                decay_ratios.append(abs(float(m30)) / abs(float(m1)))

        def _median_int(xs: List[int]) -> Optional[int]:
            if not xs:
                return None
            return int(statistics.median(xs))

        def _p90_int(xs: List[int]) -> Optional[int]:
            if not xs:
                return None
            if len(xs) < 10:
                return max(xs)
            return int(statistics.quantiles(xs, n=10)[8])

        def _mean_num(xs: List[float]) -> Optional[float]:
            if not xs:
                return None
            return round(statistics.mean(xs), 3)

        timing_med = _median_int(timing_errors_signed)
        timing_abs_p50 = _median_int(timing_errors_abs)
        timing_abs_p90 = _p90_int(timing_errors_abs)
        lead_med = _median_int(emission_leads)
        decay_ratio = round(statistics.mean(decay_ratios), 3) if decay_ratios else None

        row: Dict[str, Any] = {
            "measured_at": now_iso,
            "window_days": _TIMING_WINDOW_DAYS,
            "auditor": "timing",
            "profile": profile,
            "gate_decision": "promoted",
            "confidence": None,
            "outcome_label": None,
            "sample_n": n,
            "labeled_n": n,  # by construction — we filtered on catalyst_hit_date
            "insufficient_sample": insufficient,
            "timing_error_median_days": timing_med,
            "timing_error_abs_p50": timing_abs_p50,
            "timing_error_abs_p90": timing_abs_p90,
            "emission_lead_days": lead_med,
            "decay_ratio_30d_over_1d": decay_ratio,
            "mean_realized_move_1d": _mean_num(moves_1d),
            "mean_realized_move_7d": _mean_num(moves_7d),
            "mean_realized_move_30d": _mean_num(moves_30d),
            "mean_realized_return": _mean_num(realized_rets),
            "evidence": {"n": n, "decay_pairs": len(decay_ratios),
                         "timing_pairs": len(timing_errors_signed),
                         "lead_pairs": len(emission_leads)},
        }
        rows_to_insert.append(row)

        if insufficient:
            summary["profiles_insufficient"] += 1
            continue
        summary["profiles_measured"] += 1

        # ---- Flag: timing_drift
        if timing_abs_p50 is not None and timing_abs_p50 > _TIMING_DRIFT_DAYS:
            _upsert_flag(
                client,
                severity="warn",
                source="timing_auditor",
                kind="timing_drift",
                title=f"{profile}: timing abs p50 = {timing_abs_p50}d (threshold {_TIMING_DRIFT_DAYS})",
                evidence={"profile": profile, "timing_abs_p50": timing_abs_p50,
                          "timing_abs_p90": timing_abs_p90, "timing_median_signed": timing_med,
                          "sample_n": n},
            )
            summary["flags_raised"] += 1
        else:
            _resolve_flag(client, source="timing_auditor", kind="timing_drift",
                          note=f"auto-resolved: {profile} abs_p50 recovered")

        # ---- Flag: emission_too_late
        if lead_med is not None and lead_med < _TIMING_EMISSION_LEAD_MIN_DAYS:
            _upsert_flag(
                client,
                severity="warn",
                source="timing_auditor",
                kind="emission_too_late",
                title=f"{profile}: emission lead median = {lead_med}d (threshold ≥{_TIMING_EMISSION_LEAD_MIN_DAYS})",
                evidence={"profile": profile, "lead_days_median": lead_med, "sample_n": n},
            )
            summary["flags_raised"] += 1

        # ---- Flag: decay_anomaly
        if decay_ratio is not None and decay_ratio > _TIMING_DECAY_ANOMALY_RATIO:
            _upsert_flag(
                client,
                severity="warn",
                source="timing_auditor",
                kind="decay_anomaly",
                title=f"{profile}: 30d/1d decay ratio {decay_ratio:.2f} (threshold {_TIMING_DECAY_ANOMALY_RATIO})",
                evidence={"profile": profile, "decay_ratio": decay_ratio,
                          "mean_1d": _mean_num(moves_1d), "mean_30d": _mean_num(moves_30d),
                          "sample_n": n},
            )
            summary["flags_raised"] += 1

    _insert_accuracy_metrics(client, rows_to_insert)
    summary["rows_written"] = len(rows_to_insert)
    return summary
