"""bcfda.outcomes.run_labeler — the daily outcome LOGGER (Phase 3 §4–§5).

When a watched PDUFA RESOLVES, record what actually happened — the regulatory
verdict + the price reaction — paired with the ``p_crl``/band shown at prediction
time. LOGGING ONLY. NO refit loop, NO drift alarm: this worker NEVER reads or
writes ``bc_refit_log`` / ``l7.refit_min_crl_events`` (that loop is pointless at
~1 CRL/yr; the endorsed direction cut L7 to logging — §4.0).

Per resolved app it writes THREE ``bc_prediction_outcomes`` rows (horizon_days ∈
l4.outcome_price_horizons, default [1,7,30]), each idempotent on the UNIQUE
``(application_number, horizon_days)`` via a NULL-OMITTING merge upsert (§5.3):
  - the verdict (regulatory_outcome) the day it's known (3 rows, prices null),
  - then each price_return_pct MERGED in as that horizon matures,
  - scored_p_crl paired from the PRE-PDUFA bc_rubric_scores row (§4.2),
  - hypothesis_outcome once both a pre-PDUFA band and a terminal verdict exist (§5.4).

Lifecycle hygiene (§5.6): a past-dated PDUFA with no outcome row is re-attempted
once; if still unresolvable it is LOGGED ``stale_pending_unresolved`` to
bc_pipeline_runs.log — never fabricated, never deleted.

Fail-loud: opens/closes a ``bc_pipeline_runs(pipeline_name='bc_outcome_labeler')``
row in a finally (the only liveness sink). INVARIANT: ``p_crl`` is read ONLY here
(from bc_rubric_scores), paired into ``scored_p_crl``, and NEVER surfaced.

The Modal endpoint (``bc_outcome_labeler_once``) calls ``run_labeler()``; the
clients (Supabase REST, Polygon market-data, CRL records, Drugs@FDA submissions)
are all injectable so tests run with FAKE clients and NO network.

Run locally (DRY-RUN reads live sources, writes nothing)::

    python3 -m modal_workers.bc_outcome_labeler.run_labeler --json-out /tmp/bc_label_dryrun.json
    # --apply writes bc_prediction_outcomes + opens/closes a bc_pipeline_runs row.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from modal_workers.bc_outcome_labeler.price_returns import fetch_returns
from modal_workers.bc_outcome_labeler.resolve import (
    detect_extension,
    hypothesis_outcome,
    resolve_regulatory_outcome,
)
from modal_workers.shared.bc_pipeline_runs import close_run as _shared_close_run
from modal_workers.shared.bc_pipeline_runs import open_run as _shared_open_run

logger = logging.getLogger("bc_outcome_labeler")

PIPELINE_NAME = "bc_outcome_labeler"
SCORER_NAME = "M14_adjusted"  # the pre-PDUFA score's scorer_name (matches run_weekly)
DEFAULT_HORIZONS = [1, 7, 30]
DEFAULT_GRACE_DAYS = 14
_OUTCOMES_TABLE = "bc_prediction_outcomes"


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
def _horizons(client: Any) -> List[int]:
    """l4.outcome_price_horizons (jsonb array) -> list[int]; default [1,7,30]."""
    try:
        rows = client.select(
            "bc_config",
            params={"select": "value", "key": "eq.l4.outcome_price_horizons", "limit": "1"},
        )
    except Exception as exc:  # noqa: BLE001 — config read is best-effort, default loudly
        logger.warning("outcome_price_horizons read failed (%s) — default %s", exc, DEFAULT_HORIZONS)
        return list(DEFAULT_HORIZONS)
    if rows and isinstance(rows[0].get("value"), list):
        try:
            return [int(x) for x in rows[0]["value"]]
        except (TypeError, ValueError):
            pass
    logger.warning("l4.outcome_price_horizons missing/invalid — default %s", DEFAULT_HORIZONS)
    return list(DEFAULT_HORIZONS)


def _grace_days(client: Any) -> int:
    try:
        rows = client.select(
            "bc_config",
            params={"select": "value", "key": "eq.l4.outcome_resolve_grace_days", "limit": "1"},
        )
    except Exception:  # noqa: BLE001
        return DEFAULT_GRACE_DAYS
    if rows and rows[0].get("value") is not None:
        try:
            return int(rows[0]["value"])
        except (TypeError, ValueError):
            pass
    return DEFAULT_GRACE_DAYS


# ---------------------------------------------------------------------------
# pre-PDUFA score snapshot (§4.2): the LAST bc_rubric_scores row with
# scored_at <= pdufa_date for the app. Its p_crl -> scored_p_crl; risk_band ->
# hypothesis_outcome.
# ---------------------------------------------------------------------------
def pre_pdufa_score(client: Any, application_number: str, pdufa_date: Any) -> Dict[str, Any]:
    """Return ``{"scored_p_crl": <float|None>, "risk_band": <str|None>}`` from the
    pre-PDUFA score, or both None when no such row exists.

    Pairs against the band SHOWN WHEN THE BET WAS LIVE — not today's band."""
    pdufa_iso = _iso_date(pdufa_date)
    params = {
        "select": "p_crl,risk_band,scored_at",
        "application_number": f"eq.{application_number}",
        "scorer_name": f"eq.{SCORER_NAME}",
        "order": "scored_at.desc",
        "limit": "1",
    }
    if pdufa_iso:
        # the LAST score AT OR BEFORE the PDUFA date (the live read at prediction time)
        params["scored_at"] = f"lte.{pdufa_iso}T23:59:59+00:00"
    try:
        rows = client.select("bc_rubric_scores", params=params)
    except Exception as exc:  # noqa: BLE001 — pairing is advisory; verdict still logs
        logger.warning("pre_pdufa_score read failed for %s: %s", application_number, exc)
        return {"scored_p_crl": None, "risk_band": None}
    if not rows:
        return {"scored_p_crl": None, "risk_band": None}
    row = rows[0]
    p = row.get("p_crl")
    try:
        p_val = float(p) if p is not None else None
    except (TypeError, ValueError):
        p_val = None
    return {"scored_p_crl": p_val, "risk_band": row.get("risk_band")}


