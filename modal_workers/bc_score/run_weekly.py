"""bc_weekly_score — the weekly score-as-rank worker for the BC-FDA monitor (Light v4 Phase 1).

A thin, deterministic, ZERO-LLM worker. For the Phase-0 universe
(``bc_application_features`` rows with a non-NULL ``pdufa_date`` and
``appl_type IN ('NDA','BLA')``) it:

  1. builds the M14 feature substrate **point-in-time** (shared
     ``feature_builder_pit.build_features_pit`` — Drugs@FDA for priority/class/
     n_prior_filings + live EFTS-by-CIK for the 8-K count; NO look-ahead),
  2. scores each name with the **bc_-owned, re-vendored** M14 scorer
     (``modal_workers.bc_score._m14.score_nda``), and
  3. writes a **risk-band + percentile rank** to ``bc_rubric_scores`` (with the
     M14 columns + ``feature_quality`` on ``bc_application_features`` populated).

The score is a DEMOTED ranking input. ``p_crl`` is **persisted internally** (the
``bc_candidates`` matview gate keys on ``p_crl <= tau_nda`` for NDA/BLA) but is
**never surfaced** in the digest/dashboard (band-only v1, Pedro 2026-06-03). The
product surfaces only ``risk_band`` + ``oof_percentile_rank``.

Fail-loud: every run opens+closes a ``bc_pipeline_runs`` row (the only liveness
sink — migration 005 unapplied, so no ``bc_*`` ``operator_flags`` write). Idempotent:
``scored_at`` is anchored to the **scored snapshot date @ 00:00 UTC** (NOT ``now()``
— which would fork a row on every manual re-run; verified live), so ANY re-run on
the same snapshot merges in place on the ``(application_number, scored_at,
scorer_name)`` UNIQUE, while a new weekly snapshot adds a distinct historical row.
(This refines spec §3.3's "single per-run timestamp" to satisfy the hard
same-day-re-run-is-a-no-op guardrail.)

SUBSTRATE NOTE (verified live 2026-06-05). ``fda_application_submissions`` is
ABSENT live (its migration is intentionally not applied on this build). So the
submission-derived features come from the Drugs@FDA **API** in-memory (the shared
``DrugsFDA`` cache, ONE instance across all names for the openFDA 1000/day cap),
NOT from the absent table. Surrogate ``EDGAR8K:`` appnos (17/18 of today's
universe) have no Drugs@FDA record → those features fall absent → graceful,
flagged (``feature_quality='low'``) degradation, never a crash.

Run locally (DRY-RUN; reads live Drugs@FDA + EFTS, writes nothing)::

    SEC_USER_AGENT="Name x@y.com" OPENFDA_API_KEY=... SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python3 -m modal_workers.bc_score.run_weekly --json-out /tmp/bc_score_dryrun.json

(``--apply`` writes to bc_rubric_scores + bc_application_features + refreshes the
matview; default is DRY-RUN.)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.shared.bc_pipeline_runs import (  # noqa: E402
    close_run as _shared_close_run,
    open_run as _shared_open_run,
)
from modal_workers.shared.feature_builder_pit import (  # noqa: E402
    DrugsFDA,
    build_features_pit,
    count_8ks_by_cik_efts,
    estimate_ref_date,
    parse_compact_date,
    _REVIEW_CLOCK_DAYS,
    _shift,
)
from modal_workers.bc_score._m14 import NDA_MODEL_VERSION, score_nda, to_percentile  # noqa: E402

logger = logging.getLogger("bc_weekly_score")

PIPELINE_NAME = "bc_weekly_score"
SCORER_NAME = "M14_adjusted"  # MUST match the bc_candidates matview literal (002 §29)

# Per-row coverage floor (Phase 1 §6): >= this fraction of the v1-kept high-signal
# keys present => 'standard', else 'low'. A module constant (promote to bc_config
# l3.coverage_floor only if Pedro wants runtime tuning).
COVERAGE_FLOOR = 0.5

_FEATURES_TABLE = "bc_application_features"
_SCORES_TABLE = "bc_rubric_scores"
_FEATURES_ON_CONFLICT = "sponsor_cik,application_number,snapshot_date"  # live UNIQUE
_SCORES_ON_CONFLICT = "application_number,scored_at,scorer_name"        # live UNIQUE


# --------------------------------------------------------------------------- #
# locked-2025 M14 percentile reference (vendored data artifact — Phase 1 §0.6)
# --------------------------------------------------------------------------- #
_NDA_LOCKED2025_REF_PATH = (
    _REPO_ROOT / "modal_workers" / "bc_score" / "_m14" / "models"
    / "nda_m14_locked2025_reference.csv"
)


def _load_nda_locked2025_reference() -> List[float]:
    """The model authors' held-out 2025 calibrated predictions (``p_m14_cal``) —
    the empirical anchor for ``oof_percentile_rank``. Higher p_crl => higher
    percentile => riskier (do NOT invert)."""
    import csv

    out: List[float] = []
    try:
        with open(_NDA_LOCKED2025_REF_PATH, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                v = row.get("p_m14_cal")
                if v not in (None, ""):
                    try:
                        out.append(float(v))
                    except ValueError:
                        continue
    except FileNotFoundError:
        logger.warning("nda locked-2025 percentile reference missing at %s", _NDA_LOCKED2025_REF_PATH)
    return out


# --------------------------------------------------------------------------- #
# sponsor-name / ticker helpers (the universe stashes the ticker in parens)
# --------------------------------------------------------------------------- #
def _clean_sponsor_name(raw: Optional[str]) -> Optional[str]:
    """Strip the trailing ``  (TICKER[, TICKER2])`` suffix the Phase-0 enumerator
    appends to sponsor_name, for the Drugs@FDA ``sponsor_name:"…"`` query, and the
    EDGAR ``\\DE`` registrant suffix. Returns the bare legal name."""
    if not raw:
        return raw
    s = re.sub(r"\s*\([A-Z0-9 ,./-]+\)\s*$", "", raw).strip()
    s = re.sub(r"\\[A-Z]{2}\s*$", "", s).strip()  # e.g. "Viridian Therapeutics, Inc.\\DE"
    return s or raw


def _ticker_from_sponsor(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    m = re.search(r"\(([A-Z]{1,6})(?:[, ].*)?\)\s*$", raw)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# per-name outcome record (the dry-run product + the apply source)
# --------------------------------------------------------------------------- #
@dataclass
class ScoredName:
    application_number: str
    sponsor_cik: Optional[str]
    sponsor_name: Optional[str]
    ticker: Optional[str]
    appl_type: Optional[str]
    pdufa_date: Optional[str]

    ref_date: Optional[str] = None
    ref_date_source: Optional[str] = None

    # scorer output (p_crl is INTERNAL — never displayed; shown here only in the
    # operator-facing dry-run/run-log, which is not the product surface)
    p_crl: Optional[float] = None
    raw_p_uncalibrated: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    risk_band: Optional[str] = None
    oof_percentile_rank: Optional[float] = None
    confidence_flag: Optional[str] = None
    refusal_reason: Optional[str] = None
    scorer_version: Optional[str] = None

    coverage: Optional[float] = None
    feature_quality: Optional[str] = None
    required_feature_missing_count: Optional[int] = None
    feature_provenance: Dict[str, str] = field(default_factory=dict)
    n_8ks_30_180_clean: Optional[int] = None
    review_priority: Optional[str] = None
    submission_class_code: Optional[str] = None
    n_prior_filings: Optional[int] = None

    scored: bool = False
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# universe read (DISTINCT ON (application_number) — done in Python over an
# ordered fetch, mirroring the matview's latest_features CTE)
# --------------------------------------------------------------------------- #
def _read_universe(client) -> List[Dict[str, Any]]:
    """Latest feature row per application_number where pdufa_date is set and the
    type is NDA/BLA. Ordered snapshot_date DESC, built_at DESC; first-seen-per-
    appno wins (= the matview's DISTINCT ON)."""
    rows = client._rest(
        "GET",
        _FEATURES_TABLE,
        params={
            "select": (
                "id,sponsor_cik,sponsor_name,application_number,appl_type,pdufa_date,"
                "is_biosimilar_bla,has_bt,has_ft,has_aa,submission_date,snapshot_date,built_at"
            ),
            "pdufa_date": "not.is.null",
            "appl_type": "in.(NDA,BLA)",
            "order": "application_number.asc,snapshot_date.desc,built_at.desc",
        },
    ) or []
    seen: set = set()
    latest: List[Dict[str, Any]] = []
    for r in rows:
        an = r.get("application_number")
        if an in seen:
            continue
        seen.add(an)
        latest.append(r)
    return latest


# --------------------------------------------------------------------------- #
# ref_date resolution (no look-ahead) — Phase 1 §3.1
# --------------------------------------------------------------------------- #
def _resolve_ref_date(row: Dict[str, Any], *, dfda: DrugsFDA) -> tuple:
    """(ref_date: date, source: str). Preference (§3.1):
      1. ORIG submission filing date from Drugs@FDA (action date - review clock,
         since openFDA exposes the action date not the receipt date) — only for
         REAL appnos (surrogate EDGAR8K: have no drugsfda record);
      2. pdufa_date - 304d (standard ~10-month review clock; flagged estimated);
      3. submission_date from the Phase-0 row if present;
      4. today (last resort).
    Using pdufa_date itself would leak the outcome window — never do that."""
    appno = str(row.get("application_number") or "")
    is_surrogate = appno.upper().startswith("EDGAR8K:")
    if not is_surrogate:
        try:
            ref, method = estimate_ref_date(appno=appno, letter_date=None, dfda=dfda)
            if method == "drugsfda_orig_minus_clock":
                return ref, method
        except Exception as exc:  # noqa: BLE001 — degrade to the clock fallback
            logger.info("estimate_ref_date failed for %s: %s", appno, exc)
    pdufa = parse_compact_date(row.get("pdufa_date"))
    if pdufa is not None:
        return _shift(pdufa, _REVIEW_CLOCK_DAYS), "pdufa_minus_clock_estimated"
    sub = parse_compact_date(row.get("submission_date"))
    if sub is not None:
        return sub, "phase0_submission_date"
    return datetime.now(timezone.utc).date(), "today_fallback"


def _designations_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Carry the Phase-0 designation booleans (NULL stays absent so the scorer
    defaults; True/False are passed through)."""
    out: Dict[str, Any] = {}
    for k in ("has_bt", "has_ft", "has_aa"):
        v = row.get(k)
        if v is not None:
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# scorer-dict -> bc_rubric_scores cast helpers (Phase 1 §0.5)
# --------------------------------------------------------------------------- #
def _f(val: object) -> Optional[float]:
    """float() the scorer's "%.8f" string, or None when refused ("")."""
    if val in ("", None):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _verbatim(val: object) -> Optional[str]:
    s = str(val) if val is not None else None
    return s if s not in ("", None) else None


# --------------------------------------------------------------------------- #
# per-name: features -> score -> ScoredName (pure of DB writes)
# --------------------------------------------------------------------------- #
def _score_one(
    client,
    row: Dict[str, Any],
    *,
    dfda: DrugsFDA,
    nda_ref: List[float],
    user_agent: str,
    eight_k_counter=None,
) -> tuple:
    """Build features + score ONE universe row. Returns (ScoredName, feature_dict).
    Raises only on a genuine builder error (look-ahead, etc.) — the caller catches
    per-name so one bad row does not abort the run."""
    appno = str(row.get("application_number") or "")
    sponsor_raw = row.get("sponsor_name")
    sponsor_clean = _clean_sponsor_name(sponsor_raw)
    ticker = _ticker_from_sponsor(sponsor_raw)
    rec = ScoredName(
        application_number=appno,
        sponsor_cik=row.get("sponsor_cik"),
        sponsor_name=sponsor_clean,
        ticker=ticker,
        appl_type=row.get("appl_type"),
        pdufa_date=row.get("pdufa_date"),
    )

    ref_date, ref_source = _resolve_ref_date(row, dfda=dfda)
    rec.ref_date = ref_date.isoformat()
    rec.ref_date_source = ref_source

    if eight_k_counter is None:
        eight_k_counter = lambda c, r: count_8ks_by_cik_efts(c, r, user_agent=user_agent)  # noqa: E731

    feats = build_features_pit(
        client,
        application_number=appno,
        sponsor_cik=row.get("sponsor_cik"),
        sponsor_name=sponsor_clean,
        appl_type=row.get("appl_type"),
        ref_date=ref_date,
        designations=_designations_from_row(row),
        is_biosimilar_bla=bool(row.get("is_biosimilar_bla")),
        ticker=ticker,
        dfda=dfda,
        eight_k_counter=eight_k_counter,
        user_agent=user_agent,
    )

    rec.coverage = feats.get("_coverage")
    rec.required_feature_missing_count = feats.get("_required_feature_missing_count")
    rec.feature_provenance = feats.get("_provenance", {})
    rec.n_8ks_30_180_clean = feats.get("n_8ks_30_180_clean")
    rec.n_prior_filings = feats.get("n_prior_filings")
    rec.submission_class_code = feats.get("SubmissionClassCode")
    if "priority" in feats:
        rec.review_priority = "PRIORITY" if feats["priority"] else "STANDARD"
    # 'low' when coverage is below the floor OR the appno is a surrogate (a
    # surrogate EDGAR8K: row structurally lacks the Drugs@FDA substrate — the
    # strongest non-is_bla signals priority/class/n_prior — so its rank leans on
    # the intercept regardless of how designations/8-K nudge the coverage count;
    # Phase 1 §1.4/§8.8: surrogate rows are degraded + flagged).
    is_surrogate = appno.upper().startswith("EDGAR8K:")
    rec.feature_quality = "low" if (is_surrogate or (rec.coverage or 0.0) < COVERAGE_FLOOR) else "standard"

    out = score_nda(dict(feats))
    rec.p_crl = _f(out.get("p_crl"))
    rec.raw_p_uncalibrated = _f(out.get("raw_p_uncalibrated"))
    rec.ci_low = _f(out.get("ci_low"))
    rec.ci_high = _f(out.get("ci_high"))
    rec.risk_band = _verbatim(out.get("risk_band"))
    rec.confidence_flag = _verbatim(out.get("confidence_flag"))
    rec.refusal_reason = _verbatim(out.get("refusal_reason"))
    rec.scorer_version = out.get("model_version") or NDA_MODEL_VERSION
    if rec.p_crl is not None:
        rec.oof_percentile_rank = round(to_percentile(rec.p_crl, reference=nda_ref), 4) if nda_ref else None
    rec.scored = True
    return rec, feats


# --------------------------------------------------------------------------- #
# write bodies (pure; POSTed only under --apply) — Phase 1 §3.2 / §3.3
# --------------------------------------------------------------------------- #
def build_feature_upsert_body(rec: ScoredName, row: Dict[str, Any], *, snapshot_iso: str) -> Dict[str, Any]:
    """The bc_application_features upsert body — Phase 1's M14 columns merged onto
    Phase-0's snapshot row. Carries forward pdufa_date + designations (never
    blanks pdufa_date — the matview G3 window gate needs it). NULL-not-False for
    absent designations/warning; None -> column default for absent numerics."""
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        # identity — MUST match Phase 0's row so merge-duplicates UPDATES in place:
        "sponsor_cik": row.get("sponsor_cik"),
        "sponsor_name": row.get("sponsor_name"),           # raw (matches Phase-0 row)
        "application_number": rec.application_number,
        "appl_type": row.get("appl_type"),
        "snapshot_date": snapshot_iso,                     # the merge key
        "as_of_date": rec.ref_date,                        # point-in-time anchor
        "built_at": now_iso,                               # bump so latest_features picks this row
        "cycle_type": "first_cycle_orig",                  # v1 constant
        "is_biosimilar_bla": bool(row.get("is_biosimilar_bla")),
        "pdufa_date": row.get("pdufa_date"),               # DO NOT blank
        "submission_date": rec.ref_date if rec.ref_date_source == "drugsfda_orig_minus_clock" else row.get("submission_date"),
        # carry Phase-0 designations (NULL stays NULL, not False):
        "has_bt": row.get("has_bt"),
        "has_ft": row.get("has_ft"),
        "has_aa": row.get("has_aa"),
        # M14 feature columns Phase 1 fills (None -> column default; never fake):
        "review_priority": rec.review_priority,            # ∈ {PRIORITY,STANDARD} | None
        "submission_class_code": rec.submission_class_code,
        "n_prior_filings": rec.n_prior_filings,            # None -> default 0
        "n_8ks_30_180_clean": rec.n_8ks_30_180_clean,     # None -> default 0
        "n_drug_inspections_5y_fix": None,                 # dropped v1 -> NULL (default 0)
        "sponsor_has_warning": None,                       # empty source -> NULL not False
        "sponsor_has_orphan_history": None,
        "ctgov_failed_primary": None,
        "ctgov_any_randomized": None,
        "feature_quality": rec.feature_quality,            # 'standard' | 'low' (CHECK-safe)
        "required_feature_missing_count": rec.required_feature_missing_count or 0,
    }


