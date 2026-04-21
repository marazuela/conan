"""
Pre-Phase-3 Readout Scanner — pre-edge biotech catalyst detector.

New scanner (2026-04-20). Complementary to fda_pdufa_pipeline.py:
fda_pdufa catches the post-NDA PDUFA window (60-180d pre-PDUFA);
this scanner catches the 60-120d window BEFORE Phase 3 primary readout
— when positive data is not yet confirmed but the setup gives an edge.

Data sources:
  - ClinicalTrials.gov API v2 (/studies) — Phase 3 trials in
    "ACTIVE_NOT_RECRUITING" or "COMPLETED" with PrimaryCompletionDate
    in the next 90 days.
  - config/phase3_approval_base_rates.json — base-rate seed for
    binary_catalyst.approval_probability dim.

Emits into signals/pre_phase3_readout_scanner_output.json.
Default profile: binary_catalyst with signal_type=pre_phase3_readout.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

sys.path.insert(0, str(Path(__file__).parent))

NAME = "pre_phase3_readout_scanner"
REPO = Path(__file__).parent.parent
OUT_FILE = REPO / "signals" / f"{NAME}_output.json"
OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

BASE_RATES_FILE = REPO / "config" / "phase3_approval_base_rates.json"

CLINICALTRIALS_URL = "https://clinicaltrials.gov/api/v2/studies"
USER_AGENT = "InvestmentResearch research@example.com"
REQUEST_TIMEOUT = 15
WALL_CLOCK_BUDGET_S = 90

# Readout window
READOUT_LOOKAHEAD_DAYS = 90   # primary completion within 90d
READOUT_MIN_DAYS = -14        # allow 14d post-completion (readout often lags)

# Pagination
PAGE_SIZE = 100
MAX_PAGES = 8  # up to 800 results

logger = logging.getLogger(NAME)


# --------------------------------------------------------------------
# Indication mapping (conditions → base-rate key)
# --------------------------------------------------------------------

INDICATION_MAP = [
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


def _map_conditions_to_base_key(conditions: List[str]) -> Tuple[str, List[str]]:
    """Return (base_rate_key, matched_conditions) for the best condition match."""
    joined = " ; ".join(conditions).lower()
    matched = []
    key = "default"
    for pattern, k in INDICATION_MAP:
        if re.search(pattern, joined, re.IGNORECASE):
            if key == "default":
                key = k
            matched.append(pattern)
    return key, matched


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

def _iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sig_id(nct_id: str, primary_date: str) -> str:
    return hashlib.sha256(f"p3readout:{nct_id}:{primary_date}".encode()).hexdigest()[:32]


def _content_hash(nct_id: str, status: str, primary_date: str) -> str:
    return hashlib.sha256(f"{nct_id}|{status}|{primary_date}".encode()).hexdigest()[:16]


def _load_base_rates() -> Dict[str, Any]:
    if not BASE_RATES_FILE.exists():
        logger.warning(f"Base rates file missing: {BASE_RATES_FILE}")
        return {"indications": {"default": {"phase3_to_approval": 0.58}}}
    try:
        return json.loads(BASE_RATES_FILE.read_text())
    except Exception as e:
        logger.error(f"Failed to parse base rates: {e}")
        return {"indications": {"default": {"phase3_to_approval": 0.58}}}


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


# --------------------------------------------------------------------
# ClinicalTrials.gov v2 query
# --------------------------------------------------------------------

def _fetch_phase3_readout_trials() -> List[Dict[str, Any]]:
    """Fetch Phase 3 trials with primary completion in the readout window."""
    today = datetime.now(timezone.utc).date()
    window_start = today + timedelta(days=READOUT_MIN_DAYS)
    window_end = today + timedelta(days=READOUT_LOOKAHEAD_DAYS)

    # CT.gov v2 filter syntax: filter.advanced accepts ESSIE-like expressions.
    # We'll filter Phase 3 and active-not-recruiting OR completed via query.term.
    all_studies: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    started = time.time()

    for page_i in range(MAX_PAGES):
        if time.time() - started > WALL_CLOCK_BUDGET_S * 0.65:
            logger.info("CT.gov fetch: budget exhausted, stopping pagination")
            break
        params = {
            "pageSize": PAGE_SIZE,
            # AREA search: Phase 3 AND status filter
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
            logger.error(f"CT.gov fetch page {page_i} failed: {e}")
            break

        studies = data.get("studies", []) or []
        all_studies.extend(studies)
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    logger.info(f"CT.gov fetched {len(all_studies)} Phase 3 trials in readout window")
    return all_studies


# --------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------

def _score_trial(trial: Dict[str, Any], base_rates: Dict[str, Any]) -> Dict[str, Any]:
    """Extract features and compute dim scores for binary_catalyst rubric.

    Returns dict with: dims (1-5 scores), probability, patterns_hit, notes.
    """
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

    # Indication + base-rate
    base_key, matched_indications = _map_conditions_to_base_key(conditions)
    base_info = (base_rates.get("indications") or {}).get(base_key) or (base_rates.get("indications") or {}).get("default", {})
    approval_prob = float(base_info.get("phase3_to_approval", 0.58))

    # Dim 1: approval_probability (weight ×2.5 in binary_catalyst)
    # Rubric: ≥0.75→5, 0.65-0.75→4, 0.55-0.65→3, 0.45-0.55→2, <0.45→1
    if approval_prob >= 0.75:
        dim_approval = 5
    elif approval_prob >= 0.65:
        dim_approval = 4
    elif approval_prob >= 0.55:
        dim_approval = 3
    elif approval_prob >= 0.45:
        dim_approval = 2
    else:
        dim_approval = 1

    # Dim 2: market_mispricing (weight ×2.5) — heuristic stub at scanner level;
    # we lack live price data in the scanner. Set neutral=3 and leave a
    # raw_data flag for the candidate reviewer to refine.
    dim_mispricing = 3

    # Dim 3: magnitude (weight ×1.5) — approximate from sponsor class + trial size
    # Industry-sponsored Phase 3s with enrollment > 500 → larger magnitude.
    dim_magnitude = 3
    if sponsor_class == "INDUSTRY" and isinstance(enrollment, int) and enrollment >= 500:
        dim_magnitude = 4
    elif sponsor_class == "INDUSTRY" and isinstance(enrollment, int) and enrollment >= 200:
        dim_magnitude = 3

    # Dim 4: competitive_landscape (weight ×1.5) — neutral default; refined
    # downstream when a human/analyst writes the dossier.
    dim_competitive = 3

    # Dim 5: catalyst_timeline (weight ×1.0)
    pc_dt = _parse_date(primary_completion)
    days_until = _days_to(pc_dt) if pc_dt else 999
    if 0 <= days_until <= 45:
        dim_timeline = 5
    elif 0 <= days_until <= 75:
        dim_timeline = 4
    elif -14 <= days_until <= 90:
        dim_timeline = 3
    else:
        dim_timeline = 2

    # Dim 6: liquidity (weight ×1.0) — unknown at scanner level, default 3
    dim_liquidity = 3

    # Pattern count (5-pattern rubric from strategy doc)
    patterns_hit = 0
    pattern_names = []
    # Pattern 1 — trial design quality
    primary_outcomes = [po.get("measure", "") for po in (outcomes_mod.get("primaryOutcomes") or [])]
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
    # Pattern 4 — industry-sponsored (proxy for commercial intent)
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
        "dims": {
            "approval_probability": dim_approval,
            "market_mispricing": dim_mispricing,
            "magnitude": dim_magnitude,
            "competitive_landscape": dim_competitive,
            "catalyst_timeline": dim_timeline,
            "liquidity": dim_liquidity,
        },
        "patterns_hit": patterns_hit,
        "pattern_names": pattern_names,
    }


def _build_signal(scored: Dict[str, Any]) -> Dict[str, Any]:
    nct = scored["nct_id"]
    pcd = scored["primary_completion_date"] or ""
    sid = _sig_id(nct, pcd)
    chash = _content_hash(nct, scored["status"], pcd)
    sponsor = scored["sponsor_name"] or "Unknown sponsor"
    days = scored["days_until_readout"]
    when = f"T+{days}" if days >= 0 else f"T{days}"

    headline = f"Phase 3 readout {when}: {sponsor} — {scored['brief_title'][:90]}"
    summary = (
        f"{sponsor} ({scored['sponsor_class'] or 'n/a'}) Phase 3 trial {nct} "
        f"status={scored['status']}, primary completion {pcd} ({when}). "
        f"Indication: {scored['base_rate_key']} (base approval rate "
        f"{scored['base_rate_approval']*100:.0f}%). "
        f"Patterns hit: {scored['patterns_hit']}/5 "
        f"({', '.join(scored['pattern_names'])})."
    )

    # source_date: use primary completion if in the future (catalyst date) else today
    pc_dt = _parse_date(pcd)
    source_date = pcd if pcd else _iso()

    return {
        "signal_id": sid,
        "source_content_hash": chash,
        "scanner_source": NAME,
        "upstream_scanner": NAME,
        "scoring_profile": "binary_catalyst",
        "signal_type": "pre_phase3_readout",
        "thesis_direction": "long",
        "ticker": None,  # sponsor→ticker resolution is downstream via openfigi
        "figi": None,
        "issuer_figi": None,
        "company_name_en": sponsor,
        "scan_date": _iso(),
        "source_date": source_date,
        "headline": headline,
        "summary": summary,
        "raw_data": {
            "nct_id": nct,
            "trial_title": scored["brief_title"],
            "sponsor_class": scored["sponsor_class"],
            "status": scored["status"],
            "primary_completion_date": pcd,
            "days_until_readout": days,
            "enrollment": scored["enrollment"],
            "conditions": scored["conditions"],
            "primary_outcomes": scored["primary_outcomes"],
            "base_rate_key": scored["base_rate_key"],
            "approval_probability": scored["base_rate_approval"],
            "scanner_dims_suggested": scored["dims"],
            "patterns_hit": scored["patterns_hit"],
            "pattern_names": scored["pattern_names"],
            "source_url": f"https://clinicaltrials.gov/study/{nct}" if nct else None,
            # Auto-cap inputs for run_post_scan:
            "definitive_merger_agreement": False,
            "prior_failed_phase3_same_indication": False,
            # upside / downside / approval_probability for EV-floor auto-cap:
            "upside_pct": 50.0,   # heuristic pre-readout; dossier author refines
            "downside_pct": 35.0,
        },
    }


# --------------------------------------------------------------------
# Main scan
# --------------------------------------------------------------------

def scan() -> Dict[str, Any]:
    started = time.time()
    base_rates = _load_base_rates()
    errors: List[str] = []

    try:
        trials = _fetch_phase3_readout_trials()
    except Exception as e:
        return {
            "scanner": NAME,
            "ran_at_utc": _iso(),
            "status": "error",
            "signals": [],
            "error": f"fetch failed: {type(e).__name__}: {e}",
        }

    signals: List[Dict[str, Any]] = []
    below_gate = 0
    seen_nct = set()
    for t in trials:
        try:
            scored = _score_trial(t, base_rates)
        except Exception as e:
            errors.append(f"score: {type(e).__name__}: {e}")
            continue
        nct = scored["nct_id"]
        if not nct or nct in seen_nct:
            continue
        seen_nct.add(nct)
        # Triage gate: require >= 3 of 5 patterns per strategy spec
        if scored["patterns_hit"] < 3:
            below_gate += 1
            continue
        # Skip if industry sponsor missing (non-tradeable academic trials)
        if scored["sponsor_class"] != "INDUSTRY":
            below_gate += 1
            continue
        signals.append(_build_signal(scored))

    status = "ok"
    if errors and not signals:
        status = "error"
    elif errors:
        status = "partial"

    return {
        "scanner": NAME,
        "ran_at_utc": _iso(),
        "status": status,
        "signals": signals,
        "fetched_trials": len(trials),
        "below_triage_gate": below_gate,
        "unique_signals": len(signals),
        "errors": errors,
        "elapsed_s": round(time.time() - started, 2),
    }


def main():
    result = scan()
    tmp = OUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=2))
    os.replace(tmp, OUT_FILE)
    print(json.dumps({
        "signals": len(result["signals"]),
        "scanner": NAME,
        "status": result["status"],
        "fetched_trials": result.get("fetched_trials", 0),
        "below_gate": result.get("below_triage_gate", 0),
        "elapsed_s": result.get("elapsed_s"),
        "errors": result.get("errors", [])[:3],
    }))


if __name__ == "__main__":
    main()

# --- END OF FILE ---
