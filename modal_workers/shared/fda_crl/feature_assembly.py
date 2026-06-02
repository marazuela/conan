"""Assemble NDA / sNDA CRL-scorer feature dicts for a catalyst, from Supabase.

The CRL scorers (``nda_scorer.score_nda`` / ``snda_scorer.score_snda``) consume
flat feature dicts. This module turns the per-sponsor / per-application data the
engine already ingests into those dicts, on demand at scoring time. It is the
"Phase 2" layer referenced by ``score.py`` and ``openfda_ingest.py``.

Design choices
--------------
* **On-demand reads, no precomputed aggregate tables.** Per-catalyst scoring is
  low-QPS, so we count submissions / inspections / warning-letters / 8-Ks
  directly via PostgREST (``client._rest``). Keeps the data model small.
* **Graceful degradation — never fake a feature.** Any input that cannot be
  sourced is left *absent*. The NDA scorer defaults absent numerics to 0; we
  additionally return a ``coverage`` fraction over the high-signal features so
  the caller (``score.py`` / the Seam-1 gate) can reflect real input quality.
* **Honest gaps.** Three NDA model features have no production source yet —
  ``ctgov_failed_primary``, ``ctgov_any_randomized`` and
  ``sponsor_has_orphan_history`` (together ~4.7% of model |coef|). They are
  intentionally left absent; the in-engine backtest is the gate on whether they
  are ever worth building.

Public entrypoints
------------------
    build_catalyst(asset, event, submission=None) -> dict
    assemble_nda_features(client, asset, event, *, ref_date=None, evidence_rows=None) -> dict
    assemble_snda_features(client, asset, event, *, ref_date=None) -> dict
    score_catalyst_crl(client, asset, event, *, ref_date=None, evidence_rows=None) -> dict
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from modal_workers.shared.fda_crl import score as _score

SUBMISSIONS = "fda_application_submissions"
INSPECTIONS = "fda_drug_inspections"
WARNING_LETTERS = "fda_warning_letters"
DOCUMENTS = "documents"

_INSPECTION_WINDOW_DAYS = 5 * 365
_EDGAR_8K_LO_DAYS = 180  # window start  (ref - 180)
_EDGAR_8K_HI_DAYS = 30   # window end    (ref - 30)

# High-signal NDA features used for the coverage fraction (the three no-source
# features are deliberately excluded so coverage reflects *buildable* inputs).
_NDA_COVERAGE_KEYS = (
    "is_bla",
    "priority",
    "SubmissionClassCode",  # drives type5_or_3 in the scorer
    "n_prior_filings",
    "n_drug_inspections_5y_fix",
    "sponsor_has_warning",
    "has_bt",
    "has_ft",
    "has_aa",
    "n_8ks_30_180_clean",
)

_DESIGNATION_MAP = (
    ("has_bt", ("breakthrough", "breakthrough_therapy", "has_bt", "bt")),
    ("has_ft", ("fast_track", "fasttrack", "has_ft", "ft")),
    ("has_aa", ("accelerated_approval", "accelerated", "has_aa", "aa")),
)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _norm(value: object) -> str:
    return str(value or "").strip().upper()


def _firm_norm(name: object) -> str:
    """Mirror the inspection/warning fetchers' firm_name_norm: lower, collapse ws."""
    return re.sub(r"\s+", " ", str(name or "").strip().lower())


def appl_is_bla(application_number: object) -> Optional[int]:
    """1 if BLA, 0 if NDA, None if the prefix is absent/unknown.

    openFDA drugsfda application numbers retain their ``NDA``/``BLA`` prefix
    (e.g. ``BLA125514``, ``NDA021436``), so application_type is derivable
    without a dedicated column.
    """
    s = _norm(application_number)
    if s.startswith("BLA"):
        return 1
    if s.startswith("NDA"):
        return 0
    return None


def _event_date(event: Mapping[str, Any]) -> Optional[date]:
    return _to_date(event.get("event_date") or event.get("catalyst_date"))


