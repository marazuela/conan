"""Shared point-in-time (PIT) M14 feature builder — ONE code path, two callers.

This is the single feature substrate for both the offline A0 cohort study
(``analysis/bc_a0/``) and the live weekly score worker
(``modal_workers/bc_score/run_weekly.py``). It mirrors the definitions in
``modal_workers.bc_score._m14.feature_assembly`` BYTE-FOR-BYTE (same windows,
same coverage-key set, same priority/class mapping, same "absent ⇒ omit, never
fake a 0" discipline) so the A0 out-of-sample AUC/CI metrics transfer to the
live weekly scorer (Phase 1 §1.2, §7.2; build-handoff landmine §3).

Why a dedicated builder rather than ``assemble_nda_features`` directly
--------------------------------------------------------------------
``assemble_nda_features`` reads four Supabase tables. Live (verified 2026-06-05):

  * ``fda_application_submissions`` — ABSENT (``to_regclass`` is NULL); its
    migration is intentionally **not applied** on this build. So the
    submission-derived features (``priority`` / ``SubmissionClassCode`` /
    ``n_prior_filings``) are sourced **directly from the Drugs@FDA API**
    (the :class:`DrugsFDA` cached client), keyed on the *real* NDA/BLA appno —
    NOT from the absent table. Surrogate ``EDGAR8K:<cik>:<slug>`` appnos have
    no Drugs@FDA record → those features fall absent → graceful degradation.
  * ``documents`` 8-K path — present but has NO ``entity_id`` column and only a
    few hundred rows. So ``n_8ks_30_180_clean`` is counted via **EFTS by CIK**
    (``shared.edgar_efts``), the same EDGAR path Phase 0 uses, NOT the
    ``documents`` corpus.
  * ``fda_drug_inspections`` — ABSENT (and the fetcher's HTTP path raises
    ``NotImplementedError``). Dropped for v1 → scorer defaults ``log1p(0)=0``.
  * ``fda_warning_letters`` — present but EMPTY. The builder still *queries* it
    (read-only) so it lights up for free once a populator lands; empty → 0.

NO-LOOK-AHEAD is enforced (Phase 1 §3.1): every dated source row used for a
feature is at or before ``ref_date`` (submission history ``< ref``; 8-K window
``[ref-180, ref-30]``); the builder raises ``AssertionError`` on any violation.
The builder is handed ONLY ``(appno, sponsor_name, cik, ref_date, designations)``
— never a future/outcome field (``pdufa_date``, CRL label) — so look-ahead is
structurally impossible.

Absent inputs are LEFT ABSENT (the scorer defaults them) and counted toward a
``_coverage`` fraction over the same high-signal key set ``feature_assembly``
uses, so the rank's confidence is honest and visible.

Public surface
--------------
    build_features_pit(client, *, application_number, sponsor_cik, sponsor_name,
                       appl_type, ref_date, designations, is_biosimilar_bla=False,
                       cik=None, ticker=None, dfda=None, eight_k_counter=None,
                       enable_warning_letters=True) -> dict   # the LIVE weekly caller
    build_features(*, appno, sponsor_name, cik, ref_date, ...) -> dict  # the A0 offline caller
    DrugsFDA, orig_submission, parse_compact_date, estimate_ref_date,
    appl_is_bla, _shift, _REVIEW_CLOCK_DAYS                              # A0 re-exports

Import-safe: no network at import time; network only inside the builder fns,
each guarded so one failing source degrades a single feature, never the run.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

# Import the windows + coverage keys from feature_assembly rather than
# re-declaring them, so a drift in feature_assembly is impossible to miss
# (Phase 1 §1.2 "import the constants").
from modal_workers.bc_score._m14.feature_assembly import (
    _EDGAR_8K_HI_DAYS,
    _EDGAR_8K_LO_DAYS,
    _INSPECTION_WINDOW_DAYS,
    _NDA_COVERAGE_KEYS,
)

logger = logging.getLogger(__name__)

# Standard NDA/BLA review clock (PDUFA ~10-month) — the last-resort ref_date
# fallback when the ORIG filing date is unavailable (§3.1).
_REVIEW_CLOCK_DAYS = 304

# v1 coverage denominator (Phase 1 §1.4): the high-signal keys we actually KEEP
# in v1 — ``feature_assembly._NDA_COVERAGE_KEYS`` minus the two integrations
# dropped for v1 (``n_drug_inspections_5y_fix`` — no live source; and the
# warning-letter signal — table empty). So ``_coverage`` reflects *buildable*
# v1 inputs and 1.0 is reachable, instead of being capped at 0.8 forever.
_V1_KEPT_COVERAGE_KEYS = tuple(
    k for k in _NDA_COVERAGE_KEYS if k not in ("n_drug_inspections_5y_fix", "sponsor_has_warning")
)


# --------------------------------------------------------------------------- #
# small helpers (mirror feature_assembly)
# --------------------------------------------------------------------------- #
def _norm(value: object) -> str:
    return str(value or "").strip().upper()


def _firm_norm(name: object) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip().lower())


def appl_is_bla(application_number: object) -> Optional[int]:
    """1 if BLA, 0 if NDA, None if the prefix is absent/unknown.

    Mirrors ``feature_assembly.appl_is_bla``. Surrogate ``EDGAR8K:`` appnos have
    no NDA/BLA prefix → None (the caller supplies is_bla from appl_type instead).
    """
    s = _norm(application_number)
    if s.startswith("BLA"):
        return 1
    if s.startswith("NDA"):
        return 0
    return None


def parse_compact_date(value: object) -> Optional[date]:
    """openFDA dates are compact ``YYYYMMDD`` strings (also accepts ISO)."""
    s = str(value or "").strip()
    if not s:
        return None
    if len(s) >= 10 and s[4] == "-":
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    try:
        return datetime.strptime(s[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _parse_iso(value: object) -> Optional[date]:
    s = str(value or "")[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _shift(d: date, days: int) -> date:
    return d - timedelta(days=days)


# --------------------------------------------------------------------------- #
# Drugs@FDA submission cache (shared with build_cohort / the live worker)
# --------------------------------------------------------------------------- #
class DrugsFDA:
    """Thin cached client over openFDA ``drug/drugsfda.json``.

    One network call per application_number / per sponsor_name, memoized for the
    run. Returns ``None`` on 404 (openFDA's "no results") so callers fall back to
    absent features. Honors ``OPENFDA_API_KEY`` via the shared openfda_client.
    """

    def __init__(self, *, sleep_s: float = 0.0) -> None:
        from modal_workers.shared.openfda_client import openfda_get  # lazy

        self._get = openfda_get
        self._app_cache: Dict[str, Optional[dict]] = {}
        self._sponsor_cache: Dict[str, List[dict]] = {}
        self._sleep_s = sleep_s

    def application(self, appno: str) -> Optional[dict]:
        appno = _norm(appno)
        if appno in self._app_cache:
            return self._app_cache[appno]
        try:
            body = self._get(
                "drug/drugsfda.json", {"search": f'application_number:"{appno}"', "limit": 3}
            )
        except Exception as exc:  # noqa: BLE001 — degrade, don't crash
            logger.warning("drugsfda application(%s) error: %s", appno, exc)
            body = None
        if self._sleep_s:
            import time

            time.sleep(self._sleep_s)
        res = (body or {}).get("results") or []
        rec = res[0] if res else None
        self._app_cache[appno] = rec
        return rec

    def sponsor_applications(self, sponsor_name: str) -> List[dict]:
        """All drugsfda applications for a sponsor (paged). Memoized."""
        key = _firm_norm(sponsor_name)
        if key in self._sponsor_cache:
            return self._sponsor_cache[key]
        out: List[dict] = []
        skip = 0
        page = 100
        for _ in range(20):  # hard cap 2000 records / sponsor
            try:
                body = self._get(
                    "drug/drugsfda.json",
                    {"search": f'sponsor_name:"{sponsor_name}"', "limit": page, "skip": skip},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("drugsfda sponsor(%s) error: %s", sponsor_name, exc)
                break
            if self._sleep_s:
                import time

                time.sleep(self._sleep_s)
            res = (body or {}).get("results") or []
            out.extend(res)
            if len(res) < page:
                break
            skip += page
        self._sponsor_cache[key] = out
        return out


def orig_submission(app: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """The ORIG submission for an application record (earliest by status_date)."""
    if not app:
        return None
    subs = app.get("submissions") or []
    origs = [s for s in subs if _norm(s.get("submission_type")).startswith("ORIG")]
    if not origs:
        return None
    origs.sort(key=lambda s: str(s.get("submission_status_date") or "99999999"))
    return origs[0]


# --------------------------------------------------------------------------- #
# point-in-time feature sourcing (Drugs@FDA + EDGAR; mirror feature_assembly)
# --------------------------------------------------------------------------- #
def _n_prior_filings(
    dfda: DrugsFDA, sponsor_name: Optional[str], appno: str, ref: date
) -> Optional[Tuple[int, date]]:
    """Distinct prior ORIG appnos for this sponsor with ORIG date ``< ref``.

    Mirrors ``feature_assembly._n_prior_filings`` (sponsor-name path). Returns
    ``(count, max_date_used)`` so the no-look-ahead assertion can verify the
    upper bound. Returns ``None`` when sponsor is unknown."""
    if not sponsor_name:
        return None
    apps = dfda.sponsor_applications(sponsor_name)
    if not apps:
        return None
    seen: Dict[str, date] = {}
    max_used = date.min
    this_norm = _norm(appno)
    for app in apps:
        an = _norm(app.get("application_number"))
        if not an or an == this_norm:
            continue
        og = orig_submission(app)
        if not og:
            continue
        d = parse_compact_date(og.get("submission_status_date"))
        if d is None or d >= ref:  # strict < ref (no look-ahead)
            continue
        seen[an] = d
        if d > max_used:
            max_used = d
    if not seen:
        # sponsor known but no prior originals before ref -> a real zero
        return (0, ref)
    return (len(seen), max_used)


def _sponsor_has_warning_live(
    client: Any, sponsor_name: Optional[str], ticker: Optional[str], ref: date
) -> Optional[Tuple[int, Optional[date]]]:
    """``sponsor_has_warning`` from the LIVE ``fda_warning_letters`` table (the
    one feature_assembly source that EXISTS live; currently EMPTY), read-only,
    ``issue_date <= ref``. Mirrors ``feature_assembly._sponsor_has_warning``.

    Returns ``(flag, max_date_used)`` or ``None`` when unsourceable (no client /
    no sponsor). An empty table simply yields ``(0, None)``."""
    if client is None:
        return None
    params = {"select": "letter_id,issue_date"}
    if ref:
        params["issue_date"] = f"lte.{ref.isoformat()}"
    rows: List[dict] = []
    if ticker:
        try:
            rows = client._rest("GET", "fda_warning_letters", params={**params, "sponsor_ticker": f"eq.{ticker}"}) or []
        except Exception as exc:  # noqa: BLE001
            logger.info("warning-letter query failed (ticker=%s): %s", ticker, exc)
            rows = []
        if rows:
            return (1, _max_date([r.get("issue_date") for r in rows]))
    if sponsor_name:
        try:
            rows = client._rest("GET", "fda_warning_letters", params={**params, "firm_name_norm": f"eq.{_firm_norm(sponsor_name)}"}) or []
        except Exception as exc:  # noqa: BLE001
            logger.info("warning-letter query failed (sponsor=%s): %s", sponsor_name, exc)
            return None
        if not rows:
            return (0, None)
        return (1, _max_date([r.get("issue_date") for r in rows]))
    if ticker:
        return (0, None)
    return None


def _max_date(values: List[object]) -> Optional[date]:
    best = date.min
    for v in values:
        d = _parse_iso(v)
        if d and d > best:
            best = d
    return best if best != date.min else None


def count_8ks_by_cik_efts(cik: Optional[str], ref: date, *, user_agent: str) -> Optional[int]:
    """Count form 8-K filings for a filer ``cik`` in ``[ref-180, ref-30]`` via EFTS.

    Live EDGAR path (the same ``shared.edgar_efts`` Phase 0 uses), CIK-scoped the
    way ``edgar_filing_monitor`` scopes by filer (``q="", forms="8-K",
    ciks=<zero-padded>``). Keeps the 8-K definition identical to
    ``feature_assembly._n_8ks_30_180`` (window [ref-180, ref-30], form 8-K).

    Returns the count, or ``None`` when no CIK resolves (so the scorer flags
    ``moderate_confidence_no_edgar_signal`` rather than assuming a real 0)."""
    if not cik:
        return None
    cik_digits = re.sub(r"\D", "", str(cik))
    if not cik_digits or int(cik_digits) == 0:
        return None
    lo = _shift(ref, _EDGAR_8K_LO_DAYS)
    hi = _shift(ref, _EDGAR_8K_HI_DAYS)
    try:
        from modal_workers.scanners.edgar_filing_monitor import (
            EFTS_URL,
            REQUEST_TIMEOUT,
            _http_get,
            _rate_limiter,
        )
        import requests  # lazy

        params = {
            "q": "",
            "dateRange": "custom",
            "startdt": lo.isoformat(),
            "enddt": hi.isoformat(),
            "forms": "8-K",
            "ciks": cik_digits.zfill(10),
        }
        _rate_limiter.wait()
        resp = _http_get(
            EFTS_URL, params=params, headers={"User-Agent": user_agent}, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        hits = (resp.json().get("hits", {}) or {}).get("hits", []) or []
    except Exception as exc:  # noqa: BLE001 — degrade to absent, never crash the run
        logger.info("edgar 8-K EFTS count failed for CIK %s: %s", cik, exc)
        return None
    # EFTS already filtered by ciks + forms + date; defensively re-check the CIK
    # is in each hit's ciks[] (zero-padding-insensitive) and the form is 8-K.
    target = cik_digits.lstrip("0") or "0"
    n = 0
    for h in hits:
        src = h.get("_source", {}) or {}
        hit_ciks = {str(c).lstrip("0") or "0" for c in (src.get("ciks") or [])}
        if hit_ciks and target not in hit_ciks:
            continue
        forms = src.get("file_type") or src.get("root_forms") or src.get("form") or []
        if isinstance(forms, str):
            forms = [forms]
        if forms and not any("8-K" in str(f).upper() for f in forms):
            continue
        n += 1
    return n


# A0's offline EDGAR submissions-API 8-K counter (data.sec.gov) — kept for the
# offline cohort path which has no rate-limiter context. Mirrors the same window.
def _n_8ks_30_180_submissions_api(cik: Optional[str], ref: date) -> Optional[int]:
    if not cik:
        return None
    cik_digits = re.sub(r"\D", "", str(cik))
    if not cik_digits or int(cik_digits) == 0:
        return None
    try:
        import requests  # lazy

        url = f"https://data.sec.gov/submissions/CIK{cik_digits.zfill(10)}.json"
        r = requests.get(
            url, headers={"User-Agent": "conan-a0-study contact@example.com"}, timeout=20
        )
        if r.status_code != 200:
            return None
        recent = (r.json().get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        lo = _shift(ref, _EDGAR_8K_LO_DAYS)
        hi = _shift(ref, _EDGAR_8K_HI_DAYS)
        n = 0
        for form, fdate in zip(forms, dates):
            if str(form).upper() != "8-K":
                continue
            d = _parse_iso(fdate)
            if d and lo <= d <= hi:
                n += 1
        return n
    except Exception as exc:  # noqa: BLE001
        logger.info("edgar submissions-api 8-K source failed for CIK %s: %s", cik, exc)
        return None


# --------------------------------------------------------------------------- #
# shared assembly core
# --------------------------------------------------------------------------- #
_DESIGNATION_ALIASES = (
    ("has_bt", ("has_bt", "breakthrough", "breakthrough_therapy", "bt")),
    ("has_ft", ("has_ft", "fast_track", "fasttrack", "ft")),
    ("has_aa", ("has_aa", "accelerated_approval", "accelerated", "aa")),
)


def _designation_flag(designations: Mapping[str, Any], aliases: Tuple[str, ...]) -> Optional[int]:
    """Coerce a designation flag to 0/1, mirroring feature_assembly._flag_present.
    Returns None when the flag is absent/unknown (so the scorer defaults it)."""
    if not designations:
        return None
    for key in aliases:
        if key in designations and designations[key] not in (None, ""):
            val = designations[key]
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


def _assemble_core(
    *,
    appno: str,
    sponsor_name: Optional[str],
    ticker: Optional[str],
    cik: Optional[str],
    ref_date: date,
    dfda: DrugsFDA,
    appl_type_hint: Optional[str] = None,
    designations: Optional[Mapping[str, Any]] = None,
    is_biosimilar_bla: int = 0,
    client: Any = None,
    enable_warning_letters: bool = True,
    eight_k_counter=None,
) -> dict:
    """Build the M14 scorer-input dict point-in-time. Shared by both callers.

    ``eight_k_counter(cik, ref) -> Optional[int]`` lets each caller choose its
    8-K source (live EFTS-by-CIK for the weekly worker; submissions-API for the
    offline cohort) while keeping ONE assembly body. Absent → feature omitted.
    """
    feats: Dict[str, Any] = {"cycle_type": "first_cycle_orig", "is_biosimilar_bla": int(is_biosimilar_bla)}
    sources: Dict[str, str] = {}
    max_source_date = date.min

    def _bump(d: Optional[date]) -> None:
        nonlocal max_source_date
        if d is not None and d > max_source_date:
            max_source_date = d

    # is_bla / ApplType — appno prefix, else the appl_type hint (surrogate appnos)
    is_bla = appl_is_bla(appno)
    if is_bla is None and appl_type_hint:
        hint = _norm(appl_type_hint)
        if hint in ("BLA", "SBLA"):
            is_bla = 1
        elif hint in ("NDA", "SNDA"):
            is_bla = 0
    if is_bla is not None:
        feats["is_bla"] = is_bla
        feats["ApplType"] = "BLA" if is_bla else "NDA"
        sources["is_bla"] = "appno_prefix" if appl_is_bla(appno) is not None else "appl_type_hint"

    # priority + SubmissionClassCode — ORIG submission (Drugs@FDA, real appno only)
    app = dfda.application(appno) if appl_is_bla(appno) is not None else None
    og = orig_submission(app)
    if og:
        # priority/class are PROPERTIES OF THE FILING (set at submission); the
        # ORIG action status_date can post-date ref_date (it is the AP/CR action,
        # ref_date is the filing). We do NOT bump max_source_date for these
        # filing-property reads (mirrors A0; not look-ahead).
        rp = _norm(og.get("review_priority"))
        if rp in ("PRIORITY", "STANDARD"):
            feats["priority"] = 1 if rp == "PRIORITY" else 0
            sources["priority"] = "drugsfda_orig"
        scc = og.get("submission_class_code")
        if scc and _norm(scc) != "UNKNOWN":
            feats["SubmissionClassCode"] = scc
            sources["SubmissionClassCode"] = "drugsfda_orig"

    # n_prior_filings — distinct prior ORIG appnos for sponsor, date < ref
    npf = _n_prior_filings(dfda, sponsor_name, appno, ref_date)
    if npf is not None:
        feats["n_prior_filings"] = npf[0]
        sources["n_prior_filings"] = "drugsfda_sponsor"
        _bump(npf[1])
    else:
        sources["n_prior_filings"] = "absent"

    # n_drug_inspections_5y_fix — DROPPED for v1 (no live source) -> absent
    sources["n_drug_inspections_5y_fix"] = "absent"

    # sponsor_has_warning — live fda_warning_letters (read-only; empty -> 0)
    if enable_warning_letters:
        hw = _sponsor_has_warning_live(client, sponsor_name, ticker, ref_date)
        if hw is not None:
            feats["sponsor_has_warning"] = hw[0]
            sources["sponsor_has_warning"] = "live_warning_letters"
            _bump(hw[1])
        else:
            sources["sponsor_has_warning"] = "absent"
    else:
        sources["sponsor_has_warning"] = "absent"

    # has_bt/has_ft/has_aa — carried from the caller's designations (NOT re-derived)
    for dst, aliases in _DESIGNATION_ALIASES:
        flag = _designation_flag(designations or {}, aliases)
        if flag is not None:
            feats[dst] = flag
            sources[dst] = "designations"
        else:
            sources[dst] = "absent"

    # n_8ks_30_180_clean — caller-supplied counter (EFTS-by-CIK live / API offline)
    n8k = eight_k_counter(cik, ref_date) if eight_k_counter else None
    if n8k is not None:
        feats["n_8ks_30_180_clean"] = n8k
        sources["n_8ks_30_180_clean"] = "edgar"
        _bump(_shift(ref_date, _EDGAR_8K_HI_DAYS))
    else:
        sources["n_8ks_30_180_clean"] = "absent"

    # ctgov_* / sponsor_has_orphan_history — ALWAYS absent (honest gap, ~4.7% |coef|)
    for k in ("ctgov_failed_primary", "ctgov_any_randomized", "sponsor_has_orphan_history"):
        sources[k] = "absent_no_source"

    # NO-LOOK-AHEAD assertion: every dated source row used must be <= ref
    if max_source_date != date.min and max_source_date > ref_date:
        raise AssertionError(
            f"look-ahead: feature for {appno} used a source row dated "
            f"{max_source_date} > ref_date {ref_date}"
        )

    n_present = sum(1 for k in _V1_KEPT_COVERAGE_KEYS if k in feats)
    feats["_coverage"] = round(n_present / len(_V1_KEPT_COVERAGE_KEYS), 4)
    feats["_required_feature_missing_count"] = len(_V1_KEPT_COVERAGE_KEYS) - n_present
    feats["_max_source_date"] = max_source_date.isoformat() if max_source_date != date.min else None
    feats["_provenance"] = sources
    return feats


# --------------------------------------------------------------------------- #
# the LIVE weekly caller
# --------------------------------------------------------------------------- #
def build_features_pit(
    client: Any,
    *,
    application_number: str,
    sponsor_cik: Optional[str],
    sponsor_name: Optional[str],
    appl_type: Optional[str],
    ref_date: date,
    designations: Optional[Mapping[str, Any]] = None,
    is_biosimilar_bla: bool = False,
    cik: Optional[str] = None,
    ticker: Optional[str] = None,
    dfda: Optional[DrugsFDA] = None,
    eight_k_counter=None,
    user_agent: Optional[str] = None,
    enable_warning_letters: bool = True,
) -> dict:
    """Build the M14 scorer-input dict for ONE universe row, point-in-time
    as-of ``ref_date`` (the LIVE weekly-score caller — Phase 1 §1.2).

    Receives ONLY identity + ref_date + designations — never ``pdufa_date`` or any
    outcome — so look-ahead is structurally impossible.

    Args:
        client: a ``SupabaseClient``-shaped object (for the read-only
            ``fda_warning_letters`` query). May be ``None`` (warning -> absent).
        application_number: real ``NDA…``/``BLA…`` appno, or surrogate
            ``EDGAR8K:<cik>:<slug>`` (substrate features then fall absent).
        sponsor_cik: the filer CIK (Phase-0 universe row) — used for the 8-K
            EFTS count when ``cik`` is not separately supplied.
        appl_type: ``NDA``/``BLA`` — supplies ``is_bla`` for surrogate appnos.
        designations: ``{has_bt,has_ft,has_aa}`` carried from the Phase-0 row.
        eight_k_counter: optional override ``(cik, ref) -> Optional[int]``; the
            default is live EFTS-by-CIK (needs ``user_agent``).

    Returns the scorer-input dict + ``_coverage`` + ``_max_source_date`` +
    ``_provenance``.
    """
    dfda = dfda or DrugsFDA()
    cik = cik or sponsor_cik
    if eight_k_counter is None:
        ua = user_agent or "conan-bc-weekly-score contact@conan.local"
        eight_k_counter = lambda c, r: count_8ks_by_cik_efts(c, r, user_agent=ua)  # noqa: E731
    return _assemble_core(
        appno=str(application_number or "").strip(),
        sponsor_name=sponsor_name,
        ticker=ticker,
        cik=cik,
        ref_date=ref_date,
        dfda=dfda,
        appl_type_hint=appl_type,
        designations=designations,
        is_biosimilar_bla=int(bool(is_biosimilar_bla)),
        client=client,
        enable_warning_letters=enable_warning_letters,
        eight_k_counter=eight_k_counter,
    )


# --------------------------------------------------------------------------- #
# the A0 offline caller (preserves the prior analysis/bc_a0 signature)
# --------------------------------------------------------------------------- #
def build_features(
    *,
    appno: str,
    sponsor_name: Optional[str],
    cik: Optional[str],
    ref_date: date,
    dfda: Optional[DrugsFDA] = None,
    enable_warning_letters: bool = True,
) -> dict:
    """Build the M14 scorer-input dict for ONE A0 cohort member, point-in-time
    as-of ``ref_date`` (the OFFLINE cohort caller — unchanged contract).

    Receives ONLY ``(appno, sponsor_name, cik, ref_date)`` — never the CRL record
    — so CRL text/letter_date cannot leak into a numeric feature.

    Returns the scorer-input dict with ``_coverage`` / ``_max_source_date`` /
    ``_feature_sources`` (legacy key name kept for A0 consumers)."""
    dfda = dfda or DrugsFDA()
    # A0 has no warning-letter creds in the offline env; read via SupabaseClient
    # if available, else absent (the prior A0 behavior).
    client = None
    if enable_warning_letters:
        try:
            from modal_workers.shared.supabase_client import SupabaseClient  # lazy

            client = SupabaseClient()
        except Exception:  # noqa: BLE001 — no creds offline -> warning absent
            client = None
    feats = _assemble_core(
        appno=appno,
        sponsor_name=sponsor_name,
        ticker=None,
        cik=cik,
        ref_date=ref_date,
        dfda=dfda,
        appl_type_hint=None,
        designations=None,  # A0 has no offline designation source
        is_biosimilar_bla=0,
        client=client,
        enable_warning_letters=enable_warning_letters and client is not None,
        eight_k_counter=_n_8ks_30_180_submissions_api,
    )
    # Legacy key name expected by A0 consumers.
    feats["_feature_sources"] = feats.get("_provenance", {})
    return feats


def estimate_ref_date(
    *,
    appno: str,
    letter_date: Optional[date],
    dfda: DrugsFDA,
) -> Tuple[date, str]:
    """Point-in-time anchor (ref_date) for an A0 cohort member (§3.1).

    Preference: ORIG action date - review_clock (openFDA exposes the action
    date, not the receipt date), else letter_date - review_clock (flagged
    estimated), else today (should never happen for a real cohort row)."""
    app = dfda.application(appno)
    og = orig_submission(app)
    if og:
        action_date = parse_compact_date(og.get("submission_status_date"))
        if action_date is not None:
            return (_shift(action_date, _REVIEW_CLOCK_DAYS), "drugsfda_orig_minus_clock")
    if letter_date is not None:
        return (_shift(letter_date, _REVIEW_CLOCK_DAYS), "letter_date_minus_clock_estimated")
    return (date.today(), "today_fallback")
