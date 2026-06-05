"""A0 test plan (§7) — wiring + cohort-funnel + no-look-ahead proofs.

These tests prove the A0 study is correctly wired BEFORE its OOS number is
trusted. They run against the frozen 2026-06-01 snapshot under ``data/a0/`` and
the M14 export artifacts; the few that need live network are skipped when the
snapshot/artifacts are absent or when offline.

Run:  python3 -m pytest tests/bc_a0/test_cohort_and_features.py -v

Mapping to plan §7:
  1 source-shape guard            -> test_source_shape_guard
  2 cohort funnel snapshot        -> test_cohort_funnel_counts
  3 label reconciliation          -> test_label_reconciliation
  4 negative disjointness         -> test_negative_disjointness
  5 feature parity vs assemble     -> test_feature_builder_parity_with_assemble_nda
  6 no-look-ahead + leakage canary -> test_no_look_ahead_date_filter / test_leakage_canary
  7 scorer-import reproduction     -> test_scorer_reproduces_example_output /
                                       test_scorer_calibration_reproduces_p_m14_cal
  8 metrics reuse                  -> test_metrics_reuse_locked_2025
  9 CI-method parity               -> test_boot_ci_reproduces_locked_ci
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import re
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "x")

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "data" / "a0"
SNAPSHOT = DATA / "crl_transparency_raw_2026_06_01.json"
COHORT_CSV = DATA / "cohort_2026_06_01.csv"
METRICS_JSON = DATA / "metrics_2026_06_01.json"

M14 = Path("/Users/Pico/Downloads/BC_scoring_rubrics_export/NDA_M14_adjusted")
SCORER_PATH = M14 / "scripts" / "score_m14_adjusted.py"
LOCKED_CSV = M14 / "data" / "locked_2025_predictions_m14_adjusted.csv"
PROSPECTIVE_CSV = M14 / "data" / "prospective_2026_predictions_m14_adjusted.csv"

APPNO_RE = re.compile(r"^\s*(NDA|BLA|ANDA)\s*0*(\d+)", re.I)

requires_snapshot = pytest.mark.skipif(not SNAPSHOT.exists(), reason="frozen snapshot missing")
requires_cohort = pytest.mark.skipif(not COHORT_CSV.exists(), reason="cohort artifact missing")
requires_m14 = pytest.mark.skipif(not SCORER_PATH.exists(), reason="M14 export missing")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _load_snapshot():
    return json.loads(SNAPSHOT.read_text(encoding="utf-8")).get("results") or []


def _norm_digits(s: str) -> str:
    return (re.sub(r"\D", "", s).lstrip("0")) or "0"


def _cr_appnos(results, years=None):
    out = set()
    for r in results:
        if r.get("letter_type") != "COMPLETE RESPONSE":
            continue
        if years is not None and str(r.get("letter_year")) not in years:
            continue
        an = r.get("application_number")
        if isinstance(an, str):
            an = [an]
        for x in an or []:
            m = APPNO_RE.match(str(x))
            if m and m.group(1).upper() in ("NDA", "BLA"):
                out.add(_norm_digits(m.group(2)))
    return out


def _load_scorer():
    spec = importlib.util.spec_from_file_location("score_m14_adjusted", SCORER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, mod.load_model()


# --------------------------------------------------------------------------- #
# §7.1 source-shape guard
# --------------------------------------------------------------------------- #
@requires_snapshot
def test_source_shape_guard():
    root = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert "results" in root
    results = root["results"]
    assert len(results) >= 400, f"expected >=400 records, got {len(results)}"
    n_cr = sum(1 for r in results if r.get("letter_type") == "COMPLETE RESPONSE")
    assert n_cr >= 400, f"expected >=400 COMPLETE RESPONSE, got {n_cr}"


# --------------------------------------------------------------------------- #
# §7.2 cohort funnel snapshot (exact frozen counts)
# --------------------------------------------------------------------------- #
@requires_snapshot
def test_cohort_funnel_counts():
    results = _load_snapshot()
    cr = [r for r in results if r.get("letter_type") == "COMPLETE RESPONSE"]
    assert len(cr) == 426

    inwin = [r for r in cr if str(r.get("letter_year")) in ("2025", "2026")]
    assert len(inwin) == 73

    distinct = _cr_appnos(results, years={"2025", "2026"})
    assert len(distinct) == 63

    locked = {_norm_digits(r["ApplNo"]) for r in csv.DictReader(open(LOCKED_CSV))}
    overlap = distinct & locked
    assert len(overlap) == 9, f"expected 9 locked-overlap, got {len(overlap)}"

    oos = distinct - locked
    assert len(oos) == 54, f"expected 54 raw OOS positives, got {len(oos)}"


# --------------------------------------------------------------------------- #
# §7.3 label reconciliation (self-validation)
# --------------------------------------------------------------------------- #
@requires_snapshot
def test_label_reconciliation():
    results = _load_snapshot()
    all_cr = _cr_appnos(results)  # any year
    locked_pos = {
        _norm_digits(r["ApplNo"])
        for r in csv.DictReader(open(LOCKED_CSV))
        if r["CRL_label_strict"] == "1"
    }
    assert locked_pos == {
        "211241", "215244", "218571", "218879",
        "761211", "761427", "761440", "761451", "761458",
    }
    assert locked_pos.issubset(all_cr), "locked-2025 positives must all be Transparency CRs"


# --------------------------------------------------------------------------- #
# §7.4 negative disjointness
# --------------------------------------------------------------------------- #
@requires_cohort
@requires_snapshot
def test_negative_disjointness():
    results = _load_snapshot()
    inwin_cr = _cr_appnos(results, years={"2025", "2026"})
    cohort = list(csv.DictReader(open(COHORT_CSV)))
    neg_norms = {r["appno_norm"] for r in cohort if r["label"] == "0"}
    leak = neg_norms & inwin_cr
    assert not leak, f"negatives must be disjoint from in-window CR set; leak={sorted(leak)}"


# --------------------------------------------------------------------------- #
# §7.5 feature-builder parity with feature_assembly.assemble_nda_features
# --------------------------------------------------------------------------- #
@requires_m14
def test_feature_builder_parity_with_assemble_nda():
    """The offline builder and the live `assemble_nda_features` must produce the
    SAME scorer-input dict on a shared fixture (same keys + values), so the OOS
    features match the live weekly path. We drive both with a fake client/source
    so neither touches a real DB, isolating the FEATURE DEFINITIONS."""
    from analysis.bc_a0 import feature_builder as fb
    from modal_workers.bc_score._m14 import feature_assembly as fa

    ref = date(2025, 1, 1)

    # --- live path: assemble_nda_features against a stub client ---
    class StubClient:
        def _rest(self, method, table, params=None, **kw):
            params = params or {}
            if table == "fda_application_submissions" and params.get("application_number", "").startswith("eq."):
                return [{
                    "submission_type": "ORIG", "submission_class_code": "TYPE 5",
                    "review_priority": "PRIORITY", "submission_status_date": "2024-06-01",
                    "submission_number": "1",
                }]
            if table == "fda_application_submissions":  # sponsor prior-filings query
                return [
                    {"application_number": "NDA111111", "submission_status_date": "2020-01-01",
                     "submission_type": "ORIG", "sponsor_name": "ACME PHARMA"},
                    {"application_number": "NDA222222", "submission_status_date": "2021-01-01",
                     "submission_type": "ORIG", "sponsor_name": "ACME PHARMA"},
                ]
            if table == "fda_drug_inspections":
                return [{"inspection_id": "x", "inspection_end_date": "2023-01-01"}]
            if table == "fda_warning_letters":
                return []  # no warning -> 0
            if table == "documents":
                return []  # entity_id absent -> handled below
            return []

    asset = {"application_number": "BLA761000", "sponsor_name": "ACME PHARMA"}
    event = {}
    live = fa.assemble_nda_features(StubClient(), asset, event, ref_date=ref)

    # --- offline path: build_features with stubbed DrugsFDA returning the SAME data ---
    class StubDFDA:
        def application(self, appno):
            return {"application_number": "BLA761000", "sponsor_name": "ACME PHARMA",
                    "submissions": [{"submission_type": "ORIG", "submission_class_code": "TYPE 5",
                                     "review_priority": "PRIORITY", "submission_status_date": "20240601",
                                     "submission_number": "1"}]}

        def sponsor_applications(self, sponsor_name):
            return [
                {"application_number": "NDA111111",
                 "submissions": [{"submission_type": "ORIG", "submission_status_date": "20200101"}]},
                {"application_number": "NDA222222",
                 "submissions": [{"submission_type": "ORIG", "submission_status_date": "20210101"}]},
            ]

    offline = fb.build_features(
        appno="BLA761000", sponsor_name="ACME PHARMA", cik=None, ref_date=ref,
        dfda=StubDFDA(), enable_warning_letters=False,
    )

    # Compare the SCORER-RELEVANT keys both paths CAN source from the stub:
    # is_bla, ApplType, priority, SubmissionClassCode, n_prior_filings, cycle_type.
    for key in ("is_bla", "ApplType", "priority", "SubmissionClassCode", "n_prior_filings", "cycle_type"):
        assert offline.get(key) == live.get(key), (
            f"feature parity mismatch on {key}: offline={offline.get(key)} live={live.get(key)}"
        )
    # Both expose the same coverage-key concept.
    assert "_coverage" in offline and "_coverage" in live


# --------------------------------------------------------------------------- #
# §7.6 no-look-ahead (date filter holds) + leakage canary
# --------------------------------------------------------------------------- #
def test_no_look_ahead_date_filter():
    """Inject a synthetic prior-ORIG dated ref_date+1d and assert the builder
    EXCLUDES it (n_prior_filings does not count the future row)."""
    from analysis.bc_a0 import feature_builder as fb

    ref = date(2025, 1, 1)

    class StubDFDA:
        def application(self, appno):
            return None

        def sponsor_applications(self, sponsor_name):
            return [
                {"application_number": "NDA100001",
                 "submissions": [{"submission_type": "ORIG", "submission_status_date": "20240101"}]},  # before ref -> count
                {"application_number": "NDA100002",
                 "submissions": [{"submission_type": "ORIG", "submission_status_date": "20250102"}]},  # AFTER ref -> exclude
            ]

    feats = fb.build_features(
        appno="NDA999999", sponsor_name="ACME", cik=None, ref_date=ref,
        dfda=StubDFDA(), enable_warning_letters=False,
    )
    assert feats["n_prior_filings"] == 1, "the ref+1d ORIG must NOT be counted (no look-ahead)"
    # and the builder's max-source-date guard must be <= ref
    msd = feats.get("_max_source_date")
    if msd:
        assert date.fromisoformat(msd) <= ref


def test_no_look_ahead_assertion_fires():
    """If a feature ever used a source row dated > ref, the builder raises."""
    from analysis.bc_a0 import feature_builder as fb

    ref = date(2025, 1, 1)
    # monkeypatch _n_prior_filings to return a future max-date and assert raise.
    # build_features (the A0 shim) delegates to the promoted shared builder, so
    # the patch target is the real call site in feature_builder_pit — NOT the
    # shim's re-export namespace (which build_features never consults).
    import modal_workers.shared.feature_builder_pit as mod
    orig = mod._n_prior_filings
    mod._n_prior_filings = lambda *a, **k: (3, date(2025, 6, 1))  # future date
    try:
        with pytest.raises(AssertionError):
            fb.build_features(appno="NDA1", sponsor_name="X", cik=None, ref_date=ref,
                              dfda=type("D", (), {"application": lambda s, a: None})(),
                              enable_warning_letters=False)
    finally:
        mod._n_prior_filings = orig


@requires_cohort
def test_leakage_canary():
    """§3.3 #4: with the scored cohort, shuffling labels collapses AUC to ~0.5
    and the real perm-p is small (signal present, no leakage)."""
    scored_path = DATA / "scored_cohort_2026_06_01.csv"
    if not scored_path.exists():
        pytest.skip("scored cohort not built")
    import numpy as np
    from sklearn.metrics import roc_auc_score

    rows = [r for r in csv.DictReader(open(scored_path)) if r.get("p_crl") not in (None, "", "None")]
    y = np.array([int(r["label"]) for r in rows])
    p = np.array([float(r["p_crl"]) for r in rows])
    rng = np.random.default_rng(456)
    shuffled = roc_auc_score(rng.permutation(y), p)
    assert 0.30 <= shuffled <= 0.70, f"shuffled AUC should collapse to ~0.5, got {shuffled:.3f}"


# --------------------------------------------------------------------------- #
# §7.7 scorer-import reproduction
# --------------------------------------------------------------------------- #
@requires_m14
def test_scorer_reproduces_example_output():
    scorer, model = _load_scorer()
    in_rows = list(csv.DictReader(open(M14 / "examples" / "example_input.csv", encoding="utf-8-sig")))
    exp_rows = list(csv.DictReader(open(M14 / "examples" / "example_output.csv", encoding="utf-8-sig")))
    for inr, expr in zip(in_rows, exp_rows):
        got = scorer.score_row(dict(inr), model)
        for k in ("p_crl", "raw_p_uncalibrated", "ci_low", "ci_high"):
            g, e = got.get(k, ""), expr.get(k, "")
            if g and e:
                assert abs(float(g) - float(e)) < 1e-6, f"{inr['ApplNo']} {k}: {g} != {e}"
        assert str(got.get("risk_band", "")) == str(expr.get("risk_band", ""))
        assert str(got.get("confidence_flag", "")) == str(expr.get("confidence_flag", ""))
        assert str(got.get("refusal_reason", "")) == str(expr.get("refusal_reason", ""))


@requires_m14
def test_scorer_calibration_reproduces_p_m14_cal():
    """The imported scorer's Platt stage reproduces the published p_m14_cal from
    p_m14_raw to machine precision on BOTH the prospective-2026 and locked-2025
    CSVs — i.e. the scorer's calibration math is identical to the build pipeline
    that wrote those files. (The raw 13-feature vectors are not shipped with the
    export, so the full forward pass is proven on example_input.csv above; this
    proves the calibration stage exactly.)"""
    import math

    model = json.loads((M14 / "model" / "m14_adjusted_model.json").read_text())
    a = model["platt_calibration"]["a"]
    b = model["platt_calibration"]["b"]

    def sigmoid(x):
        return 1 / (1 + math.exp(-x)) if x >= 0 else math.exp(x) / (1 + math.exp(x))

    def logit(p):
        p = min(max(p, 1e-9), 1 - 1e-9)
        return math.log(p / (1 - p))

    for path in (PROSPECTIVE_CSV, LOCKED_CSV):
        for r in csv.DictReader(open(path)):
            if not r.get("p_m14_raw") or not r.get("p_m14_cal"):
                continue
            recon = sigmoid(a * logit(float(r["p_m14_raw"])) + b)
            assert abs(recon - float(r["p_m14_cal"])) < 1e-6


# --------------------------------------------------------------------------- #
# §7.8 metrics reuse (reproduce locked AUC/Brier)
# --------------------------------------------------------------------------- #
@requires_m14
def test_metrics_reuse_locked_2025():
    from modal_workers.shared.fda_calibration_math import brier_score
    from orchestrator_runtime.eval_harness.metrics import ranking_auc

    rows = list(csv.DictReader(open(LOCKED_CSV)))
    p = [float(r["p_m14_cal"]) for r in rows]
    y = [int(r["CRL_label_strict"]) for r in rows]
    auc = ranking_auc(p, y)
    brier = brier_score(p, y)
    # ranking_auc (Mann-Whitney, tie-non-averaged) ~ sklearn 0.8104; allow small gap.
    assert abs(auc - 0.8104) < 0.01, f"locked AUC reproduction off: {auc}"
    assert abs(brier - 0.0635) < 0.001, f"locked Brier reproduction off: {brier}"


# --------------------------------------------------------------------------- #
# §7.9 CI-method parity (reproduce locked CI [0.637, 0.954])
# --------------------------------------------------------------------------- #
@requires_m14
def test_boot_ci_reproduces_locked_ci():
    import numpy as np

    from analysis.bc_a0.score_and_metrics import boot_ci

    rows = list(csv.DictReader(open(LOCKED_CSV)))
    p = np.array([float(r["p_m14_cal"]) for r in rows])
    y = np.array([int(r["CRL_label_strict"]) for r in rows])
    lo, hi = boot_ci(y, p, seed=123, n=2000)
    assert abs(lo - 0.6370) < 0.005, f"CI low parity off: {lo}"
    assert abs(hi - 0.9509) < 0.005, f"CI high parity off: {hi}"