def _to_date(value: object) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _shift_days(d: date, days: int) -> date:
    from datetime import timedelta

    return d - timedelta(days=days)


def _rows(client: Any, table: str, params: Mapping[str, str]) -> list:
    """Defensive PostgREST GET — returns [] on any error so one missing table
    or column degrades a single feature instead of failing the whole score."""
    try:
        return client._rest("GET", table, params=dict(params)) or []
    except Exception:  # noqa: BLE001 — degrade, don't crash the scorer
        return []


def _designations(asset: Mapping[str, Any], evidence_rows: Optional[Sequence[Mapping[str, Any]]]) -> dict:
    """Designation flags from asset.extensions, falling back to active evidence
    rows of evidence_type='designations'. Same layout the pdufa pipeline writes."""
    extensions = asset.get("extensions") or {}
    flags = dict(extensions.get("designations") or {})
    for row in evidence_rows or []:
        if (row.get("evidence_type") or "") != "designations":
            continue
        if (row.get("evidence_status") or "active") != "active":
            continue
        for k, v in (row.get("payload") or {}).items():
            flags.setdefault(k, v)
    return flags


def _flag_present(flags: Mapping[str, Any], aliases: Iterable[str]) -> Optional[int]:
    for key in aliases:
        if key in flags and flags[key] not in (None, ""):
            val = flags[key]
            if isinstance(val, str):
                t = _norm(val)
                if t in ("1", "TRUE", "T", "YES", "Y"):
                    return 1
                if t in ("0", "FALSE", "F", "NO", "N"):
                    return 0
            try:
                return int(bool(float(val)))
            except (TypeError, ValueError):
                return int(bool(val))
    return None