def build_score_upsert_body(rec: ScoredName, *, scored_at_iso: str, features_id: Optional[str]) -> Dict[str, Any]:
    """The bc_rubric_scores upsert body. p_crl PERSISTED (matview gate) — never
    displayed. scorer_name pinned to the matview literal; model_version ->
    scorer_version. NULL-not-False for absent/refused payload."""
    return {
        "application_number": rec.application_number,
        "scored_at": scored_at_iso,                        # single per-run stamp -> idempotent within week
        "scorer_name": SCORER_NAME,                        # MUST match matview literal
        "scorer_version": rec.scorer_version,              # = NDA_MODEL_VERSION (renamed)
        "p_crl": rec.p_crl,                                # PERSISTED internal; never surfaced
        "raw_p_uncalibrated": rec.raw_p_uncalibrated,
        "ci_low": rec.ci_low,
        "ci_high": rec.ci_high,
        "risk_band": rec.risk_band,                        # the displayed rank tier
        "oof_percentile_rank": rec.oof_percentile_rank,    # locked-2025 reference percentile (0..100)
        "confidence_flag": rec.confidence_flag,            # verbatim scorer token
        "refusal_reason": rec.refusal_reason,
        "features_id": features_id,
    }


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def run_weekly(
    client=None,
    *,
    apply: bool = False,
    snapshot_date: Optional[str] = None,
    limit: Optional[int] = None,
    application_number: Optional[str] = None,
    user_agent: Optional[str] = None,
    openfda_sleep_s: float = 0.0,
    dfda: Optional[DrugsFDA] = None,
    eight_k_counter=None,
    _now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Score the Phase-0 universe → bc_rubric_scores (+ M14 feature columns on
    bc_application_features) → refresh bc_candidates. Fail-loud via bc_pipeline_runs.

    apply=False (default) is a DRY-RUN: builds + scores + computes the within-
    snapshot ordering, prints per-name band/coverage, writes NOTHING (no
    bc_pipeline_runs row either — like the Phase-0 dry-run). apply=True writes the
    feature columns + scores, refreshes the matview, and opens/closes a run row.

    ``client`` may be a fake (tests) or None (a real ``SupabaseClient`` is built).
    ``dfda`` / ``eight_k_counter`` are injectable so tests run with no network.
    """
    now = _now or datetime.now(timezone.utc)
    run_started_at = now.isoformat()
    snap_iso = snapshot_date or now.date().isoformat()
    user_agent = user_agent or os.environ.get("SEC_USER_AGENT") or "conan-bc-weekly-score contact@conan.local"

    def _scored_at_for(snapshot_iso: str) -> str:
        """The idempotency stamp for ``bc_rubric_scores.scored_at``: the scored
        snapshot date pinned to 00:00:00 UTC — NOT ``now()``. The live UNIQUE is
        ``(application_number, scored_at, scorer_name)``, so anchoring to the
        snapshot makes ANY re-run on the same snapshot a clean merge-in-place
        (idempotent), while a new weekly snapshot (a new ``snapshot_date``) adds a
        distinct historical row. Using ``now()`` would fork a new row on every
        manual re-run (verified: it did)."""
        try:
            d = datetime.strptime(str(snapshot_iso)[:10], "%Y-%m-%d").date()
        except ValueError:
            d = now.date()
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat()

    if client is None:
        from modal_workers.shared.supabase_client import SupabaseClient

        client = SupabaseClient()

    dfda = dfda or DrugsFDA(sleep_s=openfda_sleep_s)
    nda_ref = _load_nda_locked2025_reference()

    run_id = None
    if apply:
        run_id = _shared_open_run(client, pipeline_name=PIPELINE_NAME, snapshot_date=snap_iso)

    scored: List[ScoredName] = []
    n_failed = 0
    n_refused = 0
    matview_refreshed = False
    status = "succeeded"
    reason: Optional[str] = None
    scored_snapshot_date: Optional[str] = None

    try:
        universe = _read_universe(client)
        if application_number:
            universe = [r for r in universe if r.get("application_number") == application_number]
        if limit is not None:
            universe = universe[:limit]

        if not universe:
            log = {"n_in_universe": 0, "reason": "empty_universe", "builder_option": "A_point_in_time"}
            if apply:
                _shared_close_run(client, run_id, status="succeeded", n_processed=0,
                                  n_failed=0, cost_usd=0, log=log, reason="empty_universe")
            return {"scored": [], "stats": log, "status": "succeeded"}

        # All Phase-0 rows share today's snapshot; record the one we scored on so
        # the feature upsert lands on the SAME snapshot (matview DISTINCT-ON coherent).
        scored_snapshot_date = universe[0].get("snapshot_date") or snap_iso
        feature_snapshot_iso = str(scored_snapshot_date)
        # Idempotency stamp anchored to the scored snapshot (NOT now()) so same-
        # snapshot re-runs merge in place on the bc_rubric_scores UNIQUE.
        scored_at_stamp = _scored_at_for(feature_snapshot_iso)

        for row in universe:
            appno = str(row.get("application_number") or "")
            try:
                rec, _feats = _score_one(
                    client, row, dfda=dfda, nda_ref=nda_ref,
                    user_agent=user_agent, eight_k_counter=eight_k_counter,
                )
            except Exception as exc:  # noqa: BLE001 — per-name isolation
                logger.warning("score failed for %s: %s", appno, exc)
                rec = ScoredName(
                    application_number=appno, sponsor_cik=row.get("sponsor_cik"),
                    sponsor_name=_clean_sponsor_name(row.get("sponsor_name")),
                    ticker=_ticker_from_sponsor(row.get("sponsor_name")),
                    appl_type=row.get("appl_type"), pdufa_date=row.get("pdufa_date"),
                    error=f"{type(exc).__name__}: {str(exc)[:200]}",
                )
                scored.append(rec)
                n_failed += 1
                continue

            if rec.refusal_reason:
                n_refused += 1

            if apply:
                feat_body = build_feature_upsert_body(rec, row, snapshot_iso=feature_snapshot_iso)
                returned = client._rest_with_retry(
                    "POST",
                    f"{_FEATURES_TABLE}?on_conflict={_FEATURES_ON_CONFLICT}",
                    json_body=[feat_body],
                    prefer="resolution=merge-duplicates,return=representation",
                )
                features_id = None
                if isinstance(returned, list) and returned:
                    features_id = returned[0].get("id")
                score_body = build_score_upsert_body(rec, scored_at_iso=scored_at_stamp, features_id=features_id)
                client._rest_with_retry(
                    "POST",
                    f"{_SCORES_TABLE}?on_conflict={_SCORES_ON_CONFLICT}",
                    json_body=[score_body],
                    prefer="resolution=merge-duplicates,return=minimal",
                )
            scored.append(rec)

        # within-snapshot ordering (digest sort key; lives in the run log, NOT the
        # stored oof_percentile_rank column — §0.6 reconciliation). Riskier first.
        ranked = sorted(
            [r for r in scored if r.p_crl is not None],
            key=lambda r: r.p_crl, reverse=True,
        )
        within_snapshot_rank = {r.application_number: i + 1 for i, r in enumerate(ranked)}

        if apply:
            try:
                client._rest("POST", "rpc/bc_refresh_candidates", json_body={})
                matview_refreshed = True
            except Exception as exc:  # noqa: BLE001 — scores written; only the view lagged
                logger.warning("bc_refresh_candidates failed: %s", exc)
                status = "partial"
                reason = f"matview_refresh_failed: {type(exc).__name__}: {str(exc)[:160]}"

        n_scored = sum(1 for r in scored if r.scored)
        band_dist = _band_distribution(scored)
        coverage_hist = _coverage_hist(scored)
        if n_failed > 0 and status != "partial":
            status = "partial"
            reason = reason or f"{n_failed} name(s) failed mid-build"

        stats = {
            "n_in_universe": len(universe),
            "n_scored": n_scored,
            "n_failed": n_failed,
            "n_refused": n_refused,
            "n_low_coverage": sum(1 for r in scored if r.feature_quality == "low"),
            "band_distribution": band_dist,
            "coverage_hist": coverage_hist,
            "within_snapshot_rank": within_snapshot_rank,
            "rank_method": "p_crl_desc_within_snapshot",
            "oof_percentile_reference": "locked_2025_p_m14_cal",
            "scored_snapshot_date": str(scored_snapshot_date),
            "matview_refreshed": matview_refreshed,
            "builder_option": "A_point_in_time",
            "scorer_version": NDA_MODEL_VERSION,
            "per_name": {r.application_number: _name_log(r) for r in scored},
        }

        if apply:
            _shared_close_run(client, run_id, status=status, n_processed=n_scored,
                              n_failed=n_failed, cost_usd=0, log=stats, reason=reason)
        return {"scored": scored, "stats": stats, "status": status}

    except Exception as exc:  # noqa: BLE001 — fail-loud: close the run even on crash
        logger.exception("run_weekly crashed")
        if apply:
            _shared_close_run(
                client, run_id, status="failed", n_processed=len([r for r in scored if r.scored]),
                n_failed=n_failed, cost_usd=0,
                log={"error": str(exc)[:500], "n_in_universe_partial": len(scored)},
                reason=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
        raise


def _name_log(r: ScoredName) -> Dict[str, Any]:
    return {
        "scored": r.scored,
        "ticker": r.ticker,
        "risk_band": r.risk_band,
        "p_crl_internal": r.p_crl,
        "oof_percentile_rank": r.oof_percentile_rank,
        "coverage": r.coverage,
        "feature_quality": r.feature_quality,
        "ref_date_source": r.ref_date_source,
        "n_8ks_30_180_clean": r.n_8ks_30_180_clean,
        "confidence_flag": r.confidence_flag,
        "refusal_reason": r.refusal_reason,
        "error": r.error,
    }


def _band_distribution(scored: List[ScoredName]) -> Dict[str, int]:
    dist = {"low": 0, "moderate": 0, "elevated": 0, "high": 0, "refused": 0, "error": 0}
    for r in scored:
        if r.error:
            dist["error"] += 1
        elif r.refusal_reason:
            dist["refused"] += 1
        elif r.risk_band in dist:
            dist[r.risk_band] += 1
    return dist


def _coverage_hist(scored: List[ScoredName]) -> Dict[str, int]:
    buckets = {"0.0-0.25": 0, "0.25-0.5": 0, "0.5-0.75": 0, "0.75-1.0": 0, "none": 0}
    for r in scored:
        c = r.coverage
        if c is None:
            buckets["none"] += 1
        elif c < 0.25:
            buckets["0.0-0.25"] += 1
        elif c < 0.5:
            buckets["0.25-0.5"] += 1
        elif c < 0.75:
            buckets["0.5-0.75"] += 1
        else:
            buckets["0.75-1.0"] += 1
    return buckets


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="BC weekly score-as-rank worker (Phase 1).")
    parser.add_argument("--apply", action="store_true",
                        help="WRITE bc_rubric_scores + bc_application_features cols + refresh matview "
                             "+ bc_pipeline_runs. Default = DRY-RUN (no writes).")
    parser.add_argument("--limit", type=int, default=None, help="Score only the first N universe names.")
    parser.add_argument("--application-number", default=None, help="Score only this appno.")
    parser.add_argument("--snapshot-date", default=None, help="Override snapshot_date (ISO). Default = today.")
    parser.add_argument("--openfda-sleep-s", type=float, default=0.0,
                        help="Seconds between Drugs@FDA calls (shared-IP-cap pacing).")
    parser.add_argument("--json-out", default=None, help="Write the full result JSON to this path.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")

    if args.apply:
        logger.warning("--apply set: will WRITE bc_rubric_scores + bc_application_features + refresh matview.")
    else:
        logger.info("DRY-RUN: no DB writes. Reading live Drugs@FDA + EFTS only.")

    result = run_weekly(
        apply=args.apply,
        snapshot_date=args.snapshot_date,
        limit=args.limit,
        application_number=args.application_number,
        openfda_sleep_s=args.openfda_sleep_s,
    )

    stats = result["stats"]
    scored: List[ScoredName] = result["scored"]
    print("\n===== bc_weekly_score " + ("--apply" if args.apply else "DRY-RUN") + " =====")
    print(f"  status: {result['status']}")
    for k in ("n_in_universe", "n_scored", "n_failed", "n_refused", "n_low_coverage",
              "band_distribution", "coverage_hist", "matview_refreshed", "scored_snapshot_date",
              "scorer_version"):
        if k in stats:
            print(f"  {k}: {stats[k]}")

    print("\n--- per-name (sorted by p_crl desc; p_crl is INTERNAL, shown for operator review only) ---")
    for r in sorted(scored, key=lambda r: (r.p_crl if r.p_crl is not None else -1.0), reverse=True):
        pc = f"{r.p_crl:.4f}" if r.p_crl is not None else "  -   "
        pct = f"{r.oof_percentile_rank:5.1f}" if r.oof_percentile_rank is not None else "  -  "
        cov = f"{r.coverage:.2f}" if r.coverage is not None else " -  "
        print(
            f"  {r.ticker or '?':6s} {r.appl_type or '?':3s} pdufa={r.pdufa_date} "
            f"band={r.risk_band or '-':8s} p_crl={pc} pct={pct} cov={cov} "
            f"fq={r.feature_quality or '-':8s} 8k={r.n_8ks_30_180_clean} ref={r.ref_date_source} "
            f"flag={r.confidence_flag or '-'}{' REFUSED:'+r.refusal_reason if r.refusal_reason else ''}"
            f"{' ERR:'+r.error if r.error else ''} appno={r.application_number}"
        )

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"status": result["status"], "stats": stats, "scored": [asdict(r) for r in scored]},
            indent=2, default=str,
        ))
        print(f"\nwrote {args.json_out}")

    bd = stats.get("band_distribution", {})
    print(f"\nBANDS: low={bd.get('low',0)} moderate={bd.get('moderate',0)} "
          f"elevated={bd.get('elevated',0)} high={bd.get('high',0)} "
          f"refused={bd.get('refused',0)} error={bd.get('error',0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
