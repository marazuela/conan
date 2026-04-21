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
    resolution happens downstream via openfigi_resolver in the reactor; the
    scanner does not attempt inline OpenFIGI lookup (no ticker to pass).
  - strength_estimate: 3 default; 4 if indication base rate >= 0.60; 5 if >= 0.80.

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
    - No auth required (CT.gov v2 is public).
    - Uses cfg.timeout_soft_s (default 90s) as wall-clock budget.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from modal_workers.shared.scanner_base import Signal, ScannerResult
from modal_workers.shared.supabase_client import EntityHints, ScannerConfig, SupabaseClient

# ---------------------------------------------------------------------------
# Constants (verbatim from v1)
# ---------------------------------------------------------------------------

NAME = "pre_phase3_readout_scanner"

CLINICALTRIALS_URL = "https://clinicaltrials.gov/api/v2/studies"
USER_AGENT = "InvestmentResearch research@example.com"
REQUEST_TIMEOUT = 15

# Readout window
READOUT_LOOKAHEAD_DAYS = 90   # primary completion within 90d
READOUT_MIN_DAYS = -14        # allow 14d post-completion (readout often lags)

# Pagination
PAGE_SIZE = 100
MAX_PAGES = 8  # up to 800 results

# Default phase3->approval rate if no indication matches and table has no 'default' row.
_DEFAULT_APPROVAL_PROB = 0.58


INDICATION_MAP: List[Tuple[str, str]] = [
    # (regex pattern, base-rate key)
    (r"alzheimer", "neurology_alzheimers"),
    (r"amyotrophic lateral|ALS\b", "neurology_als"),
    (r"parkinson", "neurology_parkinsons"),
    (r"migraine", "neurology_migraine"),
    (r"epilepsy|seizure", "neurology_epilepsy"),
    (r"depress|MDD", "psychiatry_depression"),
    (r"schizophrenia", "psychiatry_schizophrenia"),
    (r"agitation", "psychiatry_agitation"),
    (r"hepatitis", "hepatology_hepb"),
    (r"NASH|steatohepatit", "gastro_nash"),
    (r"crohn|ulcerative colitis|IBD|inflammatory bowel", "gastro_ibd"),
    (r"psoriasis", "dermatology_psoriasis"),
    (r"atopic dermatitis|eczema", "dermatology_atopic_dermatitis"),
    (r"rheumatoid arthritis|RA\b", "rheumatology_ra"),
    (r"thyroid eye|graves ophthalmopath", "ophthalmology_rare"),
    (r"wet AMD|macular degeneration", "ophthalmology_wet_amd"),
    (r"IgA nephropathy|IgAN", "nephrology_rare"),
    (r"FSGS|focal segmental", "nephrology_rare"),
    (r"chronic kidney disease|CKD\b", "nephrology_ckd"),
    (r"sickle cell", "hematology_sickle_cell"),
    (r"obesity|weight management", "metabolic_obesity"),
    (r"diabetes|type 2 DM|T2DM", "metabolic_diabetes"),
    (r"hypertension|heart failure|atrial fib|MACE|cardiovascular", "cardiovascular"),
    (r"COPD|chronic obstructive", "respiratory_copd"),
    (r"asthma", "respiratory_asthma"),
    (r"pulmonary fibrosis|IPF", "respiratory_ipf"),
    (r"pain", "pain_chronic"),
    (r"lymphoma|leukemia|myeloma|myelofibrosis", "oncology_hematologic"),
    (r"carcinoma|tumor|tumour|melanoma|cancer", "oncology_solid_tumor"),
    (r"rare disease|orphan", "rare_disease_genetic"),
    (r"autoimmune|lupus|SLE", "autoimmune"),
    (r"influenza|coronavirus|covid|RSV|HIV|hepatitis C", "infectious_antiviral"),
    (r"bacterial infection|sepsis|pneumonia", "infectious_antibacterial"),
    (r"vaccine", "infectious_vaccine"),
]


# ---------------------------------------------------------------------------
# Base-rate table loader (per-process cache)
# ---------------------------------------------------------------------------

