"""
Pre-Phase-3 Readout scanner — Modal port of tools/pre_phase3_readout_scanner.py.

Preservation (v1 parity):
  - ClinicalTrials.gov API v2 query: Phase 3 trials with PrimaryCompletionDate
    in the [T-14d, T+90d] window AND OverallStatus IN (ACTIVE_NOT_RECRUITING,
    COMPLETED). Same query.term + filter.advanced ESSIE expression.
  - INDICATION_MAP regex list (34 patterns) — byte-equivalent.
  - 5-pattern triage gate (single_primary_endpoint, enrollment_complete_readout_imminent,
    high_base_rate_indication, industry_sponsored, meaningful_enrollment) requiring
    >= 3 hits AND sponsor_class == "INDUSTRY".
  - Base-rate lookup per indication, applied to raw_payload.approval_probability
    so the binary_catalyst rubric can consume it.
  - Pagination (up to 8 pages x 100 studies) with 65%-of-budget early-exit.
  - User-Agent: "InvestmentResearch research@example.com" (v1 default).

Deviations from v1:
  - No OUT_FILE; signals returned via ScannerResult for run_scanner plumbing.
  - BASE_RATES now loaded from Supabase table `phase3_base_rates` (spec.md §3.1)
    instead of config/phase3_approval_base_rates.json. Per-process dict cache.
  - source_content_hash carries the spec.md §3.4 "sha256:<64hex>" prefix.
  - Entity hint is the sponsor corporate name + country=US. Sponsor->ticker
    resolution runs inline via sec_issuer_lookup.IssuerIndex (SEC's
    company_tickers.json, ~9k US-listed issuers, 30d cached). When a sponsor
    resolves, EntityHints carries ticker+cik+title+issuer_figi+mic; OpenFIGI
    is invoked on the SEC ticker to fetch the FIGI. Without this, household
    pharma names ("AbbVie", "Sanofi", "AstraZeneca") landed with primary_ticker
    NULL because no ticker was ever passed to the reactor's openfigi step.
  - strength_estimate: 3 default; 4 if indication base rate >= 0.60; 5 if >= 0.80.
  - Already-approved-drug filter (DLQ batch 2026-04-27): after the triage gate,
    each trial's drug interventions are checked against the openFDA Orange Book
    (drugsfda endpoint, no auth) for an approved (status=AP) submission whose
    sponsor matches the trial's lead sponsor. Matches are dropped — those are
    label-extensions or geographic-bridging studies, not binary catalysts.
    Lookup failures fail open (signal emitted, warning attached); cached in
    scanner-caches/fda/orange_book/<drug>.json with a 7-day TTL.

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
    - No auth required (CT.gov v2 is public).
    - Uses cfg.timeout_soft_s (default 90s) as wall-clock budget.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from modal_workers.shared.biotech_base_rates import (
    DEFAULT_APPROVAL_PROB as _DEFAULT_APPROVAL_PROB,
    INDICATION_MAP,
    load_base_rates as _load_base_rates,
    map_conditions_to_base_key as _map_conditions_to_base_key,
)
from modal_workers.shared.scanner_base import Signal, ScannerResult
from modal_workers.shared.sec_issuer_lookup import IssuerIndex, IssuerMatch
from modal_workers.shared.supabase_client import (
    EntityHints,
    ScannerConfig,
    SupabaseClient,
    SupabaseError,
)

# ---------------------------------------------------------------------------
# Constants (verbatim from v1)
# ---------------------------------------------------------------------------

NAME = "pre_phase3_readout_scanner"

CLINICALTRIALS_URL = "https://clinicaltrials.gov/api/v2/studies"
USER_AGENT = "InvestmentResearch research@example.com"
REQUEST_TIMEOUT = 15

# openFDA Orange Book / drugsfda lookup — used to filter out label-extension
# and geographic-bridging trials whose drug is already on the market for the
# same sponsor (DLQ batch 2026-04-27: AbbVie / Sanofi MenQuadfi / AstraZeneca
# all scored 31–36 and were correctly killed by the thesis_writer for
# insufficient_signal — no asymmetry on a megacap with an approved drug).
OPENFDA_DRUGSFDA_URL = "https://api.fda.gov/drug/drugsfda.json"
APPROVED_CHECK_TIMEOUT = 10
APPROVED_CACHE_TTL_S = 7 * 24 * 3600  # 7d — Orange Book changes slowly

# Tokens that appear in both CT.gov sponsor names and FDA sponsor_name but
# carry no identity signal. Stripped before the substring-match check.
_SPONSOR_NOISE_TOKENS = {
    "inc", "inc.", "incorporated", "llc", "ltd", "ltd.", "limited",
    "plc", "corp", "corp.", "corporation", "company", "co", "co.",
    "ag", "sa", "s.a.", "sas", "s.a.s.", "se", "nv", "n.v.", "ab",
    "holdings", "holding", "group",
    "pharmaceutical", "pharmaceuticals", "pharma", "pharmacia",
    "biosciences", "biotechnology", "biotech", "therapeutics",
    "laboratories", "labs", "research", "rsch", "rd",
    "global", "international", "intl", "usa", "us", "north", "america",
    "the", "and", "&",
}

# Intervention "names" that are clearly not a candidate drug to check.
_INTERVENTION_NOISE = re.compile(
    r"^(placebo|matching placebo|sham|standard of care|soc|"
    r"physician'?s? choice|best supportive care|bsc|usual care|no treatment|"
    r"saline|vehicle)\b",
    re.IGNORECASE,
)

# Readout window
READOUT_LOOKAHEAD_DAYS = 90   # primary completion within 90d
READOUT_MIN_DAYS = -14        # allow 14d post-completion (readout often lags)

# Pagination
PAGE_SIZE = 100
MAX_PAGES = 8  # up to 800 results

# INDICATION_MAP, DEFAULT_APPROVAL_PROB, _load_base_rates,
# _map_conditions_to_base_key now live in modal_workers.shared.biotech_base_rates
# and are imported above. Symbols re-exported here for backwards compatibility
# with existing tests / call sites.


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _days_to(target: datetime) -> int:
    return (target.date() - datetime.now(timezone.utc).date()).days


# ---------------------------------------------------------------------------
# ClinicalTrials.gov v2 query
# ---------------------------------------------------------------------------

def _fetch_phase3_readout_trials(budget_s: float,
                                 scanner_cache_client: Optional[SupabaseClient] = None
                                 ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Fetch Phase 3 trials with primary completion in the readout window.

    Returns (studies, warnings). Stops paginating when 65% of budget is spent
    (v1 parity) or MAX_PAGES reached.
    """
    today = datetime.now(timezone.utc).date()
    window_start = today + timedelta(days=READOUT_MIN_DAYS)
    window_end = today + timedelta(days=READOUT_LOOKAHEAD_DAYS)

    warnings: List[str] = []
    all_studies: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    started = time.time()

    for page_i in range(MAX_PAGES):
        if time.time() - started > budget_s * 0.65:
            warnings.append(f"CT.gov pagination halted at page {page_i} (budget)")
            break
        params: Dict[str, Any] = {
            "pageSize": PAGE_SIZE,
            "query.term": "AREA[Phase]PHASE3 AND (AREA[OverallStatus]ACTIVE_NOT_RECRUITING OR AREA[OverallStatus]COMPLETED)",
            "filter.advanced": f"AREA[PrimaryCompletionDate]RANGE[{window_start.isoformat()},{window_end.isoformat()}]",
            "fields": (
                "NCTId,BriefTitle,OfficialTitle,OverallStatus,Phase,EnrollmentCount,"
                "StartDate,CompletionDate,PrimaryCompletionDate,"
                "LeadSponsorName,LeadSponsorClass,Condition,"
                "InterventionName,InterventionType,"
                "PrimaryOutcomeMeasure,StudyType,DesignAllocation,DesignPrimaryPurpose"
            ),
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            resp = requests.get(
                CLINICALTRIALS_URL, params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            warnings.append(f"CT.gov page {page_i}: {type(e).__name__}: {e}")
            break

        studies = data.get("studies", []) or []
        all_studies.extend(studies)
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return all_studies, warnings


# ---------------------------------------------------------------------------
# Intervention extraction + sponsor normalisation
# ---------------------------------------------------------------------------

# Drug-bearing intervention types in CT.gov v2 (DEVICE / PROCEDURE / BEHAVIORAL
# / OTHER are not Orange Book candidates and are skipped).
_DRUG_INTERVENTION_TYPES = {"DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT", "GENETIC"}


def _extract_drug_interventions(raw: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Pick out the drug-like interventions from CT.gov's armsInterventionsModule.
    Filters out placebo/SOC/sham control arms which would never appear as the
    sponsor's own approved drug."""
    out: List[Dict[str, str]] = []
    for iv in raw or []:
        name = (iv.get("name") or "").strip()
        itype = (iv.get("type") or "").strip().upper()
        if not name:
            continue
        if itype and itype not in _DRUG_INTERVENTION_TYPES:
            continue
        if _INTERVENTION_NOISE.match(name):
            continue
        out.append({"name": name, "type": itype})
    return out


# Leading 2-3 char route-of-administration abbreviations seen in CT.gov
# intervention names (e.g. "IV Tulisokibart", "SC Placebo"). Only stripped for
# the auto-seed drug_name hint — full names stay in raw_payload["interventions"].
_ROUTE_PREFIX_RE = re.compile(r"^(IV|SC|PO|IM|IT|SQ|IP|IN)\s+", re.IGNORECASE)


def _pick_lead_drug_name(interventions: List[Dict[str, str]]) -> Optional[str]:
    """Pick a single drug name from filtered interventions for fda_asset seeding.

    Caller must pre-filter via `_extract_drug_interventions` so placebos and
    non-drug arms are already gone. Strips short route-of-administration
    prefixes (IV/SC/PO/IM/IT/SQ/IP/IN); does not touch longer English route
    words like "Oral" or "Topical" since those often belong to the brand name
    (e.g. "Patidegib Topical Gel"). Returns None if no usable name remains.
    """
    for iv in interventions or []:
        raw_name = (iv.get("name") or "").strip()
        if not raw_name:
            continue
        cleaned = _ROUTE_PREFIX_RE.sub("", raw_name).strip()
        if cleaned:
            return cleaned
    return None


def _normalize_sponsor(name: str) -> str:
    """Lowercase, strip punctuation, drop generic corporate-suffix tokens.
    Used only for substring matching between CT.gov leadSponsor.name and
    openFDA's sponsor_name field."""
    if not name:
        return ""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    tokens = [t for t in cleaned.split() if t and t not in _SPONSOR_NOISE_TOKENS]
    return " ".join(tokens).strip()


def _sponsor_matches(ct_sponsor: str, fda_sponsor: str) -> bool:
    """True if the two sponsor names plausibly refer to the same entity (or
    parent/subsidiary). Conservative: requires substring containment after
    noise-token stripping. Empty inputs never match."""
    a, b = _normalize_sponsor(ct_sponsor), _normalize_sponsor(fda_sponsor)
    if not a or not b:
        return False
    return a in b or b in a


# ---------------------------------------------------------------------------
# openFDA Orange Book approval check (cached)
# ---------------------------------------------------------------------------

def _approval_cache_key(drug_name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", drug_name.lower().strip())[:64]
    return f"orange_book/{safe}.json"


def _read_approval_cache(client: Optional[SupabaseClient],
                         drug_name: str) -> Optional[List[Dict[str, Any]]]:
    if client is None:
        return None
    try:
        raw = client.read_cache("fda", _approval_cache_key(drug_name), timeout=3.0)
    except SupabaseError:
        return None
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if time.time() - float(payload.get("cached_at", 0)) > APPROVED_CACHE_TTL_S:
        return None
    return payload.get("results") or []


def _write_approval_cache(client: Optional[SupabaseClient], drug_name: str,
                          results: List[Dict[str, Any]]) -> None:
    if client is None:
        return
    try:
        client.write_cache(
            "fda", _approval_cache_key(drug_name),
            json.dumps({"cached_at": time.time(), "results": results}).encode("utf-8"),
            content_type="application/json",
        )
    except SupabaseError:
        pass  # best-effort


def _fetch_drug_approvals(drug_name: str,
                          client: Optional[SupabaseClient]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """openFDA drugsfda lookup. Returns (results, error). Cached in
    scanner-caches/fda/orange_book/<drug>.json."""
    cached = _read_approval_cache(client, drug_name)
    if cached is not None:
        return cached, None
    params = {
        "search": (
            f'(openfda.brand_name:"{drug_name}"'
            f' OR openfda.generic_name:"{drug_name}"'
            f' OR openfda.substance_name:"{drug_name}")'
        ),
        "limit": 5,
    }
    try:
        resp = requests.get(
            OPENFDA_DRUGSFDA_URL, params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=APPROVED_CHECK_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        return [], f"openFDA request failed: {type(e).__name__}: {e}"
    if resp.status_code == 404:
        # openFDA returns 404 for "no results matched your query" — treat as miss, cache it.
        _write_approval_cache(client, drug_name, [])
        return [], None
    if resp.status_code >= 400:
        return [], f"openFDA HTTP {resp.status_code}"
    try:
        data = resp.json()
    except ValueError:
        return [], "openFDA non-JSON response"
    results: List[Dict[str, Any]] = []
    for r in data.get("results", []):
        results.append({
            "application_number": r.get("application_number", ""),
            "sponsor_name": r.get("sponsor_name", ""),
            "submissions": [
                {
                    "type": s.get("submission_type", ""),
                    "status": s.get("submission_status", ""),
                    "status_date": s.get("submission_status_date", ""),
                }
                for s in (r.get("submissions") or [])
            ],
        })
    _write_approval_cache(client, drug_name, results)
    return results, None


def _is_already_approved(intervention_name: str, ct_sponsor: str,
                         client: Optional[SupabaseClient]
                         ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Check Orange Book for `intervention_name` and return match-info if any
    record is both (a) approved (status=AP) AND (b) sponsored by the same
    company as `ct_sponsor`. Returns (match, error)."""
    if not intervention_name or len(intervention_name) < 3:
        return None, None
    results, err = _fetch_drug_approvals(intervention_name, client)
    if err is not None:
        return None, err
    for r in results:
        fda_sponsor = r.get("sponsor_name", "") or ""
        if not _sponsor_matches(ct_sponsor, fda_sponsor):
            continue
        for sub in r.get("submissions", []):
            if (sub.get("status") or "").upper() == "AP":
                return {
                    "drug_name": intervention_name,
                    "application_number": r.get("application_number", ""),
                    "fda_sponsor_name": fda_sponsor,
                    "approval_date": sub.get("status_date", ""),
                    "submission_type": sub.get("type", ""),
                }, None
    return None, None


def _check_trial_drug_approved(scored: Dict[str, Any],
                               client: Optional[SupabaseClient]
                               ) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """For each drug intervention in the scored trial, look up the Orange Book.
    Returns (first_match, warnings). A non-None match means the trial is a
    label-extension or geographic-bridging study and should be dropped."""
    warnings: List[str] = []
    sponsor = scored.get("sponsor_name") or ""
    for iv in scored.get("interventions") or []:
        match, err = _is_already_approved(iv["name"], sponsor, client)
        if err:
            warnings.append(f"{iv['name']}: {err}")
            continue
        if match:
            return match, warnings
    return None, warnings


# ---------------------------------------------------------------------------
# Scoring + triage
# ---------------------------------------------------------------------------

def _score_trial(trial: Dict[str, Any], base_rates: Dict[str, float]) -> Dict[str, Any]:
    """Extract features for triage gate + base-rate seeding."""
    proto = trial.get("protocolSection", {})
    ident = proto.get("identificationModule", {})
    status_mod = proto.get("statusModule", {})
    design = proto.get("designModule", {})
    sponsor_mod = proto.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {})
    conditions_mod = proto.get("conditionsModule", {})
    outcomes_mod = proto.get("outcomesModule", {})
    arms_mod = proto.get("armsInterventionsModule", {}) or {}

    nct_id = ident.get("nctId", "")
    brief_title = ident.get("briefTitle", "")
    status = status_mod.get("overallStatus", "")
    primary_completion = (status_mod.get("primaryCompletionDateStruct") or {}).get("date", "")
    sponsor_name = sponsor_mod.get("name", "")
    sponsor_class = sponsor_mod.get("class", "")  # INDUSTRY, NIH, etc.
    conditions = conditions_mod.get("conditions", []) or []
    enrollment = (design.get("enrollmentInfo") or {}).get("count")
    raw_interventions = arms_mod.get("interventions") or []
    interventions = _extract_drug_interventions(raw_interventions)
    # Preserve the unfiltered intervention list (placebos, devices, behavioral
    # arms, etc.) so downstream consumers can apply their own filtering — e.g.
    # distinguish placebo-controlled from open-label, or surface combination
    # therapy arms that _extract_drug_interventions drops by type.
    interventions_all = [
        {
            "name": (iv.get("name") or "").strip(),
            "type": (iv.get("type") or "").strip().upper(),
        }
        for iv in raw_interventions
        if (iv.get("name") or "").strip()
    ]

    # Indication + base-rate from Supabase table
    base_key, matched_indications = _map_conditions_to_base_key(conditions)
    approval_prob = float(base_rates.get(base_key, base_rates.get("default", _DEFAULT_APPROVAL_PROB)))

    # Days until readout
    pc_dt = _parse_date(primary_completion)
    days_until = _days_to(pc_dt) if pc_dt else 999

    # Primary outcomes
    primary_outcomes = [po.get("measure", "") for po in (outcomes_mod.get("primaryOutcomes") or [])]

    # Pattern count (5-pattern triage rubric — preserved from v1)
    patterns_hit = 0
    pattern_names: List[str] = []
    # Pattern 1 — trial design quality (single primary endpoint)
    if len(primary_outcomes) == 1:
        patterns_hit += 1
        pattern_names.append("single_primary_endpoint")
    # Pattern 2 — enrollment complete / readout imminent
    if status in ("ACTIVE_NOT_RECRUITING", "COMPLETED") and 0 <= days_until <= 90:
        patterns_hit += 1
        pattern_names.append("enrollment_complete_readout_imminent")
    # Pattern 3 — high-base-rate indication
    if approval_prob >= 0.70:
        patterns_hit += 1
        pattern_names.append("high_base_rate_indication")
    # Pattern 4 — industry-sponsored
    if sponsor_class == "INDUSTRY":
        patterns_hit += 1
        pattern_names.append("industry_sponsored")
    # Pattern 5 — meaningful enrollment
    if isinstance(enrollment, int) and enrollment >= 200:
        patterns_hit += 1
        pattern_names.append("meaningful_enrollment")

    return {
        "nct_id": nct_id,
        "brief_title": brief_title,
        "sponsor_name": sponsor_name,
        "sponsor_class": sponsor_class,
        "status": status,
        "primary_completion_date": primary_completion,
        "days_until_readout": days_until,
        "enrollment": enrollment,
        "conditions": conditions,
        "interventions": interventions,
        "interventions_all": interventions_all,
        "primary_outcomes": primary_outcomes[:3],
        "base_rate_key": base_key,
        "base_rate_approval": approval_prob,
        "matched_indications": matched_indications,
        "patterns_hit": patterns_hit,
        "pattern_names": pattern_names,
    }


def _strength_for(approval_prob: float) -> int:
    """Map indication base rate -> strength_estimate per scanner spec."""
    if approval_prob >= 0.80:
        return 5
    if approval_prob >= 0.60:
        return 4
    return 3


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------

def _sig_id(nct_id: str, primary_date: str) -> str:
    return hashlib.sha256(f"p3readout:{nct_id}:{primary_date}".encode()).hexdigest()[:32]


def _content_hash(nct_id: str, primary_date: str) -> str:
    return f"sha256:{hashlib.sha256(f'{nct_id}|{primary_date}'.encode()).hexdigest()}"


def _build_signal(
    scored: Dict[str, Any],
    scan_date: datetime,
    issuer_index: Optional[IssuerIndex] = None,
) -> Optional[Signal]:
    nct = scored["nct_id"]
    if not nct:
        return None
    pcd = scored["primary_completion_date"] or ""
    sponsor = scored["sponsor_name"] or "Unknown sponsor"
    days = scored["days_until_readout"]
    when = f"T+{days}" if days >= 0 else f"T{days}"
    approval_prob = scored["base_rate_approval"]

    signal_id = _sig_id(nct, pcd)
    source_content_hash = _content_hash(nct, pcd)

    # source_date: primary completion date (catalyst anchor); fallback to scan_date.
    pc_dt = _parse_date(pcd)
    source_date = pc_dt or scan_date

    # Sponsor → public-issuer resolution. Without this, household pharma names
    # ("AbbVie", "Sanofi", "AstraZeneca") landed with primary_ticker NULL because
    # the reactor's openfigi step had no ticker to look up. SEC's tickers list
    # covers ~9k US-listed issuers (incl. ADRs like SNY/AZN), is free, and is
    # already cached 30d in Storage by sec_issuer_lookup.
    issuer_match: Optional[IssuerMatch] = None
    if issuer_index is not None and sponsor and sponsor != "Unknown sponsor":
        issuer_match = issuer_index.resolve(sponsor)

    # When SEC resolves, also fetch the FIGI from OpenFIGI on the resolved
    # ticker so EntityHints carries figi+mic. Best-effort; failure leaves
    # figi/mic NULL but ticker+cik are still authoritative.
    issuer_figi: Optional[str] = None
    figi_mic: Optional[str] = None
    if issuer_match is not None:
        try:
            from modal_workers.shared.openfigi_resolver import resolve_ticker
            res = resolve_ticker(issuer_match.ticker, exch_code="US")
            if res.resolved:
                issuer_figi = res.issuer_figi
                figi_mic = res.mic
        except Exception:  # noqa: BLE001 — best-effort
            pass

    headline = f"Phase 3 readout {when}: {sponsor} — {scored['brief_title'][:90]}"
    summary = (
        f"{sponsor} ({scored['sponsor_class'] or 'n/a'}) Phase 3 trial {nct} "
        f"status={scored['status']}, primary completion {pcd} ({when}). "
        f"Indication: {scored['base_rate_key']} (base approval rate "
        f"{approval_prob*100:.0f}%). "
        f"Patterns hit: {scored['patterns_hit']}/5 "
        f"({', '.join(scored['pattern_names'])})."
    )

    source_url = f"https://clinicaltrials.gov/study/{nct}" if nct else None

    # Lead drug — first non-placebo, non-comparator DRUG-type intervention.
    # Surfaced as a top-level raw_payload field so scoring, TAM modelling, and
    # competitive-landscape downstream consumers can key off a single drug name
    # instead of collapsing every signal into the heuristic-only dimension
    # vector when raw_payload.lead_drug is NULL.
    lead_drug = _pick_lead_drug_name(scored.get("interventions", []))

    raw_payload: Dict[str, Any] = {
        "nct_id": nct,
        "trial_title": scored["brief_title"],
        "sponsor_name": sponsor,
        "sponsor_class": scored["sponsor_class"],
        "status": scored["status"],
        "primary_completion_date": pcd,
        "days_until_readout": days,
        "enrollment": scored["enrollment"],
        "meaningful_enrollment": isinstance(scored["enrollment"], int) and scored["enrollment"] >= 200,
        "conditions": scored["conditions"],
        "interventions": scored.get("interventions", []),
        "interventions_all": scored.get("interventions_all", []),
        "lead_drug": lead_drug,
        "indication_keywords": scored["matched_indications"],
        "primary_outcomes": scored["primary_outcomes"],
        "single_primary_endpoint": len(scored["primary_outcomes"]) == 1,
        "industry_sponsored": scored["sponsor_class"] == "INDUSTRY",
        "base_rate_key": scored["base_rate_key"],
        # Consumed by binary_catalyst rubric (approval_probability dim).
        "approval_probability": approval_prob,
        "matched_indications": scored["matched_indications"],
        "patterns_hit": scored["patterns_hit"],
        "pattern_names": scored["pattern_names"],
        "source_url": source_url,
        "headline": headline,
        "summary": summary,
        "company_name_en": sponsor,
        # Sponsor universe-resolution trace (mirrors courtlistener_scanner).
        "universe_resolved": issuer_match is not None,
        "universe_match_kind": issuer_match.match_kind if issuer_match else None,
        "universe_ticker": issuer_match.ticker if issuer_match else None,
        "universe_cik": issuer_match.cik if issuer_match else None,
        "universe_title": issuer_match.title if issuer_match else None,
        # Auto-cap inputs preserved from v1 for run_post_scan downstream:
        "definitive_merger_agreement": False,
        "prior_failed_phase3_same_indication": False,
        "upside_pct": 50.0,
        "downside_pct": 35.0,
    }

    # Auto-seed hint: when this signal resolves to a public issuer AND has a
    # usable drug-like intervention, a SQL AFTER-INSERT trigger on signals will
    # create a stub `fda_assets` row (program_status='phase3', is_active=true)
    # so the v3 asset_linker cron can begin ingesting docs for this asset. The
    # trigger only fires when this key is present and the entity has no
    # existing fda_asset; missing/empty fields are a no-op.
    if issuer_match is not None and lead_drug:
        raw_payload["auto_seed_fda_asset"] = {
            "ticker": issuer_match.ticker,
            "drug_name": lead_drug,
            "sponsor_name": issuer_match.title or sponsor,
            "indication": scored["base_rate_key"],
            "nct_id": nct,
            "primary_completion_date": pcd,
        }

    # SEC match wins for name (authoritative spelling); raw sponsor used otherwise.
    name_for_hint = (
        issuer_match.title if issuer_match
        else (sponsor if sponsor != "Unknown sponsor" else None)
    )
    entity_hints = EntityHints(
        issuer_figi=issuer_figi,
        ticker=issuer_match.ticker if issuer_match else None,
        mic=figi_mic,
        cik=issuer_match.cik if issuer_match else None,
        name=name_for_hint,
        country="US",
    )

    return Signal(
        signal_id=signal_id,
        source_content_hash=source_content_hash,
        source_date=source_date,
        scan_date=scan_date,
        signal_type="pre_phase3_readout",
        raw_payload=raw_payload,
        source_url=source_url,
        issuer_figi=issuer_figi,
        entity_hints=entity_hints,
        thesis_direction="long",
        strength_estimate=_strength_for(approval_prob),
    )


# ---------------------------------------------------------------------------
# operator_flags helper
# ---------------------------------------------------------------------------

def _flag_unresolved_entity(client: SupabaseClient, *,
                            scanner_id: Optional[str],
                            sponsor_name: str,
                            nct_id: str,
                            signal_id: str) -> None:
    """UPSERT a `pre_phase3_unresolved_entity` operator_flag.

    Surfaces sponsors that cleared the triage gate but couldn't be mapped to
    any public-issuer identifier. The partial unique index
    `operator_flags_open_uniq` collapses repeated upserts on the same
    (source, kind, scanner_id) tuple, so the evidence carries the latest
    sample (sponsor + nct). Best-effort: never raises into the scanner loop.
    """
    try:
        evidence = {
            "sponsor_name": sponsor_name,
            "nct_id": nct_id,
            "signal_id_dropped": signal_id,
        }
        title = f"pre_phase3_readout: sponsor '{sponsor_name[:60]}' did not resolve to a public issuer"
        body = (
            f"Scanner dropped trial {nct_id} because SEC issuer lookup + OpenFIGI "
            f"produced no figi/ticker/cik for sponsor '{sponsor_name}'. Emitting "
            f"would strand the signal (band_with_bonus never stamps; thesis_writer "
            f"and v3 orchestrator can't enqueue without a ticker)."
        )
        filt = {
            "source": "eq.scanner:pre_phase3_readout_scanner",
            "kind": "eq.pre_phase3_unresolved_entity",
            "resolved_at": "is.null",
            "scanner_id": f"eq.{scanner_id}" if scanner_id else "is.null",
            "entity_id": "is.null",
            "signal_id": "is.null",
            "candidate_id": "is.null",
        }
        existing = client._rest(
            "GET", "operator_flags",
            params={**filt, "select": "id", "limit": 1},
        ) or []
        row = {
            "severity": "info",
            "source": "scanner:pre_phase3_readout_scanner",
            "kind": "pre_phase3_unresolved_entity",
            "title": title,
            "body": body,
            "evidence": evidence,
            "scanner_id": scanner_id,
        }
        if existing:
            client._rest(
                "PATCH", "operator_flags",
                params={"id": f"eq.{existing[0]['id']}"},
                json_body={k: row[k] for k in ("title", "body", "evidence", "severity")},
                prefer="return=minimal",
            )
        else:
            client._rest(
                "POST", "operator_flags",
                json_body=row,
                prefer="return=minimal",
            )
    except Exception:  # noqa: BLE001 — observability MUST NOT break the scanner
        pass


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    client = SupabaseClient()

    # Wire openfigi cache backend through Supabase Storage. Now that this
    # scanner *does* invoke openfigi inline (after a successful SEC issuer
    # match), the cache backend matters for cross-scanner FIGI cache reuse.
    try:
        from modal_workers.shared.openfigi_resolver import set_cache_backend
        set_cache_backend(*client.openfigi_cache_backend())
    except Exception:
        pass  # best-effort; scanner doesn't hard-depend on openfigi

    # Load SEC issuer index once per run — used inside _build_signal to resolve
    # sponsor name → ticker/cik/title. Failure is non-fatal; the scanner falls
    # back to name-only entity_hints (which was the pre-fix behavior).
    sec_user_agent = os.environ.get("SEC_USER_AGENT") or "Conan Scanner"
    try:
        issuer_index = IssuerIndex.load(client, user_agent=sec_user_agent)
    except Exception:  # noqa: BLE001
        issuer_index = None

    base_rates = _load_base_rates(client)

    scan_date = datetime.now(timezone.utc)
    budget = max(10, cfg.timeout_soft_s - 5)  # leave headroom for final ops
    scan_start = time.time()

    warnings: List[str] = []
    if issuer_index is None:
        warnings.append("sec_issuer_lookup unavailable — sponsors emit without ticker")
    signals: List[Signal] = []
    seen_nct: set[str] = set()

    trials, fetch_warnings = _fetch_phase3_readout_trials(budget_s=budget)
    warnings.extend(fetch_warnings)

    below_gate = 0
    skipped_no_drug_intervention = 0
    skipped_already_approved = 0
    skipped_unresolved_entity = 0
    unresolved_sponsors: List[str] = []
    # Parallel structured capture for unresolved_sponsor_log telemetry. The
    # string list above feeds the human warning; this list feeds the log
    # table that Phase-2 prioritization queries against.
    unresolved_log_rows: List[Dict[str, Any]] = []
    for t in trials:
        if time.time() - scan_start > budget:
            warnings.append(f"wall-clock budget ({budget}s) exceeded during scoring")
            break
        try:
            scored = _score_trial(t, base_rates)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"score: {type(e).__name__}: {e}")
            continue
        nct = scored["nct_id"]
        if not nct or nct in seen_nct:
            continue
        seen_nct.add(nct)

        # Drop trials with no drug-like intervention left after filtering
        # placebo / device / behavioral arms. Without a drug, the binary
        # catalyst rubric has nothing scorable to say and the resulting
        # signal collapses into the heuristic-only dimension vector. Placed
        # before the triage gate so it doesn't burn an openFDA Orange Book
        # lookup on a no-drug trial.
        if not scored.get("interventions"):
            skipped_no_drug_intervention += 1
            warnings.append(
                f"skipped {nct}: no drug-like intervention "
                f"(all arms placebo/device/behavioral)"
            )
            continue

        # Triage gate (v1 parity): >= 3 of 5 patterns AND INDUSTRY sponsor.
        if scored["patterns_hit"] < 3:
            below_gate += 1
            continue
        if scored["sponsor_class"] != "INDUSTRY":
            below_gate += 1
            continue

        # Already-approved-drug filter (DLQ batch 2026-04-27): if the trial's
        # drug is already on the market for the same sponsor, this is a
        # label-extension / geographic-bridging study, not a binary catalyst.
        # Fail-open on Orange Book lookup errors — we'd rather emit a
        # sometimes-noisy signal than silently drop legit ones.
        approved_match, approved_warnings = _check_trial_drug_approved(scored, client)
        for w in approved_warnings:
            warnings.append(f"orange_book[{nct}] {w}")
        if approved_match is not None:
            skipped_already_approved += 1
            warnings.append(
                f"skipped {nct}: {approved_match['drug_name']} already approved "
                f"(NDA/BLA {approved_match['application_number']}, "
                f"sponsor='{approved_match['fda_sponsor_name']}', "
                f"approval_date={approved_match['approval_date']})"
            )
            continue

        sig = _build_signal(scored, scan_date, issuer_index=issuer_index)
        if sig is None:
            continue
        # Phase-1 R4 telemetry: log every sponsor that cleared the triage
        # gate but failed SEC issuer-index resolution. This list is a
        # superset of the drop block below — a signal may have a FIGI from
        # OpenFIGI yet still miss the SEC index (EU/Asian listings, odd-
        # spelled subsidiaries). Phase-2 alias/name-search work ranks
        # against this log.
        if issuer_index is not None and not sig.raw_payload.get("universe_resolved"):
            sponsor_name = scored["sponsor_name"] or "?"
            unresolved_log_rows.append({
                "sponsor_name": sponsor_name,
                "context": {
                    "nct_id": nct,
                    "sponsor_class": scored.get("sponsor_class"),
                    "patterns_hit": scored.get("patterns_hit"),
                },
            })

        # Drop signals whose sponsor failed to resolve to any public-issuer
        # identifier (no FIGI, no ticker, no CIK). Emitting these strands the
        # signal downstream: resolve_or_create_entity falls through to name-
        # only matching and creates an entity with primary_ticker/primary_mic
        # NULL, which the convergence reactor and v3 orchestrator can't act on
        # — `band_with_bonus` never stamps and the row sits in
        # dashboard_signal_rows as display_band='immediate' forever.
        # Surface upstream via operator_flags so the audit can decide whether
        # to extend the SEC alias map or accept private-biotech blind spots.
        if (
            sig.entity_hints is None
            or (
                not sig.entity_hints.issuer_figi
                and not sig.entity_hints.ticker
                and not sig.entity_hints.cik
            )
        ):
            skipped_unresolved_entity += 1
            sponsor_name = scored["sponsor_name"] or "?"
            unresolved_sponsors.append(sponsor_name)
            warnings.append(
                f"skipped {nct}: sponsor '{sponsor_name}' did not resolve to a "
                f"public issuer (no figi/ticker/cik); emit would strand signal"
            )
            _flag_unresolved_entity(
                client,
                scanner_id=getattr(cfg, "scanner_id", None),
                sponsor_name=sponsor_name,
                nct_id=nct,
                signal_id=sig.signal_id,
            )
            continue
        signals.append(sig)

    if unresolved_sponsors:
        sample = ", ".join(sorted(set(unresolved_sponsors))[:5])
        warnings.append(
            f"dropped {len(unresolved_sponsors)} INDUSTRY sponsor(s) — no public "
            f"issuer identifier resolved: {sample}"
        )

    # Best-effort: write per-occurrence telemetry to unresolved_sponsor_log.
    # Phase-1 of R4 (sponsor→FIGI resolution gap) — frequency rank against
    # this table to drive Phase 2A (seed-migration aliases) and 2B (OpenFIGI
    # name-search fallback). Failure here must not break the scan run.
    if unresolved_log_rows:
        try:
            client.log_unresolved_sponsors(
                NAME,
                cfg.scanner_run_id,
                unresolved_log_rows,
            )
        except Exception as e:  # noqa: BLE001 — telemetry, not load-bearing
            warnings.append(f"unresolved_sponsor_log write failed: {type(e).__name__}: {e}")

    status = "partial" if warnings else "ok"
    if warnings and not signals and not trials:
        status = "error"

    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=signals,
        warnings=warnings,
        fetched_records=len(trials),
        run_metrics={
            "below_gate": below_gate,
            "skipped_no_drug_intervention": skipped_no_drug_intervention,
            "skipped_already_approved": skipped_already_approved,
            "skipped_unresolved_entity": skipped_unresolved_entity,
        },
    )
