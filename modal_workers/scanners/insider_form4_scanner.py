"""
Insider Form 4 Cluster Scanner — SEC Section 16 insider-transaction clustering.

Detects multi-insider buy/sell clusters within a 30-day window on a single issuer,
plus solo C-suite open-market buys as a standalone high-signal pattern.
Fills the structural/ownership signal archetype that `profile_short_positioning.md`
line 3 pre-committed to ("SEC Form 4 insider transactions (once scanner is built)")
and line 47-55 already drafted a cluster rubric for.

Signal types emitted:
  - insider_cluster_buy   — >=2 discretionary insider buys on one issuer in 30d
  - insider_cluster_sell  — >=2 discretionary insider sells on one issuer in 30d
  - c_suite_open_market_buy — single CEO/CFO/COO open-market purchase (non-10b5-1)
  - ten_percent_holder_buy — single 10%-holder accumulation event

Noise filters (per profile_short_positioning.md:129):
  - 10b5-1 plan transactions are dropped (pre-arranged trading plans carry no signal).
  - Option exercises (transaction code M) without matched open-market sale drop.
  - Single minor-insider events below the cluster gate drop (per Dimension 1 rubric).

Affiliate dedup (per profile_short_positioning.md:57):
  - Affiliated reporting owners (e.g., Citadel Capital / Citadel Advisors / Citadel
    Americas) collapse to a single holder via first-two-token normalization.

Scoring profile: short_positioning. Dim estimation is supplied via an
`insider_cluster: True` routing key consumed by `dim_estimator.project_short_positioning_heuristic`;
the estimator applies the Form 4 rubric (C-suite-weighted cluster tier) rather than
the ESMA holder-count tier.

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
    - raises MissingAuthError if SEC_USER_AGENT env unset.
    - Uses cfg.timeout_soft_s as a wall-clock budget (daily cadence, ~120s budget).
    - Reuses edgar_filing_monitor's shared rate limiter (9 req/s SEC ceiling).
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import requests

from modal_workers.scanners.edgar_filing_monitor import (
    SUBMISSIONS_URL,
    _efts_search,
    _http_get,
    _rate_limiter,
)
from modal_workers.shared.dim_estimator import project_short_positioning_heuristic
from modal_workers.shared.rubric_engine import weighted_total
from modal_workers.shared.scanner_base import MissingAuthError, ScannerResult, Signal
from modal_workers.shared.supabase_client import (
    EntityHints,
    ScannerConfig,
    SupabaseClient,
)

NAME = "insider_form4_scanner"

REQUEST_TIMEOUT = 10
DEFAULT_WALL_CLOCK_S = 120

FORM4_FORMS = "4,4/A"
LOOKBACK_DAYS = 14        # EDGAR search window; covers standard 2-day filing deadline + buffer.
CLUSTER_WINDOW_DAYS = 30  # Per profile_short_positioning.md Dim 1 rubric.

# Minimum net transaction value (absolute USD) for a single insider's 30d activity
# to count as a cluster member. Filters noise from incidental <$50k option-related
# activity while preserving meaningful discretionary transactions.
MIN_NET_VALUE_USD = 50_000

# Per-hit fetch cap; each Form 4 primary doc is small (~3-10KB) but at 9 req/s
# ceiling we must stay bounded. EFTS returns up to 100 hits/query; with 14d window
# across all issuers the raw count is typically 1.5k-3k. We cap to 500 per run.
MAX_FILINGS_PER_RUN = 500

# Post-detection emission cap mirrors esma_short_scanner. Without it Form 4
# yielded uncapped emissions to short_positioning while ESMA was throttled to 25,
# inflating short candidate volume. cfg.config.top_signal_limit overrides;
# 0 disables.
TOP_SIGNAL_LIMIT_DEFAULT = 25

# Signal-type ranking priority for top_signal_limit selection. Cluster signals
# carry more conviction than solo events; sells beat solo buys.
_SIGNAL_TYPE_PRIORITY: Dict[str, int] = {
    "insider_cluster_buy": 4,
    "insider_cluster_sell": 4,
    "c_suite_open_market_buy": 2,
    "ten_percent_holder_buy": 2,
}

# -------- Section 16 transaction codes ------------------------------------------
# Reference: SEC Form 4 instructions, Table II.
TXN_CODE_PURCHASE = "P"        # Open-market or private purchase (discretionary)
TXN_CODE_SALE = "S"            # Open-market or private sale (discretionary)
TXN_CODE_OPTION_EXERCISE = "M" # Exercise of derivative (non-discretionary)
TXN_CODE_GIFT = "G"            # Gift (non-discretionary)
TXN_CODE_AWARD = "A"           # Grant/award (non-discretionary)
TXN_CODE_TAX_WITHHOLD = "F"    # Payment of tax by surrender of shares

# Only P and S count as discretionary signals.
DISCRETIONARY_CODES = frozenset({TXN_CODE_PURCHASE, TXN_CODE_SALE})

# -------- Officer role classification -------------------------------------------
# Title matchers for C-suite tier. Order-independent; we match on normalized title.
# VP patterns are checked BEFORE CSUITE so "Senior Vice President" / "Executive
# Vice President" route to VP-tier before the unadorned `\bpresident\b` matcher
# fires. Any title containing "Chief X Officer" or an explicit C-suite initialism
# still routes to CSUITE via the _CSUITE_PATTERNS pass below.
_VP_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in [
    r"\bvice\s+president\b",
    r"\bexecutive\s+vice\s+president\b",
    r"\bsenior\s+vice\s+president\b",
    r"\bevp\b|\bsvp\b|\bvp\b",
    r"\bgeneral\s+counsel\b",
    r"\bhead\s+of\b",
])

_CSUITE_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in [
    r"\bchief\s+executive",
    r"\bchief\s+financial",
    r"\bchief\s+operating",
    r"\bchief\s+technology",
    r"\bchief\s+information",
    r"\bchief\s+medical",
    r"\bchief\s+scientific",
    r"\bchief\s+commercial",
    r"\bchief\s+marketing",
    r"\bchief\s+legal",
    r"\bchief\s+administrative",
    r"\bchief\s+accounting",
    r"\bpresident\b",                # Standalone president — VP patterns above
                                     # already consumed "Vice President" variants.
    r"\bceo\b|\bcfo\b|\bcoo\b",
    r"\bcto\b|\bcio\b|\bcmo\b",
    r"\bcso\b|\bclo\b|\bcco\b|\bcao\b",
])


def _classify_role(is_director: bool, is_officer: bool, is_ten_percent: bool,
                   officer_title: Optional[str]) -> str:
    """Return one of: 'csuite', 'vp', 'director_only', 'ten_percent_only', 'minor'.

    Mapping follows profile_short_positioning.md Dim 1 (Form 4 cluster rubric):
      - 5: 3+ C-suite in 30d window
      - 4: 2 C-suite + 1+ VP
      - 3: Cluster of VPs/directors, no C-suite
      - 2: 1-2 minor insiders
      - 1: Single minor insider

    10%-holder overrides director-only because their ownership stake makes their
    transactions more informative than director-only activity.
    """
    if is_officer and officer_title:
        # Check VP patterns first — "Senior Vice President" matches VP, avoiding
        # the `\bpresident\b` CSUITE fallback catching it erroneously.
        for pat in _VP_PATTERNS:
            if pat.search(officer_title):
                return "vp"
        for pat in _CSUITE_PATTERNS:
            if pat.search(officer_title):
                return "csuite"
    # Officer without a title we recognize → treat as VP-tier (conservative).
    if is_officer:
        return "vp"
    if is_ten_percent:
        return "ten_percent_only"
    if is_director:
        return "director_only"
    return "minor"


# -------- Parsed-form data models -----------------------------------------------

@dataclass
class _Form4Transaction:
    """One non-derivative transaction row from a parsed Form 4."""
    accession: str
    issuer_cik: str
    issuer_name: str
    reporter_cik: str
    reporter_name: str
    reporter_normalized: str       # For affiliate dedup (first-two-tokens)
    role: str                      # csuite / vp / director_only / ten_percent_only / minor
    is_director: bool
    is_officer: bool
    is_ten_percent: bool
    officer_title: Optional[str]
    txn_date: str                  # ISO YYYY-MM-DD
    txn_code: str
    shares: float
    price_per_share: Optional[float]
    acquired_disposed: str         # "A" (acquired) or "D" (disposed)
    is_10b5_1: bool
    filing_url: str
    file_date: str
    value_usd: Optional[float]     # shares * price, when both known


@dataclass
class _IssuerCluster:
    """30-day cluster state for one issuer, one direction."""
    issuer_cik: str
    issuer_name: str
    direction: str                 # "buy" or "sell"
    holders: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # keyed by reporter_normalized
    contributing_accessions: set[str] = field(default_factory=set)
    latest_txn_date: str = ""
    earliest_txn_date: str = ""


# ---------------------------------------------------------------------------
# Dedup / hash helpers
# ---------------------------------------------------------------------------

_ORG_INDICATORS = re.compile(
    r"\b("
    r"llc|l\.l\.c\.?|ltd|lp|l\.p\.?|inc\.?|corp\.?|company|co\.?|plc|"
    r"sa|ag|gmbh|trust|holdings?|partners?|"
    r"fund|funds|capital|management|advisors?|investments?|group|"
    r"americas|international|europe|asia|global|bank|asset|securities?|"
    r"associates|ventures|equities?|equity"
    r")\b",
    re.IGNORECASE,
)


def _reporter_normalized(name: str) -> str:
    """Normalize reporter-owner name for affiliate dedup.

    profile_short_positioning.md:57 — 'Affiliated funds (e.g., Citadel Capital /
    Citadel Advisors / Citadel Americas) count as ONE holder, not three.'

    Rule: if the name looks like an organization (contains an entity suffix,
    fund/capital/advisors descriptor, or a geographic family-member tag), collapse
    to the first token. Otherwise (person name) keep the full normalized form.
    """
    original = name.strip()
    if not original:
        return ""
    is_org = bool(_ORG_INDICATORS.search(original)) or "," in original
    s = _ORG_INDICATORS.sub(" ", original)
    s = re.sub(r"[,\./]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    tokens = s.split()
    if not tokens:
        return original.lower()
    if is_org:
        return tokens[0]
    return " ".join(tokens)


def _content_hash(issuer_cik: str, direction: str, accessions: List[str]) -> str:
    key = f"form4|{issuer_cik}|{direction}|" + "|".join(sorted(accessions))
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()


def _signal_id(issuer_cik: str, direction: str, earliest: str, latest: str) -> str:
    key = f"form4|{issuer_cik}|{direction}|{earliest}|{latest}"
    return "insider_" + hashlib.sha256(key.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# EFTS fetch: list Form 4 filings in the lookback window.
# ---------------------------------------------------------------------------

def _list_form4_filings(date_from: str, date_to: str, *, user_agent: str,
                        max_results: int) -> List[Dict[str, Any]]:
    """Return EFTS hits for Forms 4 / 4/A in the window.

    EFTS returns hits ordered by file_date desc. Each hit has ciks, display_names,
    adsh, form, file_date. We pull the full window unfiltered — most US issuers
    file Form 4 weekly, so raw hit count is manageable (~1.5k-3k over 14d).
    """
    # EFTS requires non-empty q; the forms filter does the real work.
    hits = _efts_search(
        query="the",
        date_from=date_from,
        date_to=date_to,
        form_type=FORM4_FORMS,
        max_results=max_results,
        user_agent=user_agent,
    )
    return hits or []


# ---------------------------------------------------------------------------
# Primary-doc fetch + XML parse
# ---------------------------------------------------------------------------

def _primary_doc_url(cik: str, adsh: str) -> str:
    """Build the URL for the Form 4 primary XML document.

    Form 4 accession numbers are formatted '0000000000-00-000000'. The primary
    XML is at /Archives/edgar/data/{cik_stripped}/{adsh_no_dashes}/primary_doc.xml.
    """
    cs = cik.lstrip("0") or "0"
    ac = adsh.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cs}/{ac}/primary_doc.xml"


def _fetch_primary_doc(url: str, *, user_agent: str) -> Optional[bytes]:
    _rate_limiter.wait()
    try:
        resp = _http_get(url, headers={"User-Agent": user_agent}, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return resp.content
    except requests.exceptions.RequestException:
        return None
    return None


def _find_text(el: Optional[ET.Element], *paths: str) -> Optional[str]:
    """Walk a list of child paths; return the first non-empty text value found."""
    if el is None:
        return None
    for path in paths:
        found = el.find(path)
        if found is not None and found.text:
            t = found.text.strip()
            if t:
                return t
    return None


def _to_bool_flag(el: Optional[ET.Element]) -> bool:
    """Form 4 boolean flags appear as '1', '0', 'true', 'false' text."""
    if el is None or el.text is None:
        return False
    v = el.text.strip().lower()
    return v in {"1", "true", "yes"}


def _to_float(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    try:
        return float(text.replace(",", "").strip())
    except ValueError:
        return None


def _parse_form4(xml_bytes: bytes, *, accession: str, filing_url: str,
                 file_date: str) -> List[_Form4Transaction]:
    """Parse one Form 4 primary_doc.xml into discretionary non-derivative transactions.

    Filters at parse time:
      - Skip derivative-table entries (options, RSUs) — only non-derivative transactions
        count for insider-buying signal per the Dim 1 rubric.
      - Skip non-discretionary codes (M, G, A, F) — only P and S emit.
      - Flag 10b5-1 plan transactions so the aggregator can drop them.

    Note: the footnote-based 10b5-1 indicator is non-standard; we check BOTH
    `<rule10b5-1>true</rule10b5-1>` attributes on the transaction AND footnote
    text containing '10b5-1' / 'Rule 10b5-1'. Defaults to False on ambiguity.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    issuer_cik = _find_text(root, "issuer/issuerCik") or ""
    issuer_name = _find_text(root, "issuer/issuerName") or ""
    # zero-pad issuer CIK for consistency with SEC's canonical form.
    issuer_cik = issuer_cik.lstrip("0").zfill(10) if issuer_cik else ""

    # Collect footnotes by id for 10b5-1 detection.
    footnotes: Dict[str, str] = {}
    for fn in root.findall("footnotes/footnote"):
        fid = fn.attrib.get("id")
        if fid and fn.text:
            footnotes[fid] = fn.text

    txns: List[_Form4Transaction] = []

    # There may be multiple reporting owners on one Form 4 (rare but legal).
    for ro in root.findall("reportingOwner"):
        rptr_cik = _find_text(ro, "reportingOwnerId/rptOwnerCik") or ""
        rptr_name = _find_text(ro, "reportingOwnerId/rptOwnerName") or ""
        rel = ro.find("reportingOwnerRelationship")
        is_director = _to_bool_flag(rel.find("isDirector") if rel is not None else None)
        is_officer = _to_bool_flag(rel.find("isOfficer") if rel is not None else None)
        is_ten_percent = _to_bool_flag(
            rel.find("isTenPercentOwner") if rel is not None else None)
        officer_title = _find_text(rel, "officerTitle") if rel is not None else None
        role = _classify_role(is_director, is_officer, is_ten_percent, officer_title)
        rptr_norm = _reporter_normalized(rptr_name)

        for txn in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
            txn_date = _find_text(txn, "transactionDate/value") or ""
            coding = txn.find("transactionCoding")
            txn_code = _find_text(coding, "transactionCode") or ""
            if txn_code not in DISCRETIONARY_CODES:
                continue

            amounts = txn.find("transactionAmounts")
            shares = _to_float(_find_text(amounts, "transactionShares/value")) or 0.0
            price = _to_float(_find_text(amounts, "transactionPricePerShare/value"))
            ad = _find_text(amounts, "transactionAcquiredDisposedCode/value") or ""

            # 10b5-1 detection — transaction attribute OR footnote reference.
            is_10b51 = False
            if coding is not None:
                el_plan = coding.find("rule10b5-1")
                if el_plan is not None and _to_bool_flag(el_plan):
                    is_10b51 = True
                el_plan_alt = coding.find("rule10b5-1Indicator")
                if el_plan_alt is not None and _to_bool_flag(el_plan_alt):
                    is_10b51 = True
            if not is_10b51:
                # Check footnote references on this transaction.
                for fref in txn.findall(".//footnoteId"):
                    fid = fref.attrib.get("id")
                    if fid and fid in footnotes:
                        if re.search(r"\b10\s*b\s*5\s*[-\s]?\s*1\b|\brule\s+10b5-1\b",
                                     footnotes[fid], re.IGNORECASE):
                            is_10b51 = True
                            break

            value_usd = shares * price if (price is not None and shares) else None

            txns.append(_Form4Transaction(
                accession=accession,
                issuer_cik=issuer_cik,
                issuer_name=issuer_name,
                reporter_cik=rptr_cik,
                reporter_name=rptr_name,
                reporter_normalized=rptr_norm,
                role=role,
                is_director=is_director,
                is_officer=is_officer,
                is_ten_percent=is_ten_percent,
                officer_title=officer_title,
                txn_date=txn_date,
                txn_code=txn_code,
                shares=shares,
                price_per_share=price,
                acquired_disposed=ad,
                is_10b5_1=is_10b51,
                filing_url=filing_url,
                file_date=file_date,
                value_usd=value_usd,
            ))

    return txns


