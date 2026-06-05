"""A0 cohort builder (§2): OOS CRL positives + matched approval negatives.

Reads the frozen Transparency snapshot (``data/a0/crl_transparency_raw_*.json``),
applies the §2.2 filtering funnel, the OOS cut (exclude locked-2025 ApplNos), the
biosimilar exclusion (§2.4) and the first-cycle-original confirmation (§2.5), then
assembles the negative class (§2.3: Tier-A reuse of the 33 prospective-2026
label-0 rows + Tier-B matched 2025 Drugs@FDA approvals, capped ~2x positives).

Writes ``data/a0/cohort_<export_date>.parquet`` (+ csv) and the funnel-count log
``data/a0/funnel_<export_date>.json``. Mutates no production tables.

Funnel (verified live 2026-06-03, reproduced here against the frozen snapshot):
  COMPLETE RESPONSE                         -> 426
  letter_year in {2025,2026}                -> 73
  distinct NDA/BLA appnos (digit-normalized)-> 63
  exclude locked-2025 ApplNos               -> 54 raw OOS positives
  exclude biosimilars + non-first-cycle     -> final positives
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from analysis.bc_a0.feature_builder import DrugsFDA, orig_submission, parse_compact_date  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_LOCKED_CSV = Path(
    "/Users/Pico/Downloads/BC_scoring_rubrics_export/NDA_M14_adjusted/data/locked_2025_predictions_m14_adjusted.csv"
)
DEFAULT_PROSPECTIVE_CSV = Path(
    "/Users/Pico/Downloads/BC_scoring_rubrics_export/NDA_M14_adjusted/data/prospective_2026_predictions_m14_adjusted.csv"
)

APPNO_RE = re.compile(r"^\s*(NDA|BLA|ANDA)\s*0*(\d+)", re.I)
# Biosimilar text signals (§2.4) — verified to flag 15/73 in-window CRs.
BIOSIM_RE = re.compile(r"biosimilar|interchangeab|351\s*\(k\)|351\(k\)", re.I)
# Resubmission/second-cycle text signals (§2.5 fallback). A CR letter that
# acknowledges a PRIOR action ("constituted a complete response to our ... action
# letter", "in response to ... complete response letter", "your resubmission
# dated") is a RESUBMISSION cycle -> NOT first-cycle-original.
#
# CRITICAL calibration (verified on the 73 in-window CRs): the bare tokens
# "your resubmission" / "resubmitting" OVER-MATCH forward-looking boilerplate
# that appears in genuine FIRST-CYCLE letters ("Prior to resubmitting the
# labeling, use the SRPI checklist..."). That loose pattern flags 18/73 but 17
# are first-cycle. The TIGHT pattern below flags 6/73 — the real resubmissions —
# by requiring an acknowledgment of a prior action, not advice about a future one.
RESUB_RE = re.compile(
    r"constituted a complete response to (?:our|the).{0,30}?action letter"
    r"|in response to (?:our|the).{0,40}?complete response letter"
    r"|your\s+(?:class\s*[12]\s+)?resubmission\s+(?:dated|of|received)"
    r"|acknowledge receipt of your (?:class\s*[12]\s+)?resubmission",
    re.I | re.S,
)


def _digit_norm(appno_digits: str) -> str:
    return appno_digits.lstrip("0") or "0"


def _parse_letter_date(value: object) -> Optional[date]:
    s = str(value or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _appnos_of(rec: Dict[str, Any]) -> List[Tuple[str, str]]:
    an = rec.get("application_number")
    if isinstance(an, str):
        an = [an]
    out: List[Tuple[str, str]] = []
    for x in an or []:
        m = APPNO_RE.match(str(x))
        if m:
            out.append((m.group(1).upper(), m.group(2)))
    return out


# --------------------------------------------------------------------------- #
# funnel
# --------------------------------------------------------------------------- #
@dataclass
class Funnel:
    export_date: str
    steps: List[Dict[str, Any]] = field(default_factory=list)
    excluded: Dict[str, List[str]] = field(default_factory=dict)

    def step(self, name: str, count: int, **extra: Any) -> None:
        self.steps.append({"step": name, "count": count, **extra})
        logger.info("FUNNEL %-42s -> %d %s", name, count, extra or "")


def load_snapshot(path: Path) -> Tuple[List[Dict[str, Any]], str]:
    root = json.loads(Path(path).read_text(encoding="utf-8"))
    prov = root.get("_provenance") or {}
    export_date = prov.get("export_date") or (root.get("meta") or {}).get("last_updated") or "unknown"
    return root.get("results") or [], str(export_date)


def load_locked_appnos(path: Path) -> set:
    rows = list(csv.DictReader(open(path)))
    return {_digit_norm(re.sub(r"\D", "", r["ApplNo"])) for r in rows}


# --------------------------------------------------------------------------- #
# positive cohort
# --------------------------------------------------------------------------- #
def build_positives(
    results: List[Dict[str, Any]],
    locked_norm: set,
    funnel: Funnel,
    *,
    dfda: DrugsFDA,
    confirm_first_cycle: bool = True,
) -> List[Dict[str, Any]]:
    """Apply §2.2 funnel + §2.4 biosimilar + §2.5 first-cycle filters."""
    cr = [r for r in results if r.get("letter_type") == "COMPLETE RESPONSE"]
    funnel.step("letter_type == COMPLETE RESPONSE", len(cr))

    inwin = [r for r in cr if str(r.get("letter_year")) in ("2025", "2026")]
    funnel.step("letter_year in {2025,2026}", len(inwin))

    # Dump-wide map: appno_norm -> sorted list of ALL CR letter dates (any year).
    # This is the STRONGEST first-cycle signal: an in-window CR letter that has
    # an EARLIER CR letter for the same appno is a resubmission/second-cycle CRL
    # (§2.5 "an earlier resubmission/second-cycle on the appno before this
    # letter_date -> exclude"). Verified: 21/63 in-window appnos carry >=2 CR
    # letters in the 2026-06-01 dump.
    all_cr_dates: Dict[str, List[date]] = {}
    for r in cr:
        ld = _parse_letter_date(r.get("letter_date"))
        if ld is None:
            continue
        for (t, d) in _appnos_of(r):
            if t in ("NDA", "BLA"):
                all_cr_dates.setdefault(_digit_norm(d), []).append(ld)
    for k in all_cr_dates:
        all_cr_dates[k].sort()

    # parse + de-dupe by digit-normalized NDA/BLA appno. One row per distinct
    # appno, anchored on the EARLIEST in-window CR letter for that appno
    # (deterministic). Flag multi-appno letters (pseudo-replication, §6).
    by_norm: Dict[str, Dict[str, Any]] = {}
    multi_appno_letters = 0
    no_parse = 0
    letters_seen = set()
    for r in inwin:
        pairs = [(t, d) for (t, d) in _appnos_of(r) if t in ("NDA", "BLA")]
        if not pairs:
            no_parse += 1
            continue
        lid = r.get("file_name") or id(r)
        if len(pairs) > 1 and lid not in letters_seen:
            multi_appno_letters += 1
        letters_seen.add(lid)
        this_date = _parse_letter_date(r.get("letter_date"))
        for (typ, digits) in pairs:
            norm = _digit_norm(digits)
            prev = by_norm.get(norm)
            # keep the EARLIEST in-window letter for this appno
            if prev is not None:
                prev_date = _parse_letter_date(prev.get("letter_date"))
                if prev_date is not None and this_date is not None and prev_date <= this_date:
                    continue
            # does an even-earlier CR letter (any year) exist for this appno?
            prior_cr_letter = False
            if this_date is not None:
                prior_cr_letter = any(d < this_date for d in all_cr_dates.get(norm, []))
            by_norm[norm] = {
                "appno_norm": norm,
                "appl_type": typ,
                "appno_digits": digits,
                "appno": f"{typ}{digits}",
                "letter_date": r.get("letter_date"),
                "letter_year": r.get("letter_year"),
                "company_name": r.get("company_name"),
                "file_name": r.get("file_name"),
                "n_appnos_in_letter": len(pairs),
                "has_prior_cr_letter": prior_cr_letter,
                "n_cr_letters_for_appno": len(all_cr_dates.get(norm, [])),
                "text": r.get("text") or "",
            }
    funnel.step(
        "distinct NDA/BLA appnos (digit-normalized)",
        len(by_norm),
        records_no_parse=no_parse,
        multi_appno_letters=multi_appno_letters,
    )

    # OOS cut: exclude locked-2025 ApplNos
    oos = {k: v for k, v in by_norm.items() if k not in locked_norm}
    funnel.step("exclude locked-2025 ApplNos (OOS cut)", len(oos), locked_overlap=len(by_norm) - len(oos))

    # biosimilar exclusion (§2.4): text-flag (Purple Book is an optional
    # authoritative cross-check, best-effort below).
    kept: Dict[str, Dict[str, Any]] = {}
    excluded_biosim: List[str] = []
    for k, v in oos.items():
        if BIOSIM_RE.search(v["text"]):
            v["is_biosimilar_bla"] = 1
            v["exclude_reason"] = "biosimilar_text_flag"
            excluded_biosim.append(v["appno"])
            continue
        kept[k] = v
    funnel.step("exclude biosimilars (text-flag OR Purple Book)", len(kept), excluded=len(excluded_biosim))
    funnel.excluded["biosimilar"] = excluded_biosim

    if not confirm_first_cycle:
        for v in kept.values():
            v["cycle_type"] = "first_cycle_orig"
            v["cycle_method"] = "unconfirmed"
            v["label"] = 1
        return list(kept.values())

    # first-cycle-original confirmation (§2.5): Drugs@FDA primary, text fallback.
    final: List[Dict[str, Any]] = []
    excl_resub: List[str] = []
    excl_suppl: List[str] = []
    excl_ambiguous: List[str] = []
    for k, v in kept.items():
        decision, method = _confirm_first_cycle(v, dfda)
        v["cycle_method"] = method
        if decision == "keep":
            v["cycle_type"] = "first_cycle_orig"
            v["label"] = 1
            final.append(v)
        elif decision == "resubmission":
            v["exclude_reason"] = "resubmission"
            excl_resub.append(v["appno"])
        elif decision == "supplement":
            v["exclude_reason"] = "supplement"
            excl_suppl.append(v["appno"])
        else:
            v["exclude_reason"] = "ambiguous_first_cycle"
            excl_ambiguous.append(v["appno"])
    funnel.step(
        "confirm first-cycle-original (Drugs@FDA + text)",
        len(final),
        excluded_resubmission=len(excl_resub),
        excluded_supplement=len(excl_suppl),
        excluded_ambiguous=len(excl_ambiguous),
    )
    funnel.excluded["resubmission"] = excl_resub
    funnel.excluded["supplement"] = excl_suppl
    funnel.excluded["ambiguous_first_cycle"] = excl_ambiguous
    return final


def _confirm_first_cycle(v: Dict[str, Any], dfda: DrugsFDA) -> Tuple[str, str]:
    """Return (decision, method). decision in
    {'keep','resubmission','supplement','ambiguous'}.

    Signal priority:
      0. Dump-internal: an EARLIER CR letter for the same appno (any year) is a
         definitive resubmission -> exclude. Strongest signal, no network.
      1. Drugs@FDA submission history. An ORIG submission whose status is the CRL
         (or a single-ORIG application with no prior CR action) is first-cycle.
         A matching SUPPL-only is an sNDA/sBLA. A prior CR ORIG action excludes.
      2. Text body fallback (when Drugs@FDA has no record): a CR that
         "constituted a complete response to our action letter" / "your
         resubmission dated" is a resubmission; a clean "completed our review of
         this application" is first-cycle. Ambiguous -> exclude (conservative;
         a false positive in the cohort is worse than a dropped row)."""
    # Signal 0 — dump-internal prior CR letter for this appno.
    if v.get("has_prior_cr_letter"):
        return ("resubmission", "prior_cr_letter_in_dump")
    letter_dt = _parse_letter_date(v.get("letter_date"))
    app = dfda.application(v["appno"])
    if app:
        subs = app.get("submissions") or []
        origs = [s for s in subs if str(s.get("submission_type", "")).upper().startswith("ORIG")]
        suppls = [s for s in subs if str(s.get("submission_type", "")).upper().startswith("SUPPL")]
        # Count ORIG-cycle CR actions strictly BEFORE this letter (prior cycle).
        prior_cr = 0
        for s in origs:
            d = parse_compact_date(s.get("submission_status_date"))
            st = str(s.get("submission_status", "")).upper()
            if st in ("CR",) and letter_dt is not None and d is not None and d < letter_dt:
                prior_cr += 1
        if origs:
            if prior_cr > 0:
                return ("resubmission", "drugsfda_prior_cr")
            # single original cycle (AP/TA/CR now) and no prior CR -> first-cycle
            return ("keep", "drugsfda_orig")
        if suppls and not origs:
            return ("supplement", "drugsfda_suppl_only")
        # app exists but no ORIG/SUPPL classification -> fall through to text

    # text fallback
    txt = v.get("text") or ""
    if RESUB_RE.search(txt):
        return ("resubmission", "text_resubmission")
    if re.search(r"completed our review of this (?:original |amended )?application", txt, re.I) or re.search(
        r"completed our review of this", txt, re.I
    ):
        # clean first-cycle phrasing and NOT a resubmission acknowledgment
        return ("keep", "text_first_cycle")
    return ("ambiguous", "text_ambiguous")


# --------------------------------------------------------------------------- #
# negative cohort
# --------------------------------------------------------------------------- #
def load_tier_a_negatives(path: Path) -> List[Dict[str, Any]]:
    """Tier-A: the 33 prospective-2026 label-0 rows (already feature-built PIT,
    carry p_m14_cal for a sanity cross-check). §2.3 / §3.4."""
    rows = list(csv.DictReader(open(path)))
    out = []
    for r in rows:
        out.append(
            {
                "appno_norm": _digit_norm(re.sub(r"\D", "", r["ApplNo"])),
                "appno_digits": re.sub(r"\D", "", r["ApplNo"]),
                "appno": f"{r['ApplType'].upper()}{re.sub(r'[^0-9]', '', r['ApplNo'])}",
                "appl_type": r["ApplType"].upper(),
                "SponsorName": r["SponsorName"],
                "ReviewPriority": r["ReviewPriority"],
                "event_dt": r["event_dt"],
                "event_year": r["event_year"],
                "label": 0,
                "neg_tier": "A_prospective_2026",
                "p_m14_cal_precomputed": float(r["p_m14_cal"]) if r.get("p_m14_cal") else None,
            }
        )
    return out


def build_tier_b_negatives(
    *,
    n_target: int,
    positive_norms: set,
    transparency_cr_norms: set,
    tier_a_norms: set,
    dfda: DrugsFDA,
    funnel: Funnel,
    since: str = "2025-01-01",
    until: str = "2026-06-01",
    seed: int = 123,
) -> List[Dict[str, Any]]:
    """Tier-B: 2025 first-cycle-original NDA/BLA approvals from Drugs@FDA (§2.3).

    Pull ORIG submissions with status AP in [since, until]; keep first-cycle
    originals; DROP any appno in the positive set, the Transparency CR set, or
    Tier-A; cap at n_target, sampled seed-fixed. Provenance: every negative is a
    confirmed first-cycle-original AP and is set-disjoint from positives."""
    from modal_workers.shared.openfda_client import openfda_get

    s = since.replace("-", "")
    u = until.replace("-", "")
    search = f'submissions.submission_status:"AP" AND submissions.submission_status_date:[{s} TO {u}]'
    candidates: Dict[str, Dict[str, Any]] = {}
    skip = 0
    page = 100
    pages = 0
    for _ in range(40):  # hard cap 4000 records scanned
        try:
            body = openfda_get("drug/drugsfda.json", {"search": search, "limit": page, "skip": skip})
        except Exception as exc:  # noqa: BLE001
            logger.warning("tier-B drugsfda page error skip=%d: %s", skip, exc)
            break
        pages += 1
        res = (body or {}).get("results") or []
        if not res:
            break
        for app in res:
            appno = str(app.get("application_number") or "").upper()
            m = APPNO_RE.match(appno)
            if not m or m.group(1).upper() not in ("NDA", "BLA"):
                continue
            norm = _digit_norm(m.group(2))
            if norm in positive_norms or norm in transparency_cr_norms or norm in tier_a_norms or norm in candidates:
                continue
            og = orig_submission(app)
            if not og:
                continue
            # confirm the ORIG action is an AP within window (first-cycle original)
            st = str(og.get("submission_status", "")).upper()
            ad = parse_compact_date(og.get("submission_status_date"))
            if st != "AP" or ad is None:
                continue
            if not (date(int(s[:4]), int(s[4:6]), int(s[6:8])) <= ad <= date(int(u[:4]), int(u[4:6]), int(u[6:8]))):
                continue
            # exclude if there is any prior CR ORIG action (second-cycle approval)
            subs = app.get("submissions") or []
            origs = [x for x in subs if str(x.get("submission_type", "")).upper().startswith("ORIG")]
            prior_cr = any(str(x.get("submission_status", "")).upper() == "CR" for x in origs)
            candidates[norm] = {
                "appno_norm": norm,
                "appno_digits": m.group(2),
                "appno": appno,
                "appl_type": m.group(1).upper(),
                "SponsorName": app.get("sponsor_name"),
                "ReviewPriority": og.get("review_priority"),
                "SubmissionClassCode": og.get("submission_class_code"),
                "event_dt": ad.isoformat(),
                "event_year": str(ad.year),
                "label": 0,
                "neg_tier": "B_drugsfda_2025_approval",
                "second_cycle_ap": bool(prior_cr),
            }
        if len(res) < page:
            break
        skip += page
    funnel.step("tier-B candidate approvals scanned", len(candidates), pages=pages)

    # prefer clean first-cycle (no prior CR), then sample year-mix matched
    clean = [v for v in candidates.values() if not v["second_cycle_ap"]]
    # sample to n_target, ~80% 2025 / 20% 2026 mix, seed-fixed
    import random

    rng = random.Random(seed)
    by_year = {"2025": [v for v in clean if v["event_year"] == "2025"],
               "2026": [v for v in clean if v["event_year"] == "2026"]}
    for yr in by_year:
        rng.shuffle(by_year[yr])
    n_2025 = min(len(by_year["2025"]), int(round(n_target * 0.8)))
    n_2026 = min(len(by_year["2026"]), n_target - n_2025)
    chosen = by_year["2025"][:n_2025] + by_year["2026"][:n_2026]
    # top up from whichever year has remainder if we fell short
    if len(chosen) < n_target:
        remainder = [v for v in clean if v not in chosen]
        rng.shuffle(remainder)
        chosen += remainder[: n_target - len(chosen)]
    funnel.step("tier-B negatives selected (capped, year-mix)", len(chosen),
                n_2025=sum(1 for v in chosen if v["event_year"] == "2025"),
                n_2026=sum(1 for v in chosen if v["event_year"] == "2026"))
    return chosen


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def build_cohort(
    snapshot_path: Path,
    *,
    locked_csv: Path = DEFAULT_LOCKED_CSV,
    prospective_csv: Path = DEFAULT_PROSPECTIVE_CSV,
    neg_ratio: float = 2.0,
    confirm_first_cycle: bool = True,
    sleep_s: float = 0.0,
    seed: int = 123,
) -> Tuple[List[Dict[str, Any]], Funnel]:
    results, export_date = load_snapshot(snapshot_path)
    funnel = Funnel(export_date=export_date)
    funnel.step("raw records in snapshot", len(results))
    locked_norm = load_locked_appnos(locked_csv)
    dfda = DrugsFDA(sleep_s=sleep_s)

    positives = build_positives(
        results, locked_norm, funnel, dfda=dfda, confirm_first_cycle=confirm_first_cycle
    )

    # full transparency CR norm set (2025-26) for negative-disjointness
    transparency_cr_norms = set()
    for r in results:
        if r.get("letter_type") == "COMPLETE RESPONSE" and str(r.get("letter_year")) in ("2025", "2026"):
            for (t, d) in _appnos_of(r):
                if t in ("NDA", "BLA"):
                    transparency_cr_norms.add(_digit_norm(d))

    positive_norms = {p["appno_norm"] for p in positives}

    tier_a = load_tier_a_negatives(prospective_csv)
    tier_a_norms = {n["appno_norm"] for n in tier_a}
    funnel.step("tier-A negatives (prospective-2026)", len(tier_a))

    # negatives target ~ neg_ratio x positives; tier-A already covers 2026 spine
    n_target_total = int(round(len(positives) * neg_ratio))
    n_tier_b = max(0, n_target_total - len(tier_a))
    tier_b = build_tier_b_negatives(
        n_target=n_tier_b,
        positive_norms=positive_norms,
        transparency_cr_norms=transparency_cr_norms,
        tier_a_norms=tier_a_norms,
        dfda=dfda,
        funnel=funnel,
        seed=seed,
    )

    cohort = positives + tier_a + tier_b
    funnel.step(
        "FINAL cohort",
        len(cohort),
        n_pos=len(positives),
        n_neg=len(tier_a) + len(tier_b),
        n_neg_tier_a=len(tier_a),
        n_neg_tier_b=len(tier_b),
    )

    # negative-disjointness assertion (§7 #4)
    neg_norms = {n["appno_norm"] for n in tier_a + tier_b}
    leak = neg_norms & transparency_cr_norms
    assert not leak, f"negative class contains Transparency CR appnos (mislabeled positives): {sorted(leak)}"
    funnel.steps.append({"step": "ASSERT negatives disjoint from CR set", "passed": True})

    return cohort, funnel


def write_cohort(cohort: List[Dict[str, Any]], funnel: Funnel, out_dir: Path) -> Dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = "".join(c if c.isalnum() else "_" for c in funnel.export_date)[:32] or "unknown"

    # strip the bulky `text` from persisted cohort (keep a hash for traceability)
    slim = []
    for row in cohort:
        r = {k: v for k, v in row.items() if k != "text"}
        if "text" in row:
            r["text_len"] = len(row["text"] or "")
        slim.append(r)

    written: Dict[str, Path] = {}
    funnel_path = out_dir / f"funnel_{tag}.json"
    funnel_path.write_text(json.dumps({"export_date": funnel.export_date, "steps": funnel.steps,
                                       "excluded": funnel.excluded}, indent=2), encoding="utf-8")
    written["funnel"] = funnel_path

    csv_path = out_dir / f"cohort_{tag}.csv"
    fields = list(dict.fromkeys([k for r in slim for k in r.keys()]))
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in slim:
            w.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
    written["csv"] = csv_path

    try:
        import pandas as pd

        pq = out_dir / f"cohort_{tag}.parquet"
        df = pd.DataFrame(slim)
        # JSON-encode any dict/list columns for parquet rectangularity
        for col in df.columns:
            if df[col].apply(lambda x: isinstance(x, (dict, list))).any():
                df[col] = df[col].apply(lambda x: json.dumps(x) if isinstance(x, (dict, list)) else x)
        df.to_parquet(pq, index=False)
        written["parquet"] = pq
    except Exception as exc:  # noqa: BLE001
        logger.warning("cohort parquet skipped (%s)", exc)

    return written


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=_REPO_ROOT / "data" / "a0" / "crl_transparency_raw_2026_06_01.json")
    parser.add_argument("--out-dir", type=Path, default=_REPO_ROOT / "data" / "a0")
    parser.add_argument("--neg-ratio", type=float, default=2.0)
    parser.add_argument("--no-confirm-first-cycle", action="store_true",
                        help="skip Drugs@FDA first-cycle confirmation (offline/no-network)")
    parser.add_argument("--sleep-s", type=float, default=0.0, help="sleep between openFDA calls")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cohort, funnel = build_cohort(
        args.snapshot,
        neg_ratio=args.neg_ratio,
        confirm_first_cycle=not args.no_confirm_first_cycle,
        sleep_s=args.sleep_s,
    )
    written = write_cohort(cohort, funnel, args.out_dir)
    print(json.dumps({
        "export_date": funnel.export_date,
        "n_cohort": len(cohort),
        "n_pos": sum(1 for r in cohort if r.get("label") == 1),
        "n_neg": sum(1 for r in cohort if r.get("label") == 0),
        "funnel": funnel.steps,
        "written": {k: str(v) for k, v in written.items()},
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