def _iso_date(value: Any) -> Optional[str]:
    if isinstance(value, date):
        return value.isoformat()
    if not value:
        return None
    s = str(value)[:10]
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# the 3-row, null-omitting merge upsert body (§5.3)
# ---------------------------------------------------------------------------
def build_outcome_rows(
    *,
    application_number: str,
    horizons: List[int],
    regulatory_outcome: Optional[str],
    returns: Dict[int, Optional[float]],
    scored_p_crl: Optional[float],
    band: Optional[str],
) -> List[Dict[str, Any]]:
    """One row per horizon, INCLUDING ONLY the fields it can fill (omit nulls) so a
    later merge never clobbers a set value with null. UNIQUE(application_number,
    horizon_days) makes each merge idempotent.

    ``regulatory_outcome`` is OMITTED when not yet known; ``price_return_pct``
    OMITTED when that horizon is immature; ``hypothesis_outcome`` OMITTED until both
    a band and a terminal verdict exist; ``scored_p_crl`` included whenever known."""
    hypo = hypothesis_outcome(band, regulatory_outcome)
    rows: List[Dict[str, Any]] = []
    for h in horizons:
        row: Dict[str, Any] = {
            "application_number": application_number,
            "horizon_days": int(h),
        }
        if regulatory_outcome is not None:
            row["regulatory_outcome"] = str(regulatory_outcome).lower()  # CHECK: lowercase
        ret = returns.get(h)
        if ret is not None:
            row["price_return_pct"] = ret
        if hypo is not None:
            row["hypothesis_outcome"] = hypo
        if scored_p_crl is not None:
            row["scored_p_crl"] = scored_p_crl
        rows.append(row)
    return rows


def _upsert_outcomes(client: Any, rows: List[Dict[str, Any]]) -> None:
    """Merge-upsert on (application_number, horizon_days). Null-omitting bodies
    mean a partial row is filled, never clobbered."""
    if not rows:
        return
    client.upsert(
        _OUTCOMES_TABLE,
        rows,
        on_conflict="application_number,horizon_days",
        prefer="resolution=merge-duplicates,return=minimal",
    )


