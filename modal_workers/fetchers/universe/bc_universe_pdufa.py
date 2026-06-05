"""bc_universe_pdufa — approach-1 PDUFA universe enumerator for the BC-FDA monitor (Light v4).

THE GATE (Phase 0). Discovers the set of **pending, in-window, tradeable NDA/BLA names with a
real PDUFA date** by mining EDGAR 8-K/6-K disclosures, then resolves tradeability via Polygon.
Writes the three BC tables idempotently — but **defaults to dry-run** (no DB writes). The
``--apply`` flag is provided for a later increment and is intentionally NOT exercised in the
Phase-0 spike (see the build handoff: dry-run-only until a Pedro go/no-go).

Pipeline (per spec §3 / §6):

    EFTS discover (8-K,6-K; PDUFA phrases; window ±120d)        # reuse edgar_efts.efts_search
      -> fetch filing body                                      # reuse edgar_efts.fetch_filing_text
      -> parse (date + designations + appl_type hint + drug)    # bc_pdufa_extract.extract_pdufa
      -> resolve filer CIK + ticker                             # reuse edgar_8k_pdufa._extract_cik/_ticker
      -> Polygon tradeability (market_cap, ADV, options-exists)  # providers/polygon/* + reference-contracts
      -> idempotent snapshot-versioned upserts into bc_*         # ONLY when --apply
      -> open/close a bc_pipeline_runs row (fail-loud)

Design notes baked in from the Phase-0 live probes (2026-06-04):
  - Polygon options *snapshots* are 403 (entitlement-blocked), but the **reference-contracts**
    endpoint ``/v3/reference/options/contracts?underlying_ticker=`` returns contract-existence
    (HTTP 200). So ``options_chain_exists`` is sourced from reference data, NOT the snapshot.
  - Polygon is rate-limited to ~5 req/min on the current plan. The enumerator paces Polygon
    calls (``--polygon-pace-s``, default 13s) and caches per-CIK so a daily ~20-name universe
    stays under the ceiling. Set ``--polygon-pace-s 0`` only with a paid plan.

CHECK-safety (verified live 2026-06-04 against xvwvwbnxdsjpnealarkh):
  - ``bc_application_features.feature_quality`` ∈ {standard, low, built_at_install}; surrogate
    (``EDGAR8K:`` appno) rows get ``'low'``, real-appno rows ``'standard'``.
  - ``bc_application_features.appl_type`` ∈ {NDA, BLA, sNDA, sBLA}; we emit NDA/BLA only.
  - ``bc_application_features.review_priority`` ∈ {STANDARD, PRIORITY} (nullable) — left NULL.
  - ``bc_application_features.sponsor_name`` is NOT NULL — always populated (filer name fallback).
  - ``bc_pipeline_runs.status`` ∈ {running, succeeded, failed, partial}.
  - designations / borrow_available are NULL when unknown (never False).

Run locally (dry-run; reads live EFTS + Polygon, writes nothing):
    SEC_USER_AGENT="Name contact@example.com" POLYGON_API_KEY=... \\
    python3 -m modal_workers.fetchers.universe.bc_universe_pdufa \\
        --window-days 120 --json-out /tmp/bc_universe_dryrun.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.shared.bc_pdufa_extract import PdufaExtract, extract_pdufa  # noqa: E402
from modal_workers.shared.bc_appno_recover import (  # noqa: E402
    RecoveredAppno,
    recover_real_appno,
)
from modal_workers.shared.bc_pipeline_runs import (  # noqa: E402
    open_run as _shared_open_run,
    close_run as _shared_close_run,
)
from modal_workers.fetchers.universe.edgar_8k_pdufa import (  # noqa: E402
    _extract_cik,
    _extract_ticker,
)

logger = logging.getLogger("bc_universe_pdufa")

PIPELINE_NAME = "bc_universe_pdufa"

# High-precision PDUFA 8-K phrasings (mirrors edgar_8k_pdufa._PDUFA_QUERIES; the
# canonical IR wordings). Each query is issued separately; we dedupe on accession.
_PDUFA_QUERIES = (
    '"PDUFA goal date"',
    '"PDUFA action date"',
    '"PDUFA target action"',
)

# Forms to search. 8-K = US domestic; 6-K = foreign private issuers (ADRs) that
# don't file 8-Ks. Both carry PDUFA disclosures.
_FORMS = "8-K,6-K"

EDGAR_POLITE_SLEEP_S = 0.12  # 10 req/s SEC ceiling (between body fetches)


# ---------------------------------------------------------------------------
# Per-name candidate record (in-memory; the dry-run product)
# ---------------------------------------------------------------------------


@dataclass
class UniverseCandidate:
    """One discovered pending-PDUFA application + its tradeability, in memory.

    This is what the dry-run emits and the gate counts. When --apply is set, it
    is also the source for the three bc_* upsert bodies (see _build_write_bodies).
    """

    # identity / discovery
    cik: Optional[str]
    ticker: Optional[str]
    sponsor_name: Optional[str]
    drug_name: Optional[str]
    accession: str
    file_date: Optional[str]
    forms_hit: str

    # parsed payload
    pdufa_date: Optional[str] = None          # ISO
    appl_type: Optional[str] = None           # NDA | BLA
    application_number: Optional[str] = None   # real or surrogate EDGAR8K:<cik>:<slug>
    is_surrogate_appno: bool = False
    has_bt: Optional[bool] = None
    has_ft: Optional[bool] = None
    has_aa: Optional[bool] = None
    review_priority: Optional[str] = None      # PRIORITY | STANDARD | None (from drugsfda ORIG)

    # real-appno recovery (drugsfda join) — see _recover_appnos
    surrogate_appno: Optional[str] = None      # the EDGAR8K: surrogate we'd otherwise use
    appno_recovered: bool = False              # True once a real NDA/BLA replaced the surrogate
    appno_match_basis: Optional[str] = None    # 'brand' | 'sole' | None

    # window
    days_to_pdufa: Optional[int] = None
    in_window: bool = False                   # 0 <= days_to_pdufa <= window_days

    # tradeability (Polygon)
    market_cap_usd: Optional[float] = None
    avg_daily_volume_usd: Optional[float] = None
    options_chain_exists: Optional[bool] = None
    borrow_available: Optional[bool] = None    # always None (no source)
    polygon_errors: List[str] = field(default_factory=list)

    # derived gates
    passes_g2: bool = False                    # mcap+adv+(options OR borrow)
    passes_mcap_adv_only: bool = False         # the options-relaxed cut (M')


# ---------------------------------------------------------------------------
# Surrogate application-number synthesis
# ---------------------------------------------------------------------------

def _drug_slug(drug: Optional[str], accession: str, pdufa_date: Optional[str] = None) -> str:
    """Stable surrogate-suffix for an application disclosure.

    **The PDUFA date is the PRIMARY key**, not the drug name. Rationale (idempotency —
    gate criterion 4): the parsed drug name is NON-DETERMINISTIC across daily runs (EFTS
    returns a sponsor's 8-Ks in varying order, so different filings parse different
    drug-name candidates — e.g. VRDN's 06-30 application parsed as "vrdn-006" one run and
    as nothing -> a date fallback the next). Keying the surrogate on the (unstable) drug
    therefore created a NEW ``EDGAR8K:`` row every time the parse drifted, so a second
    same-day ``--apply`` was NOT a no-op (it forked rows like ``…:vrdn-006`` vs
    ``…:d20260630``). The PDUFA *date* is the sponsor-application's stable identifier — two
    8-Ks about the same goal date always collapse to ``d<date>`` regardless of drug-parse
    variance — so we key on it first. Genuinely-distinct same-sponsor applications carry
    DIFFERENT goal dates (e.g. IONS olezarsen 06-30 vs zilganersen 09-22), so date-keying
    still separates them. Only when NO date was extracted do we fall back to the drug slug,
    then the accession tail, as a last-resort uniquifier."""
    if pdufa_date:
        return "d" + re.sub(r"[^0-9]+", "", pdufa_date)
    if drug:
        slug = re.sub(r"[^a-z0-9]+", "-", drug.lower()).strip("-")
        if slug:
            return slug[:40]
    return re.sub(r"[^a-z0-9]+", "", accession.lower())[-12:] or "unknown"


def _surrogate_appno(cik: Optional[str], drug: Optional[str], accession: str,
                     pdufa_date: Optional[str] = None) -> str:
    return f"EDGAR8K:{cik or '0'}:{_drug_slug(drug, accession, pdufa_date)}"


# ---------------------------------------------------------------------------
# EFTS discovery (reuse edgar_efts.efts_search) — window goes back AND forward.
# A PDUFA goal date is frequently disclosed months ahead, so the filing window
# (when the 8-K was filed) is the recent past; but we also keep filings up to
# `window_days` forward in case a re-disclosure lands. EFTS dateRange filters on
# FILE date, so the practical window is [today - lookback, today].
# ---------------------------------------------------------------------------

def _discover(
    *,
    user_agent: str,
    lookback_days: int,
    size: int,
    sleep_between_bodies: float = EDGAR_POLITE_SLEEP_S,
) -> Dict[str, Dict[str, Any]]:
    """Return {accession: hit_info} for 8-K/6-K filings matching any PDUFA query
    in [today - lookback_days, today]. Deduped on accession across queries.

    hit_info carries the EFTS source fields we need downstream: first display
    name (for CIK/ticker/company extraction), adsh, file_id, file_date.
    """
    from modal_workers.shared.edgar_efts import efts_search

    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=lookback_days)).isoformat()
    end = today.isoformat()

    by_accession: Dict[str, Dict[str, Any]] = {}
    for q in _PDUFA_QUERIES:
        try:
            hits = efts_search(q, start, end, forms=_FORMS, size=size, user_agent=user_agent)
        except Exception as e:  # noqa: BLE001
            logger.warning("EFTS query %s failed: %s", q, e)
            continue
        for h in hits:
            src = h.get("_source") or {}
            adsh = src.get("adsh") or ""
            if not adsh or adsh in by_accession:
                continue
            names = src.get("display_names") or []
            first_name = names[0] if names else ""
            by_accession[adsh] = {
                "accession": adsh,
                "file_id": h.get("_id", ""),
                "display_name": first_name,
                "cik_src": (src.get("ciks") or [""])[0] if src.get("ciks") else "",
                "file_date": src.get("file_date"),
                "forms": ",".join(src.get("file_type", []) if isinstance(src.get("file_type"), list) else [src.get("file_type") or ""]),
            }
    return by_accession


# ---------------------------------------------------------------------------
# Per-hit parse → candidate
# ---------------------------------------------------------------------------

def _days_to(iso_date: str, today: date) -> Optional[int]:
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
        return (d - today).days
    except (ValueError, TypeError):
        return None


def _hit_to_candidate(
    info: Dict[str, Any],
    *,
    user_agent: str,
    window_days: int,
    today: date,
) -> Optional[UniverseCandidate]:
    """Fetch the filing body, parse it, and build a UniverseCandidate. Returns
    None only when the body can't be fetched (network) — a parse miss still
    yields a candidate (with pdufa_date=None) so coverage stats see it."""
    from modal_workers.shared.edgar_efts import fetch_filing_text

    display = info.get("display_name") or ""
    cik = _extract_cik(display) or (info.get("cik_src") or "").lstrip("0") or None
    ticker = _extract_ticker(display)
    company = display.split(" (CIK")[0].strip() if display else None
    accession = info["accession"]

    text = fetch_filing_text(info.get("file_id", ""), cik or "", accession, user_agent=user_agent)
    if text is None:
        return None

    parsed: PdufaExtract = extract_pdufa(text)
    pdufa_iso = parsed.pdufa_date_iso

    # appl_type: prefer the keyword hint; default NDA when a date was found but
    # type is ambiguous (NDA is the modal case for 8-K PDUFA disclosures).
    appl_type = parsed.appl_type_hint
    if appl_type not in ("NDA", "BLA"):
        appl_type = "NDA" if pdufa_iso else None

    # application_number: start with the surrogate. A later drugsfda join
    # (_recover_appnos) may replace it with the real NDA/BLA number. The pdufa
    # date is the dedup fallback when the drug name is junk/absent.
    surrogate = _surrogate_appno(cik, parsed.drug_name, accession, pdufa_iso)

    cand = UniverseCandidate(
        cik=cik,
        ticker=ticker,
        sponsor_name=company,
        drug_name=parsed.drug_name,
        accession=accession,
        file_date=info.get("file_date"),
        forms_hit=info.get("forms") or _FORMS,
        pdufa_date=pdufa_iso,
        appl_type=appl_type,
        application_number=surrogate,
        is_surrogate_appno=True,
        surrogate_appno=surrogate,
        has_bt=parsed.has_bt,
        has_ft=parsed.has_ft,
        has_aa=parsed.has_aa,
    )
    if pdufa_iso:
        d = _days_to(pdufa_iso, today)
        cand.days_to_pdufa = d
        cand.in_window = d is not None and 0 <= d <= window_days
    return cand


# ---------------------------------------------------------------------------
# Polygon tradeability
# ---------------------------------------------------------------------------

def _options_exist_via_reference(client, ticker: str) -> Optional[bool]:
    """Contract-EXISTENCE via /v3/reference/options/contracts (reference data,
    available even when the options *snapshot* endpoint is 403'd — verified live
    2026-06-04). Returns True if >=1 non-expired contract exists, False if zero,
    None on error/unknown."""
    try:
        body = client.get(
            "/v3/reference/options/contracts",
            params={"underlying_ticker": ticker, "limit": 1, "expired": "false"},
        )
    except Exception as e:  # noqa: BLE001 — PolygonError for non-404 4xx (e.g. 403/429)
        logger.info("polygon options-ref %s error: %s", ticker, e)
        return None
    if not isinstance(body, dict):
        return None
    results = body.get("results")
    if results is None:
        return None
    return len(results) > 0


def _resolve_tradeability(
    cand: UniverseCandidate,
    *,
    market_data,
    poly_client,
    pace_s: float,
    cik_cache: Dict[str, Dict[str, Any]],
) -> None:
    """Populate market_cap / ADV / options_chain_exists on the candidate and set
    the G2 flags. Caches per-CIK (one universe name == one CIK == one tradeability
    snapshot). Paces Polygon calls by `pace_s` to respect the ~5 req/min ceiling.
    """
    if not cand.ticker:
        cand.polygon_errors.append("no_ticker")
        return
    cache_key = cand.cik or cand.ticker
    if cache_key in cik_cache:
        c = cik_cache[cache_key]
        cand.market_cap_usd = c["mc"]
        cand.avg_daily_volume_usd = c["adv"]
        cand.options_chain_exists = c["opt"]
        cand.polygon_errors.extend(c["errs"])
    else:
        errs: List[str] = []
        mc = None
        adv = None
        opt = None
        try:
            mc = market_data.get_market_cap(cand.ticker)
        except Exception as e:  # noqa: BLE001
            errs.append(f"market_cap:{type(e).__name__}")
        if pace_s:
            time.sleep(pace_s)
        try:
            adv = market_data.get_adv(cand.ticker, days=30)
        except Exception as e:  # noqa: BLE001
            errs.append(f"adv:{type(e).__name__}")
        if pace_s:
            time.sleep(pace_s)
        opt = _options_exist_via_reference(poly_client, cand.ticker)
        if pace_s:
            time.sleep(pace_s)
        cik_cache[cache_key] = {"mc": mc, "adv": adv, "opt": opt, "errs": errs}
        cand.market_cap_usd = mc
        cand.avg_daily_volume_usd = adv
        cand.options_chain_exists = opt
        cand.polygon_errors.extend(errs)

    cand.borrow_available = None  # no source

    mc_ok = cand.market_cap_usd is not None and cand.market_cap_usd >= 250_000_000
    adv_ok = cand.avg_daily_volume_usd is not None and cand.avg_daily_volume_usd >= 2_000_000
    liq_ok = bool(cand.options_chain_exists) or bool(cand.borrow_available)
    cand.passes_g2 = mc_ok and adv_ok and liq_ok
    cand.passes_mcap_adv_only = mc_ok and adv_ok


# ---------------------------------------------------------------------------
# Real NDA/BLA application-number recovery (drugsfda join). READ-ONLY (only GETs
# openFDA; writes nothing) so it runs in BOTH the dry-run and --apply paths and
# improves the universe either way. Per-CIK cache so one sponsor is queried once
# per run; paced to respect the openFDA shared-IP cap. A miss keeps the surrogate.
# ---------------------------------------------------------------------------

def _recover_one(cand: UniverseCandidate, *, recover_fn, cache: Dict[str, Any]) -> None:
    """Attempt to replace cand's surrogate appno with a real NDA/BLA via
    Drugs@FDA. Mutates cand in place. Cache key is (cik, normalized drug) so two
    8-Ks about the same drug from the same filer share the single query."""
    cache_key = f"{cand.cik or '0'}|{(cand.drug_name or '').strip().lower()}"
    if cache_key in cache:
        rec: Optional[RecoveredAppno] = cache[cache_key]
    else:
        rec = recover_fn(cand.drug_name, cand.sponsor_name)
        cache[cache_key] = rec
    if rec is None:
        return
    # Recovered a real number — adopt it + the authoritative type/priority.
    cand.application_number = rec.application_number
    cand.appl_type = rec.appl_type
    cand.is_surrogate_appno = False
    cand.appno_recovered = True
    cand.appno_match_basis = rec.match_basis
    if rec.review_priority:
        cand.review_priority = rec.review_priority


def _recover_appnos(
    in_window: List[UniverseCandidate],
    *,
    recover_fn=recover_real_appno,
    pace_s: float = 0.0,
    max_lookups: Optional[int] = None,
) -> Dict[str, Any]:
    """Run drugsfda recovery over the in-window candidates. Returns a small stats
    dict. ``recover_fn`` is injectable for tests (signature
    ``(drug, sponsor) -> Optional[RecoveredAppno]``)."""
    cache: Dict[str, Any] = {}
    lookups = 0
    recovered = 0
    for cand in in_window:
        before_keys = len(cache)
        if (max_lookups is not None and lookups >= max_lookups
                and f"{cand.cik or '0'}|{(cand.drug_name or '').strip().lower()}" not in cache):
            continue
        _recover_one(cand, recover_fn=recover_fn, cache=cache)
        if len(cache) > before_keys:
            lookups += 1
            if pace_s and lookups < len(in_window):
                time.sleep(pace_s)
        if cand.appno_recovered:
            recovered += 1
    return {"appno_lookups": lookups, "appno_recovered": recovered,
            "appno_surrogate_remaining": sum(1 for c in in_window if c.is_surrogate_appno)}


# ---------------------------------------------------------------------------
# Write bodies (designed per §3; EXECUTED ONLY WHEN --apply). Returns the three
# upsert payloads for one candidate so they can be asserted offline by tests.
# ---------------------------------------------------------------------------

def _build_write_bodies(cand: UniverseCandidate, snapshot_iso: str) -> Dict[str, Dict[str, Any]]:
    """Construct the idempotent, snapshot-versioned, CHECK-safe upsert bodies for
    bc_applications / bc_application_features / bc_company_tradeable for one
    candidate. Pure — no I/O. The enumerator only POSTs these under --apply.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    appno = cand.application_number or _surrogate_appno(
        cand.cik, cand.drug_name, cand.accession, cand.pdufa_date)
    feature_quality = "low" if appno.startswith("EDGAR8K:") else "standard"
    sponsor_name = cand.sponsor_name or (cand.ticker or "unknown")

    applications = {
        "application_number": appno,
        "sponsor_cik": cand.cik or "0",
        "sponsor_name": sponsor_name,
        "appl_type": cand.appl_type or "NDA",
    }
    features = {
        "sponsor_cik": cand.cik or "0",
        "sponsor_name": sponsor_name,                # NOT NULL (verified live)
        "application_number": appno,
        "appl_type": cand.appl_type or "NDA",
        "pdufa_date": cand.pdufa_date,               # the payload
        "has_bt": cand.has_bt,                        # NULL when unknown (not False)
        "has_ft": cand.has_ft,
        "has_aa": cand.has_aa,
        "review_priority": cand.review_priority,      # ∈ {STANDARD,PRIORITY} or NULL (drugsfda ORIG)
        "submission_date": None,
        "cycle_type": "unknown",                      # NOT NULL placeholder (Phase 1 refines)
        "is_biosimilar_bla": False,                   # NOT NULL default
        "as_of_date": snapshot_iso,                   # NOT NULL
        "snapshot_date": snapshot_iso,                # NOT NULL
        "built_at": now_iso,                          # NOT NULL
        "feature_quality": feature_quality,           # CHECK-safe
        # all M14 feature columns left to their column defaults (Phase 1 fills)
    }
    tradeable = {
        "sponsor_cik": cand.cik or "0",
        "ticker": cand.ticker,
        "snapshot_date": snapshot_iso,
        "market_cap_usd": cand.market_cap_usd,
        "avg_daily_volume_usd": cand.avg_daily_volume_usd,
        "options_chain_exists": cand.options_chain_exists,  # may be NULL (unknown)
        "borrow_available": None,                            # no source
        "borrow_cost_bps": None,
        "data_source": "polygon",
        "fetched_at": now_iso,
    }
    return {"applications": applications, "features": features, "tradeable": tradeable}


