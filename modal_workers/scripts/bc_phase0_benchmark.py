"""bc_phase0_benchmark — Phase 0 GO/NO-GO benchmark for the BC-FDA universe source.

THE GATE deliverable (spec §2.2/§2.3/§4). Runs the **approach-1** enumerator
(`bc_universe_pdufa`) over the truth-set window **READ-ONLY** (never `--apply`),
scores it against the hand-built truth set, prints the §2.3 weighted rubric and
the §4 GO/NO-GO verdict, and records the §1 assessments of approaches 2 (paid
calendar) and 3 (FDA-primary) so the recommendation is auditable.

What it computes (per the winning approach = approach 1), against the truth set
at ``modal_workers/fetchers/universe/testdata/bc_pdufa_truthset.json``:

  - **recall-in-window** = surfaced pending-in-window truth catalysts / all pending
    truth catalysts (the §2.3 0.35-weighted term). Also overall recall + a
    market-cap-bucket breakdown (exposes the 8-K large-cap skew quantitatively).
  - **date-exact-rate** = of surfaced catalysts, fraction with |extracted − true|
    == 0 days, plus the 0 / ≤7 / ≤30 / >30 day-bucket distribution (§2.2).
  - **false-positive-rate** (CORRECTED — see ``_score_false_positives``) = of the
    **truth-covered in-window slice** (emitted dated candidates that are in-window
    AND from a sponsor the truth set covers), the fraction that do NOT correctly bind
    to a covered truth row (a phantom/contradicting catalyst for that sponsor). An
    emitted date for a real catalyst the 37-row truth set simply doesn't enumerate is
    **NOT** counted — that is truth-set incompleteness, not a precision failure. The
    old unrestricted 74-vs-37 proxy (which wrongly counted those) is still reported as
    ``false_pos_rate_raw`` for transparency but does not gate.
  - **latency** = filing-date → PDUFA-date lead time distribution (the 8-K *is*
    the disclosure, so detect-latency ≈ 0 on a daily cron; we report disclosure
    lead instead, which is the operationally meaningful number).
  - **cost** = $0 marginal (SEC EFTS + openFDA free) + ToS verdict (clean).

Approaches 2 and 3 are **assessed, not benchmarked** (no scraper, no paid call):
their verdicts are printed from the spec's planning findings + a live FedReg /
Drugs@FDA forward-date confirm so the GO/NO-GO names what each rejected approach
would add.

Run (read-only; reads live EFTS + Polygon + openFDA, writes NOTHING):
    SEC_USER_AGENT="Name x@y.com" POLYGON_API_KEY=... OPENFDA_API_KEY=... \\
    python3 -m modal_workers.scripts.bc_phase0_benchmark \\
        --window-days 120 --json-out /tmp/bc_phase0_benchmark.json

If the truth set file is absent (the truth-set agent owns it), the benchmark
still runs the live enumerator and reports the universe-size gate (§4.1 crit 1–2),
but skips the date-accuracy / recall / FP terms that require truth — and says so.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logger = logging.getLogger("bc_phase0_benchmark")

# Default truth-set location (the truth-set agent writes this; we only read it).
_TRUTHSET_PATH = (
    _REPO_ROOT / "modal_workers" / "fetchers" / "universe" / "testdata"
    / "bc_pdufa_truthset.json"
)

# §2.3 rubric weights.
_W_RECALL = 0.35
_W_DATE_EXACT = 0.25
_W_FP = 0.15
_W_REPRO = 0.15
_W_COST = 0.10

# §4.1 GO thresholds.
_GATE_IN_WINDOW = 15
_GATE_TRADEABLE = 12
_GATE_DATE_EXACT = 0.80
_GATE_FP_MAX = 0.15

# Market-cap bucket edges (spec §2.2: micro <$250M / small $250M–2B / mid+ >$2B).
_SMALL_FLOOR = 250_000_000
_MID_FLOOR = 2_000_000_000


# ---------------------------------------------------------------------------
# Truth set
# ---------------------------------------------------------------------------

@dataclass
class TruthRow:
    ticker: Optional[str]
    cik: Optional[str]
    drug: Optional[str]
    true_pdufa_date: Optional[str]
    appl_number: Optional[str] = None
    appl_type: Optional[str] = None
    status: Optional[str] = None          # 'pending' | 'resolved' (if provided)
    market_cap_bucket: Optional[str] = None
    source: Optional[str] = None


def _norm_cik(cik: Optional[str]) -> Optional[str]:
    if cik is None:
        return None
    s = str(cik).lstrip("0")
    return s or "0"


def load_truthset(path: Path) -> Tuple[List[TruthRow], Optional[str]]:
    """Load the truth-set JSON. Returns (rows, error_or_None). Missing file is a
    soft condition — we return ([], message) so the benchmark degrades, not crashes."""
    if not path.exists():
        return [], f"truth set not found at {path} (truth-set agent owns it)"
    try:
        raw = json.loads(path.read_text())
    except Exception as e:  # noqa: BLE001
        return [], f"truth set unreadable: {type(e).__name__}: {e}"
    if not isinstance(raw, list):
        return [], "truth set is not a JSON array"
    rows: List[TruthRow] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        rows.append(TruthRow(
            ticker=(r.get("ticker") or None),
            cik=_norm_cik(r.get("cik")),
            drug=(r.get("drug") or None),
            true_pdufa_date=(r.get("true_pdufa_date") or r.get("pdufa_date") or None),
            appl_number=(r.get("appl_number") or r.get("application_number") or None),
            appl_type=(r.get("appl_type") or None),
            status=(r.get("status") or None),
            market_cap_bucket=(r.get("market_cap_bucket") or None),
            source=(r.get("source") or None),
        ))
    return rows, None


# ---------------------------------------------------------------------------
# Matching enumerator candidates ↔ truth rows
# ---------------------------------------------------------------------------

def _cand_keys(cand: Any) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    return (
        _norm_cik(getattr(cand, "cik", None)),
        (getattr(cand, "ticker", None) or None),
        ((getattr(cand, "drug_name", None) or "").strip().lower() or None),
    )


def _sponsor_matches(truth: TruthRow, cand: Any) -> bool:
    """True iff the candidate is from the SAME SPONSOR as the truth row (CIK or
    ticker). This is the sponsor-identity key — NOT enough on its own to bind a
    specific drug's truth row when the sponsor has several pending applications."""
    c_cik, c_ticker, _ = _cand_keys(cand)
    if truth.cik and c_cik and truth.cik == c_cik:
        return True
    if truth.ticker and c_ticker and truth.ticker.upper() == c_ticker.upper():
        return True
    return False