# ---------------------------------------------------------------------------
# label a single app (pure orchestration over injected data — testable)
# ---------------------------------------------------------------------------
def label_app(
    client: Any,
    app: Dict[str, Any],
    *,
    horizons: List[int],
    market_data: Any,
    crl_records: Optional[List[Dict[str, Any]]],
    submissions_by_app: Dict[str, List[Dict[str, Any]]],
    crl_source_available: bool,
    apply: bool,
) -> Dict[str, Any]:
    """Resolve + price + pair one app; write its 3 rows when ``apply``.

    ``app`` carries ``application_number, pdufa_date, ticker, last_seen_pdufa``.
    Returns a per-app result dict (the labeler's log entry)."""
    appno = str(app.get("application_number") or "")
    pdufa = app.get("pdufa_date")
    ticker = app.get("ticker")
    last_seen = app.get("last_seen_pdufa")

    res = resolve_regulatory_outcome(
        application_number=appno,
        pdufa_date=pdufa,
        crl_records=crl_records,
        submissions=submissions_by_app.get(appno),
        last_seen_pdufa=last_seen,
        crl_source_available=crl_source_available,
    )
    regulatory_outcome = res["outcome"]

    # price returns (independent of the regulatory clock — §5.2)
    price = fetch_returns(market_data, ticker or "", pdufa, horizons) if market_data else {
        "returns": {h: None for h in horizons}, "base": None, "n_bars": 0, "log": "no_market_data",
    }
    returns = price["returns"]

    # pair the pre-PDUFA band/p_crl (§4.2)
    snap = pre_pdufa_score(client, appno, pdufa)

    rows = build_outcome_rows(
        application_number=appno,
        horizons=horizons,
        regulatory_outcome=regulatory_outcome,
        returns=returns,
        scored_p_crl=snap["scored_p_crl"],
        band=snap["risk_band"],
    )

    wrote = False
    # Write only when the app is actually RESOLVED (a regulatory verdict exists) OR
    # at least one price horizon has matured. An app with only a paired scored_p_crl
    # (no verdict, no price) is still "pending" -> no row (§5.1: "normally no outcome
    # row written"). This keeps scored_p_crl-only rows out of the ledger.
    has_verdict = regulatory_outcome is not None
    has_mature_price = any(v is not None for v in returns.values())
    if apply and (has_verdict or has_mature_price):
        _upsert_outcomes(client, rows)
        wrote = True

    return {
        "application_number": appno,
        "regulatory_outcome": regulatory_outcome,
        "source": res["source"],
        "is_terminal": res["is_terminal"],
        "scored_p_crl": snap["scored_p_crl"],
        "band": snap["risk_band"],
        "hypothesis_outcome": hypothesis_outcome(snap["risk_band"], regulatory_outcome),
        "returns": {str(k): v for k, v in returns.items()},
        "price_log": price.get("log"),
        "resolve_log": res.get("log"),
        "wrote": wrote,
        "n_rows": len(rows) if wrote else 0,
    }


# ---------------------------------------------------------------------------
# candidate apps to label
# ---------------------------------------------------------------------------
def _read_candidate_apps(client: Any, today: date) -> List[Dict[str, Any]]:
    """Apps with pdufa_date <= today (resolution can lag the date) that may need a
    row or a maturing price. Reads bc_candidates (the universe surface) for
    application_number/pdufa_date/sponsor_cik; the ticker is hydrated per app.

    NOTE: reads p_crl is NOT in this select — the labeler pairs scored_p_crl from
    bc_rubric_scores (§4.2), and the universe read here needs only identity/date.
    """
    today_iso = today.isoformat()
    rows = client.select(
        "bc_candidates",
        params={
            "select": "application_number,pdufa_date,sponsor_cik",
            "pdufa_date": f"lte.{today_iso}",
            "order": "pdufa_date.desc",
        },
    )
    return rows or []


def _ticker_for(client: Any, sponsor_cik: Any) -> Optional[str]:
    if sponsor_cik is None:
        return None
    rows = client.select(
        "bc_company_tradeable",
        params={
            "select": "ticker,snapshot_date",
            "sponsor_cik": f"eq.{sponsor_cik}",
            "order": "snapshot_date.desc",
            "limit": "1",
        },
    )
    if rows:
        return rows[0].get("ticker")
    return None


def _existing_outcome_appnos(client: Any) -> set:
    """Set of application_numbers that already have at least one outcome row (so a
    'no outcome row' check for the stale sweep is cheap)."""
    rows = client.select(
        _OUTCOMES_TABLE,
        params={"select": "application_number"},
    )
    return {r.get("application_number") for r in (rows or [])}