# Idempotency on_conflict targets (the live composite UNIQUEs).
_ON_CONFLICT = {
    "applications": "application_number",
    "features": "sponsor_cik,application_number,snapshot_date",
    "tradeable": "sponsor_cik,snapshot_date",
}
_TABLE = {
    "applications": "bc_applications",
    "features": "bc_application_features",
    "tradeable": "bc_company_tradeable",
}


def _apply_writes(client, candidates: List[UniverseCandidate],
                  snapshot_iso: str) -> Dict[str, int]:
    """POST the idempotent upserts for every in-window candidate. ONLY called
    under --apply. Returns {written, skipped_no_cik, tradeable_written}.

    A candidate with **no real CIK** is SKIPPED entirely (not written under a
    ``sponsor_cik="0"`` placeholder). Rationale: the ``bc_candidates`` matview joins
    ``bc_company_tradeable.sponsor_cik = bc_application_features.sponsor_cik``, so a
    ``"0"`` sponsor_cik would (a) collide two distinct CIK-less sponsors on the
    tradeable composite UNIQUE ``(sponsor_cik, snapshot_date)`` — silently dropping
    the second — and (b) cross-link every CIK-less feature row to whichever ``"0"``
    tradeable row survived, fabricating tradeability. CIK is effectively always
    present on real EFTS hits (it comes from ``_source.ciks``), so this is a
    correctness guard that only ever UNDER-counts; it can never manufacture a false
    GO. Skips are surfaced in stats + ``bc_pipeline_runs.log`` so coverage loss is
    visible, not hidden."""
    written = 0
    skipped_no_cik = 0
    tradeable_written = 0
    seen_tradeable_cik: set = set()
    for cand in candidates:
        if not cand.cik:
            skipped_no_cik += 1
            logger.warning(
                "skip write (no real CIK): ticker=%s drug=%s pdufa=%s accession=%s",
                cand.ticker, cand.drug_name, cand.pdufa_date, cand.accession)
            continue
        bodies = _build_write_bodies(cand, snapshot_iso)
        client._rest_with_retry(
            "POST",
            f"{_TABLE['applications']}?on_conflict={_ON_CONFLICT['applications']}",
            json_body=[bodies["applications"]],
            prefer="resolution=merge-duplicates,return=minimal",
        )
        client._rest_with_retry(
            "POST",
            f"{_TABLE['features']}?on_conflict={_ON_CONFLICT['features']}",
            json_body=[bodies["features"]],
            prefer="resolution=merge-duplicates,return=minimal",
        )
        cik = cand.cik  # guaranteed real here
        if cik not in seen_tradeable_cik and cand.ticker:
            client._rest_with_retry(
                "POST",
                f"{_TABLE['tradeable']}?on_conflict={_ON_CONFLICT['tradeable']}",
                json_body=[bodies["tradeable"]],
                prefer="resolution=merge-duplicates,return=minimal",
            )
            seen_tradeable_cik.add(cik)
            tradeable_written += 1
        written += 1
    return {"written": written, "skipped_no_cik": skipped_no_cik,
            "tradeable_written": tradeable_written}