def _drug_matches(truth: TruthRow, cand: Any) -> bool:
    """True iff the truth row's drug name and the candidate's drug name overlap
    (case-insensitive substring, either direction)."""
    _, _, c_drug = _cand_keys(cand)
    if not (truth.drug and c_drug):
        return False
    td = truth.drug.strip().lower()
    return bool(td) and (td == c_drug or td in c_drug or c_drug in td)


def _matches(truth: TruthRow, cand: Any) -> bool:
    """A truth row *could* correspond to a candidate if the SPONSOR matches (CIK or
    ticker) OR the drug name matches. This is the loose candidacy gate; picking the
    RIGHT candidate among several from the same sponsor is done by ``_match_rank``
    (drug-level disambiguation — see ``score_against_truth``).

    Kept for backward-compatibility and used as the membership predicate; the
    selection logic no longer binds to the *first* row that returns True here."""
    return _sponsor_matches(truth, cand) or _drug_matches(truth, cand)


# Match-strength tiers (higher == stronger binding). Used to pick the single best
# candidate for a truth row so a multi-drug sponsor is scored against the RIGHT
# application instead of whichever same-CIK candidate happened to come first.
_RANK_SPONSOR_AND_DRUG = 3   # same sponsor AND drug overlap -> unambiguous
_RANK_DRUG_ONLY = 2          # drug overlap but sponsor unknown/mismatched (truth lacks CIK)
_RANK_SPONSOR_ONLY = 1       # same sponsor only -> acceptable ONLY when unambiguous
_RANK_NONE = 0


def _match_rank(truth: TruthRow, cand: Any) -> int:
    """Rank how strongly ``cand`` binds to ``truth`` (see _RANK_* tiers)."""
    s = _sponsor_matches(truth, cand)
    d = _drug_matches(truth, cand)
    if s and d:
        return _RANK_SPONSOR_AND_DRUG
    if d:
        return _RANK_DRUG_ONLY
    if s:
        return _RANK_SPONSOR_ONLY
    return _RANK_NONE


def _select_match(truth: TruthRow, candidates: List[Any], used_ids: set) -> Optional[Any]:
    """Pick the single best candidate for a truth row with drug-level
    disambiguation (fixes the multi-drug-sponsor mis-scoring):

      1. Prefer the strongest binding tier (sponsor+drug > drug-only > sponsor-only).
      2. A **sponsor-only** match is accepted only when it is UNAMBIGUOUS — i.e. the
         truth row carries no drug name, OR the sponsor has exactly one candidate.
         If the truth row HAS a drug but the sponsor has several candidates and none
         match the drug, we do NOT bind a wrong-drug same-CIK candidate (that is the
         exact 0.767->0.818 bug: IONS olezarsen scored against zilganersen).
      3. Among equally-ranked candidates, prefer one not already bound to another
         truth row, so two truth rows from the same sponsor don't collapse onto one
         candidate; ties then break on the smallest date distance to the truth date.
    """
    ranked = [(c, _match_rank(truth, c)) for c in candidates]
    ranked = [(c, r) for c, r in ranked if r > _RANK_NONE]
    if not ranked:
        return None
    best_rank = max(r for _, r in ranked)

    # Guard: a SPONSOR-ONLY best match is only trustworthy when unambiguous.
    if best_rank == _RANK_SPONSOR_ONLY and truth.drug:
        sponsor_cands = [c for c in candidates if _sponsor_matches(truth, c)]
        if len(sponsor_cands) > 1:
            # Sponsor has multiple pending apps and NONE matched this drug ->
            # refuse to bind a wrong-drug candidate. Treat as not surfaced.
            return None

    pool = [c for c, r in ranked if r == best_rank]
    # Prefer an unused candidate; then the closest date.
    def _key(c: Any):
        diff = _days_diff(getattr(c, "pdufa_date", None), truth.true_pdufa_date)
        return (id(c) in used_ids, diff if diff is not None else 10 ** 9)
    pool.sort(key=_key)
    return pool[0]