# ---------------------------------------------------------------------------
# the run (fail-loud open/close in a finally)
# ---------------------------------------------------------------------------
def run_labeler(
    client=None,
    *,
    apply: bool = False,
    today: Optional[date] = None,
    market_data: Any = None,
    crl_records: Optional[List[Dict[str, Any]]] = None,
    submissions_by_app: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    apps: Optional[List[Dict[str, Any]]] = None,
    crl_source_available: Optional[bool] = None,
) -> Dict[str, Any]:
    """Resolve + price + log outcomes for resolved/maturing PDUFAs.

    Injectable for tests: ``client`` (fake Supabase), ``market_data`` (fake Polygon),
    ``crl_records`` / ``submissions_by_app`` (fixtures), ``apps`` (skip the live
    universe read). ``crl_source_available`` defaults to "is the transparency module
    importable"; pass False to exercise the degraded approvals-only path.

    Opens/closes a bc_pipeline_runs row in a finally; NEVER touches bc_refit_log /
    l7.* (logging only — §4.0)."""
    now = datetime.now(timezone.utc)
    today = today or now.date()

    if client is None:
        from modal_workers.shared.supabase_client import SupabaseClient

        client = SupabaseClient()

    if crl_source_available is None:
        crl_source_available = _crl_source_importable()

    if market_data is None and apply:
        market_data = _build_market_data()

    horizons = _horizons(client)
    grace = _grace_days(client)
    submissions_by_app = submissions_by_app or {}

    run_id = None
    status = "succeeded"
    reason: Optional[str] = None
    results: List[Dict[str, Any]] = []
    stale: List[str] = []
    n_failed = 0

    if apply:
        run_id = _shared_open_run(client, pipeline_name=PIPELINE_NAME, snapshot_date=today.isoformat())

    try:
        if apps is None:
            apps = _read_candidate_apps(client, today)
            for a in apps:
                if a.get("ticker") is None:
                    a["ticker"] = _ticker_for(client, a.get("sponsor_cik"))

        existing = _existing_outcome_appnos(client) if apply else set()

        for app in apps:
            appno = str(app.get("application_number") or "")
            try:
                res = label_app(
                    client, app,
                    horizons=horizons,
                    market_data=market_data,
                    crl_records=crl_records,
                    submissions_by_app=submissions_by_app,
                    crl_source_available=crl_source_available,
                    apply=apply,
                )
                results.append(res)
            except Exception as exc:  # noqa: BLE001 — per-app isolation
                logger.warning("label failed for %s: %s", appno, exc)
                n_failed += 1
                results.append({"application_number": appno, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
                continue

            # stale-pending sweep (§5.6): a past-dated PDUFA still unresolved past
            # the grace window, with no outcome row -> LOG (never fabricate/delete).
            pdufa_d = _parse_iso(app.get("pdufa_date"))
            unresolved = res.get("regulatory_outcome") is None and not res.get("wrote")
            if (
                pdufa_d is not None
                and (today - pdufa_d).days > grace
                and unresolved
                and appno not in existing
            ):
                stale.append(appno)

        n_resolved = sum(1 for r in results if r.get("regulatory_outcome"))
        n_wrote = sum(1 for r in results if r.get("wrote"))
        log = {
            "n_candidate_apps": len(apps),
            "n_resolved": n_resolved,
            "n_wrote": n_wrote,
            "n_failed": n_failed,
            "horizons": horizons,
            "grace_days": grace,
            "crl_source_available": crl_source_available,
            "stale_pending_unresolved": stale,  # operator eyes — never fabricated
            "results": results,
        }
        status = "partial" if n_failed > 0 else "succeeded"
        if apply:
            _shared_close_run(
                client, run_id, status=status,
                n_processed=n_wrote, n_failed=n_failed, cost_usd=0, log=log,
                reason=(f"{n_failed} app(s) failed" if n_failed else None),
            )
        return {"status": status, "stats": log, "results": results}

    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}: {str(exc)[:200]}"
        if apply:
            _shared_close_run(
                client, run_id, status="failed",
                n_processed=0, n_failed=n_failed, cost_usd=0,
                log={"error": reason, "results": results, "stale_pending_unresolved": stale},
                reason=reason,
            )
        raise


def _parse_iso(value: Any) -> Optional[date]:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _crl_source_importable() -> bool:
    """The CRL transparency module exists; its FETCH needs the network. We gate
    only on importability so the approvals path runs even if the module is absent
    (it is the A0 deliverable; §5.1 risk 2)."""
    try:
        import modal_workers.bc_outcome_labeler.openfda_crl_transparency  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _build_market_data() -> Any:
    """Build the Polygon market-data provider (the §0.8 reuse). None when no key —
    the labeler then records verdicts with price null (graceful).

    conan's ``PolygonMarketData.__init__(self, client)`` takes a ``PolygonClient``,
    and ``PolygonClient()`` reads ``POLYGON_API_KEY`` from the env (raising if unset)
    — the same construction shape as bcfda's, so this is a 1:1 import rewire."""
    try:
        from modal_workers.providers.polygon.base import PolygonClient
        from modal_workers.providers.polygon.market_data import PolygonMarketData

        return PolygonMarketData(PolygonClient())
    except Exception as exc:  # noqa: BLE001
        logger.warning("polygon market-data unavailable (%s) — prices will be null", exc)
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="BC outcome labeler (Phase 3 §4-§5; logging only).")
    parser.add_argument("--apply", action="store_true",
                        help="WRITE bc_prediction_outcomes + open/close a bc_pipeline_runs row (default: DRY-RUN).")
    parser.add_argument("--today", default=None, help="Override 'today' (ISO). Default = now (UTC).")
    parser.add_argument("--json-out", default=None, help="Write the full result JSON to this path.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(levelname)s %(name)s %(message)s")
    if args.apply:
        logger.warning("--apply set: will WRITE bc_prediction_outcomes (logging only; NO refit).")

    today = _parse_iso(args.today) if args.today else None
    out = run_labeler(apply=args.apply, today=today)

    print("\n===== bc_outcome_labeler " + ("--apply" if args.apply else "DRY-RUN") + " =====")
    print(json.dumps(out["stats"], indent=2, default=str))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
