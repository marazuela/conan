"""
FDA PDUFA Calendar Pipeline  (v2.0 — 2026-04-10)
===================================================
Tracks FDA PDUFA target action dates for binary approval/rejection events.
Enriches with ClinicalTrials.gov trial data and openFDA approval history.
Auto-discovers new PDUFA dates from EDGAR 8-K filings.
Produces signals for approaching PDUFA dates.

Architecture:
1. PDUFA Watchlist (JSON) — auto-populated from EDGAR 8-K + manual curation
2. ClinicalTrials.gov v2 — Phase 3 trial data, endpoints, results
3. openFDA drugsfda — drug approval history, CRL precedents
4. Signal engine — generates signals based on proximity, data quality, coverage

Data Sources:
- ClinicalTrials.gov API v2: https://clinicaltrials.gov/api/v2/studies
- openFDA: https://api.fda.gov/drug/drugsfda.json
- Both free, no auth, verified accessible April 2026

Usage:
    python fda_pdufa_pipeline.py                   # Scan watchlist
    python fda_pdufa_pipeline.py --enrich          # Enrich watchlist with trial data
    python fda_pdufa_pipeline.py --add TICKER DRUG PDUFA_DATE  # Add to watchlist
    python fda_pdufa_pipeline.py --dry-run         # Print without saving
"""

import json
import os
import logging
import hashlib
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple

import requests

# Try to import from tools.mcap_cache; fall back to local import if running standalone
try:
    from tools.mcap_cache import get_market_cap_cached as _get_market_cap
except ImportError:
    from mcap_cache import get_market_cap_cached as _get_market_cap

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CLINICALTRIALS_URL = "https://clinicaltrials.gov/api/v2/studies"
OPENFDA_URL = "https://api.fda.gov/drug/drugsfda.json"
REQUEST_TIMEOUT = 15

# Monitoring windows
WINDOW_ACTIVE = 14      # Days — generate signals
WINDOW_WATCHLIST = 90   # Days — track on watchlist

# Triage
MARKET_CAP_FLOOR_MM = 215  # €200M ≈ $215M — minimum for liquidity

# Disqualification list — tickers + reasons that should be suppressed from signals.
# Added per D-039 to prevent known false positives from reappearing each session.
# Format: { "TICKER": "reason" }
DISQUALIFIED_TICKERS = {
    "ZLAB": "Augtyro already FDA-approved (Jun 2024). Scanner picks up China NMPA milestones.",
    "CORT": "Relacorilant (Lifyorli) approved early Mar 25, 2026. Not a pending PDUFA.",
    "ORCA": "Private company, not publicly traded. Cannot be actioned.",
}
# To add new disqualifications, append to this dict with a clear reason.
# To re-enable a ticker, remove it from this dict and document in DECISIONS.md.

# Output — default paths based on script location. Overridden by CLI main().
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SCRIPT_DIR)
SIGNALS_DIR = os.path.join(_PROJECT_DIR, "signals")
WATCHLIST_FILE = os.path.join(_PROJECT_DIR, "signals", "pdufa_watchlist.json")

logger = logging.getLogger("fda_pdufa_pipeline")


# ---------------------------------------------------------------------------
# Watchlist Management
# ---------------------------------------------------------------------------

def load_watchlist(filepath: str) -> List[dict]:
    """Load PDUFA watchlist from JSON file.

    Each entry:
    {
        "ticker": "ACME",
        "company_name": "Acme Biotech Inc",
        "drug_name": "acmecillin",
        "indication": "non-small cell lung cancer",
        "pdufa_date": "2026-06-15",
        "nda_type": "NDA",  # or BLA
        "application_number": "NDA213456",
        "phase3_nctid": "NCT12345678",
        "adcom_date": null,
        "adcom_vote": null,
        "is_resubmission": false,
        "crl_date": null,
        "notes": "Strong Phase 3 data, p<0.001",
        "added_date": "2026-04-01",
        "status": "active",  # active, approved, rejected, withdrawn
        "enrichment": {}  # Populated by enrich_watchlist()
    }
    """
    if filepath and os.path.exists(filepath):
        try:
            with open(filepath) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load watchlist: {e}")
    return []


