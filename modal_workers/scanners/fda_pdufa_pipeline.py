"""
FDA PDUFA Pipeline scanner — Modal port of tools/fda_pdufa_pipeline.py.

Preservation (v1 parity):
  - Watchlist-driven architecture: long-lived (ticker, drug, pdufa_date) entries, enriched
    with ClinicalTrials.gov v2 trial data + openFDA drug approval history.
  - EDGAR 8-K auto-discovery: search EFTS for "PDUFA action date" + regex-extract the date
    from filing text; append novel tickers to the watchlist.
  - openFDA early-approval cross-check (D-046): any active watchlist entry whose drug has
    already been approved within 180d before PDUFA is marked status='approved'.
  - DISQUALIFIED_TICKERS dict (D-039) preserved verbatim as a module-level constant.
  - Strength heuristics (`_assess_strength`) preserved: base 2, +1 for trial data, +1 for
    completed trial, +1 for resubmission, +1 for favourable adcom vote; imminent (<=7d)
    gets +1 again, capped at 5.
  - WINDOW_ACTIVE (14d) / WINDOW_WATCHLIST (90d) preserved.
  - Signal subtypes preserved: pdufa_imminent (<=7d) / pdufa_approaching (<=30d) /
    pdufa_watchlist (<=90d). Plus adcom_scheduled, clinical_readout, fda_decision for
    state-triggered variants. v2 additionally surfaces recent PDUFA date moves as
    pdufa_date_advanced / pdufa_date_delayed so decision-adjacent shifts are visible
    without waiting for the final FDA action. signal_type_profile_map on the scanner
    row routes these to binary_catalyst.

Deviations from v1:
  - No OUT_FILE / no CLI: single scan(cfg) public entrypoint; --enrich / --add / --dry-run
    are gone. Auto-discover + auto-enrich run unconditionally unless gated via cfg.config.
  - Watchlist migrated from signals/pdufa_watchlist.json (local file) to Supabase Storage
    scanner-caches/fda/pdufa_watchlist.json via SupabaseClient read_cache / write_cache.
    openFDA approval-history responses cached under scanner-caches/fda/approvals/{drug}.json
    with a 7-day TTL to keep the wall-clock under 60s when the watchlist is dense.
  - Market cap floor (D-003) dropped — scanner emits all signals regardless of mcap.
    Downstream auto-caps + market_cap_floor_usd_mm on the rubric gate the dashboard.
  - source_content_hash now carries the spec.md §3.4 "sha256:<64hex>" prefix, keyed on
    (ticker, drug, pdufa_date, signal_subtype) so each subtype is a separate convergence bucket.
  - EntityHints(ticker=..., mic=None, country="US") — MIC not known at emit-time; the
    entity_identifiers cascade resolves XNAS/XNYS downstream. OpenFIGI resolved lazily
    (best-effort) just like edgar_filing_monitor.

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
    - raises MissingAuthError if SEC_USER_AGENT env unset (required for EDGAR 8-K discovery).
    - ClinicalTrials.gov + openFDA are public / unauthenticated.
    - Uses cfg.timeout_soft_s (60s default per registry) as the wall-clock budget. Budget
      is spent across auto-discovery, enrichment, approval cross-check, and signal build.
    - Returns signals for every active watchlist entry with PDUFA <= WINDOW_WATCHLIST days
      out, minus DISQUALIFIED_TICKERS.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from modal_workers.shared.scanner_base import MissingAuthError, Signal, ScannerResult
from modal_workers.shared.supabase_client import (
    EntityHints,
    ScannerConfig,
    SupabaseClient,
    SupabaseError,
)

NAME = "fda_pdufa_pipeline"

logger = logging.getLogger(NAME)

# ---------------------------------------------------------------------------
# Endpoints + constants (verbatim from v1)
# ---------------------------------------------------------------------------

CLINICALTRIALS_URL = "https://clinicaltrials.gov/api/v2/studies"
OPENFDA_URL = "https://api.fda.gov/drug/drugsfda.json"
EDGAR_EFTS_URL = "https://efts.sec.gov/LATEST/search-index"

REQUEST_TIMEOUT = 15

# Monitoring windows (days)
WINDOW_ACTIVE = 14      # signals at full strength
WINDOW_WATCHLIST = 90   # tracked-only threshold

# Approval cache TTL — openFDA data rarely changes minute-to-minute; 7d is plenty.
APPROVAL_CACHE_TTL_S = 7 * 24 * 3600

# Minimum plausible watchlist size — if we read back fewer entries than this the file is
# likely corrupted (storage hiccup, partial write). We bail out of auto-discover saves to
# avoid overwriting a recoverable backup. v1 parity.
MIN_EXPECTED_ENTRIES = 20

# Disqualification list (D-039) — preserved verbatim from v1. Pedro edits this in-place.
# Format: {"TICKER": "reason"}
DISQUALIFIED_TICKERS: Dict[str, str] = {
    "ZLAB": "Augtyro already FDA-approved (Jun 2024). Scanner picks up China NMPA milestones.",
    "CORT": "Relacorilant (Lifyorli) approved early Mar 25, 2026. Not a pending PDUFA.",
    "ORCA": "Private company, not publicly traded. Cannot be actioned.",
}

# Signal type routing — all PDUFA / AdCom / FDA decision-adjacent subtypes route to
# binary_catalyst via the scanner row's signal_type_profile_map. Keep the list here
# for subtype selection logic.
SIGNAL_TYPE_IMMINENT = "pdufa_imminent"          # <= 7 days
SIGNAL_TYPE_APPROACHING = "pdufa_approaching"     # <= 30 days
SIGNAL_TYPE_WATCHLIST = "pdufa_watchlist"         # <= 90 days
SIGNAL_TYPE_DATE_ADVANCED = "pdufa_date_advanced"
SIGNAL_TYPE_DATE_DELAYED = "pdufa_date_delayed"
SIGNAL_TYPE_ADCOM = "adcom_scheduled"
SIGNAL_TYPE_READOUT = "clinical_readout"
SIGNAL_TYPE_DECISION = "fda_decision"
DATE_CHANGE_SIGNAL_WINDOW_DAYS = 14

# Thesis direction — long by default (approval is upside), short when we see confirmed
# CRL / near-term rejection risk. We detect this by scanning notes / crl_date fields that
# v1 populates when a CRL lands.
DIRECTION_LONG = "long"
DIRECTION_SHORT = "short"


# ---------------------------------------------------------------------------
# Watchlist I/O (Supabase Storage)
# ---------------------------------------------------------------------------

_WATCHLIST_KEY = "pdufa_watchlist.json"


def _load_watchlist(client: SupabaseClient) -> List[dict]:
    raw = client.read_cache("fda", _WATCHLIST_KEY)
    if raw is None:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, UnicodeDecodeError):
        logger.warning("pdufa_watchlist.json could not be decoded; treating as empty")
        return []


def _save_watchlist(client: SupabaseClient, entries: List[dict]) -> None:
    try:
        client.write_cache(
            "fda", _WATCHLIST_KEY,
            json.dumps(entries, indent=2).encode("utf-8"),
            content_type="application/json",
        )
    except SupabaseError as e:
        logger.warning(f"Failed to save watchlist: {e}")


def _classify_date_change(old_date: Optional[str], new_date: str) -> Optional[str]:
    try:
        old_dt = datetime.strptime(old_date or "", "%Y-%m-%d")
        new_dt = datetime.strptime(new_date, "%Y-%m-%d")
    except ValueError:
        return None
    if new_dt > old_dt:
        return "delayed"
    if new_dt < old_dt:
        return "advanced"
    return None


def _apply_pdufa_date_update(entry: dict, new_date: str) -> bool:
    old_date = entry.get("pdufa_date")
    if old_date == new_date:
        return False

    change_kind = _classify_date_change(old_date, new_date)
    entry["previous_pdufa_date"] = old_date
    entry["pdufa_date"] = new_date
    entry["pdufa_date_change_kind"] = change_kind
    entry["pdufa_date_changed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return True


def _recent_pdufa_date_change_kind(entry: dict) -> Optional[str]:
    kind = entry.get("pdufa_date_change_kind")
    changed_at = entry.get("pdufa_date_changed_at")
    if kind not in ("advanced", "delayed") or not changed_at:
        return None
    days = _days_until(changed_at)
    if days is None:
        return None
    age_days = abs(days)
    if age_days > DATE_CHANGE_SIGNAL_WINDOW_DAYS:
        return None
    return kind


def _add_to_watchlist(entries: List[dict], ticker: str, drug_name: str,
                      pdufa_date: str, company_name: str = "",
                      indication: str = "", nda_type: str = "NDA",
                      application_number: str = "",
                      phase3_nctid: str = "",
                      is_resubmission: bool = False,
                      notes: str = "") -> List[dict]:
    """Append a new watchlist entry with the v1 dedup contract:
       1. Auto-discovered entries (drug_name == '(auto-discovered)') are blocked if any
          prior entry for the ticker exists, in any status.
       2. Curated entries dedup on (ticker, lowercased drug_name, status=='active').
       In both cases the pdufa_date is refreshed if it changed.
    """
    is_auto = drug_name == "(auto-discovered)"
    blocking_statuses = ("active", "linked_to_GILD", "linked_to_TVTX",
                         "resolved_crl", "approved", "non_tradeable",
                         "killed", "excluded")
    for e in entries:
        if e.get("ticker") != ticker:
            continue
        status = e.get("status", "")
        if is_auto and status in blocking_statuses:
            if status == "active" and e.get("pdufa_date") != pdufa_date:
                _apply_pdufa_date_update(e, pdufa_date)
            return entries
        if (not is_auto
                and e.get("drug_name", "").lower() == drug_name.lower()
                and status == "active"):
            _apply_pdufa_date_update(e, pdufa_date)
            return entries

    entries.append({
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
        "previous_pdufa_date": None,
        "pdufa_date_change_kind": None,
        "pdufa_date_changed_at": None,
        "notes": notes,
        "added_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "status": "active",
        "enrichment": {},
    })
    return entries


# ---------------------------------------------------------------------------
# EDGAR 8-K PDUFA auto-discovery
# ---------------------------------------------------------------------------

def _discover_pdufa_from_edgar(existing_watchlist: List[dict],
                               user_agent: str,
                               lookback_days: int = 90) -> List[dict]:
    """Query EFTS for 8-K filings mentioning "PDUFA" + "action date" and extract dates.

    Returns a list of {ticker, company_name, cik, pdufa_date, file_date, is_new} dicts.
    New = ticker not already on the watchlist (any status).
    """
    today = datetime.now(timezone.utc)
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
        }, headers={"User-Agent": user_agent}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        hits = r.json().get("hits", {}).get("hits", [])
    except Exception as e:
        logger.warning(f"EDGAR PDUFA discovery failed: {e}")
        return []

    # Dedup by ticker (keep first / most recent filing)
    companies: Dict[str, Dict[str, str]] = {}
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
                "cik": src.get("ciks", [""])[0] if src.get("ciks") else "",
                "adsh": src.get("adsh", ""),
                "file_date": src.get("file_date", ""),
                "file_id": h.get("_id", ""),
            }

    existing_tickers = {e.get("ticker") for e in existing_watchlist}
    discovered: List[dict] = []

    for ticker, info in companies.items():
        pdufa_date = _extract_pdufa_date_from_filing(
            info["file_id"], info["cik"], info["adsh"], user_agent=user_agent,
        )
        if not pdufa_date:
            continue
        try:
            pd = datetime.strptime(pdufa_date, "%Y-%m-%d")
            if pd < today.replace(tzinfo=None):
                continue
        except ValueError:
            continue

        discovered.append({
            "ticker": ticker,
            "company_name": info["name"],
            "cik": info["cik"],
            "pdufa_date": pdufa_date,
            "file_date": info["file_date"],
            "source": "edgar_8k",
            "is_new": ticker not in existing_tickers,
        })
        time.sleep(0.12)  # SEC rate limit headroom

    return discovered


def _extract_pdufa_date_from_filing(file_id: str, cik: str, adsh: str,
                                    *, user_agent: str) -> Optional[str]:
    """Fetch an 8-K body and regex-extract the PDUFA target action date."""
    parts = file_id.split(":")
    if len(parts) != 2:
        return None
    filename = parts[1]
    cik_clean = cik.lstrip("0") or "0"
    adsh_nodash = adsh.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{adsh_nodash}/{filename}"

    try:
        r = requests.get(url, headers={"User-Agent": user_agent}, timeout=REQUEST_TIMEOUT)
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
                for fmt in ("%B %d, %Y", "%B %d %Y"):
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

def _search_trials(query: str, max_results: int = 5) -> List[dict]:
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
        logger.debug(f"ClinicalTrials.gov search failed: {e}")
        return []

    data = resp.json()
    studies = data.get("studies", [])
    results: List[dict] = []
    for s in studies:
        proto = s.get("protocolSection", {})
        ident = proto.get("identificationModule", {})
        status_mod = proto.get("statusModule", {})
        design = proto.get("designModule", {})
        sponsor = proto.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {})
        conditions = proto.get("conditionsModule", {})
        interventions = proto.get("armsInterventionsModule", {})
        outcomes = proto.get("outcomesModule", {})
        phases = design.get("phases", [])
        primary_outcomes = [po.get("measure", "") for po in (outcomes.get("primaryOutcomes") or [])]
        intervention_names = [iv.get("name", "") for iv in (interventions.get("interventions") or [])]
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
    return results


def _get_trial_by_nctid(nct_id: str) -> Optional[dict]:
    if not nct_id:
        return None
    results = _search_trials(nct_id, max_results=1)
    for r in results:
        if r.get("nct_id") == nct_id:
            return r
    return results[0] if results else None


# ---------------------------------------------------------------------------
# openFDA drug approvals (cached)
# ---------------------------------------------------------------------------

def _approval_cache_key(drug_name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", drug_name.lower().strip())[:64]
    return f"approvals/{safe}.json"


def _read_approval_cache(client: SupabaseClient, drug_name: str) -> Optional[List[dict]]:
    raw = client.read_cache("fda", _approval_cache_key(drug_name))
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        ts = payload.get("cached_at", 0)
        if time.time() - ts > APPROVAL_CACHE_TTL_S:
            return None
        return payload.get("results") or []
    except (ValueError, UnicodeDecodeError):
        return None


def _write_approval_cache(client: SupabaseClient, drug_name: str, results: List[dict]) -> None:
    try:
        client.write_cache(
            "fda", _approval_cache_key(drug_name),
            json.dumps({"cached_at": time.time(), "results": results}).encode("utf-8"),
            content_type="application/json",
        )
    except SupabaseError:
        pass  # best-effort


def _search_drug_approvals(drug_name: str, client: SupabaseClient,
                           max_results: int = 5) -> List[dict]:
    cached = _read_approval_cache(client, drug_name)
    if cached is not None:
        return cached

    params = {
        "search": f'openfda.brand_name:"{drug_name}"',
        "limit": max_results,
    }
    try:
        resp = requests.get(OPENFDA_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.debug(f"openFDA search failed for '{drug_name}': {e}")
        return []

    data = resp.json()
    results: List[dict] = []
    for r in data.get("results", []):
        openfda = r.get("openfda", {})
        submissions = r.get("submissions", [])
        sub_list = [{
            "type": sub.get("submission_type", ""),
            "number": sub.get("submission_number", ""),
            "status": sub.get("submission_status", ""),
            "status_date": sub.get("submission_status_date", ""),
            "review_priority": sub.get("review_priority", ""),
        } for sub in submissions[:5]]
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

    _write_approval_cache(client, drug_name, results)
    return results


def _check_fda_approval_status(drug_name: str, user_agent: str,
                               client: SupabaseClient) -> Optional[dict]:
    """openFDA lookup — returns {approved, approval_date, application_type, application_number}
    for the most recent AP submission, or None. Used by early-approval cross-check (D-046)."""
    if not drug_name:
        return None
    clean_name = drug_name.lower().strip()
    for remove in ("(auto-discovered)", "snda", "bla", "nda"):
        clean_name = clean_name.replace(remove, "").strip()
    if not clean_name or len(clean_name) < 3:
        return None

    # Use the cache for the approval crosscheck as well — same endpoint, same data.
    cached = _read_approval_cache(client, clean_name)
    if cached is not None:
        return _first_approval(cached, clean_name)

    try:
        r = requests.get(OPENFDA_URL, params={
            "search": f'openfda.generic_name:"{clean_name}" OR '
                      f'openfda.brand_name:"{clean_name}"',
            "limit": 5,
        }, headers={"User-Agent": user_agent}, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        results = data.get("results", [])
    except Exception as e:
        logger.debug(f"openFDA approval check failed for '{clean_name}': {e}")
        return None

    # Normalise to the cache shape for consistency.
    normalised: List[dict] = []
    for result in results:
        subs = result.get("submissions", [])
        normalised.append({
            "application_number": result.get("application_number", ""),
            "submissions": [{
                "type": s.get("submission_type", ""),
                "status": s.get("submission_status", ""),
                "status_date": s.get("submission_status_date", ""),
            } for s in subs[:5]],
        })
    _write_approval_cache(client, clean_name, normalised)
    return _first_approval(normalised, clean_name)


def _first_approval(results: List[dict], clean_name: str) -> Optional[dict]:
    for result in results:
        for sub in result.get("submissions", []):
            if sub.get("status") == "AP" or sub.get("submission_status") == "AP":
                return {
                    "approved": True,
                    "approval_date": sub.get("status_date") or sub.get("submission_status_date", ""),
                    "application_type": sub.get("type") or sub.get("submission_type", ""),
                    "application_number": result.get("application_number", ""),
                    "drug_name": clean_name,
                }
    return None


# ---------------------------------------------------------------------------
# Enrichment + approval cross-check
# ---------------------------------------------------------------------------

def _enrich_watchlist(entries: List[dict], client: SupabaseClient,
                      budget_deadline: float, max_calls: int = 30) -> int:
    """Populate entry['enrichment'] with ClinicalTrials.gov + openFDA data.
    Returns number of API calls made. Stops when budget_deadline (monotonic time) passes
    or max_calls is reached."""
    call_count = 0
    for entry in entries:
        if time.time() > budget_deadline or call_count >= max_calls:
            break
        if entry.get("status") != "active":
            continue
        enrichment = entry.get("enrichment", {}) or {}

        # ClinicalTrials.gov
        nct_id = entry.get("phase3_nctid", "")
        if nct_id:
            trial = _get_trial_by_nctid(nct_id)
            if trial:
                enrichment["trial"] = trial
        else:
            query = f"{entry.get('drug_name', '')} {entry.get('company_name', '')} phase 3"
            trials = _search_trials(query, max_results=3)
            if trials:
                enrichment["trials"] = trials
        call_count += 1

        if time.time() > budget_deadline or call_count >= max_calls:
            entry["enrichment"] = enrichment
            break

        # openFDA history
        drug_name = entry.get("drug_name", "")
        if drug_name and drug_name != "(auto-discovered)":
            approvals = _search_drug_approvals(drug_name, client)
            if approvals:
                enrichment["fda_history"] = approvals
            call_count += 1

        entry["enrichment"] = enrichment
    return call_count


def _run_approval_crosscheck(watchlist: List[dict], user_agent: str,
                             client: SupabaseClient,
                             max_checks: int = 10,
                             budget_deadline: Optional[float] = None) -> List[str]:
    """D-046: mark watchlist entries as 'approved' if openFDA shows an AP within 180d
    before the PDUFA date. Returns list of tickers newly marked."""
    newly_approved: List[str] = []
    check_count = 0
    active = sorted(
        (e for e in watchlist if e.get("status") == "active"),
        key=lambda e: e.get("pdufa_date", "9999-99-99"),
    )
    for entry in active:
        if check_count >= max_checks:
            break
        if budget_deadline is not None and time.time() > budget_deadline:
            break
        ticker = entry.get("ticker", "")
        if ticker in DISQUALIFIED_TICKERS:
            continue
        drug_name = entry.get("drug_name", "")
        check_count += 1
        result = _check_fda_approval_status(drug_name, user_agent, client)
        if not result or not result.get("approved"):
            continue
        approval_date_str = result.get("approval_date", "")
        try:
            approval_dt = datetime.strptime(approval_date_str, "%Y%m%d")
            pdufa_dt = datetime.strptime(entry.get("pdufa_date", ""), "%Y-%m-%d")
            if (pdufa_dt - timedelta(days=180)) <= approval_dt <= pdufa_dt:
                entry["status"] = "approved"
                entry["notes"] = (entry.get("notes", "") +
                    f" | AUTO-DETECTED: Approved {approval_date_str} per openFDA. "
                    f"App# {result.get('application_number', 'N/A')}.")
                newly_approved.append(ticker)
        except (ValueError, TypeError):
            pass
    return newly_approved


# ---------------------------------------------------------------------------
# Strength + signal builder
# ---------------------------------------------------------------------------

def _days_until(date_str: str) -> Optional[int]:
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d")
        return (target - datetime.now()).days
    except (ValueError, TypeError):
        return None


def _assess_strength(entry: dict) -> int:
    """Score 2-5 based on data quality. v1 parity."""
    strength = 2
    enrichment = entry.get("enrichment", {}) or {}
    trials_list = enrichment.get("trials") or []
    trial = enrichment.get("trial") or (trials_list[0] if trials_list else None)
    if trial:
        strength += 1
        if trial.get("status") in ("COMPLETED", "ACTIVE_NOT_RECRUITING"):
            strength += 1
    if entry.get("is_resubmission"):
        strength += 1
    adcom_vote = entry.get("adcom_vote")
    if adcom_vote and isinstance(adcom_vote, str):
        parts = adcom_vote.split("-")
        if len(parts) == 2:
            try:
                yes, no = int(parts[0]), int(parts[1])
                if yes > no * 2:
                    strength += 1
            except ValueError:
                pass
    return min(strength, 5)


def _adcom_support_ratio(vote: Any) -> Optional[float]:
    if isinstance(vote, str):
        parts = vote.split("-")
        if len(parts) == 2:
            try:
                yes, no = int(parts[0]), int(parts[1])
            except ValueError:
                return None
            total = yes + no
            if total > 0:
                return yes / total
    return None


def _approval_history_count(history: Any) -> int:
    if not isinstance(history, list):
        return 0
    count = 0
    for item in history:
        if not isinstance(item, dict):
            continue
        for sub in item.get("submissions") or []:
            if not isinstance(sub, dict):
                continue
            if (sub.get("status") or sub.get("submission_status")) == "AP":
                count += 1
                break
    return count


def _classify_subtype(entry: dict, days: int) -> str:
    """Pick the signal subtype. Priority:
    1. fda_decision if approved / CRL has landed (crl_date or status in approved/CRL).
    2. recent PDUFA date move (advanced / delayed) within the alert window.
    3. adcom_scheduled if adcom_date is set and within window.
    4. pdufa_{imminent,approaching,watchlist} by proximity.
    (clinical_readout is emitted when the enrichment trial has PrimaryCompletion within window.)
    """
    status = entry.get("status", "")
    if status in ("approved",) or entry.get("crl_date"):
        return SIGNAL_TYPE_DECISION
    date_change_kind = _recent_pdufa_date_change_kind(entry)
    if date_change_kind == "advanced":
        return SIGNAL_TYPE_DATE_ADVANCED
    if date_change_kind == "delayed":
        return SIGNAL_TYPE_DATE_DELAYED
    adcom_date = entry.get("adcom_date")
    if adcom_date:
        adcom_days = _days_until(adcom_date)
        if adcom_days is not None and 0 <= adcom_days <= WINDOW_WATCHLIST:
            return SIGNAL_TYPE_ADCOM
    enrichment = entry.get("enrichment", {}) or {}
    trial = enrichment.get("trial") or None
    if trial:
        pc = trial.get("completion_date") or ""
        try:
            pc_dt = datetime.strptime(pc[:10], "%Y-%m-%d")
            pc_days = (pc_dt - datetime.now()).days
            # Readout imminent if completion within 30d and the trial isn't completed yet.
            if 0 <= pc_days <= 30 and trial.get("status") != "COMPLETED":
                return SIGNAL_TYPE_READOUT
        except (ValueError, TypeError):
            pass
    if days <= 7:
        return SIGNAL_TYPE_IMMINENT
    if days <= 30:
        return SIGNAL_TYPE_APPROACHING
    return SIGNAL_TYPE_WATCHLIST


def _thesis_direction(entry: dict) -> str:
    """Short when CRL confirmed / status suggests rejection; long otherwise (approval upside)."""
    if entry.get("crl_date"):
        return DIRECTION_SHORT
    status = entry.get("status", "")
    if status in ("rejected", "crl", "resolved_crl"):
        return DIRECTION_SHORT
    if _recent_pdufa_date_change_kind(entry) == "delayed":
        return DIRECTION_SHORT
    notes = (entry.get("notes") or "").lower()
    if "complete response letter" in notes or "crl issued" in notes:
        return DIRECTION_SHORT
    return DIRECTION_LONG


def _signal_hash(ticker: str, drug: str, pdufa_date: str, subtype: str) -> str:
    return f"sha256:{hashlib.sha256(f'{ticker}|{drug}|{pdufa_date}|{subtype}'.encode()).hexdigest()}"


def _build_signal(entry: dict, days: int, scan_date: datetime,
                  *, issuer_figi: Optional[str], client: SupabaseClient) -> Optional[Signal]:
    pdufa_date_str = entry.get("pdufa_date", "")
    if not pdufa_date_str:
        return None
    ticker = entry.get("ticker", "")
    drug = entry.get("drug_name", "")
    subtype = _classify_subtype(entry, days)
    strength = _assess_strength(entry)
    if subtype in (
        SIGNAL_TYPE_IMMINENT,
        SIGNAL_TYPE_DATE_ADVANCED,
        SIGNAL_TYPE_DATE_DELAYED,
    ):
        strength = min(strength + 1, 5)

    try:
        source_date = datetime.strptime(pdufa_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        source_date = scan_date

    enrichment = entry.get("enrichment", {}) or {}
    trial = enrichment.get("trial") or ((enrichment.get("trials") or [None])[0])
    fda_history = (enrichment.get("fda_history") or [])[:3]
    raw_payload: Dict[str, Any] = {
        "ticker": ticker,
        "company_name": entry.get("company_name", ""),
        "drug_name": drug,
        "indication": entry.get("indication", ""),
        "pdufa_date": pdufa_date_str,
        "previous_pdufa_date": entry.get("previous_pdufa_date"),
        "pdufa_date_change_kind": entry.get("pdufa_date_change_kind"),
        "pdufa_date_changed_at": entry.get("pdufa_date_changed_at"),
        "days_until_pdufa": days,
        "nda_type": entry.get("nda_type", ""),
        "application_number": entry.get("application_number", ""),
        "phase3_nctid": entry.get("phase3_nctid", ""),
        "is_resubmission": entry.get("is_resubmission", False),
        "adcom_date": entry.get("adcom_date"),
        "adcom_vote": entry.get("adcom_vote"),
        "adcom_support_ratio": _adcom_support_ratio(entry.get("adcom_vote")),
        "crl_date": entry.get("crl_date"),
        "status": entry.get("status"),
        "trial_status": trial.get("status") if isinstance(trial, dict) else None,
        "approval_history_count": _approval_history_count(fda_history),
        "notes": entry.get("notes", ""),
        "enrichment": {
            "trial": enrichment.get("trial"),
            "trials_top": (enrichment.get("trials") or [])[:2],
            "fda_history": fda_history,
        },
        "headline": f"{ticker} {drug} PDUFA {pdufa_date_str} (T-{days}d)",
    }
    if ticker:
        try:
            from modal_workers.shared.market_snapshot import load_market_snapshot
            snapshot = load_market_snapshot(ticker, client=client)
            if snapshot:
                raw_payload.update(snapshot)
        except Exception:
            pass

    nct = entry.get("phase3_nctid", "")
    source_url = (f"https://clinicaltrials.gov/study/{nct}" if nct
                  else "https://www.fda.gov/drugs")

    source_content_hash = _signal_hash(ticker, drug, pdufa_date_str, subtype)
    signal_id = hashlib.sha256(
        f"{NAME}:{ticker}:{drug}:{pdufa_date_str}:{subtype}".encode()
    ).hexdigest()[:32]

    entity_hints = EntityHints(
        issuer_figi=issuer_figi,
        ticker=ticker or None,
        mic=None,  # XNAS/XNYS unknown at emit; resolved downstream
        cik=entry.get("cik") or None,
        name=entry.get("company_name") or None,
        country="US",
    )

    return Signal(
        signal_id=signal_id,
        source_content_hash=source_content_hash,
        source_date=source_date,
        scan_date=scan_date,
        signal_type=subtype,
        raw_payload=raw_payload,
        source_url=source_url,
        issuer_figi=issuer_figi,
        entity_hints=entity_hints,
        thesis_direction=_thesis_direction(entry),
        strength_estimate=strength,
    )


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        raise MissingAuthError(
            "SEC_USER_AGENT env var missing — required for EDGAR 8-K PDUFA auto-discovery. "
            "Set via Modal secret `scanner-secrets`.")

    client = SupabaseClient()

    # OpenFIGI cache routed through Supabase Storage — parity with edgar_filing_monitor.
    try:
        from modal_workers.shared.openfigi_resolver import set_cache_backend
        set_cache_backend(*client.openfigi_cache_backend())
    except Exception as e:
        logger.debug(f"OpenFIGI cache backend init failed (non-fatal): {e}")

    scan_date = datetime.now(timezone.utc)
    budget = max(10, cfg.timeout_soft_s - 5)  # leave 5s for final writes
    scan_start = time.time()
    budget_deadline = scan_start + budget

    do_enrich = bool(cfg.config.get("enrich", True))
    do_discover = bool(cfg.config.get("auto_discover", True))
    do_crosscheck = bool(cfg.config.get("approval_crosscheck", True))

    warnings: List[str] = []
    fetched_records = 0

    # ------------------------------------------------------------------
    # 1. Load watchlist
    # ------------------------------------------------------------------
    watchlist = _load_watchlist(client)
    watchlist_possibly_corrupt = 0 < len(watchlist) < MIN_EXPECTED_ENTRIES
    if watchlist_possibly_corrupt:
        warnings.append(
            f"watchlist has {len(watchlist)} entries (<{MIN_EXPECTED_ENTRIES}); "
            f"auto-discover will not save changes")

    # ------------------------------------------------------------------
    # 2. EDGAR 8-K auto-discovery (budget-guarded)
    # ------------------------------------------------------------------
    watchlist_dirty = False
    if do_discover and time.time() < budget_deadline:
        try:
            discovered = _discover_pdufa_from_edgar(watchlist, user_agent=user_agent)
            fetched_records += len(discovered)
            new_entries = [d for d in discovered if d.get("is_new")]
            if new_entries and not watchlist_possibly_corrupt:
                for d in new_entries:
                    watchlist = _add_to_watchlist(
                        watchlist,
                        ticker=d["ticker"],
                        drug_name="(auto-discovered)",
                        pdufa_date=d["pdufa_date"],
                        company_name=d["company_name"],
                        notes=f"Auto-discovered from EDGAR 8-K filed {d['file_date']}",
                    )
                    # Carry the cik so entity-resolution has it on first emit.
                    if d.get("cik"):
                        for e in watchlist:
                            if e.get("ticker") == d["ticker"] and not e.get("cik"):
                                e["cik"] = d["cik"]
                                break
                watchlist_dirty = True
            # Refresh dates on existing entries when 8-K is newer.
            for d in discovered:
                if d.get("is_new"):
                    continue
                for e in watchlist:
                    if (e.get("ticker") == d["ticker"]
                            and e.get("status") == "active"
                            and e.get("pdufa_date") != d["pdufa_date"]
                            and d["file_date"] > (e.get("added_date") or "")):
                        old_date = e["pdufa_date"]
                        _apply_pdufa_date_update(e, d["pdufa_date"])
                        e["notes"] = (e.get("notes", "") +
                            f" | Date updated {old_date}->{d['pdufa_date']} "
                            f"per 8-K filed {d['file_date']}")
                        watchlist_dirty = True
        except Exception as e:
            warnings.append(f"auto-discovery failed: {e}")

    # ------------------------------------------------------------------
    # 3. openFDA approval cross-check (budget-guarded)
    # ------------------------------------------------------------------
    if do_crosscheck and time.time() < budget_deadline:
        try:
            newly_approved = _run_approval_crosscheck(
                watchlist, user_agent=user_agent, client=client,
                budget_deadline=min(budget_deadline, time.time() + 15),
            )
            if newly_approved:
                watchlist_dirty = True
        except Exception as e:
            warnings.append(f"approval crosscheck failed: {e}")

    # ------------------------------------------------------------------
    # 4. Enrichment (budget-guarded)
    # ------------------------------------------------------------------
    if do_enrich and time.time() < budget_deadline:
        try:
            calls = _enrich_watchlist(
                watchlist, client,
                budget_deadline=min(budget_deadline, time.time() + 20),
            )
            fetched_records += calls
            if calls > 0:
                watchlist_dirty = True
        except Exception as e:
            warnings.append(f"enrichment failed: {e}")

    # ------------------------------------------------------------------
    # 5. Build signals
    # ------------------------------------------------------------------
    signals: List[Signal] = []
    for entry in watchlist:
        if time.time() > budget_deadline:
            warnings.append("wall-clock budget exceeded during signal build")
            break

        status = entry.get("status", "")
        # Emit for active entries + fda_decision-eligible statuses (approved/crl).
        if status not in ("active", "approved", "crl", "resolved_crl"):
            continue

        pdufa_date = entry.get("pdufa_date", "")
        days = _days_until(pdufa_date)
        if days is None:
            continue
        date_change_kind = _recent_pdufa_date_change_kind(entry)
        # Keep recent post-PDUFA decisions within 14d window for fda_decision signals.
        if days < -14 or (days > WINDOW_WATCHLIST and date_change_kind is None):
            continue

        ticker = entry.get("ticker", "")
        if ticker in DISQUALIFIED_TICKERS:
            continue

        # Lazy OpenFIGI lookup; best-effort, mirror edgar.
        issuer_figi: Optional[str] = None
        if ticker:
            try:
                from modal_workers.shared.openfigi_resolver import resolve_ticker
                res = resolve_ticker(ticker, exch_code="US")
                if res.resolved:
                    issuer_figi = res.issuer_figi
            except Exception:
                pass

        sig = _build_signal(entry, days, scan_date, issuer_figi=issuer_figi, client=client)
        if sig is not None:
            signals.append(sig)

    # ------------------------------------------------------------------
    # 6. Persist watchlist (one write, end of scan)
    # ------------------------------------------------------------------
    if watchlist_dirty and not watchlist_possibly_corrupt:
        _save_watchlist(client, watchlist)

    status: str = "ok"
    if warnings:
        status = "partial"

    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=signals,
        warnings=warnings,
        fetched_records=fetched_records,
    )