def _days_diff(a_iso: Optional[str], b_iso: Optional[str]) -> Optional[int]:
    try:
        a = datetime.strptime(a_iso, "%Y-%m-%d").date()
        b = datetime.strptime(b_iso, "%Y-%m-%d").date()
        return abs((a - b).days)
    except (ValueError, TypeError):
        return None


@dataclass
class ScoreResult:
    n_truth: int = 0
    n_truth_pending: int = 0
    n_truth_in_window: int = 0
    surfaced: int = 0
    surfaced_pending: int = 0
    surfaced_in_window: int = 0
    date_exact: int = 0
    date_buckets: Dict[str, int] = field(default_factory=lambda: {"0": 0, "<=7": 0, "<=30": 0, ">30": 0})
    bucket_recall: Dict[str, Dict[str, int]] = field(default_factory=dict)  # bucket -> {truth, surfaced}
    false_positives: int = 0
    emitted_with_date: int = 0
    # corrected FP slice (see _score_false_positives): denominator/numerator are
    # restricted to emitted in-window dates for TRUTH-COVERED sponsors only.
    fp_eval_pool: int = 0                 # emitted in-window dated cands for covered sponsors
    fp_contradictions: int = 0            # of those, the ones that don't correctly match a covered truth row
    fp_examples: List[Dict[str, Any]] = field(default_factory=list)
    per_truth: List[Dict[str, Any]] = field(default_factory=list)

    # derived rates (filled by finalize)
    recall_overall: float = 0.0
    recall_in_window: float = 0.0
    date_exact_rate: float = 0.0
    false_pos_rate: float = 0.0
    false_pos_rate_raw: float = 0.0       # the OLD unrestricted proxy (kept for transparency)


def _bucket_of(truth: TruthRow, matched_cand: Optional[Any]) -> str:
    """Resolve a market-cap bucket: prefer the truth row's declared bucket; else
    derive from the matched candidate's market cap; else 'unknown'."""
    if truth.market_cap_bucket:
        return truth.market_cap_bucket
    mc = getattr(matched_cand, "market_cap_usd", None) if matched_cand else None
    if mc is None:
        return "unknown"
    if mc < _SMALL_FLOOR:
        return "micro"
    if mc < _MID_FLOOR:
        return "small"
    return "mid+"


def score_against_truth(truth_rows: List[TruthRow], candidates: List[Any],
                        window_days: int, today: date) -> ScoreResult:
    """Compute recall / date-accuracy / FP of the enumerator vs the truth set."""
    res = ScoreResult(n_truth=len(truth_rows))
    matched_cand_ids: set = set()

    for truth in truth_rows:
        is_pending = (truth.status or "").lower() == "pending"
        in_window = False
        if truth.true_pdufa_date:
            dd = _days_diff(truth.true_pdufa_date, today.isoformat())
            try:
                tdate = datetime.strptime(truth.true_pdufa_date, "%Y-%m-%d").date()
                delta = (tdate - today).days
                in_window = 0 <= delta <= window_days
            except (ValueError, TypeError):
                in_window = False
        if is_pending:
            res.n_truth_pending += 1
        if in_window:
            res.n_truth_in_window += 1

        # find the best matching candidate with drug-level disambiguation
        # (NOT the first same-CIK candidate — that mis-scores multi-drug sponsors).
        match = _select_match(truth, candidates, matched_cand_ids)

        bucket = _bucket_of(truth, match)
        res.bucket_recall.setdefault(bucket, {"truth": 0, "surfaced": 0})
        res.bucket_recall[bucket]["truth"] += 1

        rec = {
            "truth_ticker": truth.ticker, "truth_drug": truth.drug,
            "true_pdufa_date": truth.true_pdufa_date, "status": truth.status,
            "in_window": in_window, "bucket": bucket, "surfaced": match is not None,
        }
        if match is not None:
            res.surfaced += 1
            res.bucket_recall[bucket]["surfaced"] += 1
            matched_cand_ids.add(id(match))
            if is_pending:
                res.surfaced_pending += 1
            if in_window:
                res.surfaced_in_window += 1
            extracted = getattr(match, "pdufa_date", None)
            rec["extracted_pdufa_date"] = extracted
            diff = _days_diff(extracted, truth.true_pdufa_date)
            rec["date_diff_days"] = diff
            if diff is not None:
                if diff == 0:
                    res.date_exact += 1
                    res.date_buckets["0"] += 1
                elif diff <= 7:
                    res.date_buckets["<=7"] += 1
                elif diff <= 30:
                    res.date_buckets["<=30"] += 1
                else:
                    res.date_buckets[">30"] += 1
        res.per_truth.append(rec)

    _score_false_positives(res, truth_rows, candidates, matched_cand_ids,
                           window_days, today)
    return _finalize(res)