def save_watchlist(entries: List[dict], filepath: str):
    """Save watchlist to JSON file."""
    if filepath:
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, "w") as f:
                json.dump(entries, f, indent=2)
            logger.info(f"Saved watchlist: {len(entries)} entries to {filepath}")
        except Exception as e:
            logger.warning(f"Failed to save watchlist: {e}")


def add_to_watchlist(entries: List[dict], ticker: str, drug_name: str,
                     pdufa_date: str, company_name: str = "",
                     indication: str = "", nda_type: str = "NDA",
                     application_number: str = "",
                     phase3_nctid: str = "",
                     is_resubmission: bool = False,
                     notes: str = "") -> List[dict]:
    """Add a new entry to the watchlist."""
    # Check for duplicate — two layers of protection:
    # 1. Exact match: same ticker + same drug_name + active status
    # 2. Auto-discover guard: if incoming drug_name is "(auto-discovered)",
    #    match on ticker alone — prevents auto-discover from adding duplicates
    #    when the curated entry has a real drug name.
    is_auto = drug_name == "(auto-discovered)"
    for e in entries:
        e_ticker = e.get("ticker", "")
        e_status = e.get("status", "")
        if e_ticker != ticker:
            continue
        # For auto-discovered entries, ANY existing entry for this ticker blocks addition
        if is_auto and e_status in ("active", "linked_to_GILD", "linked_to_TVTX",
                                     "resolved_crl", "approved", "non_tradeable",
                                     "killed", "excluded"):
            logger.info(f"Auto-discover blocked: {ticker} already on watchlist as '{e.get('drug_name', '')}'")
            if e_status == "active" and e.get("pdufa_date") != pdufa_date:
                e["pdufa_date"] = pdufa_date  # Still update date if changed
            return entries
        # For curated entries, require exact drug_name match
        if (not is_auto
                and e.get("drug_name", "").lower() == drug_name.lower()
                and e_status == "active"):
            logger.info(f"Already on watchlist: {ticker} / {drug_name}")
            e["pdufa_date"] = pdufa_date  # Update date if changed
            return entries

    entry = {
        "ticker": ticker.upper(),
        "company_name": company_name,
        "drug_name": drug_name,
        "indication": indication,
        "pdufa_date": pdufa_date,
        "nda_type": nda_type,
        "application_number": application_number,
        "phase3_nctid": phase3_nctid,
        "adcom_date": None,
        "adcom_vote": None,
        "is_resubmission": is_resubmission,
        "crl_date": None,
        "notes": notes,
        "added_date": datetime.now().strftime("%Y-%m-%d"),
        "status": "active",
        "enrichment": {},
    }
    entries.append(entry)
    logger.info(f"Added to watchlist: {ticker} / {drug_name} — PDUFA {pdufa_date}")
    return entries


# ---------------------------------------------------------------------------
# PDUFA Auto-Discovery (via EDGAR 8-K filings)
# ---------------------------------------------------------------------------

EDGAR_EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_UA = "InvestmentDiscovery admin@example.com"

import re