_BASE_RATES_CACHE: Optional[Dict[str, float]] = None


def _load_base_rates(client: SupabaseClient) -> Dict[str, float]:
    """Fetch {indication: phase3_to_approval} from Supabase. Cached per-process."""
    global _BASE_RATES_CACHE
    if _BASE_RATES_CACHE is not None:
        return _BASE_RATES_CACHE
    try:
        rows = client._rest("GET", "phase3_base_rates", params={"select": "*"})
    except Exception:
        _BASE_RATES_CACHE = {}
        return _BASE_RATES_CACHE
    base_rates: Dict[str, float] = {}
    for r in rows or []:
        key = r.get("indication")
        val = r.get("phase3_to_approval")
        if key and val is not None:
            try:
                base_rates[key] = float(val)
            except (TypeError, ValueError):
                continue
    _BASE_RATES_CACHE = base_rates
    return base_rates


def _map_conditions_to_base_key(conditions: List[str]) -> Tuple[str, List[str]]:
    """Return (base_rate_key, matched_patterns) for the best condition match."""
    joined = " ; ".join(conditions).lower()
    matched: List[str] = []
    key = "default"
    for pattern, k in INDICATION_MAP:
        if re.search(pattern, joined, re.IGNORECASE):
            if key == "default":
                key = k
            matched.append(pattern)
    return key, matched


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
                "LeadSponsorName,LeadSponsorClass,Condition,InterventionName,"
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

    nct_id = ident.get("nctId", "")
    brief_title = ident.get("briefTitle", "")
    status = status_mod.get("overallStatus", "")
    primary_completion = (status_mod.get("primaryCompletionDateStruct") or {}).get("date", "")
    sponsor_name = sponsor_mod.get("name", "")
    sponsor_class = sponsor_mod.get("class", "")  # INDUSTRY, NIH, etc.
    conditions = conditions_mod.get("conditions", []) or []
    enrollment = (design.get("enrollmentInfo") or {}).get("count")

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


def _build_signal(scored: Dict[str, Any], scan_date: datetime) -> Optional[Signal]:
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
        # Auto-cap inputs preserved from v1 for run_post_scan downstream:
        "definitive_merger_agreement": False,
        "prior_failed_phase3_same_indication": False,
        "upside_pct": 50.0,
        "downside_pct": 35.0,
    }

    entity_hints = EntityHints(
        name=sponsor if sponsor != "Unknown sponsor" else None,
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
        entity_hints=entity_hints,
        thesis_direction="long",
        strength_estimate=_strength_for(approval_prob),
    )


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    client = SupabaseClient()

    # Wire openfigi cache backend through Supabase Storage (even though this
    # scanner doesn't invoke openfigi inline, the convention is set for parity
    # with other Modal scanners and downstream resolvers reuse the same process).
    try:
        from modal_workers.shared.openfigi_resolver import set_cache_backend
        set_cache_backend(*client.openfigi_cache_backend())
    except Exception:
        pass  # best-effort; scanner doesn't hard-depend on openfigi

    base_rates = _load_base_rates(client)

    scan_date = datetime.now(timezone.utc)
    budget = max(10, cfg.timeout_soft_s - 5)  # leave headroom for final ops
    scan_start = time.time()

    warnings: List[str] = []
    signals: List[Signal] = []
    seen_nct: set[str] = set()

    trials, fetch_warnings = _fetch_phase3_readout_trials(budget_s=budget)
    warnings.extend(fetch_warnings)

    below_gate = 0
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

        # Triage gate (v1 parity): >= 3 of 5 patterns AND INDUSTRY sponsor.
        if scored["patterns_hit"] < 3:
            below_gate += 1
            continue
        if scored["sponsor_class"] != "INDUSTRY":
            below_gate += 1
            continue

        sig = _build_signal(scored, scan_date)
        if sig is None:
            continue
        signals.append(sig)

    status = "partial" if warnings else "ok"
    if warnings and not signals and not trials:
        status = "error"

    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=signals,
        warnings=warnings,
        fetched_records=len(trials),
    )