# --------------------------------------------------------------------------- #
# routing metadata
# --------------------------------------------------------------------------- #
def build_catalyst(
    asset: Mapping[str, Any],
    event: Mapping[str, Any],
    submission: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Routing metadata for ``router.classify_scope`` — best-effort from asset,
    event.extensions and (optionally) the resolved submission row."""
    ext = {**(asset.get("extensions") or {}), **(event.get("extensions") or {})}
    is_bla = appl_is_bla(asset.get("application_number"))
    appl_type = ext.get("application_type")
    if not appl_type and is_bla is not None:
        appl_type = "BLA" if is_bla else "NDA"
    # The event's own extensions describe THIS catalyst (e.g. a supplement
    # PDUFA), so they win over the looked-up original submission — otherwise
    # every event would route as 'original'. The ORIG submission is a fallback
    # that supplies type/class when the event metadata is sparse (common for
    # first-cycle originals).
    return {
        "application_type": appl_type,
        "submission_type": ext.get("submission_type") or (submission or {}).get("submission_type"),
        "submission_class_code": ext.get("submission_class_code")
        or (submission or {}).get("submission_class_code"),
        "is_biosimilar": ext.get("is_biosimilar"),
        "is_resubmission": ext.get("is_resubmission"),
        "cycle_type": ext.get("cycle_type"),
    }


# --------------------------------------------------------------------------- #
# per-feature sourcing
# --------------------------------------------------------------------------- #
def _orig_submission(client: Any, application_number: str) -> Optional[dict]:
    rows = _rows(
        client,
        SUBMISSIONS,
        {
            "application_number": f"eq.{application_number}",
            "select": "submission_type,submission_class_code,review_priority,submission_status_date,submission_number",
            "order": "submission_status_date.asc.nullslast",
        },
    )
    origs = [r for r in rows if _norm(r.get("submission_type")).startswith("ORIG")]
    return (origs or rows or [None])[0]


def _n_prior_filings(
    client: Any, ticker: Optional[str], sponsor_name: Optional[str], application_number: str, ref: Optional[date]
) -> Optional[int]:
    """Distinct prior ORIG application_numbers for this sponsor before ``ref``."""
    if not ticker and not sponsor_name:
        return None
    params = {
        "submission_type": "like.ORIG*",
        "select": "application_number,submission_status_date,ticker,sponsor_name",
    }
    params["ticker" if ticker else "sponsor_name"] = f"eq.{ticker or sponsor_name}"
    if ref:
        params["submission_status_date"] = f"lt.{ref.isoformat()}"
    rows = _rows(client, SUBMISSIONS, params)
    appnos = {
        _norm(r.get("application_number"))
        for r in rows
        if _norm(r.get("application_number")) and _norm(r.get("application_number")) != _norm(application_number)
    }
    return len(appnos)


def _n_inspections_5y(
    client: Any, ticker: Optional[str], sponsor_name: Optional[str], ref: Optional[date]
) -> Optional[int]:
    if not ref:
        return None
    lo = _shift_days(ref, _INSPECTION_WINDOW_DAYS).isoformat()
    hi = ref.isoformat()
    base = {
        "select": "inspection_id,inspection_end_date",
        "and": f"(inspection_end_date.gte.{lo},inspection_end_date.lte.{hi})",
    }
    if ticker:
        rows = _rows(client, INSPECTIONS, {**base, "sponsor_ticker": f"eq.{ticker}"})
        if rows:
            return len(rows)
    if sponsor_name:
        rows = _rows(client, INSPECTIONS, {**base, "firm_name_norm": f"eq.{_firm_norm(sponsor_name)}"})
        return len(rows)
    return None


def _sponsor_has_warning(
    client: Any, ticker: Optional[str], sponsor_name: Optional[str], ref: Optional[date]
) -> Optional[int]:
    params = {"select": "letter_id,issue_date"}
    if ref:
        params["issue_date"] = f"lte.{ref.isoformat()}"
    if ticker:
        rows = _rows(client, WARNING_LETTERS, {**params, "sponsor_ticker": f"eq.{ticker}"})
        if rows:
            return 1
    if sponsor_name:
        rows = _rows(client, WARNING_LETTERS, {**params, "firm_name_norm": f"eq.{_firm_norm(sponsor_name)}"})
        return int(bool(rows))
    if ticker:
        return 0
    return None


def _n_8ks_30_180(client: Any, asset: Mapping[str, Any], ref: Optional[date]) -> Optional[int]:
    """8-K count in [ref-180, ref-30]. Best-effort: requires an entity link on
    the documents feed. Returns None when the link/feed is unavailable so the
    scorer flags 'no_edgar_signal' rather than silently assuming zero."""
    entity_id = asset.get("entity_id")
    if not entity_id or not ref:
        return None
    lo = _shift_days(ref, _EDGAR_8K_LO_DAYS).isoformat()
    hi = _shift_days(ref, _EDGAR_8K_HI_DAYS).isoformat()
    rows = _rows(
        client,
        DOCUMENTS,
        {
            "select": "id,published_at",
            "source": "eq.edgar",
            "doc_type": "eq.8-K",
            "entity_id": f"eq.{entity_id}",
            "and": f"(published_at.gte.{lo},published_at.lte.{hi})",
        },
    )
    # None signal when nothing came back AND we cannot prove the feed exists for
    # this entity — but here an explicit empty result with a valid entity is a
    # real zero, so return the count.
    return len(rows)


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #
def assemble_nda_features(
    client: Any,
    asset: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    ref_date: Optional[date] = None,
    evidence_rows: Optional[Sequence[Mapping[str, Any]]] = None,
) -> dict:
    """Assemble the NDA M14 feature dict. Absent inputs are omitted (the scorer
    defaults them); a ``_coverage`` float over the high-signal features is
    attached for the caller's confidence accounting."""
    appno = str(asset.get("application_number") or "").strip()
    ticker = asset.get("ticker")
    sponsor_name = asset.get("sponsor_name")
    ref = ref_date or _event_date(event) or datetime.now(timezone.utc).date()

    feats: dict[str, Any] = {"cycle_type": "first_cycle_orig"}

    is_bla = appl_is_bla(appno)
    if is_bla is not None:
        feats["is_bla"] = is_bla
        feats["ApplType"] = "BLA" if is_bla else "NDA"

    sub = _orig_submission(client, appno) if appno else None
    if sub:
        rp = _norm(sub.get("review_priority"))
        if rp in ("PRIORITY", "STANDARD"):
            feats["priority"] = 1 if rp == "PRIORITY" else 0
        scc = sub.get("submission_class_code")
        if scc:
            feats["SubmissionClassCode"] = scc

    npf = _n_prior_filings(client, ticker, sponsor_name, appno, ref)
    if npf is not None:
        feats["n_prior_filings"] = npf

    ninsp = _n_inspections_5y(client, ticker, sponsor_name, ref)
    if ninsp is not None:
        feats["n_drug_inspections_5y_fix"] = ninsp

    hw = _sponsor_has_warning(client, ticker, sponsor_name, ref)
    if hw is not None:
        feats["sponsor_has_warning"] = hw

    flags = _designations(asset, evidence_rows)
    for dst, aliases in _DESIGNATION_MAP:
        present = _flag_present(flags, aliases)
        if present is not None:
            feats[dst] = present

    n8k = _n_8ks_30_180(client, asset, ref)
    if n8k is not None:
        feats["n_8ks_30_180_clean"] = n8k

    feats["_coverage"] = round(
        sum(1 for k in _NDA_COVERAGE_KEYS if k in feats) / len(_NDA_COVERAGE_KEYS), 4
    )
    return feats


# sNDA efficacy supplement class-code → act_* flags (rank-only model).
_ACT_TOKENS = (
    ("act_new_indication", ("NEW INDICATION", "TYPE 6")),
    ("act_new_dosing", ("NEW DOSING", "NEW DOSE", "TYPE 4")),
    ("act_new_patient_population", ("NEW PATIENT POPULATION", "PEDIATRIC")),
    ("act_accel_app", ("ACCELERATED",)),
    ("act_confirmatory", ("CONFIRMATORY",)),
)


def assemble_snda_features(
    client: Any,
    asset: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    ref_date: Optional[date] = None,
) -> dict:
    """Best-effort sNDA feature dict. The sNDA model is RANK-ONLY and treats
    absent features as the training mean, so partial coverage is acceptable —
    we populate the class-code-derived act_* flags + priority/is_bla and leave
    the sponsor-history features absent (sourcing them is a later enhancement)."""
    appno = str(asset.get("application_number") or "").strip()
    feats: dict[str, Any] = {}

    is_bla = appl_is_bla(appno)
    if is_bla is not None:
        feats["is_bla"] = is_bla

    sub = _orig_submission(client, appno) if appno else None
    scc = _norm((sub or {}).get("submission_class_code")) or _norm(
        (event.get("extensions") or {}).get("submission_class_code")
    )
    if (sub or {}).get("review_priority"):
        feats["priority"] = 1 if _norm(sub.get("review_priority")) == "PRIORITY" else 0
    if scc:
        for dst, tokens in _ACT_TOKENS:
            feats[dst] = int(any(tok in scc for tok in tokens))
    return feats


def score_catalyst_crl(
    client: Any,
    asset: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    ref_date: Optional[date] = None,
    evidence_rows: Optional[Sequence[Mapping[str, Any]]] = None,
) -> dict:
    """One-call entrypoint for the bridge: build routing metadata + features and
    return the normalized ``score_crl`` decision. Used for both the Seam-1
    pre-gate and the Seam-2 fair_probability override (scored once)."""
    nda_features = assemble_nda_features(
        client, asset, event, ref_date=ref_date, evidence_rows=evidence_rows
    )
    snda_features = assemble_snda_features(client, asset, event, ref_date=ref_date)
    sub = _orig_submission(client, str(asset.get("application_number") or "").strip()) if asset.get(
        "application_number"
    ) else None
    catalyst = build_catalyst(asset, event, submission=sub)
    decision = _score.score_crl(catalyst, nda_features=nda_features, snda_features=snda_features)
    # Surface assembly coverage so the Seam-1 gate can blend it with the
    # scorer's own flag-derived confidence.
    decision["crl_feature_coverage"] = nda_features.get("_coverage")
    return decision