def _cand_in_window(cand: Any, window_days: int, today: date) -> bool:
    """True iff the candidate's parsed PDUFA date is 0..window_days out from today."""
    pd = getattr(cand, "pdufa_date", None)
    if not pd:
        return False
    try:
        d = datetime.strptime(pd, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return 0 <= (d - today).days <= window_days


def _truth_sponsor_keys(truth_rows: List[TruthRow]) -> Tuple[set, set]:
    """Return (cik_set, ticker_set) of sponsors PRESENT in the truth set. Used to
    restrict the FP metric to the truth-covered slice."""
    ciks = {t.cik for t in truth_rows if t.cik}
    tickers = {t.ticker.upper() for t in truth_rows if t.ticker}
    return ciks, tickers


def _truth_dates_by_sponsor(truth_rows: List[TruthRow]) -> Dict[str, set]:
    """Map each sponsor key (``cik:<n>`` and ``ticker:<T>``) to the SET of PDUFA dates
    the truth set lists for that sponsor. An emitted in-window date that IS in its
    sponsor's truth-date set is corroborating (not an FP); one that is NOT is a
    contradiction (a date the truth set says the sponsor does not have)."""
    out: Dict[str, set] = {}
    for t in truth_rows:
        if not t.true_pdufa_date:
            continue
        if t.cik:
            out.setdefault(f"cik:{t.cik}", set()).add(t.true_pdufa_date)
        if t.ticker:
            out.setdefault(f"ticker:{t.ticker.upper()}", set()).add(t.true_pdufa_date)
    return out


def _sponsor_truth_dates(c_cik: Optional[str], c_ticker: Optional[str],
                         dates_by_sponsor: Dict[str, set]) -> set:
    """Union of truth PDUFA dates for the candidate's sponsor (by CIK and/or ticker)."""
    acc: set = set()
    if c_cik is not None:
        acc |= dates_by_sponsor.get(f"cik:{c_cik}", set())
    if c_ticker is not None:
        acc |= dates_by_sponsor.get(f"ticker:{c_ticker.upper()}", set())
    return acc


def _score_false_positives(res: ScoreResult, truth_rows: List[TruthRow],
                           candidates: List[Any], matched_cand_ids: set,
                           window_days: int, today: date) -> None:
    """Corrected false-positive definition (fixes the structurally-invalid 74-vs-37 proxy).

    THE PROBLEM with the old proxy: it scored *every* emitted dated candidate ROW against
    only the 37-row truth set, so (a) an emitted date for a **real catalyst the hand-built
    truth set simply doesn't enumerate** was counted as a "false positive," and (b) the
    SAME application disclosed across multiple 8-Ks (e.g. VRDN's 06-30 date filed 4×) was
    counted 4× — both inflate FP to ~0.66–0.76 even when the enumerator is right. Neither
    is a precision failure.

    THE CORRECTED METRIC (matches the gate's spec wording: *"only count as FP an emitted
    date that contradicts a truth row it matches, or is in-window for a sponsor the truth
    set marks as having no such catalyst"*):

      1. **Restrict to the truth-covered slice.** Consider only emitted candidates that are
         (a) dated, (b) in-window, and (c) from a sponsor the truth set COVERS (CIK or
         ticker present). Candidates for sponsors ABSENT from the truth set are real
         catalysts out of scope — excluded, never FPs.
      2. **Dedup to distinct (sponsor, emitted-date) pairs** so duplicate 8-Ks about one
         application count once (the gate counts distinct applications too).
      3. **FP = a contradiction**: a distinct in-window (sponsor, emitted-date) pair whose
         date is NOT among the PDUFA dates the truth set lists for that sponsor. A pair
         whose date MATCHES one of its sponsor's truth dates is corroborating — NOT an FP,
         even if the drug name didn't parse or a duplicate already claimed the binding. (A
         candidate that bound a truth row but with a wrong date is already penalized by
         ``date_exact_rate``; an *additional* emitted date the sponsor doesn't have is the
         genuine false positive.)

      - **false_pos_rate** = fp_contradictions / fp_eval_pool (0.0 when the pool is empty),
        where the pool is the count of distinct in-window (sponsor, date) pairs for covered
        sponsors.

    The OLD unrestricted row-level proxy is still computed as ``false_pos_rate_raw`` for
    transparency, but the GATE reads the corrected ``false_pos_rate``.
    """
    truth_ciks, truth_tickers = _truth_sponsor_keys(truth_rows)
    dates_by_sponsor = _truth_dates_by_sponsor(truth_rows)

    # Build the deduped pool of distinct (sponsor-key, emitted-date) pairs for
    # covered sponsors, in-window only.
    seen_pairs: set = set()
    for cand in candidates:
        pd = getattr(cand, "pdufa_date", None)
        if not pd:
            continue
        if not _cand_in_window(cand, window_days, today):
            continue
        c_cik, c_ticker, _ = _cand_keys(cand)
        sponsor_covered = (
            (c_cik is not None and c_cik in truth_ciks)
            or (c_ticker is not None and c_ticker.upper() in truth_tickers)
        )
        if not sponsor_covered:
            continue  # real catalyst outside the truth set's scope -> NOT an FP
        sponsor_key = c_cik if c_cik is not None else (c_ticker or "").upper()
        pair = (sponsor_key, pd)
        if pair in seen_pairs:
            continue  # duplicate 8-K about the same application -> count once
        seen_pairs.add(pair)
        res.fp_eval_pool += 1

        sponsor_dates = _sponsor_truth_dates(c_cik, c_ticker, dates_by_sponsor)
        if pd not in sponsor_dates:
            # An emitted in-window date the truth set does NOT list for this covered
            # sponsor -> a genuine contradiction / phantom catalyst: a true FP.
            res.fp_contradictions += 1
            if len(res.fp_examples) < 20:
                res.fp_examples.append({
                    "cik": c_cik, "ticker": c_ticker,
                    "drug": (getattr(cand, "drug_name", None) or None),
                    "pdufa_date": pd,
                    "sponsor_truth_dates": sorted(sponsor_dates),
                })

    # Raw (old) proxy: every dated candidate ROW not bound to a truth row (kept only
    # for transparency in the report; does NOT gate).
    res.emitted_with_date = sum(1 for c in candidates if getattr(c, "pdufa_date", None))
    res.false_positives = sum(
        1 for c in candidates
        if getattr(c, "pdufa_date", None) and id(c) not in matched_cand_ids
    )


def _finalize(res: ScoreResult) -> ScoreResult:
    res.recall_overall = round(res.surfaced / res.n_truth, 3) if res.n_truth else 0.0
    res.recall_in_window = (
        round(res.surfaced_in_window / res.n_truth_in_window, 3)
        if res.n_truth_in_window else 0.0
    )
    res.date_exact_rate = round(res.date_exact / res.surfaced, 3) if res.surfaced else 0.0
    # Corrected FP rate: restricted to the truth-covered in-window slice.
    res.false_pos_rate = (
        round(res.fp_contradictions / res.fp_eval_pool, 3)
        if res.fp_eval_pool else 0.0
    )
    # Old unrestricted proxy, kept for transparency in the report.
    res.false_pos_rate_raw = (
        round(res.false_positives / res.emitted_with_date, 3)
        if res.emitted_with_date else 0.0
    )
    return res


# ---------------------------------------------------------------------------
# §2.3 weighted rubric
# ---------------------------------------------------------------------------

def rubric_score(*, recall_in_window: float, date_exact_rate: float,
                 false_pos_rate: float, reproducible_daily: float,
                 cost_score: float) -> Dict[str, Any]:
    terms = {
        "recall_in_window": _W_RECALL * recall_in_window,
        "date_exact_rate": _W_DATE_EXACT * date_exact_rate,
        "false_pos_inv": _W_FP * (1.0 - false_pos_rate),
        "reproducible_daily": _W_REPRO * reproducible_daily,
        "cost_score": _W_COST * cost_score,
    }
    return {"terms": {k: round(v, 4) for k, v in terms.items()},
            "winner_score": round(sum(terms.values()), 4)}


# ---------------------------------------------------------------------------
# Approach 2 / 3 assessments (no scraper, no paid call). Verdicts from spec §1
# planning + a light live confirm of the FDA-primary forward-date weakness.
# ---------------------------------------------------------------------------

def approach2_assessment() -> Dict[str, Any]:
    """Approach 2 (3rd-party biopharma catalyst calendar): ToS/cost assessment.
    No scraper is built (spec §1 / §2). Verdict reproduced from planning findings."""
    return {
        "approach": "2 — third-party biopharma catalyst calendar",
        "buildable_free_daily": False,
        "real_pdufa_date": "yes (if a sanctioned API existed)",
        "coverage": "broadest (their business is completeness, incl. small-caps 8-K misses)",
        "cost": "paid subscription / per-call API ($/mo) — a thesis-relevant cost line",
        "tos_verdict": "gray-to-prohibited for automated access",
        "findings": [
            "biopharmacatalyst.com/calendars/fda-calendar -> HTTP 404 to unauth fetch "
            "(login/JS-gated; not openly machine-readable).",
            "No public/free documented API found for BioPharmaCatalyst, RTTNews, "
            "Evaluate, Nasdaq/Benzinga FDA calendars during planning.",
            "edgar_8k_pdufa.py header note corroborates: 'curated databases "
            "(BioPharma Catalyst, etc.) are paid + scraping-hostile.'",
        ],
        "recommendation": "RESERVE as a paid coverage-booster only if the thesis "
                          "later justifies a fixed data-subscription cost; not a v1 daily source.",
        "cost_score": 0.2,           # paid + ToS-risk -> low
        "reproducible_daily": 0.0,   # not unattended/free
    }


def approach3_assessment(*, live_confirm: bool, user_agent: Optional[str]) -> Dict[str, Any]:
    """Approach 3 (FDA-primary: Drugs@FDA + Federal Register + AdComm + inference).
    Optionally does a light LIVE confirm that FedReg has no forward PDUFA date and
    that Drugs@FDA is post-decision only (so 'FDA primary date' is inference, ±wks)."""
    out: Dict[str, Any] = {
        "approach": "3 — FDA primary (Drugs@FDA + Federal Register + AdComm + inference)",
        "real_pdufa_date": "NO — only an inferred ±weeks window (no FDA source publishes a forward goal-date calendar)",
        "coverage": "broad on approved history; poor on pending forward dates; AdComm covers only the committee subset",
        "cost": "~$0 (openFDA needs OPENFDA_API_KEY for 120k/day; FedReg free)",
        "role": "SIDECAR — Drugs@FDA join recovers real application_number/appl_type/review_priority "
                "(see bc_appno_recover) and retires a pending date once a decision posts; it does NOT "
                "produce forward PDUFA dates.",
        "cost_score": 1.0,           # ~$0 + ToS-clean
        "reproducible_daily": 1.0,   # unattended daily, but dates are weak
        "live_confirm": None,
    }
    if not live_confirm:
        out["live_confirm"] = "skipped (pass --live-confirm-approach3 to probe FedReg/Drugs@FDA)"
        return out
    confirm: Dict[str, Any] = {}
    # FedReg: search "PDUFA action date" -> expect procedural notices w/ no concrete forward date.
    try:
        import requests
        r = requests.get(
            "https://www.federalregister.gov/api/v1/documents.json",
            params={"conditions[term]": "PDUFA action date", "per_page": 5,
                    "fields[]": "title"},
            headers={"User-Agent": user_agent or "bc-phase0-benchmark"}, timeout=20)
        confirm["fedreg_status"] = r.status_code
        if r.ok:
            titles = [d.get("title", "") for d in (r.json().get("results") or [])]
            confirm["fedreg_sample_titles"] = titles[:5]
            confirm["fedreg_verdict"] = (
                "procedural notices; no forward PDUFA goal date in titles "
                "(confirms FDA-primary forward-date inference is weak)")
    except Exception as e:  # noqa: BLE001
        confirm["fedreg_error"] = f"{type(e).__name__}: {str(e)[:160]}"
    # Drugs@FDA: confirm post-decision only (RL pending-status 404s).
    try:
        import requests
        r = requests.get("https://api.fda.gov/drug/drugsfda.json",
                         params={"search": "submissions.submission_status:RL", "limit": 1},
                         timeout=20)
        confirm["drugsfda_RL_status"] = r.status_code
        confirm["drugsfda_verdict"] = (
            "submission_status:RL 404 -> Drugs@FDA carries no pending/forward target; "
            "post-decision only (AP/TA/CR)." if r.status_code == 404
            else f"RL query returned {r.status_code} (unexpected; investigate)")
    except Exception as e:  # noqa: BLE001
        confirm["drugsfda_error"] = f"{type(e).__name__}: {str(e)[:160]}"
    out["live_confirm"] = confirm
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_benchmark(*, window_days: int, size: int, polygon_pace_s: float,
                  max_polygon_names: Optional[int], recover_appno: bool,
                  openfda_pace_s: float, truthset_path: Path,
                  live_confirm_approach3: bool) -> Dict[str, Any]:
    """Run the approach-1 enumerator READ-ONLY and score it. Returns the full
    report dict (also printed by main)."""
    import os
    from modal_workers.fetchers.universe import bc_universe_pdufa as enum

    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        raise RuntimeError('SEC_USER_AGENT required (e.g. "Name x@y.com").')

    from modal_workers.providers.polygon.base import PolygonClient
    from modal_workers.providers.polygon.market_data import PolygonMarketData
    poly_client = PolygonClient()
    market_data = PolygonMarketData(poly_client)

    today = datetime.now(timezone.utc).date()

    # ---- run approach 1 enumerator (in-memory; NO --apply) ----
    result = enum.enumerate_universe(
        user_agent=user_agent, poly_client=poly_client, market_data=market_data,
        window_days=window_days, size=size, polygon_pace_s=polygon_pace_s,
        max_polygon_names=max_polygon_names, recover_appno=recover_appno,
        openfda_pace_s=openfda_pace_s,
    )
    candidates = result["candidates"]
    in_window = result["in_window"]
    enum_stats = result["stats"]

    # ---- score against truth ----
    truth_rows, truth_err = load_truthset(truthset_path)
    score: Optional[ScoreResult] = None
    if truth_rows:
        score = score_against_truth(truth_rows, candidates, window_days, today)

    # ---- latency: disclosure lead (filing_date -> pdufa_date), in-window only ----
    leads: List[int] = []
    for c in in_window:
        fd, pd = getattr(c, "file_date", None), getattr(c, "pdufa_date", None)
        d = _days_diff(pd, fd)
        if d is not None:
            leads.append(d)
    leads.sort()
    latency = {
        "metric": "disclosure_lead_days (8-K file_date -> PDUFA date); detect-latency ~0 on a daily cron",
        "n": len(leads),
        "min": leads[0] if leads else None,
        "median": leads[len(leads) // 2] if leads else None,
        "max": leads[-1] if leads else None,
    }

    # ---- rubric (approach 1) ----
    reproducible_daily = 1.0  # unattended Modal cron, free, idempotent
    cost_score = 1.0          # ~$0 + ToS-clean (SEC EFTS + openFDA free tiers)
    if score is not None:
        rubric = rubric_score(
            recall_in_window=score.recall_in_window,
            date_exact_rate=score.date_exact_rate,
            false_pos_rate=score.false_pos_rate,
            reproducible_daily=reproducible_daily, cost_score=cost_score)
    else:
        rubric = {"note": "truth set absent -> recall/date/FP terms unscorable",
                  "partial_terms": {"reproducible_daily": round(_W_REPRO * reproducible_daily, 4),
                                    "cost_score": round(_W_COST * cost_score, 4)}}

    # ---- §4 GO/NO-GO ----
    N = enum_stats.get("N_in_window_pending_nda_bla", 0)
    M = enum_stats.get("M_in_window_tradeable_G2", 0)
    crit = {
        "c1_universe_ge_15": (N >= _GATE_IN_WINDOW, f"N={N} (need >={_GATE_IN_WINDOW})"),
        "c2_tradeable_ge_12": (M >= _GATE_TRADEABLE, f"M={M} (need >={_GATE_TRADEABLE})"),
    }
    if score is not None:
        crit["c3_date_exact_ge_0.80"] = (
            score.date_exact_rate >= _GATE_DATE_EXACT,
            f"date_exact_rate={score.date_exact_rate} (need >={_GATE_DATE_EXACT})")
        crit["c3_false_pos_le_0.15"] = (
            score.false_pos_rate <= _GATE_FP_MAX,
            f"false_pos_rate={score.false_pos_rate} (need <={_GATE_FP_MAX})")
    else:
        crit["c3_date_trust"] = (None, "UNSCORED — truth set absent")
    crit["c4_reproducible_failloud"] = (
        True, "enumerator is a Modal cron writing bc_pipeline_runs every run; same-day re-run idempotent (composite UNIQUEs)")
    crit["c5_cost_zero"] = (True, "marginal cost ~$0 (SEC EFTS + openFDA free)")

    hard_pass = all(v[0] is True for k, v in crit.items() if k.startswith(("c1", "c2")))
    date_pass = (score is None) or (
        score.date_exact_rate >= _GATE_DATE_EXACT and score.false_pos_rate <= _GATE_FP_MAX)
    if hard_pass and date_pass and score is not None:
        verdict = "GO"
    elif hard_pass and score is None:
        verdict = "GO (universe gate met; date-trust UNVERIFIED pending truth set)"
    elif N >= 10:
        verdict = "MARGINAL — escalate (reduced-scope monitor or buy approach 2)"
    else:
        verdict = "NO-GO — escalate to Pedro (universe too small / dates untrusted)"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "today": today.isoformat(),
        "window_days": window_days,
        "approach1_enumerator_stats": enum_stats,
        "truthset": {"path": str(truthset_path), "loaded": len(truth_rows), "error": truth_err},
        "approach1_score": (score.__dict__ if score else None),
        "approach1_latency": latency,
        "approach1_rubric": rubric,
        "approach2_assessment": approach2_assessment(),
        "approach3_assessment": approach3_assessment(
            live_confirm=live_confirm_approach3, user_agent=user_agent),
        "go_no_go": {"criteria": {k: {"pass": v[0], "detail": v[1]} for k, v in crit.items()},
                     "verdict": verdict},
        "recommendation": {
            "v1_source": "Approach 1 (EDGAR 8-K extraction) — real dates, free, reproducible, "
                         "working extractor already in-repo.",
            "sidecar": "Approach 3 Drugs@FDA join for real application_number/appl_type/review_priority "
                       "(bc_appno_recover) + decision-retirement; NOT for forward dates.",
            "approach2": "Reserved as a paid coverage-booster only if Pedro accepts the cost/ToS.",
        },
    }


def _print_report(rep: Dict[str, Any]) -> None:
    p = print
    p("\n" + "=" * 72)
    p("BC PHASE 0 — UNIVERSE SOURCE BENCHMARK (read-only; no --apply)")
    p("=" * 72)
    p(f"generated_at: {rep['generated_at']}  today: {rep['today']}  window_days: {rep['window_days']}")

    st = rep["approach1_enumerator_stats"]
    p("\n-- Approach 1 enumerator (live EFTS+Polygon+Drugs@FDA, in-memory) --")
    for k in ("discovered_accessions", "candidates_total", "candidates_with_parsed_date",
              "parse_success_rate", "N_in_window_pending_nda_bla", "M_in_window_tradeable_G2",
              "M_prime_in_window_mcap_adv_only", "in_window_real_appno",
              "in_window_surrogate_appno", "appno_recovered", "appno_lookups",
              "polygon_market_cap_hits", "polygon_options_known"):
        if k in st:
            p(f"   {k}: {st[k]}")

    ts = rep["truthset"]
    p(f"\n-- Truth set: loaded={ts['loaded']} from {ts['path']}")
    if ts["error"]:
        p(f"   NOTE: {ts['error']}")

    sc = rep["approach1_score"]
    if sc:
        p("\n-- Approach 1 vs truth --")
        p(f"   recall_overall:     {sc['recall_overall']}  ({sc['surfaced']}/{sc['n_truth']})")
        p(f"   recall_in_window:   {sc['recall_in_window']}  ({sc['surfaced_in_window']}/{sc['n_truth_in_window']})  [§2.3 0.35 term]")
        p(f"   date_exact_rate:    {sc['date_exact_rate']}  ({sc['date_exact']}/{sc['surfaced']})  [gate >=0.80]")
        p(f"   date_diff_buckets:  {sc['date_buckets']}")
        p(f"   false_pos_rate:     {sc['false_pos_rate']}  ({sc['fp_contradictions']}/{sc['fp_eval_pool']} truth-covered in-window)  [gate <=0.15]")
        p(f"   false_pos_rate_raw: {sc['false_pos_rate_raw']}  ({sc['false_positives']}/{sc['emitted_with_date']} unrestricted proxy — NOT gating; counts real out-of-truthset catalysts)")
        p(f"   recall_by_mcap_bucket: {sc['bucket_recall']}")
    else:
        p("\n-- Approach 1 vs truth: SKIPPED (no truth set) — universe-gate only --")

    lat = rep["approach1_latency"]
    p(f"\n-- Latency: {lat['metric']}")
    p(f"   n={lat['n']} min={lat['min']} median={lat['median']} max={lat['max']} (days)")

    rb = rep["approach1_rubric"]
    p("\n-- §2.3 weighted rubric (approach 1) --")
    if "terms" in rb:
        for k, v in rb["terms"].items():
            p(f"   {k}: {v}")
        p(f"   => winner_score: {rb['winner_score']}")
    else:
        p(f"   {rb.get('note')}")
        p(f"   partial_terms: {rb.get('partial_terms')}")

    p("\n-- Approach 2 (3rd-party calendar) assessment --")
    a2 = rep["approach2_assessment"]
    p(f"   buildable_free_daily={a2['buildable_free_daily']} cost={a2['cost']} tos={a2['tos_verdict']}")
    p(f"   recommendation: {a2['recommendation']}")

    p("\n-- Approach 3 (FDA primary) assessment --")
    a3 = rep["approach3_assessment"]
    p(f"   real_pdufa_date: {a3['real_pdufa_date']}")
    p(f"   role: {a3['role']}")
    if isinstance(a3.get("live_confirm"), dict):
        lc = a3["live_confirm"]
        if "fedreg_verdict" in lc:
            p(f"   live FedReg: {lc.get('fedreg_status')} — {lc['fedreg_verdict']}")
        if "drugsfda_verdict" in lc:
            p(f"   live Drugs@FDA: {lc['drugsfda_verdict']}")

    gg = rep["go_no_go"]
    p("\n-- §4 GO / NO-GO --")
    for k, v in gg["criteria"].items():
        mark = "PASS" if v["pass"] is True else ("FAIL" if v["pass"] is False else "N/A")
        p(f"   [{mark}] {k}: {v['detail']}")
    p(f"\n   VERDICT: {gg['verdict']}")

    rec = rep["recommendation"]
    p("\n-- Recommendation --")
    p(f"   v1 source: {rec['v1_source']}")
    p(f"   sidecar:   {rec['sidecar']}")
    p(f"   approach2: {rec['approach2']}")
    p("=" * 72 + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="BC Phase 0 universe-source benchmark (read-only).")
    ap.add_argument("--window-days", type=int, default=120)
    ap.add_argument("--size", type=int, default=100)
    ap.add_argument("--polygon-pace-s", type=float, default=13.0)
    ap.add_argument("--max-polygon-names", type=int, default=None)
    ap.add_argument("--no-recover-appno", action="store_true",
                    help="Skip the read-only Drugs@FDA appno-recovery join.")
    ap.add_argument("--openfda-pace-s", type=float, default=0.0)
    ap.add_argument("--truthset", default=str(_TRUTHSET_PATH),
                    help="Path to bc_pdufa_truthset.json (truth-set agent owns it).")
    ap.add_argument("--live-confirm-approach3", action="store_true",
                    help="Live-probe FedReg + Drugs@FDA to confirm the FDA-primary forward-date weakness.")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")

    rep = run_benchmark(
        window_days=args.window_days, size=args.size, polygon_pace_s=args.polygon_pace_s,
        max_polygon_names=args.max_polygon_names, recover_appno=not args.no_recover_appno,
        openfda_pace_s=args.openfda_pace_s, truthset_path=Path(args.truthset),
        live_confirm_approach3=args.live_confirm_approach3,
    )
    _print_report(rep)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rep, indent=2, default=str))
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