# ---------------------------------------------------------------------------
# bc_pipeline_runs open/close (fail-loud). ONLY called under --apply; the dry-run
# does not write a pipeline-runs row (it writes nothing).
#
# The open/close contract lives in the reusable shared helper
# (modal_workers/shared/bc_pipeline_runs.py) — this enumerator is its FIRST
# consumer (phase0 §5.1; Phase 1/2/3 import the same two functions). These thin
# module-level wrappers bind the enumerator's fixed pipeline_name so call sites
# (and the offline write-contract tests) read cleanly; all behavior — including
# the live CHECK-safe status domain — is enforced in the shared helper.
# ---------------------------------------------------------------------------

def _open_run(client, snapshot_iso: str) -> Optional[str]:
    """Open this enumerator's bc_pipeline_runs row (status='running'). Delegates
    to the shared helper, binding pipeline_name=PIPELINE_NAME."""
    return _shared_open_run(client, pipeline_name=PIPELINE_NAME, snapshot_date=snapshot_iso)


def _close_run(client, run_id: Optional[str], *, status: str, n_processed: int,
               n_failed: int, log: Dict[str, Any], reason: Optional[str] = None) -> None:
    """Close this enumerator's bc_pipeline_runs row. Delegates to the shared
    helper (cost_usd=0 — no LLM on the universe path). No-op when run_id is falsy."""
    _shared_close_run(
        client, run_id, status=status, n_processed=n_processed,
        n_failed=n_failed, cost_usd=0, log=log, reason=reason,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def enumerate_universe(
    *,
    user_agent: str,
    poly_client,
    market_data,
    window_days: int = 120,
    lookback_days: Optional[int] = None,
    size: int = 100,
    polygon_pace_s: float = 13.0,
    max_polygon_names: Optional[int] = None,
    recover_appno: bool = True,
    openfda_pace_s: float = 0.0,
    max_appno_lookups: Optional[int] = None,
) -> Dict[str, Any]:
    """Run the full approach-1 enumeration IN MEMORY (no DB writes). Returns a
    dict with the candidate list + measured coverage/gate stats.

    lookback_days defaults to window_days (symmetric horizon framing): we look
    back `window_days` for filings and forward `window_days` for the PDUFA date.

    When ``recover_appno`` is True, after the in-window set is settled we attempt
    to replace each surrogate ``EDGAR8K:`` appno with the real NDA/BLA number via a
    read-only Drugs@FDA join (``_recover_appnos``). This is read-only and runs in
    dry-run too. A miss keeps the surrogate (``feature_quality='low'``).
    """
    today = datetime.now(timezone.utc).date()
    lookback = lookback_days if lookback_days is not None else window_days

    discovered = _discover(user_agent=user_agent, lookback_days=lookback, size=size)
    logger.info("EFTS discovery: %d distinct accessions", len(discovered))

    candidates: List[UniverseCandidate] = []
    body_fetch_failures = 0
    for info in discovered.values():
        cand = _hit_to_candidate(info, user_agent=user_agent, window_days=window_days, today=today)
        if cand is None:
            body_fetch_failures += 1
            continue
        candidates.append(cand)
        time.sleep(EDGAR_POLITE_SLEEP_S)

    # Tradeability only for in-window candidates with a ticker (the gate universe).
    # We dedup by CIK so we don't spend Polygon calls twice on the same sponsor.
    in_window = [c for c in candidates if c.in_window and c.appl_type in ("NDA", "BLA")]
    # Distinct by (cik or ticker) keeps the Polygon spend ~= number of names.
    cik_cache: Dict[str, Dict[str, Any]] = {}
    polled = 0
    for cand in in_window:
        if max_polygon_names is not None and polled >= max_polygon_names and (cand.cik or cand.ticker) not in cik_cache:
            cand.polygon_errors.append("skipped_polygon_budget")
            continue
        before = len(cik_cache)
        _resolve_tradeability(
            cand, market_data=market_data, poly_client=poly_client,
            pace_s=polygon_pace_s, cik_cache=cik_cache,
        )
        if len(cik_cache) > before:
            polled += 1

    # Real NDA/BLA number recovery via Drugs@FDA (read-only). Replaces surrogates
    # where a confident match exists; misses keep the EDGAR8K: surrogate.
    recovery_stats: Dict[str, Any] = {
        "appno_lookups": 0, "appno_recovered": 0,
        "appno_surrogate_remaining": sum(1 for c in in_window if c.is_surrogate_appno),
    }
    if recover_appno and in_window:
        try:
            recovery_stats = _recover_appnos(
                in_window, pace_s=openfda_pace_s, max_lookups=max_appno_lookups)
        except Exception as e:  # noqa: BLE001 — recovery is advisory, never fatal
            logger.warning("appno recovery pass raised (keeping surrogates): %s", e)
            recovery_stats["appno_error"] = f"{type(e).__name__}: {str(e)[:200]}"

    stats = _compute_stats(candidates, in_window, window_days, today,
                           body_fetch_failures=body_fetch_failures,
                           discovered_n=len(discovered))
    stats.update(recovery_stats)
    return {"candidates": candidates, "in_window": in_window, "stats": stats}


def _compute_stats(candidates, in_window, window_days, today, *,
                   body_fetch_failures: int, discovered_n: int) -> Dict[str, Any]:
    parsed_dates = [c for c in candidates if c.pdufa_date]
    # distinct in-window pending NDA/BLA names by application_number
    distinct_in_window = {c.application_number for c in in_window}
    g2_pass = [c for c in in_window if c.passes_g2]
    mcap_adv_pass = [c for c in in_window if c.passes_mcap_adv_only]
    distinct_g2 = {c.application_number for c in g2_pass}
    distinct_mcap_adv = {c.application_number for c in mcap_adv_pass}

    mc_hits = [c for c in in_window if c.market_cap_usd is not None]
    adv_hits = [c for c in in_window if c.avg_daily_volume_usd is not None]
    opt_known = [c for c in in_window if c.options_chain_exists is not None]
    no_ticker = [c for c in in_window if not c.ticker]
    no_cik = [c for c in candidates if not c.cik]

    # appno provenance split (in-window): real recovered vs surrogate remaining
    real_appno = [c for c in in_window if not c.is_surrogate_appno]
    surrogate_appno = [c for c in in_window if c.is_surrogate_appno]

    return {
        "today": today.isoformat(),
        "window_days": window_days,
        "discovered_accessions": discovered_n,
        "body_fetch_failures": body_fetch_failures,
        "candidates_total": len(candidates),
        "candidates_with_parsed_date": len(parsed_dates),
        "parse_success_rate": round(len(parsed_dates) / len(candidates), 3) if candidates else 0.0,
        # THE GATE NUMBERS
        "N_in_window_pending_nda_bla": len(distinct_in_window),
        "M_in_window_tradeable_G2": len(distinct_g2),
        "M_prime_in_window_mcap_adv_only": len(distinct_mcap_adv),
        # coverage / resolution
        "polygon_market_cap_hits": len(mc_hits),
        "polygon_adv_hits": len(adv_hits),
        "polygon_options_known": len(opt_known),
        "in_window_missing_ticker": len(no_ticker),
        "candidates_missing_cik": len(no_cik),
        # appno provenance (real recovered via drugsfda vs surrogate EDGAR8K:)
        "in_window_real_appno": len(real_appno),
        "in_window_surrogate_appno": len(surrogate_appno),
        "gate_threshold_in_window": 15,
        "gate_threshold_tradeable": 12,
    }


def run(
    *,
    apply: bool,
    window_days: int,
    lookback_days: Optional[int],
    size: int,
    polygon_pace_s: float,
    max_polygon_names: Optional[int],
    recover_appno: bool = True,
    openfda_pace_s: float = 0.0,
    max_appno_lookups: Optional[int] = None,
) -> Dict[str, Any]:
    """Top-level: build providers, enumerate, optionally apply. Fail-loud via
    bc_pipeline_runs ONLY when apply=True."""
    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        raise RuntimeError(
            "SEC_USER_AGENT env var required (Modal scanner-secrets). "
            'Format: "Name contact@example.com"'
        )

    from modal_workers.providers.polygon.base import PolygonClient
    from modal_workers.providers.polygon.market_data import PolygonMarketData

    poly_client = PolygonClient()                 # raises if POLYGON_API_KEY unset
    market_data = PolygonMarketData(poly_client)

    snapshot_iso = datetime.now(timezone.utc).date().isoformat()

    # --apply path is fail-loud (open/close a run row even on crash).
    client = None
    run_id = None
    if apply:
        from modal_workers.shared.supabase_client import SupabaseClient
        client = SupabaseClient()
        run_id = _open_run(client, snapshot_iso)

    try:
        result = enumerate_universe(
            user_agent=user_agent,
            poly_client=poly_client,
            market_data=market_data,
            window_days=window_days,
            lookback_days=lookback_days,
            size=size,
            polygon_pace_s=polygon_pace_s,
            max_polygon_names=max_polygon_names,
            recover_appno=recover_appno,
            openfda_pace_s=openfda_pace_s,
            max_appno_lookups=max_appno_lookups,
        )
    except Exception as e:  # noqa: BLE001
        if apply and client is not None:
            _close_run(client, run_id, status="failed", n_processed=0, n_failed=0,
                       log={"error": str(e)[:500]}, reason=f"{type(e).__name__}: {str(e)[:200]}")
        raise

    if apply and client is not None:
        wstats = _apply_writes(client, result["in_window"], snapshot_iso)
        written = wstats["written"]
        result["stats"]["written"] = written
        result["stats"]["tradeable_written"] = wstats["tradeable_written"]
        result["stats"]["skipped_no_cik"] = wstats["skipped_no_cik"]
        # A skipped (no-CIK) candidate is a coverage loss, not a hard failure; count
        # it toward n_failed so bc_pipeline_runs surfaces it, and mark the run
        # 'partial' (vs 'succeeded') when any in-window candidate could not be written.
        status = "partial" if wstats["skipped_no_cik"] else "succeeded"
        _close_run(
            client, run_id, status=status,
            n_processed=written,
            n_failed=result["stats"]["body_fetch_failures"] + wstats["skipped_no_cik"],
            log=result["stats"],
            reason=(f"{wstats['skipped_no_cik']} in-window candidate(s) skipped: no real CIK"
                    if wstats["skipped_no_cik"] else None),
        )

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="BC PDUFA universe enumerator (approach 1).")
    parser.add_argument("--window-days", type=int, default=120,
                        help="In-window horizon (days to PDUFA). Default 120 (= l3.window_days).")
    parser.add_argument("--lookback-days", type=int, default=None,
                        help="EFTS filing lookback. Default = window-days.")
    parser.add_argument("--size", type=int, default=100, help="EFTS page size per query.")
    parser.add_argument("--polygon-pace-s", type=float, default=13.0,
                        help="Seconds between Polygon calls (rate-limit pacing). 0 = no pacing.")
    parser.add_argument("--max-polygon-names", type=int, default=None,
                        help="Cap distinct tickers polled on Polygon (dry-run budget guard).")
    parser.add_argument("--no-recover-appno", action="store_true",
                        help="Skip the read-only Drugs@FDA real-NDA/BLA-number recovery join "
                             "(default: recovery ON; misses keep the EDGAR8K: surrogate).")
    parser.add_argument("--openfda-pace-s", type=float, default=0.0,
                        help="Seconds between Drugs@FDA recovery queries (shared-IP-cap pacing).")
    parser.add_argument("--max-appno-lookups", type=int, default=None,
                        help="Cap distinct Drugs@FDA recovery lookups (budget guard).")
    parser.add_argument("--apply", action="store_true",
                        help="WRITE to bc_* + bc_pipeline_runs. Default = DRY-RUN (no writes).")
    parser.add_argument("--json-out", default=None, help="Write the full result JSON to this path.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")

    if args.apply:
        logger.warning("--apply set: will WRITE to bc_* tables.")
    else:
        logger.info("DRY-RUN: no DB writes. Reading live EFTS + Polygon only.")

    result = run(
        apply=args.apply,
        window_days=args.window_days,
        lookback_days=args.lookback_days,
        size=args.size,
        polygon_pace_s=args.polygon_pace_s,
        max_polygon_names=args.max_polygon_names,
        recover_appno=not args.no_recover_appno,
        openfda_pace_s=args.openfda_pace_s,
        max_appno_lookups=args.max_appno_lookups,
    )

    stats = result["stats"]
    print("\n===== bc_universe_pdufa dry-run gate =====" if not args.apply
          else "\n===== bc_universe_pdufa --apply =====")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    print("\n--- in-window candidates ---")
    for c in sorted(result["in_window"], key=lambda c: (c.days_to_pdufa if c.days_to_pdufa is not None else 1e9)):
        appno_disp = c.application_number if not c.is_surrogate_appno else "EDGAR8K:(surrogate)"
        print(
            f"  {c.ticker or '?':6s} {c.appl_type or '?':3s} pdufa={c.pdufa_date} "
            f"(+{c.days_to_pdufa}d) mc={_fmt_usd(c.market_cap_usd)} adv={_fmt_usd(c.avg_daily_volume_usd)} "
            f"opt={c.options_chain_exists} G2={c.passes_g2} mcap_adv={c.passes_mcap_adv_only} "
            f"appno={appno_disp}{'['+c.appno_match_basis+']' if c.appno_recovered else ''} "
            f"drug={c.drug_name or '-'} errs={c.polygon_errors or ''}"
        )

    if args.json_out:
        payload = {
            "stats": stats,
            "in_window": [asdict(c) for c in result["in_window"]],
            "all_candidates": [asdict(c) for c in result["candidates"]],
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2, default=str))
        print(f"\nwrote {args.json_out}")

    # verdict echo
    N = stats["N_in_window_pending_nda_bla"]
    M = stats["M_in_window_tradeable_G2"]
    Mp = stats["M_prime_in_window_mcap_adv_only"]
    verdict = "PASS" if (N >= 15 and M >= 12) else ("MARGINAL" if N >= 10 else "FAIL")
    print(f"\nGATE: N={N} (>=15) M={M} (>=12) M'={Mp} -> {verdict}")
    return 0


def _fmt_usd(v: Optional[float]) -> str:
    if v is None:
        return "None"
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    return f"${v:.0f}"


if __name__ == "__main__":
    raise SystemExit(main())