# ---------------------------------------------------------------------------
# Clustering + signal shaping
# ---------------------------------------------------------------------------

def _net_value_by_holder(txns: List[_Form4Transaction]) -> float:
    """Net signed value: buys positive, sells negative."""
    total = 0.0
    for t in txns:
        v = t.value_usd
        if v is None:
            # Fall back to notional share count only when both sides agree;
            # otherwise skip.
            continue
        if t.txn_code == TXN_CODE_PURCHASE and t.acquired_disposed == "A":
            total += v
        elif t.txn_code == TXN_CODE_SALE and t.acquired_disposed == "D":
            total -= v
    return total


def _cluster_meets_gate(holders: Dict[str, Dict[str, Any]], direction: str) -> bool:
    """Triage gate: require >=2 holders OR a single high-signal pattern.

    Matches the profile rubric's 'Single minor insider → 1' band (which would
    auto-score <15 and archive anyway), so we don't emit single-minor events.
    Solo C-suite buys and solo 10%-holder events ARE emitted as dedicated
    signal_types (c_suite_open_market_buy / ten_percent_holder_buy) upstream
    of this gate.
    """
    if direction == "buy":
        # Solo C-suite or solo 10%-holder buys already route to their own
        # signal_type; this gate handles the aggregate cluster case only.
        if len(holders) >= 2:
            return True
        return False
    # Sell-side: require >=2 holders, since any one insider selling is noise
    # without confirmation from peers.
    return len(holders) >= 2


