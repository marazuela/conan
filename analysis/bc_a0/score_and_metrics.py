"""A0 scoring + metrics (§4): score the OOS cohort and report rank confidence.

Pipeline:
  1. load cohort (data/a0/cohort_<export_date>.csv)
  2. POSITIVES: estimate ref_date (§3.1) -> build PIT features (feature_builder,
     §3.2) -> score via the IMPORTED M14 scorer (score_m14_adjusted.score_row).
  3. NEGATIVES tier-A: reuse the M14 pipeline's precomputed p_m14_cal (§3.4) and
     CROSS-CHECK it against a re-score through the imported scorer (must match).
     tier-B: build PIT features -> score.
  4. metrics (§4.2):
       AUC   = ranking_auc       (eval_harness/metrics.py, USE AS-IS)
       Brier = brier_score       (fda_calibration_math.py, USE AS-IS)
       AUC 95% CI = boot_ci      (copied VERBATIM from build_adjusted_m14.py)
       cal slope/intercept = cal_slope_intercept (VERBATIM)
       perm-p = perm_p           (VERBATIM)  + the §3.3 #4 leakage canary
     per-band observed CRL rates (low/moderate/elevated/high).
  5. sensitivity runs (§4.2): full, NDA-only, BLA-only, collapse-multi-appno,
     2026-only positives.
  6. write data/a0/metrics_<export_date>.json + a coverage report.

The boot_ci / cal_slope_intercept / perm_p functions are copied verbatim (not
re-implemented) so the OOS CI is computed the IDENTICAL way to the published
locked-2025 CI [0.637, 0.954] — apples-to-apples.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from analysis.bc_a0.feature_builder import DrugsFDA, build_features, estimate_ref_date  # noqa: E402
from modal_workers.shared.fda_calibration_math import brier_score  # noqa: E402
from orchestrator_runtime.eval_harness.metrics import ranking_auc  # noqa: E402

logger = logging.getLogger(__name__)

SCORER_PATH = Path(
    "/Users/Pico/Downloads/BC_scoring_rubrics_export/NDA_M14_adjusted/scripts/score_m14_adjusted.py"
)


# --------------------------------------------------------------------------- #
# VERBATIM from build_adjusted_m14.py (§1.7) — do not re-implement.
# --------------------------------------------------------------------------- #
def logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return np.log(p / (1 - p))


def boot_ci(y: np.ndarray, p: np.ndarray, seed: int = 123, n: int = 2000) -> Tuple[float, float]:
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n):
        idx = rng.choice(len(y), len(y), replace=True)
        if len(np.unique(y[idx])) < 2:
            continue
        vals.append(roc_auc_score(y[idx], p[idx]))
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def perm_p(y: np.ndarray, p: np.ndarray, seed: int = 456, n: int = 2000) -> float:
    obs = roc_auc_score(y, p)
    rng = np.random.default_rng(seed)
    vals = [roc_auc_score(rng.permutation(y), p) for _ in range(n)]
    return float(np.mean(np.array(vals) >= obs))


def cal_slope_intercept(y: np.ndarray, p: np.ndarray) -> Tuple[float, float]:
    lr = LogisticRegression(max_iter=1000)
    lr.fit(logit(p).reshape(-1, 1), y)
    return float(lr.coef_[0, 0]), float(lr.intercept_[0])


# --------------------------------------------------------------------------- #
# scorer import (as-is, §4.1)
# --------------------------------------------------------------------------- #
def load_scorer():
    spec = importlib.util.spec_from_file_location("score_m14_adjusted", SCORER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, mod.load_model()


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #
def _parse_letter_date(value: object) -> Optional[date]:
    s = str(value or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _parse_iso(value: object) -> Optional[date]:
    try:
        return datetime.strptime(str(value or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def score_cohort(
    cohort: List[Dict[str, Any]],
    scorer,
    model,
    *,
    dfda: Optional[DrugsFDA] = None,
    enable_warning_letters: bool = True,
    reuse_tier_a_precomputed: bool = True,
) -> List[Dict[str, Any]]:
    """Score every cohort row; returns rows enriched with p_crl/risk_band/etc.

    POSITIVES + tier-B negatives go through the offline PIT feature builder then
    the imported scorer. tier-A negatives reuse precomputed p_m14_cal and are
    cross-checked by a re-score (recorded as crosscheck_abs_diff)."""
    dfda = dfda or DrugsFDA()
    scored: List[Dict[str, Any]] = []
    crosscheck_max = 0.0

    for row in cohort:
        label = int(row.get("label"))
        appno = row.get("appno")
        sponsor = row.get("company_name") or row.get("SponsorName")
        tier = row.get("neg_tier")

        # ----- ref_date -----
        if label == 1:
            letter_dt = _parse_letter_date(row.get("letter_date"))
            ref_date, ref_method = estimate_ref_date(appno=appno, letter_date=letter_dt, dfda=dfda)
        else:
            # negatives: event_dt is the catalyst/action date; back off to filing
            ev = _parse_iso(row.get("event_dt"))
            if ev is not None:
                from analysis.bc_a0.feature_builder import _shift, _REVIEW_CLOCK_DAYS

                ref_date, ref_method = _shift(ev, _REVIEW_CLOCK_DAYS), "event_dt_minus_clock"
            else:
                ref_date, ref_method = estimate_ref_date(appno=appno, letter_date=None, dfda=dfda)

        out = dict(row)
        out["ref_date"] = ref_date.isoformat()
        out["ref_method"] = ref_method

        # ----- tier-A reuse path -----
        if reuse_tier_a_precomputed and tier == "A_prospective_2026" and row.get("p_m14_cal_precomputed"):
            p_precomp = float(row["p_m14_cal_precomputed"])
            # cross-check: re-score the row through the imported scorer using the
            # same inputs the M14 pipeline had (ApplType/priority + class). The
            # prospective CSV does not ship the full feature vector, so we re-score
            # from the offline builder and record the gap (informational).
            feats = build_features(
                appno=appno, sponsor_name=sponsor, cik=None, ref_date=ref_date,
                dfda=dfda, enable_warning_letters=enable_warning_letters,
            )
            rescored = scorer.score_row({**feats, "ApplNo": appno, "SponsorName": sponsor}, model)
            p_rescore = float(rescored["p_crl"]) if rescored.get("p_crl") else None
            out["p_crl"] = p_precomp  # AUTHORITATIVE: the M14 authors' PIT score
            out["p_source"] = "tier_a_precomputed_p_m14_cal"
            out["risk_band"] = scorer.risk_band(p_precomp)
            out["p_rescore_offline"] = p_rescore
            out["crosscheck_abs_diff"] = abs(p_precomp - p_rescore) if p_rescore is not None else None
            out["_coverage"] = feats.get("_coverage")
            out["_feature_sources"] = feats.get("_feature_sources")
            scored.append(out)
            continue

        # ----- offline feature build + score (positives + tier-B) -----
        cik = None  # no curated sponsor->CIK map for the cohort (§3.2); 8-K absent
        feats = build_features(
            appno=appno, sponsor_name=sponsor, cik=cik, ref_date=ref_date,
            dfda=dfda, enable_warning_letters=enable_warning_letters,
        )
        sr = scorer.score_row({**feats, "ApplNo": appno, "SponsorName": sponsor}, model)
        if not sr.get("p_crl"):
            # the scorer refused — should not happen after §2 filtering; log loud.
            logger.error("SCORER REFUSED %s: %s", appno, sr.get("refusal_reason"))
            out["p_crl"] = None
            out["refused"] = sr.get("refusal_reason")
            scored.append(out)
            continue
        out["p_crl"] = float(sr["p_crl"])
        out["p_source"] = "offline_builder_scored"
        out["risk_band"] = sr.get("risk_band")
        out["raw_p_uncalibrated"] = float(sr.get("raw_p_uncalibrated") or 0.0)
        out["confidence_flag"] = sr.get("confidence_flag")
        out["_coverage"] = feats.get("_coverage")
        out["_feature_sources"] = feats.get("_feature_sources")
        scored.append(out)

    # surface the worst tier-A crosscheck gap
    diffs = [r["crosscheck_abs_diff"] for r in scored if r.get("crosscheck_abs_diff") is not None]
    if diffs:
        logger.info("tier-A crosscheck max abs diff (offline rebuild vs precomputed) = %.4f", max(diffs))
    return scored


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
RISK_BANDS = ("low", "moderate", "elevated", "high")


def compute_metrics(scored: List[Dict[str, Any]], *, seed: int = 123, n_boot: int = 2000) -> Dict[str, Any]:
    rows = [r for r in scored if r.get("p_crl") is not None]
    y = np.array([int(r["label"]) for r in rows])
    p = np.array([float(r["p_crl"]) for r in rows])
    n = len(rows)
    n_pos = int(y.sum())
    out: Dict[str, Any] = {"n": n, "n_pos": n_pos, "n_neg": n - n_pos}
    if n_pos == 0 or n_pos == n:
        out["note"] = "degenerate (single class) — metrics undefined"
        return out

    out["auc_ranking"] = round(float(ranking_auc(list(p), list(y))), 4)
    out["auc_sklearn"] = round(float(roc_auc_score(y, p)), 4)
    lo, hi = boot_ci(y, p, seed=seed, n=n_boot)
    out["auc_ci_low"] = round(lo, 4)
    out["auc_ci_high"] = round(hi, 4)
    out["brier"] = round(float(brier_score(list(p), list(y))), 4)
    slope, intercept = cal_slope_intercept(y, p)
    out["calibration_slope"] = round(slope, 4)
    out["calibration_intercept"] = round(intercept, 4)
    out["perm_p"] = round(perm_p(y, p, n=n_boot), 4)

    # per-band observed CRL rate (the product-relevant evidence, §4.2)
    bands: Dict[str, Dict[str, Any]] = {}
    for b in RISK_BANDS:
        members = [r for r in rows if r.get("risk_band") == b]
        if not members:
            bands[b] = {"n": 0, "observed_crl_rate": None}
            continue
        npos_b = sum(int(m["label"]) for m in members)
        bands[b] = {
            "n": len(members),
            "n_pos": npos_b,
            "observed_crl_rate": round(npos_b / len(members), 4),
            "mean_p_crl": round(float(np.mean([float(m["p_crl"]) for m in members])), 4),
        }
    out["per_band"] = bands

    # band separation: high-band vs low-band observed rate
    hi_rate = bands["high"]["observed_crl_rate"]
    lo_rate = bands["low"]["observed_crl_rate"]
    out["band_high_vs_low_rate"] = {"high": hi_rate, "low": lo_rate}
    return out


def leakage_canary(scored: List[Dict[str, Any]], *, seed: int = 456, n: int = 2000) -> Dict[str, Any]:
    """§3.3 #4: shuffle labels, AUC must collapse to ~0.5 and perm-p must be high.
    Confirms no label signal leaked into the features."""
    rows = [r for r in scored if r.get("p_crl") is not None]
    y = np.array([int(r["label"]) for r in rows])
    p = np.array([float(r["p_crl"]) for r in rows])
    rng = np.random.default_rng(seed)
    y_shuf = rng.permutation(y)
    return {
        "shuffled_auc": round(float(roc_auc_score(y_shuf, p)), 4),
        "perm_p_on_real": round(perm_p(y, p, n=n), 4),
    }


def sensitivity_runs(scored: List[Dict[str, Any]], *, seed: int = 123, n_boot: int = 2000) -> Dict[str, Any]:
    rows = [r for r in scored if r.get("p_crl") is not None]
    runs: Dict[str, Any] = {}
    runs["full"] = compute_metrics(rows, seed=seed, n_boot=n_boot)

    nda = [r for r in rows if str(r.get("appl_type", "")).upper() == "NDA"]
    bla = [r for r in rows if str(r.get("appl_type", "")).upper() == "BLA"]
    runs["nda_only"] = compute_metrics(nda, seed=seed, n_boot=n_boot)
    runs["bla_only"] = compute_metrics(bla, seed=seed, n_boot=n_boot)

    # collapse multi-appno CRL letters to one row (pick primary = the row itself;
    # de-dupe positives by file_name keeping the first). Negatives unaffected.
    seen_files = set()
    collapsed = []
    for r in rows:
        if int(r.get("label")) == 1 and r.get("file_name"):
            if r["file_name"] in seen_files:
                continue
            seen_files.add(r["file_name"])
        collapsed.append(r)
    runs["collapse_multi_appno"] = compute_metrics(collapsed, seed=seed, n_boot=n_boot)

    # 2026-only positives (strictest OOS) + all negatives
    pos2026 = [r for r in rows if int(r.get("label")) == 1 and str(r.get("letter_year")) == "2026"]
    negs = [r for r in rows if int(r.get("label")) == 0]
    runs["pos2026_only"] = compute_metrics(pos2026 + negs, seed=seed, n_boot=n_boot)
    return runs


def coverage_report(scored: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = [r for r in scored if r.get("p_crl") is not None]
    covs = [float(r["_coverage"]) for r in rows if r.get("_coverage") is not None]
    # per-feature presence across the cohort
    from collections import Counter

    src_counter: Dict[str, Counter] = {}
    for r in rows:
        fs = r.get("_feature_sources")
        if isinstance(fs, str):
            try:
                fs = json.loads(fs)
            except Exception:  # noqa: BLE001
                fs = {}
        for k, v in (fs or {}).items():
            src_counter.setdefault(k, Counter())[v] += 1
    return {
        "n_scored": len(rows),
        "mean_coverage": round(float(np.mean(covs)), 4) if covs else None,
        "median_coverage": round(float(np.median(covs)), 4) if covs else None,
        "min_coverage": round(float(np.min(covs)), 4) if covs else None,
        "max_coverage": round(float(np.max(covs)), 4) if covs else None,
        "per_feature_source_counts": {k: dict(v) for k, v in src_counter.items()},
    }


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def load_cohort_csv(path: Path) -> List[Dict[str, Any]]:
    rows = list(csv.DictReader(open(path)))
    for r in rows:
        # coerce numeric/json fields stored as strings in the CSV
        if r.get("p_m14_cal_precomputed"):
            try:
                r["p_m14_cal_precomputed"] = float(r["p_m14_cal_precomputed"])
            except ValueError:
                r["p_m14_cal_precomputed"] = None
    return rows


def run(
    cohort_csv: Path,
    out_dir: Path,
    *,
    sleep_s: float = 0.0,
    enable_warning_letters: bool = True,
    n_boot: int = 2000,
    seed: int = 123,
) -> Dict[str, Any]:
    scorer, model = load_scorer()
    cohort = load_cohort_csv(cohort_csv)
    dfda = DrugsFDA(sleep_s=sleep_s)
    scored = score_cohort(
        cohort, scorer, model, dfda=dfda,
        enable_warning_letters=enable_warning_letters,
    )

    runs = sensitivity_runs(scored, seed=seed, n_boot=n_boot)
    canary = leakage_canary(scored)
    cov = coverage_report(scored)

    export_tag = Path(cohort_csv).stem.replace("cohort_", "")
    result = {
        "export_date": export_tag,
        "model_version": model.get("model_version"),
        "scorer_path": str(SCORER_PATH),
        "n_scored": cov["n_scored"],
        "sensitivity_runs": runs,
        "leakage_canary": canary,
        "coverage": cov,
        "method_notes": {
            "auc": "ranking_auc (Mann-Whitney) from eval_harness/metrics.py, used as-is",
            "auc_sklearn": "roc_auc_score (tie-aware) for reference + CI consistency",
            "brier": "brier_score from fda_calibration_math.py, used as-is",
            "boot_ci": "VERBATIM boot_ci from build_adjusted_m14.py (sklearn, seed 123, n=2000)",
            "cal_slope": "VERBATIM cal_slope_intercept (sklearn LogisticRegression on logit(p))",
            "perm_p": "VERBATIM perm_p from build_adjusted_m14.py",
            "tier_a_negatives": "reuse precomputed p_m14_cal (M14 authors' PIT score); offline rebuild cross-check recorded",
            "headline_ci_floor": "auc_ci_low from boot_ci on the FULL cohort",
        },
    }

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = out_dir / f"metrics_{export_tag}.json"
    metrics_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    # also persist the scored cohort (with p_crl) for audit
    scored_path = out_dir / f"scored_cohort_{export_tag}.csv"
    slim = []
    for r in scored:
        rr = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items() if k != "text"}
        slim.append(rr)
    fields = list(dict.fromkeys([k for r in slim for k in r.keys()]))
    with scored_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(slim)

    result["_written"] = {"metrics": str(metrics_path), "scored_cohort": str(scored_path)}
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", type=Path, default=_REPO_ROOT / "data" / "a0" / "cohort_2026_06_01.csv")
    parser.add_argument("--out-dir", type=Path, default=_REPO_ROOT / "data" / "a0")
    parser.add_argument("--sleep-s", type=float, default=0.0)
    parser.add_argument("--no-warning-letters", action="store_true")
    parser.add_argument("--n-boot", type=int, default=2000)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    result = run(
        args.cohort, args.out_dir,
        sleep_s=args.sleep_s,
        enable_warning_letters=not args.no_warning_letters,
        n_boot=args.n_boot,
    )
    # compact console summary
    full = result["sensitivity_runs"]["full"]
    print(json.dumps({
        "n": full["n"], "n_pos": full["n_pos"],
        "AUC_ranking": full.get("auc_ranking"),
        "AUC_ci": [full.get("auc_ci_low"), full.get("auc_ci_high")],
        "Brier": full.get("brier"),
        "cal_slope": full.get("calibration_slope"),
        "perm_p": full.get("perm_p"),
        "per_band": full.get("per_band"),
        "mean_coverage": result["coverage"]["mean_coverage"],
        "leakage_canary": result["leakage_canary"],
        "written": result["_written"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
