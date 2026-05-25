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
from urllib.parse import quote

import requests

from modal_workers.shared.biotech_base_rates import (
    DEFAULT_APPROVAL_PROB,
    load_base_rates,
    map_conditions_to_base_key,
)
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

# WI-3: sponsor-history cache for the v2 strength rubric's first-time-sponsor
# proxy. Same 7d TTL as the approval cache — sponsor NDA counts move on quarter+
# timescales, not minutes. Keyed on slugified sponsor name.
SPONSOR_HISTORY_CACHE_TTL_S = 7 * 24 * 3600

# WI-3: feature flag that selects v1 (current heuristic) vs v2 (port of
# v2_skills screen-pdufa-pipeline-forward). Read once at import time from env;
# Modal config or pg_cron-side wrapper can flip it. The internal_config
# 'pdufa_strength_rubric' row is the canonical source of truth for operators
# but is not queried per-call (reactor-side Supabase reads were rejected for
# this scanner — see plan WI-3).
PDUFA_STRENGTH_RUBRIC = os.environ.get("PDUFA_STRENGTH_RUBRIC", "v1").lower()

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
SIGNAL_TYPE_EOP2 = "eop2_meeting"   # End-of-Phase-2 / Type B meeting (upstream)
DATE_CHANGE_SIGNAL_WINDOW_DAYS = 14

# Thesis direction — long by default (approval is upside), short when we see confirmed
# CRL / near-term rejection risk. We detect this by scanning notes / crl_date fields that
# v1 populates when a CRL lands.
DIRECTION_LONG = "long"
DIRECTION_SHORT = "short"

# Approval-probability modifiers applied to the indication base rate. Sourced from
# config/phase3_approval_base_rates.json _trial_design_adjustments block (currently
# unwired into the rubric's trial_design_adjustments JSONB column — see plan
# "Out of scope" item).
PRIORITY_REVIEW_LIFT = 0.05
BREAKTHROUGH_LIFT = 0.04
ACCELERATED_LIFT = 0.03
RESUBMISSION_PENALTY = 0.10  # Class-2 resubmissions historically score worse
PROB_MIN, PROB_MAX = 0.0, 0.95


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