def discover_pdufa_from_edgar(lookback_days: int = 90,
                              existing_watchlist: List[dict] = None
                              ) -> List[dict]:
    """Auto-discover PDUFA dates by searching EDGAR 8-K filings.

    Companies announce FDA acceptance of their drug applications in 8-K
    filings, which include the PDUFA target action date. This function
    searches for those filings and extracts the dates.

    Returns list of dicts: {ticker, company_name, pdufa_date, source_url, file_date}
    """
    today = datetime.now()
    start = (today - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    try:
        r = requests.get(EDGAR_EFTS_URL, params={
            "q": '"PDUFA" "action date"',
            "dateRange": "custom",
            "startdt": start,
            "enddt": end,
            "forms": "8-K",
            "from": 0,
            "size": 50,
        }, headers={"User-Agent": EDGAR_UA}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        hits = r.json().get("hits", {}).get("hits", [])
    except Exception as e:
        logger.warning(f"EDGAR PDUFA discovery failed: {e}")
        return []

    # De-dup by ticker (keep first / most recent filing)
    companies = {}
    for h in hits:
        src = h.get("_source", {})
        names = src.get("display_names", [])
        if not names:
            continue
        name = names[0]
        ticker_match = re.search(r"\(([A-Z]+)", name)
        ticker = ticker_match.group(1) if ticker_match else ""
        if ticker and ticker not in companies:
            companies[ticker] = {
                "ticker": ticker,
                "name": name.split("(")[0].strip(),
                "cik": src.get("ciks", [""])[0],
                "adsh": src.get("adsh", ""),
                "file_date": src.get("file_date", ""),
                "file_id": h.get("_id", ""),
            }

    logger.info(f"EDGAR PDUFA discovery: {len(companies)} unique companies from {len(hits)} filings")

    # Extract PDUFA dates from filing text
    discovered = []
    existing_tickers = set()
    if existing_watchlist:
        # Consider ANY watchlist entry as "existing" regardless of status
        # (active, linked_to_TVTX, killed, excluded, etc.) — we've seen this
        # entry before, auto-discovery should not re-add it.
        existing_tickers = {e["ticker"] for e in existing_watchlist}

    for ticker, info in companies.items():
        pdufa_date = _extract_pdufa_date_from_filing(info["file_id"], info["cik"], info["adsh"])
        if not pdufa_date:
            continue
        # Skip past dates
        try:
            pd = datetime.strptime(pdufa_date, "%Y-%m-%d")
            if pd < today:
                continue
        except ValueError:
            continue

        discovered.append({
            "ticker": ticker,
            "company_name": info["name"],
            "pdufa_date": pdufa_date,
            "file_date": info["file_date"],
            "source": "edgar_8k",
            "is_new": ticker not in existing_tickers,
        })
        time.sleep(0.12)  # SEC rate limit

    new_count = sum(1 for d in discovered if d["is_new"])
    logger.info(f"PDUFA auto-discovery: {len(discovered)} future dates, {new_count} new entries")
    return discovered


def _extract_pdufa_date_from_filing(file_id: str, cik: str, adsh: str) -> Optional[str]:
    """Fetch an EDGAR filing and extract the PDUFA date from its text."""
    parts = file_id.split(":")
    if len(parts) != 2:
        return None
    filename = parts[1]
    cik_clean = cik.lstrip("0")
    adsh_nodash = adsh.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{adsh_nodash}/{filename}"

    try:
        r = requests.get(url, headers={"User-Agent": EDGAR_UA}, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return None
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"&[^;]+;", " ", text)
        text = re.sub(r"\s+", " ", text)

        patterns = [
            r"PDUFA[^.]{0,200}?(?:action date|target date|date)[^.]{0,100}?(?:of|for|is|set for|assigned|to)\s*(\w+ \d{1,2},?\s*\d{4})",
            r"(?:action date|target date)[^.]{0,100}?(\w+ \d{1,2},?\s*\d{4})",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                date_str = m.group(1).strip()
                for fmt in ["%B %d, %Y", "%B %d %Y"]:
                    try:
                        return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
                    except ValueError:
                        continue
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# ClinicalTrials.gov v2 API
# ---------------------------------------------------------------------------

def search_trials(query: str, max_results: int = 5) -> List[dict]:
    """Search ClinicalTrials.gov for trials matching query.

    Returns list of simplified trial dicts.
    """
    params = {
        "query.term": query,
        "pageSize": min(max_results, 20),
        "fields": (
            "NCTId,BriefTitle,OverallStatus,Phase,EnrollmentCount,"
            "StartDate,CompletionDate,PrimaryCompletionDate,"
            "LeadSponsorName,Condition,InterventionName,"
            "PrimaryOutcomeMeasure,StudyType"
        ),
    }

    try:
        resp = requests.get(CLINICALTRIALS_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"ClinicalTrials.gov search failed: {e}")
        return []

    data = resp.json()
    studies = data.get("studies", [])
    results = []

    for s in studies:
        proto = s.get("protocolSection", {})
        ident = proto.get("identificationModule", {})
        status_mod = proto.get("statusModule", {})
        design = proto.get("designModule", {})
        sponsor = proto.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {})
        conditions = proto.get("conditionsModule", {})
        interventions = proto.get("armsInterventionsModule", {})
        outcomes = proto.get("outcomesModule", {})

        # Extract phases
        phases = design.get("phases", [])

        # Extract primary outcomes
        primary_outcomes = []
        for po in (outcomes.get("primaryOutcomes") or []):
            primary_outcomes.append(po.get("measure", ""))

        # Extract interventions
        intervention_names = []
        for iv in (interventions.get("interventions") or []):
            intervention_names.append(iv.get("name", ""))

        results.append({
            "nct_id": ident.get("nctId", ""),
            "title": ident.get("briefTitle", ""),
            "status": status_mod.get("overallStatus", ""),
            "phases": phases,
            "enrollment": design.get("enrollmentInfo", {}).get("count"),
            "sponsor": sponsor.get("name", ""),
            "conditions": conditions.get("conditions", []),
            "interventions": intervention_names,
            "primary_outcomes": primary_outcomes[:3],
            "start_date": status_mod.get("startDateStruct", {}).get("date"),
            "completion_date": status_mod.get("completionDateStruct", {}).get("date"),
        })

    logger.info(f"ClinicalTrials.gov: {len(results)} results for '{query}'")
    return results


def get_trial_by_nctid(nct_id: str) -> Optional[dict]:
    """Fetch a specific trial by NCT ID."""
    if not nct_id:
        return None
    results = search_trials(nct_id, max_results=1)
    for r in results:
        if r.get("nct_id") == nct_id:
            return r
    return results[0] if results else None


# ---------------------------------------------------------------------------
# openFDA Drug Approval History
# ---------------------------------------------------------------------------

def search_drug_approvals(drug_name: str, max_results: int = 5) -> List[dict]:
    """Search openFDA for drug approval history.

    Returns list of simplified approval dicts.
    """
    params = {
        "search": f'openfda.brand_name:"{drug_name}"',
        "limit": max_results,
    }

    try:
        resp = requests.get(OPENFDA_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"openFDA search failed: {e}")
        return []

    data = resp.json()
    results = []

    for r in data.get("results", []):
        openfda = r.get("openfda", {})
        submissions = r.get("submissions", [])

        # Parse submissions
        sub_list = []
        for sub in submissions[:5]:
            sub_list.append({
                "type": sub.get("submission_type", ""),
                "number": sub.get("submission_number", ""),
                "status": sub.get("submission_status", ""),
                "status_date": sub.get("submission_status_date", ""),
                "review_priority": sub.get("review_priority", ""),
            })

        results.append({
            "application_number": r.get("application_number", ""),
            "sponsor_name": r.get("sponsor_name", ""),
            "brand_name": openfda.get("brand_name", [None])[0],
            "generic_name": openfda.get("generic_name", [None])[0],
            "substance_name": openfda.get("substance_name", [None])[0],
            "route": openfda.get("route", [None])[0],
            "product_type": openfda.get("product_type", [None])[0],
            "submissions": sub_list,
        })

    logger.info(f"openFDA: {len(results)} results for '{drug_name}'")
    return results


def search_sponsor_approvals(sponsor_name: str, max_results: int = 10) -> List[dict]:
    """Search openFDA for all drugs by a sponsor (company)."""
    params = {
        "search": f'sponsor_name:"{sponsor_name}"',
        "sort": "submissions.submission_status_date:desc",
        "limit": max_results,
    }
    try:
        resp = requests.get(OPENFDA_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [])
    except Exception as e:
        logger.error(f"openFDA sponsor search failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

def enrich_watchlist(entries: List[dict]) -> List[dict]:
    """Enrich watchlist entries with trial data and approval history.

    For each active entry:
    1. Search ClinicalTrials.gov for Phase 3 trial data
    2. Search openFDA for drug approval history
    3. Store enrichment data in entry["enrichment"]
    """
    call_count = 0
    MAX_CALLS = 30  # Rate limit guard

    for entry in entries:
        if entry.get("status") != "active":
            continue

        enrichment = entry.get("enrichment", {})

        # ClinicalTrials.gov — use NCT ID if available, else search
        if call_count < MAX_CALLS:
            nct_id = entry.get("phase3_nctid", "")
            if nct_id:
                trial = get_trial_by_nctid(nct_id)
                if trial:
                    enrichment["trial"] = trial
            else:
                # Search by drug name + company
                query = f"{entry.get('drug_name', '')} {entry.get('company_name', '')} phase 3"
                trials = search_trials(query, max_results=3)
                if trials:
                    enrichment["trials"] = trials
            call_count += 1
            time.sleep(0.5)

        # openFDA — search by drug name
        if call_count < MAX_CALLS and entry.get("drug_name"):
            approvals = search_drug_approvals(entry["drug_name"])
            if approvals:
                enrichment["fda_history"] = approvals
            call_count += 1
            time.sleep(0.5)

        entry["enrichment"] = enrichment

    logger.info(f"Enriched {sum(1 for e in entries if e.get('enrichment'))} entries ({call_count} API calls)")
    return entries


# ---------------------------------------------------------------------------
# Market cap
# ---------------------------------------------------------------------------

# DEPRECATED — use mcap_cache.get_market_cap_cached() instead
# def _get_market_cap(ticker: str) -> Optional[float]:
#     """Get market cap in millions via yfinance."""
#     if not ticker:
#         return None
#     try:
#         import yfinance as yf
#         stock = yf.Ticker(ticker)
#         info = stock.info
#         mcap = info.get("marketCap")
#         if mcap:
#             return mcap / 1_000_000
#     except Exception as e:
#         logger.debug(f"Market cap lookup failed for {ticker}: {e}")
#     return None


# ---------------------------------------------------------------------------
# FDA Approval Cross-Check (D-046 — prevents stale PDUFA entries)
# ---------------------------------------------------------------------------

def check_fda_approval_status(drug_name: str, company_name: str = "") -> Optional[dict]:
    """Query openFDA to check if a drug has already been approved.

    Returns dict with approval info if found, None otherwise.
    This catches early approvals (before PDUFA date) that would make
    watchlist entries stale.

    Added per D-046 after discovering CORT (relacorilant/Lifyorli) was
    approved March 25, 2026 but still listed as active PDUFA Jul 11.
    """
    if not drug_name:
        return None

    # Clean drug name for search (remove common suffixes/prefixes)
    clean_name = drug_name.lower().strip()
    for remove in ["(auto-discovered)", "snda", "bla", "nda"]:
        clean_name = clean_name.replace(remove, "").strip()

    if not clean_name or len(clean_name) < 3:
        return None

    try:
        # Search openFDA drugsfda endpoint
        r = requests.get(OPENFDA_URL, params={
            "search": f'openfda.generic_name:"{clean_name}" OR '
                      f'openfda.brand_name:"{clean_name}"',
            "limit": 5,
        }, headers={"User-Agent": EDGAR_UA}, timeout=REQUEST_TIMEOUT)

        if r.status_code != 200:
            return None

        data = r.json()
        results = data.get("results", [])

        for result in results:
            submissions = result.get("submissions", [])
            for sub in submissions:
                if sub.get("submission_status") == "AP":  # Approved
                    approval_date = sub.get("submission_status_date", "")
                    app_type = sub.get("submission_type", "")
                    app_number = result.get("application_number", "")
                    return {
                        "approved": True,
                        "approval_date": approval_date,
                        "application_type": app_type,
                        "application_number": app_number,
                        "drug_name": clean_name,
                    }
    except Exception as e:
        logger.debug(f"openFDA approval check failed for '{clean_name}': {e}")

    return None


def run_approval_crosscheck(watchlist: List[dict],
                            max_checks: int = 10,
                            total_timeout: float = 30.0) -> List[str]:
    """Check active watchlist entries against FDA approvals database.

    Returns list of tickers that were found to be already approved.
    Updates watchlist entries in place (sets status to 'approved').

    Limits to max_checks entries per run and total_timeout seconds
    to prevent scanner timeouts (S42 fix). Prioritizes entries with
    PDUFA dates in the past or within 30 days.
    """
    newly_approved = []
    check_count = 0
    start_time = time.time()

    # Sort active entries by PDUFA proximity — check soonest first
    active_entries = [e for e in watchlist if e.get("status") == "active"]
    active_entries.sort(key=lambda e: e.get("pdufa_date", "9999-99-99"))

    for entry in active_entries:
        if check_count >= max_checks:
            logger.debug(f"Approval crosscheck: hit max_checks ({max_checks}), stopping")
            break
        if time.time() - start_time > total_timeout:
            logger.debug(f"Approval crosscheck: hit total_timeout ({total_timeout}s), stopping")
            break

        ticker = entry.get("ticker", "")
        drug_name = entry.get("drug_name", "")

        # Skip if already in disqualification list
        if ticker in DISQUALIFIED_TICKERS:
            continue

        check_count += 1
        result = check_fda_approval_status(drug_name, entry.get("company_name", ""))
        if result and result.get("approved"):
            approval_date_str = result.get("approval_date", "")
            # Only flag if approval is RECENT (within 180 days before PDUFA)
            # to avoid false positives from prior-indication approvals.
            # 180 days covers early approvals (CORT was 108 days early).
            try:
                approval_dt = datetime.strptime(approval_date_str, "%Y%m%d")
                pdufa_dt = datetime.strptime(entry.get("pdufa_date", ""), "%Y-%m-%d")
                if (pdufa_dt - timedelta(days=180)) <= approval_dt <= pdufa_dt:
                    logger.info(
                        f"EARLY APPROVAL DETECTED: {ticker} ({drug_name}) approved "
                        f"{approval_date_str} — marking as approved"
                    )
                    entry["status"] = "approved"
                    entry["notes"] = (
                        entry.get("notes", "") +
                        f" | AUTO-DETECTED: Approved {approval_date_str} per openFDA. "
                        f"App# {result.get('application_number', 'N/A')}."
                    )
                    newly_approved.append(ticker)
                else:
                    logger.debug(
                        f"Prior approval for {ticker} ({drug_name}) on "
                        f"{approval_date_str} — likely different indication"
                    )
            except (ValueError, TypeError):
                logger.debug(f"Could not parse dates for {ticker}: {approval_date_str}")

    return newly_approved


# ---------------------------------------------------------------------------
# Signal Generation
# ---------------------------------------------------------------------------

def _days_until(date_str: str) -> Optional[int]:
    """Calculate days until a date string (YYYY-MM-DD)."""
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d")
        return (target - datetime.now()).days
    except (ValueError, TypeError):
        return None


def _assess_strength(entry: dict) -> int:
    """Assess signal strength based on available data.

    Scoring:
    - Base: 2 (approaching PDUFA date)
    - +1 if has Phase 3 trial data
    - +1 if trial completed with results
    - +1 if is resubmission (higher approval rate)
    - +1 if adcom vote is favorable
    - Cap at 5
    """
    strength = 2

    enrichment = entry.get("enrichment", {})
    trial = enrichment.get("trial") or (enrichment.get("trials", [None])[0] if enrichment.get("trials") else None)

    if trial:
        strength += 1
        if trial.get("status") in ("COMPLETED", "ACTIVE_NOT_RECRUITING"):
            strength += 1

    if entry.get("is_resubmission"):
        strength += 1

    adcom_vote = entry.get("adcom_vote")
    if adcom_vote and isinstance(adcom_vote, str):
        # Parse "12-1" format
        parts = adcom_vote.split("-")
        if len(parts) == 2:
            try:
                yes, no = int(parts[0]), int(parts[1])
                if yes > no * 2:
                    strength += 1
            except ValueError:
                pass

    return min(strength, 5)


def _signal_hash(ticker: str, drug: str) -> str:
    return hashlib.md5(f"{ticker}|{drug}|pdufa".encode()).hexdigest()


def _build_signal(entry: dict, days_until: int, strength: int,
                  market_cap_mm: float = 0) -> dict:
    """Build a PDUFA signal in common pipeline format."""
    raw_data = {
        "drug_name": entry.get("drug_name", ""),
        "indication": entry.get("indication", ""),
        "pdufa_date": entry.get("pdufa_date", ""),
        "days_until_pdufa": days_until,
        "nda_type": entry.get("nda_type", ""),
        "is_resubmission": entry.get("is_resubmission", False),
        "adcom_date": entry.get("adcom_date"),
        "adcom_vote": entry.get("adcom_vote"),
        "application_number": entry.get("application_number", ""),
        "notes": entry.get("notes", ""),
    }

    # Determine signal type based on proximity
    if days_until <= 7:
        signal_type = "pdufa_imminent"
    elif days_until <= 30:
        signal_type = "pdufa_approaching"
    else:
        signal_type = "pdufa_watchlist"

    return {
        "ticker": entry.get("ticker", ""),
        "isin": None,
        "company_name": entry.get("company_name", ""),
        "market_cap_mm": round(market_cap_mm, 1) if market_cap_mm else None,
        "signal_type": signal_type,
        "signal_category": "fda_pdufa",
        "strength_estimate": strength,
        "source_url": (f"https://clinicaltrials.gov/study/{entry.get('phase3_nctid', '')}"
                       if entry.get("phase3_nctid")
                       else "https://www.fda.gov/drugs"),
        "source_date": entry.get("pdufa_date", ""),
        "scan_date": datetime.now().strftime("%Y-%m-%d"),
        "raw_data": raw_data,
    }


# ---------------------------------------------------------------------------
# Main Scan
# ---------------------------------------------------------------------------

def run_scan(market_cap_filter: bool = True,
             save_signals: bool = True,
             auto_discover: bool = True) -> List[dict]:
    """Scan PDUFA watchlist and generate signals for approaching dates.

    If auto_discover=True (default), first runs EDGAR 8-K auto-discovery
    to find and add new PDUFA dates to the watchlist.

    Returns list of signal dicts.
    """
    watchlist = load_watchlist(WATCHLIST_FILE)

    # Safety: if watchlist loaded with suspiciously few entries, something may
    # have corrupted the file. Log a warning and prevent auto-discover from
    # saving (which could overwrite a backup restoration).
    MIN_EXPECTED_ENTRIES = 20  # We maintain ~40 curated entries
    watchlist_possibly_corrupt = len(watchlist) < MIN_EXPECTED_ENTRIES
    if watchlist_possibly_corrupt:
        logger.warning(
            f"Watchlist has only {len(watchlist)} entries (expected >={MIN_EXPECTED_ENTRIES}). "
            f"Possible file corruption — auto-discover will NOT save changes.")

    # Auto-discover new PDUFA dates from EDGAR 8-K filings
    if auto_discover:
        try:
            discovered = discover_pdufa_from_edgar(
                lookback_days=90, existing_watchlist=watchlist)
            new_entries = [d for d in discovered if d.get("is_new")]
            if new_entries and not watchlist_possibly_corrupt:
                for d in new_entries:
                    watchlist = add_to_watchlist(
                        watchlist,
                        ticker=d["ticker"],
                        drug_name="(auto-discovered)",
                        pdufa_date=d["pdufa_date"],
                        company_name=d["company_name"],
                        notes=f"Auto-discovered from EDGAR 8-K filed {d['file_date']}",
                    )
                save_watchlist(watchlist, WATCHLIST_FILE)
                logger.info(f"Auto-discovery added {len(new_entries)} new watchlist entries")
            elif new_entries and watchlist_possibly_corrupt:
                logger.warning(
                    f"Skipping save of {len(new_entries)} auto-discovered entries "
                    f"due to possible watchlist corruption")
            # Also update dates for existing entries if EDGAR has newer date
            updated_any = False
            for d in discovered:
                if not d.get("is_new"):
                    for e in watchlist:
                        if (e["ticker"] == d["ticker"]
                                and e.get("status") == "active"
                                and e.get("pdufa_date") != d["pdufa_date"]
                                and d["file_date"] > e.get("added_date", "")):
                            old_date = e["pdufa_date"]
                            e["pdufa_date"] = d["pdufa_date"]
                            e["notes"] = (e.get("notes", "") +
                                f" | Date updated {old_date}->{d['pdufa_date']} "
                                f"per 8-K filed {d['file_date']}")
                            logger.info(f"Updated {d['ticker']} PDUFA: {old_date} -> {d['pdufa_date']}")
                            updated_any = True
            if updated_any:
                save_watchlist(watchlist, WATCHLIST_FILE)
        except Exception as e:
            logger.warning(f"Auto-discovery failed (non-fatal): {e}")

    # D-046: Cross-check watchlist against FDA approvals database
    # to detect early approvals and prevent stale PDUFA entries
    try:
        newly_approved = run_approval_crosscheck(watchlist)
        if newly_approved:
            save_watchlist(watchlist, WATCHLIST_FILE)
            logger.info(
                f"Approval cross-check: {len(newly_approved)} entries marked approved: "
                f"{', '.join(newly_approved)}"
            )
    except Exception as e:
        logger.debug(f"Approval cross-check failed (non-fatal): {e}")

    if not watchlist:
        logger.info("Watchlist is empty — no signals to generate.")
        return []

    all_signals = []

    for entry in watchlist:
        if entry.get("status") != "active":
            continue

        pdufa_date = entry.get("pdufa_date", "")
        days = _days_until(pdufa_date)
        if days is None:
            continue

        # Only signal for upcoming dates within watchlist window
        if days < 0 or days > WINDOW_WATCHLIST:
            continue

        ticker = entry.get("ticker", "")

        # Disqualification filter (D-039)
        if ticker in DISQUALIFIED_TICKERS:
            logger.debug(f"Disqualified: {ticker} — {DISQUALIFIED_TICKERS[ticker]}")
            continue

        # Market cap triage
        market_cap_mm = 0
        if market_cap_filter and ticker:
            market_cap_mm = _get_market_cap(ticker) or 0
            if 0 < market_cap_mm < MARKET_CAP_FLOOR_MM:
                logger.debug(f"Below market cap floor: {ticker} ${market_cap_mm:.0f}M")
                continue

        # Assess strength
        strength = _assess_strength(entry)

        # Boost for imminent dates
        if days <= 7:
            strength = min(strength + 1, 5)

        signal = _build_signal(entry, days, strength, market_cap_mm)
        all_signals.append(signal)

    # Save signals
    if save_signals and SIGNALS_DIR and all_signals:
        os.makedirs(SIGNALS_DIR, exist_ok=True)
        output_file = os.path.join(
            SIGNALS_DIR,
            f"fda_pdufa_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        )
        with open(output_file, "w") as f:
            json.dump(all_signals, f, indent=2)
        logger.info(f"Saved {len(all_signals)} signals to {output_file}")

    return all_signals


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="FDA PDUFA Calendar Pipeline")
    parser.add_argument("--add", nargs=3, metavar=("TICKER", "DRUG", "PDUFA_DATE"),
                        help="Add entry: TICKER DRUG YYYY-MM-DD")
    parser.add_argument("--enrich", action="store_true",
                        help="Enrich watchlist with ClinicalTrials + openFDA data")
    parser.add_argument("--discover", action="store_true",
                        help="Run EDGAR 8-K PDUFA auto-discovery only")
    parser.add_argument("--no-discover", action="store_true",
                        help="Skip auto-discovery, use existing watchlist only")
    parser.add_argument("--no-market-cap", action="store_true",
                        help="Disable market cap filtering")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan without saving signals")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    global SIGNALS_DIR, WATCHLIST_FILE
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    SIGNALS_DIR = os.path.join(project_dir, "signals")
    WATCHLIST_FILE = os.path.join(project_dir, "signals", "pdufa_watchlist.json")

    # Add entry
    if args.add:
        ticker, drug, pdufa_date = args.add
        watchlist = load_watchlist(WATCHLIST_FILE)
        watchlist = add_to_watchlist(watchlist, ticker, drug, pdufa_date)
        save_watchlist(watchlist, WATCHLIST_FILE)
        return

    # Enrich
    if args.enrich:
        watchlist = load_watchlist(WATCHLIST_FILE)
        watchlist = enrich_watchlist(watchlist)
        save_watchlist(watchlist, WATCHLIST_FILE)
        return

    # Discover only
    if args.discover:
        watchlist = load_watchlist(WATCHLIST_FILE)
        discovered = discover_pdufa_from_edgar(90, watchlist)
        print(f"\nDiscovered {len(discovered)} PDUFA dates from EDGAR 8-K filings:")
        for d in discovered:
            marker = "NEW" if d["is_new"] else "existing"
            print(f"  {d['ticker']:6s} | {d['company_name'][:30]:30s} | {d['pdufa_date']} | {marker}")
        return

    # Run scan (default action)
    signals = run_scan(
        market_cap_filter=not args.no_market_cap,
        save_signals=not args.dry_run,
        auto_discover=not args.no_discover,
    )

    print(f"\nFDA PDUFA Pipeline — {len(signals)} signals")
    for s in sorted(signals, key=lambda x: x.get("raw_data", {}).get("days_until_pdufa", 999)):
        rd = s["raw_data"]
        days = rd.get("days_until_pdufa", "?")
        drug = rd.get("drug_name", "")[:25]
        ticker = s.get("ticker", "")
        strength = s.get("strength_estimate", 0)
        stype = s.get("signal_type", "")
        print(f"  [{strength}] {ticker:6s} | {drug:25s} | T-{days:>3} days | {stype}")


if __name__ == "__main__":
    main()

# --- END OF FILE ---