def _count_tiers(holders: Dict[str, Dict[str, Any]]) -> Tuple[int, int, int, int]:
    """Return (c_suite_count, vp_count, director_only_count, ten_percent_count)."""
    cs = vp = do = tp = 0
    for h in holders.values():
        r = h.get("role")
        if r == "csuite":
            cs += 1
        elif r == "vp":
            vp += 1
        elif r == "director_only":
            do += 1
        elif r == "ten_percent_only":
            tp += 1
    return cs, vp, do, tp


def _iso_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _coerce_signal_limit(value: Any, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _project_form4_score(sig: Signal) -> float:
    """Heuristic ranking score for top_signal_limit selection only.

    Uses the preserved short_positioning heuristic (Form 4 cluster branch).
    Score is NOT persisted — actual dim resolution happens via signal_resolver
    after emission, since short_positioning is registered to `_estimate_none`.
    """
    estimate = project_short_positioning_heuristic(sig.raw_payload)
    if estimate is None:
        return 0.0
    return float(weighted_total(estimate.dimensions, "short_positioning"))


def _form4_signal_priority(sig: Signal) -> Tuple[int, float, int, float, str, str]:
    raw = sig.raw_payload
    holder_count = raw.get("holder_count") or 0
    total_value = raw.get("total_value_usd") or 0.0
    return (
        _SIGNAL_TYPE_PRIORITY.get(sig.signal_type, 0),
        _project_form4_score(sig),
        int(holder_count),
        float(total_value),
        sig.source_date.isoformat(),
        sig.signal_id,
    )


def _apply_form4_top_signal_limit(
    signals: List[Signal],
    limit: int,
) -> Tuple[List[Signal], List[Signal]]:
    if limit == 0 or len(signals) <= limit:
        return signals, []
    ranked = sorted(signals, key=_form4_signal_priority, reverse=True)
    return ranked[:limit], ranked[limit:]


def _form4_signal_type_breakdown(signals: List[Signal]) -> str:
    counts: Dict[str, int] = {}
    for sig in signals:
        counts[sig.signal_type] = counts.get(sig.signal_type, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        raise MissingAuthError(
            "SEC_USER_AGENT env var missing — SEC requires a valid contact email "
            "in the User-Agent header. Set via Modal secret `scanner-secrets`.")

    client = SupabaseClient()
    from modal_workers.shared.openfigi_resolver import set_cache_backend
    set_cache_backend(*client.openfigi_cache_backend())

    budget_s = max(20, (cfg.timeout_soft_s or DEFAULT_WALL_CLOCK_S) - 10)
    scan_start = time.time()
    scan_date = datetime.now(timezone.utc)
    date_from = (scan_date - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to = scan_date.strftime("%Y-%m-%d")

    warnings: List[str] = []
    fetched = 0
    parsed = 0
    skipped_10b5_1 = 0
    budget_exhausted = False

    # ---- 1. List Form 4 filings in window ------------------------------------
    try:
        hits = _list_form4_filings(date_from, date_to, user_agent=user_agent,
                                   max_results=MAX_FILINGS_PER_RUN)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"form4 efts list: {type(e).__name__}: {e}")
        hits = []
    fetched = len(hits)

    # ---- 2. Fetch + parse each primary doc -----------------------------------
    all_txns: List[_Form4Transaction] = []
    for hit in hits:
        if time.time() - scan_start > budget_s * 0.75:
            budget_exhausted = True
            warnings.append("form4 fetch: soft budget reached")
            break
        ciks = hit.get("ciks") or hit.get("cik") or []
        if isinstance(ciks, str):
            ciks = [ciks]
        primary_cik = ciks[0] if ciks else hit.get("cik", "")
        adsh = hit.get("adsh") or ""
        file_date = hit.get("file_date") or ""
        if not primary_cik or not adsh:
            continue
        url = _primary_doc_url(primary_cik, adsh)
        body = _fetch_primary_doc(url, user_agent=user_agent)
        if body is None:
            continue
        txns = _parse_form4(body, accession=adsh, filing_url=url, file_date=file_date)
        # Drop 10b5-1 plan transactions here — they're noise per profile spec.
        discretionary = [t for t in txns if not t.is_10b5_1]
        skipped_10b5_1 += len(txns) - len(discretionary)
        all_txns.extend(discretionary)
        parsed += 1

    # ---- 3. Aggregate by (issuer, direction) within 30d window ---------------
    window_cutoff = (scan_date - timedelta(days=CLUSTER_WINDOW_DAYS)).strftime("%Y-%m-%d")
    clusters: Dict[Tuple[str, str], _IssuerCluster] = {}
    for t in all_txns:
        if t.txn_date and t.txn_date < window_cutoff:
            continue
        if not t.issuer_cik:
            continue
        if t.txn_code == TXN_CODE_PURCHASE and t.acquired_disposed == "A":
            direction = "buy"
        elif t.txn_code == TXN_CODE_SALE and t.acquired_disposed == "D":
            direction = "sell"
        else:
            continue
        key = (t.issuer_cik, direction)
        cluster = clusters.get(key)
        if cluster is None:
            cluster = _IssuerCluster(
                issuer_cik=t.issuer_cik,
                issuer_name=t.issuer_name,
                direction=direction,
            )
            clusters[key] = cluster
        h = cluster.holders.setdefault(t.reporter_normalized, {
            "reporter_name": t.reporter_name,
            "reporter_cik": t.reporter_cik,
            "role": t.role,
            "officer_title": t.officer_title,
            "txns": [],
            "net_value_usd": 0.0,
            "latest_date": "",
        })
        # Upgrade role if a later txn reports a more senior role (same person may
        # reappear with a refined title).
        _ROLE_ORDER = {"csuite": 4, "vp": 3, "ten_percent_only": 2,
                       "director_only": 1, "minor": 0}
        if _ROLE_ORDER.get(t.role, 0) > _ROLE_ORDER.get(h["role"], 0):
            h["role"] = t.role
            h["officer_title"] = t.officer_title or h["officer_title"]
        h["txns"].append({
            "accession": t.accession,
            "txn_date": t.txn_date,
            "code": t.txn_code,
            "shares": t.shares,
            "price": t.price_per_share,
            "value_usd": t.value_usd,
        })
        if t.value_usd is not None:
            sign = 1 if direction == "buy" else -1
            h["net_value_usd"] += sign * t.value_usd
        if t.txn_date > h["latest_date"]:
            h["latest_date"] = t.txn_date
        cluster.contributing_accessions.add(t.accession)
        if t.txn_date:
            if not cluster.latest_txn_date or t.txn_date > cluster.latest_txn_date:
                cluster.latest_txn_date = t.txn_date
            if not cluster.earliest_txn_date or t.txn_date < cluster.earliest_txn_date:
                cluster.earliest_txn_date = t.txn_date

    # ---- 4. Filter per-holder below min-value; prune empty clusters ----------
    for cluster in list(clusters.values()):
        for hk in list(cluster.holders.keys()):
            if abs(cluster.holders[hk]["net_value_usd"]) < MIN_NET_VALUE_USD:
                # Preserve if the holder is C-suite or 10%-holder — their
                # presence matters even if notional < $50k.
                if cluster.holders[hk]["role"] not in {"csuite", "ten_percent_only"}:
                    cluster.holders.pop(hk)
        if not cluster.holders:
            clusters.pop((cluster.issuer_cik, cluster.direction), None)

    # ---- 5. Build signals ----------------------------------------------------
    signals: List[Signal] = []
    for (issuer_cik, direction), cluster in clusters.items():
        c_suite, vp_count, director_only, ten_pct = _count_tiers(cluster.holders)
        holder_count = len(cluster.holders)

        # Classify signal_type:
        # - Multi-holder → insider_cluster_{buy|sell}
        # - Solo C-suite buy  → c_suite_open_market_buy
        # - Solo 10%-holder buy → ten_percent_holder_buy
        # - Solo minor/director/VP/etc. → below gate (drop)
        if direction == "buy" and holder_count == 1:
            only = next(iter(cluster.holders.values()))
            if only["role"] == "csuite":
                signal_type = "c_suite_open_market_buy"
            elif only["role"] == "ten_percent_only":
                signal_type = "ten_percent_holder_buy"
            else:
                continue  # below gate
        elif holder_count >= 2:
            signal_type = "insider_cluster_buy" if direction == "buy" else "insider_cluster_sell"
        else:
            # Solo sell (any role) or solo non-csuite/non-10%  → noise
            continue

        # Strength estimate: 5 for C-suite-dense clusters, 4 for mixed, 3 otherwise.
        if c_suite >= 3:
            strength = 5
        elif c_suite >= 2 and vp_count >= 1:
            strength = 4
        elif c_suite >= 1 or holder_count >= 4:
            strength = 4
        elif holder_count >= 3:
            strength = 3
        else:
            strength = 3  # 2-holder cluster

        # thesis_direction: buys long, sells short. (Solo C-suite buy / 10%
        # holder buy always long.)
        thesis_direction = "long" if direction == "buy" else "short"

        # Resolve ticker/exchange for entity + market-cap snapshot.
        from modal_workers.scanners.edgar_filing_monitor import _get_company_tickers
        tickers, exchange = _get_company_tickers(issuer_cik, user_agent=user_agent)
        ticker = tickers[0] if tickers else None

        issuer_figi: Optional[str] = None
        if ticker:
            try:
                from modal_workers.shared.openfigi_resolver import resolve_ticker
                res = resolve_ticker(ticker, exch_code="US")
                if res.resolved:
                    issuer_figi = res.issuer_figi
            except Exception:
                pass

        # Aggregate totals for raw_payload.
        total_value_usd = sum(abs(h["net_value_usd"]) for h in cluster.holders.values())
        total_shares = sum(sum(t["shares"] for t in h["txns"])
                           for h in cluster.holders.values())

        # Build holders payload in ESMA-compatible shape + Form 4 extensions.
        # `insider_cluster: True` routes to the Form 4 branch of the short_positioning
        # dim estimator.
        holders_payload = [
            {
                "holder_name": h["reporter_name"],
                "reporter_cik": h["reporter_cik"],
                "role": h["role"],
                "officer_title": h["officer_title"],
                "net_value_usd": h["net_value_usd"],
                "position_date": h["latest_date"] or cluster.latest_txn_date,
                "txn_count": len(h["txns"]),
            }
            for h in cluster.holders.values()
        ]

        raw_payload: Dict[str, Any] = {
            # Routing key: dim_estimator.project_short_positioning_heuristic branches
            # on this to apply the Form 4 rubric rather than the ESMA rubric.
            "insider_cluster": True,
            "direction": direction,
            # ESMA-compatible keys for downstream tools that assume that shape:
            "holders": holders_payload,
            "holder_count": holder_count,
            "regulators": ["SEC"],
            # Form 4-specific:
            "issuer_cik": issuer_cik,
            "issuer_name": cluster.issuer_name,
            "c_suite_count": c_suite,
            "vp_count": vp_count,
            "director_only_count": director_only,
            "ten_percent_holder_count": ten_pct,
            "total_value_usd": total_value_usd,
            "total_shares": total_shares,
            "contributing_accessions": sorted(cluster.contributing_accessions),
            "earliest_txn_date": cluster.earliest_txn_date,
            "latest_txn_date": cluster.latest_txn_date,
            "tickers": [ticker] if ticker else [],
            "exchange": exchange,
        }
        # Market snapshot enriches size_vs_float + liquidity dims.
        if ticker:
            try:
                from modal_workers.shared.market_snapshot import load_market_snapshot
                snapshot = load_market_snapshot(ticker, client=client)
                if snapshot:
                    raw_payload.update(snapshot)
            except Exception as e:  # noqa: BLE001
                from modal_workers.observability import record_snapshot_fetch_failure
                record_snapshot_fetch_failure(
                    client, scanner_name=NAME, ticker=ticker, exc=e)

        # Dedup/signal keys.
        sources_sorted = sorted(cluster.contributing_accessions)
        src_content_hash = _content_hash(issuer_cik, direction, sources_sorted)
        sig_id = _signal_id(issuer_cik, direction,
                            cluster.earliest_txn_date or date_from,
                            cluster.latest_txn_date or date_to)

        # source_date = the latest txn date in the cluster (not file_date — that's
        # when the form was filed, but we care about the economic event).
        try:
            source_date = datetime.strptime(
                cluster.latest_txn_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            source_date = scan_date

        entity_hints = EntityHints(
            issuer_figi=issuer_figi,
            ticker=ticker,
            mic=None,
            cik=issuer_cik,
            name=cluster.issuer_name or None,
            country="US",
        )

        # source_url points to the most recent contributing filing for human
        # auditability. Frontend links directly to this.
        latest_url = ""
        for h in cluster.holders.values():
            for t in h["txns"]:
                if t["txn_date"] == cluster.latest_txn_date:
                    # Reconstruct filing URL from the most recent accession.
                    adsh_last = t["accession"]
                    cs = issuer_cik.lstrip("0") or "0"
                    ac = adsh_last.replace("-", "")
                    latest_url = f"https://www.sec.gov/Archives/edgar/data/{cs}/{ac}"
                    break
            if latest_url:
                break

        signals.append(Signal(
            signal_id=sig_id,
            source_content_hash=src_content_hash,
            source_date=source_date,
            scan_date=scan_date,
            signal_type=signal_type,
            raw_payload=raw_payload,
            source_url=latest_url or None,
            issuer_figi=issuer_figi,
            entity_hints=entity_hints,
            thesis_direction=thesis_direction,
            strength_estimate=strength,
        ))

    pre_cap_count = len(signals)
    top_signal_limit = _coerce_signal_limit(
        cfg.config.get("top_signal_limit") if cfg.config else None,
        TOP_SIGNAL_LIMIT_DEFAULT,
    )
    kept_signals, dropped_signals = _apply_form4_top_signal_limit(
        signals, top_signal_limit,
    )
    if dropped_signals:
        warnings.append(
            f"top_signal_limit: kept {len(kept_signals)}/{pre_cap_count} "
            f"(limit={top_signal_limit}); dropped_by_type: "
            f"{_form4_signal_type_breakdown(dropped_signals)}"
        )

    status = "partial" if (budget_exhausted or warnings) else "ok"

    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=kept_signals,
        warnings=warnings,
        fetched_records=fetched,
        run_metrics={
            "filings_parsed": parsed,
            "10b5_1_skipped_txns": skipped_10b5_1,
            "clusters_emitted": pre_cap_count,
            "clusters_kept": len(kept_signals),
            "top_signal_capped": len(dropped_signals),
        },
    )