def _hits_to_company_index(hits: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """Index EFTS hits by ticker (first display-name parenthetical group)."""
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
    return companies


def _discover_pdufa_from_edgar(existing_watchlist: List[dict],
                               user_agent: str,
                               lookback_days: int = 90) -> List[dict]:
    """Query EFTS for 8-K filings mentioning "PDUFA" + "action date" and extract dates.

    Returns a list of {ticker, company_name, cik, pdufa_date, file_date, is_new} dicts.
    New = ticker not already on the watchlist (any status).
    """
    from modal_workers.shared.edgar_efts import efts_search

    today = datetime.now(timezone.utc)
    start = (today - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    try:
        # 6-K covers foreign-listed biotech ADRs (AZN, NVS, RHHBY, BAYRY) which
        # don't file 8-Ks. 10-Q catches PDUFA mentions buried in "Subsequent
        # Events" sections without a parallel 8-K.
        hits = efts_search(
            '"PDUFA" "action date"', start, end,
            forms="8-K,6-K,10-Q", size=50, user_agent=user_agent,
        )
    except Exception as e:
        logger.warning(f"EDGAR PDUFA discovery failed: {e}")
        return []

    companies = _hits_to_company_index(hits)
    existing_tickers = {e.get("ticker") for e in existing_watchlist}
    discovered: List[dict] = []

    for ticker, info in companies.items():
        date_iso, drug_candidate = _parse_filing_for_pdufa(
            info["file_id"], info["cik"], info["adsh"], user_agent=user_agent,
        )
        if not date_iso:
            continue
        try:
            pd = datetime.strptime(date_iso, "%Y-%m-%d")
            if pd < today.replace(tzinfo=None):
                continue
        except ValueError:
            continue

        discovered.append({
            "ticker": ticker,
            "company_name": info["name"],
            "cik": info["cik"],
            "pdufa_date": date_iso,
            "drug_name": drug_candidate,
            "file_date": info["file_date"],
            "source": "edgar_8k",
            "is_new": ticker not in existing_tickers,
        })

    return discovered


# INN suffix patterns — high-precision drug-class endings used by the WHO
# International Nonproprietary Name nomenclature. Curated for low false-positive
# rate against English: short generic stems (`ide`, `vir`, `stat`) are excluded
# because they hit common words ("outside", "thermostat"); only their longer
# class-specific variants are kept ("vastatin", "navir", "glutide", etc.).
# Coverage is intentionally partial — any drug whose INN stem isn't here will
# leave the entry at "(auto-discovered)" for the AI thesis writer to fill in.
_INN_SUFFIXES = (
    # Monoclonal antibodies
    "mab", "zumab", "ximab", "umab", "lumab",
    # Kinase / pathway inhibitors
    "tinib", "afenib", "rafenib", "lisib", "ciclib", "sertib",
    # Statins
    "vastatin",
    # Antivirals (specific class stems only)
    "navir", "tegravir", "ciclovir", "fovir", "buvir", "asvir", "pravir", "cabir",
    # GLP-1 / peptide therapeutics
    "glutide", "lutide", "tide",
    # Antifungals / antiparasitics
    "conazole", "prazole",
    # Cardiovascular
    "sartan", "olol", "dipine",
    # Diabetes
    "formin", "gliflozin", "gliptin",
    # CNS
    "azepam", "azolam", "melteon", "stigmine",
    # Cytokines / immunomodulators
    "kira", "leukin", "cept",
    # Oligonucleotides / RNA therapeutics
    "rsen", "drisen", "siran", "mersen",
    # Cortisol receptor antagonists
    "corilant",
    # Renin inhibitors
    "kiren",
)
# INN names are conventionally lowercase but sentence-case is also common.
# Match both. The leading non-word boundary anchors the start; the suffix
# alternation must be the final stem.
_DRUG_NAME_RE = re.compile(
    r"\b([A-Za-z]{3,20}(?:" + "|".join(_INN_SUFFIXES) + r"))\b",
)
# Branded codeified names like "VK2735" or "AXS-05" — high-precision and very
# common in biotech 8-K subjects.
_DRUG_CODE_RE = re.compile(r"\b([A-Z]{2,5}-?\d{2,5})\b")

# Tokens that match an INN suffix but are common English words — filter out.
_DRUG_NAME_BLOCKLIST = {
    "report", "import", "support", "account", "amount", "submit",
    "permit", "consult", "result", "default", "agreement",
}


def _extract_drug_name(text: str) -> Optional[str]:
    """Return the most likely drug name from an 8-K body, or None.

    Two-pass strategy: INN-suffix match (`relacorilant`, `tovorafenib`) wins; if
    none, fall back to a code form (`VK2735`, `AXS-05`). Confined to the first
    20 KB of text since drug names appear in the lede, not exhibits.
    """
    head = text[:20_000]
    for m in _DRUG_NAME_RE.finditer(head):
        candidate = m.group(1)
        if candidate.lower() in _DRUG_NAME_BLOCKLIST:
            continue
        return candidate
    m = _DRUG_CODE_RE.search(head)
    return m.group(1) if m else None


def _parse_filing_for_pdufa(file_id: str, cik: str, adsh: str,
                            *, user_agent: str) -> tuple[Optional[str], Optional[str]]:
    """Fetch an 8-K/6-K/10-Q body and extract (PDUFA date, drug name candidate).

    Either or both fields may be None. The text is fetched once for both
    extractions to avoid duplicate body downloads.
    """
    from modal_workers.shared.edgar_efts import fetch_filing_text
    text = fetch_filing_text(file_id, cik, adsh, user_agent=user_agent)
    if text is None:
        return None, None
    # \b anchors prevent matches like "non-PDUFA" / "future action date" (audit F-114).
    date_patterns = [
        r"\bPDUFA\b[^.]{0,200}?(?:\baction date\b|\btarget date\b|\bdate\b)[^.]{0,100}?(?:of|for|is|set for|assigned|to)\s*(\w+ \d{1,2},?\s*\d{4})",
        r"(?:\baction date\b|\btarget date\b)[^.]{0,100}?(\w+ \d{1,2},?\s*\d{4})",
    ]
    date_iso: Optional[str] = None
    for pat in date_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
            for fmt in ("%B %d, %Y", "%B %d %Y"):
                try:
                    date_iso = datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
            if date_iso:
                break
    drug = _extract_drug_name(text)
    return date_iso, drug


def _extract_pdufa_date_from_filing(file_id: str, cik: str, adsh: str,
                                    *, user_agent: str) -> Optional[str]:
    """Backwards-compat shim — returns just the date. Prefer
    `_parse_filing_for_pdufa` which also returns a drug name candidate."""
    date_iso, _drug = _parse_filing_for_pdufa(file_id, cik, adsh, user_agent=user_agent)
    return date_iso


# ---------------------------------------------------------------------------
# EDGAR 8-K CRL discovery (Phase 2b)
# ---------------------------------------------------------------------------

CRL_LOOKBACK_DAYS = 30
CRL_PDUFA_MATCH_WINDOW_DAYS_BEFORE = 30
CRL_PDUFA_MATCH_WINDOW_DAYS_AFTER = 7
PRESUMED_CRL_MIN_DAYS_PAST = 3
PRESUMED_CRL_AP_LOOKBACK_DAYS = 30


def _discover_crls_from_edgar(watchlist: List[dict],
                              user_agent: str,
                              lookback_days: int = CRL_LOOKBACK_DAYS) -> List[str]:
    """Find recent 8-K / 6-K filings mentioning "complete response letter" and
    flip matching active watchlist entries to status='crl'.

    Returns the list of tickers newly marked as CRL. Mutates watchlist in place.
    Match criteria: ticker on the watchlist with status=='active' and
    pdufa_date within [today − 30d, today + 7d].
    """
    from modal_workers.shared.edgar_efts import efts_search

    today = datetime.now(timezone.utc)
    start = (today - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    try:
        hits = efts_search(
            '"complete response letter"', start, end,
            forms="8-K,6-K", size=50, user_agent=user_agent,
        )
    except Exception as e:
        logger.warning(f"EDGAR CRL discovery failed: {e}")
        return []

    companies = _hits_to_company_index(hits)
    today_d = today.date()
    newly_marked: List[str] = []

    for ticker, info in companies.items():
        for entry in watchlist:
            if entry.get("ticker") != ticker:
                continue
            if entry.get("status") != "active":
                continue
            try:
                pdufa_dt = datetime.strptime(entry.get("pdufa_date", ""), "%Y-%m-%d").date()
            except ValueError:
                continue
            window_start = today_d - timedelta(days=CRL_PDUFA_MATCH_WINDOW_DAYS_BEFORE)
            window_end = today_d + timedelta(days=CRL_PDUFA_MATCH_WINDOW_DAYS_AFTER)
            if not (window_start <= pdufa_dt <= window_end):
                continue
            file_date = info.get("file_date", "") or today_d.isoformat()
            entry["status"] = "crl"
            entry["crl_date"] = file_date
            entry["notes"] = (entry.get("notes", "") +
                f" | AUTO-DETECTED: CRL per 8-K filed {file_date}.")
            newly_marked.append(ticker)
            break  # one entry per ticker — break out of watchlist loop

    return newly_marked


def _apply_presumed_crl(watchlist: List[dict], client: SupabaseClient,
                        user_agent: str) -> List[str]:
    """Promote stale active entries to status='presumed_crl'.

    Trigger: pdufa_date is at least 3 days past, status=='active', and openFDA
    shows no AP submission in the trailing 30d window. Used as a fallback after
    `_discover_crls_from_edgar` so post-PDUFA passes don't keep emitting
    'pdufa_imminent' indefinitely.
    """
    today = datetime.now(timezone.utc).date()
    promoted: List[str] = []
    for entry in watchlist:
        if entry.get("status") != "active":
            continue
        try:
            pdufa_dt = datetime.strptime(entry.get("pdufa_date", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        days_past = (today - pdufa_dt).days
        if days_past < PRESUMED_CRL_MIN_DAYS_PAST:
            continue

        drug_name = entry.get("drug_name", "")
        if not drug_name or drug_name == "(auto-discovered)":
            # Without a drug name we can't reliably check openFDA. Promote
            # anyway since the PDUFA passed without resolution.
            entry["status"] = "presumed_crl"
            entry["notes"] = (entry.get("notes", "") +
                " | AUTO-DETECTED: PDUFA passed without openFDA AP signal "
                "(presumed_crl; drug name unknown).")
            promoted.append(entry.get("ticker", ""))
            continue

        result = _check_fda_approval_status(drug_name, user_agent, client)
        ap_within_window = False
        if result and result.get("approved"):
            try:
                approval_dt = datetime.strptime(result.get("approval_date", ""), "%Y%m%d").date()
                if (today - approval_dt).days <= PRESUMED_CRL_AP_LOOKBACK_DAYS:
                    ap_within_window = True
            except (ValueError, TypeError):
                pass

        if ap_within_window:
            continue
        entry["status"] = "presumed_crl"
        entry["notes"] = (entry.get("notes", "") +
            f" | AUTO-DETECTED: PDUFA {entry.get('pdufa_date')} passed "
            f"({days_past}d ago) with no openFDA AP — presumed_crl.")
        promoted.append(entry.get("ticker", ""))
    return promoted


# ---------------------------------------------------------------------------
# EOP2 / Type B meeting discovery (upstream of NDA — option 3 from the
# Phase-1/2 coverage discussion). Companies announce successful end-of-Phase-2
# meetings via 8-K because they're a positive milestone (FDA agreed to the
# Phase 3 design). Failed/inconclusive meetings are typically NOT 8-K'd and
# instead show up in subsequent 10-Q risk factors — so detection of an EOP2
# 8-K is itself a positive selection signal.
#
# The signal bypasses the PDUFA watchlist (those entries are post-NDA) and
# emits directly. Catalyst_timeline maps low — Phase 3 enrollment is ~12 months
# out, readout ~24-36 months out. This is correct: EOP2 announcements move
# the stock 5-15% on the day, not on a future binary event.
# ---------------------------------------------------------------------------

EOP2_LOOKBACK_DAYS = 30

# Phrases that confirm the 8-K is announcing an EOP2 / Type B / pre-Phase-3
# meeting outcome. The EFTS query is broad ("end of phase 2") to maximize
# recall; a second-pass body regex below filters precision.
_EOP2_KEYWORDS_EFTS = '"end of phase 2 meeting" OR "end-of-phase 2 meeting" OR "Type B meeting"'
_EOP2_BODY_RE = re.compile(
    r"\b("
    r"end[- ]of[- ]phase\s*2\s*meeting"
    r"|type\s*b\s*meeting"
    r"|pre[- ]phase\s*3\s*meeting"
    r")\b",
    re.IGNORECASE,
)
# Positive-outcome phrases — strengthen the signal when present. Absence
# doesn't downgrade (companies write both "alignment" and "guidance" phrasings
# and both are positive milestones).
_EOP2_POSITIVE_RE = re.compile(
    r"\b(alignment|agreed|agreement|positive feedback|supported|reached agreement"
    r"|successful (?:meeting|outcome)|pivotal trial design|written minutes)\b",
    re.IGNORECASE,
)
# Anti-keywords — phrases that suggest the 8-K is about a failed or
# inconclusive meeting, or about a competitor's meeting being referenced.
_EOP2_NEGATIVE_RE = re.compile(
    r"\b(no agreement|did not reach|disagreement|not in alignment|further (?:data|study|trial))\b",
    re.IGNORECASE,
)


def _discover_eop2_from_edgar(user_agent: str,
                              lookback_days: int = EOP2_LOOKBACK_DAYS,
                              ) -> List[Dict[str, Any]]:
    """Find recent 8-K / 6-K filings announcing an End-of-Phase-2 / Type B
    meeting outcome. Returns a list of {ticker, company_name, cik, file_date,
    file_id, adsh, drug_name, sentiment} dicts ready to feed `_build_eop2_signal`.
    """
    from modal_workers.shared.edgar_efts import efts_search

    today = datetime.now(timezone.utc)
    start = (today - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    try:
        hits = efts_search(
            _EOP2_KEYWORDS_EFTS, start, end,
            forms="8-K,6-K", size=50, user_agent=user_agent,
        )
    except Exception as e:
        logger.warning(f"EDGAR EOP2 discovery failed: {e}")
        return []

    companies = _hits_to_company_index(hits)
    discovered: List[Dict[str, Any]] = []

    for ticker, info in companies.items():
        # Body confirmation — guards against the EFTS keyword matching in
        # exhibits or unrelated context.
        from modal_workers.shared.edgar_efts import fetch_filing_text
        text = fetch_filing_text(info["file_id"], info["cik"], info["adsh"],
                                 user_agent=user_agent)
        if text is None:
            continue
        if not _EOP2_BODY_RE.search(text):
            continue
        # Skip if the body has any explicit negative language. Cannot fall back
        # to "negative AND not positive" because the positive regex's "agreement"
        # token matches inside "no agreement" — the negative phrasing wins
        # outright when present.
        if _EOP2_NEGATIVE_RE.search(text):
            continue

        sentiment = "positive" if _EOP2_POSITIVE_RE.search(text) else "neutral"
        drug_name = _extract_drug_name(text)

        discovered.append({
            "ticker": ticker,
            "company_name": info["name"],
            "cik": info["cik"],
            "file_date": info["file_date"],
            "file_id": info["file_id"],
            "adsh": info["adsh"],
            "drug_name": drug_name,
            "sentiment": sentiment,
            "source_url": (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{(info['cik'] or '0').lstrip('0') or '0'}/"
                f"{info['adsh'].replace('-', '')}"
            ),
        })

    return discovered


def _build_eop2_signal(hit: Dict[str, Any], scan_date: datetime,
                       *, client: SupabaseClient) -> Optional[Signal]:
    """Build an EOP2 Signal from a discovered 8-K hit. No watchlist round-trip;
    dedup is by adsh via source_content_hash."""
    ticker = hit.get("ticker", "")
    cik = hit.get("cik", "") or None
    adsh = hit.get("adsh", "")
    if not ticker or not adsh:
        return None

    file_date = hit.get("file_date", "")
    try:
        source_date = datetime.strptime(file_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        source_date = scan_date

    drug = hit.get("drug_name") or ""
    sentiment = hit.get("sentiment") or "neutral"

    # Strength: 3 default; +1 if drug name extracted (gives downstream
    # enrichment something to work with); +1 if positive sentiment.
    strength = 3
    if drug:
        strength += 1
    if sentiment == "positive":
        strength += 1
    strength = min(strength, 5)

    # The catalyst_timeline dim consumes days_until_pdufa or days_until_readout.
    # Phase 3 trials typically start ~6 months post-EOP2; first readout is
    # ~24 months. Set days_until_readout to 365 as a midpoint estimate so the
    # rubric maps to catalyst_timeline=1 (low) rather than missing the dim.
    days_until_readout_estimate = 365

    raw_payload: Dict[str, Any] = {
        "ticker": ticker,
        "company_name": hit.get("company_name", ""),
        "cik": cik,
        "drug_name": drug,
        "meeting_type": "EOP2",
        "sentiment": sentiment,
        "file_date": file_date,
        "adsh": adsh,
        "days_until_readout": days_until_readout_estimate,
        "next_milestone_estimate": "phase_3_initiation_~6mo",
        "headline": (
            f"{ticker} EOP2 meeting "
            f"({drug or 'undisclosed drug'}) — {sentiment}"
        ),
        # Magnitude defaults — EOP2 announcements are smaller than PDUFA
        # decisions even for small caps. Override the legacy 50/35.
        "upside_pct": 15.0,
        "downside_pct": 5.0,
    }

    if ticker:
        try:
            from modal_workers.shared.market_snapshot import load_market_snapshot
            snapshot = load_market_snapshot(ticker, client=client)
            if snapshot:
                raw_payload.update(snapshot)
        except Exception as e:
            from modal_workers.observability import record_snapshot_fetch_failure
            record_snapshot_fetch_failure(client, scanner_name=NAME, ticker=ticker, exc=e)
    # Re-apply EOP2 magnitude after the snapshot.update so it doesn't
    # accidentally restore a PDUFA-style default. Then derive small lift from
    # mcap (megacap = 2/1, small = 25/8).
    mc = raw_payload.get("market_cap_usd")
    if mc is not None:
        if mc < 1_000_000_000:        # < $1B
            raw_payload["upside_pct"], raw_payload["downside_pct"] = 25.0, 8.0
        elif mc < 10_000_000_000:     # $1-10B
            raw_payload["upside_pct"], raw_payload["downside_pct"] = 12.0, 4.0
        else:
            raw_payload["upside_pct"], raw_payload["downside_pct"] = 3.0, 1.0
    else:
        raw_payload["upside_pct"], raw_payload["downside_pct"] = 15.0, 5.0

    source_content_hash = f"sha256:{hashlib.sha256(f'eop2|{adsh}'.encode()).hexdigest()}"
    signal_id = hashlib.sha256(f"{NAME}:eop2:{adsh}".encode()).hexdigest()[:32]
    source_url = hit.get("source_url") or "https://www.sec.gov/edgar/search/"

    issuer_figi: Optional[str] = None
    if ticker:
        try:
            from modal_workers.shared.openfigi_resolver import resolve_ticker
            res = resolve_ticker(ticker, exch_code="US")
            if res.resolved:
                issuer_figi = res.issuer_figi
        except Exception:
            pass

    return Signal(
        signal_id=signal_id,
        source_content_hash=source_content_hash,
        source_date=source_date,
        scan_date=scan_date,
        signal_type=SIGNAL_TYPE_EOP2,
        raw_payload=raw_payload,
        source_url=source_url,
        issuer_figi=issuer_figi,
        entity_hints=EntityHints(
            issuer_figi=issuer_figi,
            ticker=ticker or None,
            mic=None,
            cik=cik,
            name=hit.get("company_name") or None,
            country="US",
        ),
        thesis_direction=DIRECTION_LONG,
        strength_estimate=strength,
    )


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


def _extract_designations(history: Any) -> Dict[str, bool]:
    """Surface FDA designation flags from openFDA submission rows.

    Looks at the latest non-AP submission (the in-flight NDA/sNDA the PDUFA refers to),
    then falls back to any submission. Returns booleans for priority_review,
    breakthrough_designation, accelerated_approval, orphan_drug.
    """
    flags = {
        "priority_review": False,
        "breakthrough_designation": False,
        "accelerated_approval": False,
        "orphan_drug": False,
    }
    if not isinstance(history, list):
        return flags

    candidate_subs: List[dict] = []
    fallback_subs: List[dict] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        for sub in item.get("submissions") or []:
            if not isinstance(sub, dict):
                continue
            status = sub.get("status") or sub.get("submission_status") or ""
            (fallback_subs if status == "AP" else candidate_subs).append(sub)
        product_type = (item.get("product_type") or "").lower()
        if "orphan" in product_type:
            flags["orphan_drug"] = True

    for sub in candidate_subs or fallback_subs:
        rp = (sub.get("review_priority") or "").upper()
        if rp == "PRIORITY":
            flags["priority_review"] = True
        sub_type = (sub.get("type") or sub.get("submission_type") or "").upper()
        if "AA" in sub_type or "ACCELERATED" in sub_type:
            flags["accelerated_approval"] = True
        if (sub.get("breakthrough_designation") is True
                or "BREAKTHROUGH" in (sub.get("submission_class_code_description") or "").upper()):
            flags["breakthrough_designation"] = True

    return flags


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
                enrichment["designations"] = _extract_designations(approvals)
            call_count += 1

        entry["enrichment"] = enrichment
    return call_count


def _run_approval_crosscheck(watchlist: List[dict], user_agent: str,
                             client: SupabaseClient,
                             max_checks: int = 10,
                             budget_deadline: Optional[float] = None) -> List[str]:
    """D-046: mark watchlist entries as 'approved' if openFDA shows an approval
    in the window [PDUFA - 180d, PDUFA + 60d]. Returns list of tickers newly
    marked.

    P0 #1 (2026-05-08): widened the upper bound from `<= pdufa_dt` to
    `<= pdufa_dt + 60d`. The original window only captured early approvals
    (where FDA acted before the target). For approvals on or after the PDUFA
    date — the typical case — `approval_dt > pdufa_dt`, so the original check
    failed silently and the watchlist entry stayed `active`, never triggering
    a fda_decision signal emission. AXSM (PDUFA 2026-04-30, approved 2026-05-04
    -> not detected for 8d) is the canonical example. The 60d post-PDUFA buffer
    covers FDA's typical PDUFA-extension and late-decision tail without picking
    up the next cycle.
    """
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
            window_lo = pdufa_dt - timedelta(days=180)
            window_hi = pdufa_dt + timedelta(days=60)
            if window_lo <= approval_dt <= window_hi:
                entry["status"] = "approved"
                offset_days = (approval_dt - pdufa_dt).days
                offset_label = (
                    f"{offset_days}d before PDUFA" if offset_days < 0
                    else (f"on PDUFA" if offset_days == 0 else f"{offset_days}d after PDUFA")
                )
                entry["notes"] = (entry.get("notes", "") +
                    f" | AUTO-DETECTED: Approved {approval_date_str} per openFDA "
                    f"({offset_label}). App# {result.get('application_number', 'N/A')}.")
                newly_approved.append(ticker)
        except (ValueError, TypeError):
            pass
    return newly_approved


# ---------------------------------------------------------------------------
# Strength + signal builder
# ---------------------------------------------------------------------------

def _days_until(date_str: str) -> Optional[int]:
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (target - datetime.now(timezone.utc)).days
    except (ValueError, TypeError):
        return None


def _assess_strength(entry: dict) -> int:
    """Score 2-5 based on data quality.

    is_resubmission is no longer a +1 bump — historically Class-2 resubmissions
    underperform de novo NDAs, and the penalty now lives on approval_probability
    (RESUBMISSION_PENALTY). priority_review is +1 here as a quality marker.
    """
    strength = 2
    enrichment = entry.get("enrichment", {}) or {}
    trials_list = enrichment.get("trials") or []
    trial = enrichment.get("trial") or (trials_list[0] if trials_list else None)
    if trial:
        strength += 1
        if trial.get("status") in ("COMPLETED", "ACTIVE_NOT_RECRUITING"):
            strength += 1
    designations = (enrichment.get("designations") or {})
    if designations.get("priority_review"):
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


# ---------------------------------------------------------------------------
# WI-3 — v2 strength rubric (port of v2_skills screen-pdufa-pipeline-forward).
# ---------------------------------------------------------------------------
# Weights are explicit rather than the v1 base-plus-bonus pattern, and the
# scoring inputs are FDA-specific (Breakthrough, Priority Review, class
# precedent count, first-time-sponsor proxy). Max composite = 14; clipped to
# 0-10 in signals.strength_estimate to preserve compatibility with downstream
# rubric_engine consumers.

PDUFA_RUBRIC_V2_WEIGHTS = {
    "breakthrough_designation": 6,
    "priority_review": 3,
    "class_precedent": 2,   # multiplier applied to clipped-class-peer count
    "first_time_sponsor": 3,
}
PDUFA_RUBRIC_V2_MAX_SCORE = 10  # what signals.strength_estimate is clipped to
PDUFA_RUBRIC_V2_VERSION = "v2.2026-06-03"


def _count_class_peer_approvals(enrichment: Dict[str, Any]) -> int:
    """Deterministic class-precedent proxy: count distinct application_numbers
    in enrichment['fda_history']. Caps at 5 to avoid mega-class dominance in
    fully-genericized indications. No network — reads only the data we already
    fetched for the current drug.

    NOTE: this is a coarse proxy. The v2 export's "class precedent" used a
    full mechanism-of-action lookup (P2 research-clinical-class-precedent).
    When the bc_class_precedent_refresher table lands we should swap this
    helper for a table read — see plan WI-2 follow-up.
    """
    history = enrichment.get("fda_history") or []
    if not isinstance(history, list):
        return 0
    seen: set[str] = set()
    for item in history:
        if not isinstance(item, dict):
            continue
        app = item.get("application_number")
        if isinstance(app, str) and app:
            seen.add(app)
    return min(len(seen), 5)


def _sponsor_history_cache_key(sponsor_name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", sponsor_name.lower().strip())[:64]
    return f"sponsor_history/{safe}.json"


def _read_sponsor_history_cache(client: SupabaseClient,
                                sponsor_name: str) -> Optional[int]:
    raw = client.read_cache("fda", _sponsor_history_cache_key(sponsor_name))
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        ts = payload.get("cached_at", 0)
        if time.time() - ts > SPONSOR_HISTORY_CACHE_TTL_S:
            return None
        n = payload.get("n_prior_nda")
        return int(n) if isinstance(n, int) else None
    except (ValueError, UnicodeDecodeError, TypeError):
        return None


def _write_sponsor_history_cache(client: SupabaseClient, sponsor_name: str,
                                 n_prior_nda: int) -> None:
    try:
        client.write_cache(
            "fda", _sponsor_history_cache_key(sponsor_name),
            json.dumps({
                "cached_at": time.time(),
                "n_prior_nda": int(n_prior_nda),
            }).encode("utf-8"),
            content_type="application/json",
        )
    except SupabaseError:
        pass  # best-effort


def _count_sponsor_prior_p3(client: SupabaseClient,
                            sponsor_name: Optional[str]) -> Optional[int]:
    """Count the sponsor's prior NDA/BLA submissions via openFDA. Cached 7d.

    Returns None when the lookup cannot establish a count (empty sponsor name,
    openFDA error, parse error). The first_time_sponsor bonus only fires on a
    confirmed zero-hit lookup, not on "unknown".
    """
    if not sponsor_name or not sponsor_name.strip():
        return None
    cached = _read_sponsor_history_cache(client, sponsor_name)
    if cached is not None:
        return cached

    # openFDA /drug/drugsfda.json — count distinct application_numbers for
    # this sponsor with at least one NDA submission. The exhaustive truth is
    # behind paging; we cap at limit=100 (one page) which is sufficient
    # signal: anyone with ≥100 NDAs is clearly not a first-time sponsor.
    sponsor_clean = sponsor_name.replace('"', "").strip()
    if not sponsor_clean:
        return None
    search = (
        f'sponsor_name:"{sponsor_clean}" '
        'AND submissions.submission_type:"NDA"'
    )
    url = f"https://api.fda.gov/drug/drugsfda.json?search={quote(search)}&limit=100"
    n_prior_nda: Optional[int] = None
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            results = (resp.json() or {}).get("results") or []
            distinct: set[str] = set()
            for r in results:
                if not isinstance(r, dict):
                    continue
                app = r.get("application_number")
                if isinstance(app, str) and app:
                    distinct.add(app)
            n_prior_nda = len(distinct)
        elif resp.status_code == 404:
            n_prior_nda = 0  # openFDA returns 404 for zero hits
    except requests.RequestException:
        n_prior_nda = None
    except (ValueError, KeyError):
        n_prior_nda = None

    if n_prior_nda is not None:
        _write_sponsor_history_cache(client, sponsor_name, n_prior_nda)
    return n_prior_nda


def _assess_strength_v2(entry: dict, client: SupabaseClient) -> int:
    """v2 strength rubric — explicit weights per v2_skills export.

    Components:
      Breakthrough designation        +6
      Priority Review                 +3
      Class Precedent proxy           +2 (when ≥1 class peer approval exists)
      First-time sponsor proxy        +3 (when sponsor has 0 prior NDAs)

    Max composite = 14; clipped to 0..PDUFA_RUBRIC_V2_MAX_SCORE (10) for
    signals.strength_estimate compatibility. The full unclipped composite is
    surfaced via the raw_payload field 'strength_rubric_v2_raw' so the
    operator dashboard can show the headroom that v1 doesn't have.
    """
    enrichment = entry.get("enrichment", {}) or {}
    designations = enrichment.get("designations") or {}

    raw = 0
    if designations.get("breakthrough_designation"):
        raw += PDUFA_RUBRIC_V2_WEIGHTS["breakthrough_designation"]
    if designations.get("priority_review"):
        raw += PDUFA_RUBRIC_V2_WEIGHTS["priority_review"]

    if _count_class_peer_approvals(enrichment) >= 1:
        raw += PDUFA_RUBRIC_V2_WEIGHTS["class_precedent"]

    sponsor_name = (entry.get("sponsor_name")
                    or entry.get("company_name")
                    or enrichment.get("sponsor_name"))
    sponsor_prior_p3 = _count_sponsor_prior_p3(client, sponsor_name)
    if sponsor_prior_p3 == 0 and sponsor_name:
        raw += PDUFA_RUBRIC_V2_WEIGHTS["first_time_sponsor"]

    return max(0, min(raw, PDUFA_RUBRIC_V2_MAX_SCORE))


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
    if status in ("approved", "crl", "presumed_crl") or entry.get("crl_date"):
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
            pc_dt = datetime.strptime(pc[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            pc_days = (pc_dt - datetime.now(timezone.utc)).days
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
    if status in ("rejected", "crl", "resolved_crl", "presumed_crl"):
        return DIRECTION_SHORT
    if _recent_pdufa_date_change_kind(entry) == "delayed":
        return DIRECTION_SHORT
    notes = (entry.get("notes") or "").lower()
    if "complete response letter" in notes or "crl issued" in notes:
        return DIRECTION_SHORT
    return DIRECTION_LONG


def _signal_hash(ticker: str, drug: str, pdufa_date: str, subtype: str) -> str:
    return f"sha256:{hashlib.sha256(f'{ticker}|{drug}|{pdufa_date}|{subtype}'.encode()).hexdigest()}"


def _collect_conditions(entry: dict, trial: Any) -> List[str]:
    out: List[str] = []
    if isinstance(trial, dict):
        for c in trial.get("conditions") or []:
            if isinstance(c, str) and c:
                out.append(c)
    indication = entry.get("indication") or ""
    if indication:
        out.append(indication)
    return out


def _apply_designation_modifiers(base_prob: float, designations: Dict[str, bool],
                                 is_resubmission: bool) -> float:
    prob = base_prob
    if designations.get("priority_review"):
        prob += PRIORITY_REVIEW_LIFT
    if designations.get("breakthrough_designation"):
        prob += BREAKTHROUGH_LIFT
    if designations.get("accelerated_approval"):
        prob += ACCELERATED_LIFT
    if is_resubmission:
        prob -= RESUBMISSION_PENALTY
    return max(PROB_MIN, min(PROB_MAX, prob))


def _magnitude_defaults_for(market_cap_usd: Optional[float]) -> tuple[float, float]:
    """Return (upside_pct, downside_pct) defaults for a binary catalyst, scaled by mcap.

    Calibrated against observed biotech PDUFA reactions. The legacy 50/35 default
    over-rates magnitude on megacaps where a single drug rarely moves the stock
    more than a few percent. None or unknown mcap → legacy default.
    """
    if market_cap_usd is None:
        return 50.0, 35.0
    mc_mm = market_cap_usd / 1_000_000.0
    if mc_mm < 1_000:        # < $1B small-cap single-asset
        return 60.0, 40.0
    if mc_mm < 10_000:       # $1-10B mid-cap
        return 30.0, 20.0
    if mc_mm < 50_000:       # $10-50B large-cap
        return 12.0, 8.0
    return 4.0, 3.0          # > $50B megacap


def _build_signal(entry: dict, days: int, scan_date: datetime,
                  *, issuer_figi: Optional[str], client: SupabaseClient) -> Optional[Signal]:
    pdufa_date_str = entry.get("pdufa_date", "")
    if not pdufa_date_str:
        return None
    ticker = entry.get("ticker", "")
    drug = entry.get("drug_name", "")
    subtype = _classify_subtype(entry, days)
    # WI-3: rubric dispatch. v1 retains the heuristic-strength ladder (2..5);
    # v2 emits the new explicit-weights composite (0..10) and the imminent /
    # date-change +1 booster is preserved so the subtype tiering still matters.
    if PDUFA_STRENGTH_RUBRIC == "v2":
        strength = _assess_strength_v2(entry, client)
        strength_ceiling = PDUFA_RUBRIC_V2_MAX_SCORE
    else:
        strength = _assess_strength(entry)
        strength_ceiling = 5
    if subtype in (
        SIGNAL_TYPE_IMMINENT,
        SIGNAL_TYPE_DATE_ADVANCED,
        SIGNAL_TYPE_DATE_DELAYED,
    ):
        strength = min(strength + 1, strength_ceiling)

    try:
        source_date = datetime.strptime(pdufa_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        source_date = scan_date

    enrichment = entry.get("enrichment", {}) or {}
    trial = enrichment.get("trial") or ((enrichment.get("trials") or [None])[0])
    fda_history = (enrichment.get("fda_history") or [])[:3]
    designations = enrichment.get("designations") or {}

    base_rates = load_base_rates(client)
    conditions = _collect_conditions(entry, trial)
    base_key, matched_indications = map_conditions_to_base_key(conditions)
    base_prob = float(base_rates.get(base_key, base_rates.get("default", DEFAULT_APPROVAL_PROB)))
    is_resubmission = bool(entry.get("is_resubmission", False))
    approval_probability = _apply_designation_modifiers(base_prob, designations, is_resubmission)

    raw_payload: Dict[str, Any] = {
        "ticker": ticker,
        "discovery_lane": "regulatory_calendar",
        "review_priority": 1 if subtype in (SIGNAL_TYPE_IMMINENT, SIGNAL_TYPE_DECISION) else 2,
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
        "is_resubmission": is_resubmission,
        "adcom_date": entry.get("adcom_date"),
        "adcom_vote": entry.get("adcom_vote"),
        "adcom_support_ratio": _adcom_support_ratio(entry.get("adcom_vote")),
        "crl_date": entry.get("crl_date"),
        "status": entry.get("status"),
        "trial_status": trial.get("status") if isinstance(trial, dict) else None,
        "approval_history_count": _approval_history_count(fda_history),
        # Approval-probability dim consumers (dim_estimator binary_catalyst).
        "base_rate_key": base_key,
        "matched_indications": matched_indications,
        "approval_probability": approval_probability,
        # Magnitude dim + biotech_enricher EV inputs (parity with pre_phase3_readout_scanner).
        "upside_pct": 50.0,
        "downside_pct": 35.0,
        # Designation flags (also lift strength_estimate via _assess_strength).
        "priority_review": bool(designations.get("priority_review")),
        "breakthrough_designation": bool(designations.get("breakthrough_designation")),
        "accelerated_approval": bool(designations.get("accelerated_approval")),
        "orphan_drug": bool(designations.get("orphan_drug")),
        # WI-3: rubric version + clipped/raw composite so the operator dashboard
        # and the reactor's bc_pregate_inputs reader can both know which rubric
        # scored this row without joining elsewhere.
        "strength_rubric": (PDUFA_RUBRIC_V2_VERSION if PDUFA_STRENGTH_RUBRIC == "v2"
                            else "v1.legacy"),
        "notes": entry.get("notes", ""),
        "enrichment": {
            "trial": enrichment.get("trial"),
            "trials_top": (enrichment.get("trials") or [])[:2],
            "fda_history": fda_history,
            "designations": designations,
        },
        "headline": f"{ticker} {drug} PDUFA {pdufa_date_str} (T-{days}d)",
    }
    if ticker:
        try:
            from modal_workers.shared.market_snapshot import load_market_snapshot
            snapshot = load_market_snapshot(ticker, client=client)
            if snapshot:
                raw_payload.update(snapshot)
        except Exception as e:
            from modal_workers.observability import record_snapshot_fetch_failure
            record_snapshot_fetch_failure(client, scanner_name="fda_pdufa_pipeline", ticker=ticker, exc=e)

    # Override magnitude defaults from snapshot mcap (after the snapshot.update so
    # we read the freshly-set value). 50/35 over-rates magnitude on megacaps where
    # a single PDUFA rarely moves the stock more than a few percent.
    upside_pct, downside_pct = _magnitude_defaults_for(raw_payload.get("market_cap_usd"))
    raw_payload["upside_pct"] = upside_pct
    raw_payload["downside_pct"] = downside_pct

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
    do_enrich = bool(cfg.config.get("enrich", True))
    do_discover = bool(cfg.config.get("auto_discover", True))
    do_crosscheck = bool(cfg.config.get("approval_crosscheck", True))

    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent and do_discover:
        raise MissingAuthError(
            "SEC_USER_AGENT env var missing — required for EDGAR 8-K PDUFA auto-discovery. "
            "Set via Modal secret `scanner-secrets` or set cfg.config.auto_discover=false.")
    # openFDA endpoints accept any reasonable User-Agent; fall back so the
    # crosscheck can still run when discovery is gated off.
    openfda_user_agent = user_agent or "InvestmentResearch research@example.com"

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
            # Refresh dates + drug-name on existing entries when 8-K is newer.
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
            # Promote auto-discovered entries with a parsed drug-name candidate.
            # Runs against the full discovered set (new + existing) so an entry
            # added on a prior scan can pick up the drug name on a later parse.
            for d in discovered:
                drug_candidate = d.get("drug_name") or ""
                if not drug_candidate:
                    continue
                for e in watchlist:
                    if (e.get("ticker") == d["ticker"]
                            and e.get("drug_name") == "(auto-discovered)"
                            and e.get("status") in ("active",)):
                        e["drug_name"] = drug_candidate
                        e["notes"] = (e.get("notes", "") +
                            f" | Drug name auto-extracted from 8-K: {drug_candidate}")
                        watchlist_dirty = True
                        break
        except Exception as e:
            warnings.append(f"auto-discovery failed: {e}")

    # ------------------------------------------------------------------
    # 3. openFDA approval cross-check (budget-guarded)
    # ------------------------------------------------------------------
    if do_crosscheck and time.time() < budget_deadline:
        try:
            newly_approved = _run_approval_crosscheck(
                watchlist, user_agent=openfda_user_agent, client=client,
                budget_deadline=min(budget_deadline, time.time() + 15),
            )
            if newly_approved:
                watchlist_dirty = True
        except Exception as e:
            warnings.append(f"approval crosscheck failed: {e}")

    # ------------------------------------------------------------------
    # 3b. CRL discovery via 8-K / 6-K full-text + presumed_crl fallback
    # ------------------------------------------------------------------
    if do_discover and user_agent and time.time() < budget_deadline:
        try:
            crl_marked = _discover_crls_from_edgar(watchlist, user_agent=user_agent)
            fetched_records += len(crl_marked)
            if crl_marked:
                watchlist_dirty = True
        except Exception as e:
            warnings.append(f"CRL discovery failed: {e}")

    if do_crosscheck and time.time() < budget_deadline:
        try:
            presumed = _apply_presumed_crl(watchlist, client, openfda_user_agent)
            if presumed:
                watchlist_dirty = True
        except Exception as e:
            warnings.append(f"presumed_crl sweep failed: {e}")

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
    # 4b. AdCom calendar hydration from Federal Register (Phase 3)
    # ------------------------------------------------------------------
    if cfg.config.get("adcom_hydration", True) and time.time() < budget_deadline:
        try:
            from modal_workers.shared.fda_advisory_calendar import (
                fetch_advisory_committee_meetings,
                hydrate_watchlist_adcom_dates,
            )
            meetings = fetch_advisory_committee_meetings(
                lookback_days=30, lookahead_days=120, client=client,
            )
            adcom_updated = hydrate_watchlist_adcom_dates(watchlist, meetings)
            if adcom_updated:
                watchlist_dirty = True
        except Exception as e:
            warnings.append(f"adcom hydration failed: {e}")

    # ------------------------------------------------------------------
    # 5. Build signals
    # ------------------------------------------------------------------
    signals: List[Signal] = []
    for entry in watchlist:
        if time.time() > budget_deadline:
            warnings.append("wall-clock budget exceeded during signal build")
            break

        status = entry.get("status", "")
        # Emit for active entries + fda_decision-eligible statuses (approved/crl/presumed_crl).
        if status not in ("active", "approved", "crl", "resolved_crl", "presumed_crl"):
            continue

        pdufa_date = entry.get("pdufa_date", "")
        days = _days_until(pdufa_date)
        if days is None:
            continue
        date_change_kind = _recent_pdufa_date_change_kind(entry)
        # Post-PDUFA emission window. P0 #1 (2026-05-08): for resolved statuses
        # (approved/crl/resolved_crl/presumed_crl) extend to T+60 to match the
        # widened approval crosscheck. For 'active' watchlist entries keep the
        # original T+14 cutoff — those get demoted by Stage A in candidate_aging
        # if the catalyst is genuinely past without a decision.
        post_pdufa_floor = -60 if status in ("approved", "crl", "resolved_crl", "presumed_crl") else -14
        if days < post_pdufa_floor or (days > WINDOW_WATCHLIST and date_change_kind is None):
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
    # 5b. EOP2 / Type B meeting discovery — direct emit, no watchlist
    # ------------------------------------------------------------------
    if (do_discover and user_agent
            and cfg.config.get("eop2_discovery", True)
            and time.time() < budget_deadline):
        try:
            eop2_hits = _discover_eop2_from_edgar(user_agent=user_agent)
            fetched_records += len(eop2_hits)
            for hit in eop2_hits:
                sig = _build_eop2_signal(hit, scan_date, client=client)
                if sig is not None:
                    signals.append(sig)
        except Exception as e:
            warnings.append(f"EOP2 discovery failed: {e}")

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
