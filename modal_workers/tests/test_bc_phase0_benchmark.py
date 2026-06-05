"""Unit tests for the Phase-0 benchmark's date-trust scorer (phase0 §2.2/§2.3/§4).

These pin the MATH of the GO/NO-GO gate — specifically gate criterion 3 ("date-exact
rate ≥0.80 and false-positive rate ≤0.15"), which is the whole point of Phase 0. The live
run (enumerator over real EFTS+Polygon) is gated on operator-exported secrets and is NOT
exercised here; what IS proven here is that *once* live candidates exist, the scorer turns
them into the right recall / date-exact / false-positive / rubric / verdict numbers. Without
this, the gate verdict would be computed by untested code.

Covered:
  - score_against_truth: recall (overall + in-window), date-exact rate + day-bucketing,
    false-positive rate, market-cap-bucket recall, candidate↔truth matching (CIK / ticker /
    drug-substring), pending/in-window classification.
  - _finalize: the four derived rates incl. division-by-zero guards.
  - rubric_score: the §2.3 weighted sum (0.35/0.25/0.15/0.15/0.10) + (1-FP) inversion.
  - the §4.1 gate thresholds + the verdict decision rule (replicated from run_benchmark).
  - load_truthset: array parse, _meta-row tolerance, missing-file soft-degrade.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import modal_workers.scripts.bc_phase0_benchmark as bm
from modal_workers.scripts.bc_phase0_benchmark import (
    TruthRow,
    _GATE_DATE_EXACT,
    _GATE_FP_MAX,
    _GATE_IN_WINDOW,
    _GATE_TRADEABLE,
    _W_COST,
    _W_DATE_EXACT,
    _W_FP,
    _W_RECALL,
    _W_REPRO,
    _drug_matches,
    _match_rank,
    _matches,
    _select_match,
    _sponsor_matches,
    load_truthset,
    rubric_score,
    score_against_truth,
)

TODAY = date(2026, 6, 4)
WINDOW = 120


def _cand(*, cik=None, ticker=None, drug=None, pdufa=None, mcap=None):
    """A minimal enumerator-candidate stand-in (the scorer reads attrs via getattr)."""
    return SimpleNamespace(
        cik=cik, ticker=ticker, drug_name=drug,
        pdufa_date=pdufa, market_cap_usd=mcap,
    )


def _truth(**over):
    base = dict(ticker=None, cik=None, drug=None, true_pdufa_date=None,
                appl_number=None, appl_type=None, status=None,
                market_cap_bucket=None, source=None)
    base.update(over)
    return TruthRow(**base)


# ---------------------------------------------------------------------------
# Gate constants stay aligned with phase0 §4.1.
# ---------------------------------------------------------------------------

def test_gate_constants_match_spec():
    assert _GATE_IN_WINDOW == 15
    assert _GATE_TRADEABLE == 12
    assert _GATE_DATE_EXACT == 0.80
    assert _GATE_FP_MAX == 0.15


def test_rubric_weights_sum_to_one_and_match_spec():
    assert _W_RECALL == 0.35
    assert _W_DATE_EXACT == 0.25
    assert _W_FP == 0.15
    assert _W_REPRO == 0.15
    assert _W_COST == 0.10
    assert round(_W_RECALL + _W_DATE_EXACT + _W_FP + _W_REPRO + _W_COST, 6) == 1.0


# ---------------------------------------------------------------------------
# Candidate ↔ truth matching (_matches via score_against_truth surfacing)
# ---------------------------------------------------------------------------

def test_match_by_cik():
    truth = [_truth(cik="1016504", true_pdufa_date="2026-09-01", status="pending")]
    cands = [_cand(cik="1016504", pdufa="2026-09-01")]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.surfaced == 1
    assert r.recall_overall == 1.0


def test_match_by_ticker_case_insensitive():
    truth = [_truth(ticker="exel", true_pdufa_date="2026-09-01", status="pending")]
    cands = [_cand(ticker="EXEL", pdufa="2026-09-01")]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.surfaced == 1


def test_match_by_drug_substring_both_directions():
    truth = [_truth(drug="zanzalintinib", true_pdufa_date="2026-09-01", status="pending")]
    # candidate drug is a superset string containing the truth drug
    cands = [_cand(drug="zanzalintinib tablets", pdufa="2026-09-01")]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.surfaced == 1


def test_no_match_yields_zero_recall():
    truth = [_truth(ticker="AAAA", true_pdufa_date="2026-09-01", status="pending")]
    cands = [_cand(ticker="ZZZZ", pdufa="2026-09-01")]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.surfaced == 0
    assert r.recall_overall == 0.0


# ---------------------------------------------------------------------------
# Date-exact rate + day bucketing (the gate-3 numerator)
# ---------------------------------------------------------------------------

def test_date_exact_when_extracted_equals_truth():
    truth = [_truth(ticker="EXEL", true_pdufa_date="2026-09-01", status="pending")]
    cands = [_cand(ticker="EXEL", pdufa="2026-09-01")]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.date_exact == 1
    assert r.date_exact_rate == 1.0
    assert r.date_buckets["0"] == 1


def test_date_off_by_three_is_not_exact_but_in_le7_bucket():
    truth = [_truth(ticker="EXEL", true_pdufa_date="2026-09-01", status="pending")]
    cands = [_cand(ticker="EXEL", pdufa="2026-09-04")]  # +3 days
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.date_exact == 0
    assert r.date_exact_rate == 0.0
    assert r.date_buckets["<=7"] == 1
    assert r.date_buckets["0"] == 0


def test_date_buckets_partitions_le30_and_gt30():
    truth = [
        _truth(ticker="A", true_pdufa_date="2026-09-01", status="pending"),
        _truth(ticker="B", true_pdufa_date="2026-09-01", status="pending"),
    ]
    cands = [
        _cand(ticker="A", pdufa="2026-09-21"),   # +20 -> <=30
        _cand(ticker="B", pdufa="2026-12-01"),   # +91 -> >30
    ]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.date_buckets["<=30"] == 1
    assert r.date_buckets[">30"] == 1
    assert r.date_exact_rate == 0.0


def test_date_exact_rate_is_over_surfaced_only():
    # 3 truth, 2 surfaced (1 exact, 1 off); the unmatched truth doesn't dilute the rate.
    truth = [
        _truth(ticker="A", true_pdufa_date="2026-09-01", status="pending"),
        _truth(ticker="B", true_pdufa_date="2026-09-01", status="pending"),
        _truth(ticker="C", true_pdufa_date="2026-09-01", status="pending"),  # not surfaced
    ]
    cands = [
        _cand(ticker="A", pdufa="2026-09-01"),   # exact
        _cand(ticker="B", pdufa="2026-09-10"),   # off by 9
    ]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.surfaced == 2
    assert r.date_exact == 1
    assert r.date_exact_rate == 0.5         # 1/2 surfaced, NOT 1/3 truth
    assert r.recall_overall == round(2 / 3, 3)


# ---------------------------------------------------------------------------
# False-positive rate (emitted-with-date that match no truth)
# ---------------------------------------------------------------------------

def test_raw_fp_proxy_counts_unmatched_dated_candidates():
    """The OLD unrestricted proxy (now exposed as false_pos_rate_raw + false_positives)
    flags every dated candidate that binds no truth row. The CORRECTED gate metric
    (false_pos_rate) is 0 here because the unmatched tickers X/Y are absent from the
    truth set entirely (real out-of-scope catalysts, not precision failures)."""
    truth = [_truth(ticker="A", true_pdufa_date="2026-09-01", status="pending")]
    cands = [
        _cand(ticker="A", pdufa="2026-09-01"),    # matched -> not FP
        _cand(ticker="X", pdufa="2026-10-01"),    # dated, unmatched, uncovered sponsor
        _cand(ticker="Y", pdufa="2026-11-01"),    # dated, unmatched, uncovered sponsor
    ]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.emitted_with_date == 3
    assert r.false_positives == 2                 # raw proxy
    assert r.false_pos_rate_raw == round(2 / 3, 3)
    # corrected metric: X/Y sponsors aren't in the truth set -> NOT false positives
    assert r.fp_eval_pool == 1                    # only the covered 'A' candidate
    assert r.fp_contradictions == 0
    assert r.false_pos_rate == 0.0


def test_candidates_without_date_are_not_counted_as_emitted():
    truth = [_truth(ticker="A", true_pdufa_date="2026-09-01", status="pending")]
    cands = [
        _cand(ticker="A", pdufa="2026-09-01"),    # matched, dated
        _cand(ticker="X", pdufa=None),            # no date -> not emitted_with_date, not FP
    ]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.emitted_with_date == 1
    assert r.false_positives == 0
    assert r.false_pos_rate == 0.0


# ---------------------------------------------------------------------------
# in-window vs pending classification + in-window recall (the §2.3 0.35 term)
# ---------------------------------------------------------------------------

def test_in_window_recall_only_counts_in_window_truth():
    truth = [
        _truth(ticker="A", true_pdufa_date="2026-07-01", status="pending"),  # +27 in window
        _truth(ticker="B", true_pdufa_date="2027-09-01", status="pending"),  # far out of window
    ]
    cands = [
        _cand(ticker="A", pdufa="2026-07-01"),
        _cand(ticker="B", pdufa="2027-09-01"),
    ]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.n_truth_in_window == 1
    assert r.surfaced_in_window == 1
    assert r.recall_in_window == 1.0          # 1/1 in-window, not 2/2 overall


def test_out_of_window_past_date_excluded():
    # A PDUFA date in the past (negative delta) is NOT in-window (gate is 0..120).
    truth = [_truth(ticker="A", true_pdufa_date="2026-01-01", status="resolved")]
    cands = [_cand(ticker="A", pdufa="2026-01-01")]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.n_truth_in_window == 0
    assert r.surfaced == 1                     # still surfaced overall
    assert r.recall_in_window == 0.0           # but no in-window truth -> guarded 0.0


# ---------------------------------------------------------------------------
# market-cap bucket recall (exposes the 8-K large-cap skew)
# ---------------------------------------------------------------------------

def test_bucket_recall_prefers_truth_declared_bucket():
    truth = [
        _truth(ticker="A", true_pdufa_date="2026-09-01", status="pending", market_cap_bucket="micro"),
        _truth(ticker="B", true_pdufa_date="2026-09-01", status="pending", market_cap_bucket="mid+"),
    ]
    cands = [_cand(ticker="B", pdufa="2026-09-01")]   # only mid+ surfaced
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.bucket_recall["micro"] == {"truth": 1, "surfaced": 0}
    assert r.bucket_recall["mid+"] == {"truth": 1, "surfaced": 1}


def test_bucket_derived_from_candidate_mcap_when_truth_silent():
    truth = [_truth(ticker="A", true_pdufa_date="2026-09-01", status="pending")]  # no bucket
    cands = [_cand(ticker="A", pdufa="2026-09-01", mcap=5_000_000_000)]           # >$2B -> mid+
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.bucket_recall.get("mid+", {}).get("surfaced") == 1


# ---------------------------------------------------------------------------
# _finalize division-by-zero guards (empty truth / empty emitted)
# ---------------------------------------------------------------------------

def test_empty_truth_and_candidates_yields_zero_rates_no_crash():
    r = score_against_truth([], [], WINDOW, TODAY)
    assert r.recall_overall == 0.0
    assert r.recall_in_window == 0.0
    assert r.date_exact_rate == 0.0
    assert r.false_pos_rate == 0.0


# ---------------------------------------------------------------------------
# rubric_score — the §2.3 weighted sum
# ---------------------------------------------------------------------------

def test_rubric_perfect_scores_sum_to_one():
    rb = rubric_score(recall_in_window=1.0, date_exact_rate=1.0, false_pos_rate=0.0,
                      reproducible_daily=1.0, cost_score=1.0)
    assert rb["winner_score"] == 1.0


def test_rubric_applies_each_weight_and_inverts_fp():
    rb = rubric_score(recall_in_window=1.0, date_exact_rate=0.0, false_pos_rate=1.0,
                      reproducible_daily=0.0, cost_score=0.0)
    # only recall term (0.35*1) + (1-FP)=0 term contributes; fp=1 -> false_pos_inv=0
    assert rb["terms"]["recall_in_window"] == 0.35
    assert rb["terms"]["false_pos_inv"] == 0.0
    assert rb["terms"]["date_exact_rate"] == 0.0
    assert rb["winner_score"] == 0.35


def test_rubric_known_mixed_value():
    # recall .8, date .9, fp .1, repro 1, cost 1
    rb = rubric_score(recall_in_window=0.8, date_exact_rate=0.9, false_pos_rate=0.1,
                      reproducible_daily=1.0, cost_score=1.0)
    expected = (0.35 * 0.8) + (0.25 * 0.9) + (0.15 * 0.9) + (0.15 * 1.0) + (0.10 * 1.0)
    assert rb["winner_score"] == round(expected, 4)


# ---------------------------------------------------------------------------
# The §4 verdict decision rule (replicated from run_benchmark so the gate logic
# is pinned even though run_benchmark itself needs the live enumerator).
# ---------------------------------------------------------------------------

def _verdict(*, N, M, date_exact_rate, false_pos_rate, has_truth=True):
    """Mirror of run_benchmark's verdict assembly (bc_phase0_benchmark.py ~488-498)."""
    hard_pass = (N >= _GATE_IN_WINDOW) and (M >= _GATE_TRADEABLE)
    date_pass = (not has_truth) or (
        date_exact_rate >= _GATE_DATE_EXACT and false_pos_rate <= _GATE_FP_MAX)
    if hard_pass and date_pass and has_truth:
        return "GO"
    if hard_pass and not has_truth:
        return "GO (universe gate met; date-trust UNVERIFIED pending truth set)"
    if N >= 10:
        return "MARGINAL — escalate (reduced-scope monitor or buy approach 2)"
    return "NO-GO — escalate to Pedro (universe too small / dates untrusted)"


def test_verdict_go_when_all_criteria_clear():
    assert _verdict(N=16, M=13, date_exact_rate=0.85, false_pos_rate=0.10) == "GO"


def test_verdict_blocked_by_low_date_exact_even_if_universe_big():
    # Universe + tradeability pass, but date-exact < 0.80 -> NOT GO (this is gate-3 biting).
    v = _verdict(N=18, M=14, date_exact_rate=0.70, false_pos_rate=0.10)
    assert v != "GO"
    assert v.startswith("MARGINAL")


def test_verdict_blocked_by_high_false_positive():
    v = _verdict(N=18, M=14, date_exact_rate=0.90, false_pos_rate=0.30)
    assert v != "GO"


def test_verdict_unverified_when_truth_absent():
    v = _verdict(N=16, M=13, date_exact_rate=0.0, false_pos_rate=0.0, has_truth=False)
    assert v.startswith("GO (universe gate met; date-trust UNVERIFIED")


def test_verdict_no_go_when_universe_tiny():
    assert _verdict(N=5, M=3, date_exact_rate=0.0, false_pos_rate=0.0,
                    has_truth=False).startswith("NO-GO")


# ---------------------------------------------------------------------------
# load_truthset — array parse, _meta tolerance, missing-file degrade
# ---------------------------------------------------------------------------

def test_load_truthset_real_fixture_parses_37_rows():
    rows, err = load_truthset(bm._TRUTHSET_PATH)
    assert err is None
    # 37 data rows (the _meta sentinel carries no true_pdufa_date and is filtered by callers;
    # load_truthset itself keeps only dict rows, and the _meta row has no date so it scores
    # as a non-surfaceable no-op). Assert the real cohort size is present.
    dated = [r for r in rows if r.true_pdufa_date]
    assert len(dated) == 37


def test_load_truthset_missing_file_soft_degrades(tmp_path):
    rows, err = load_truthset(tmp_path / "nope.json")
    assert rows == []
    assert err and "not found" in err


def test_load_truthset_tolerates_meta_and_non_dict_entries(tmp_path):
    p = tmp_path / "ts.json"
    p.write_text(json.dumps([
        {"_meta": True, "purpose": "x"},                       # meta sentinel (no date)
        "junk-string",                                          # non-dict -> skipped
        {"ticker": "AAA", "true_pdufa_date": "2026-09-01"},     # real row
    ]))
    rows, err = load_truthset(p)
    assert err is None
    dated = [r for r in rows if r.true_pdufa_date]
    assert len(dated) == 1
    assert dated[0].ticker == "AAA"


def test_load_truthset_accepts_pdufa_date_alias(tmp_path):
    p = tmp_path / "ts.json"
    p.write_text(json.dumps([{"ticker": "B", "pdufa_date": "2026-09-01"}]))  # alias key
    rows, _ = load_truthset(p)
    assert rows[0].true_pdufa_date == "2026-09-01"


# ===========================================================================
# FIX 1 — drug-level disambiguation (multi-drug sponsor scoring). The bug: a
# truth row bound to the FIRST same-CIK candidate, so IONS olezarsen (true
# 06-30) was scored against the IONS zilganersen candidate (09-22), an 84-day
# "miss" that wrongly depressed date-exact (0.767 -> 0.818 once fixed).
# ===========================================================================

def test_match_predicates_split_sponsor_and_drug():
    t = _truth(cik="874015", drug="olezarsen", true_pdufa_date="2026-06-30")
    same_sponsor_other_drug = _cand(cik="874015", drug="zilganersen", pdufa="2026-09-22")
    assert _sponsor_matches(t, same_sponsor_other_drug) is True
    assert _drug_matches(t, same_sponsor_other_drug) is False
    # loose candidacy gate still true (sponsor matches) ...
    assert _matches(t, same_sponsor_other_drug) is True
    # ... but the rank is only SPONSOR_ONLY, not SPONSOR_AND_DRUG
    assert _match_rank(t, same_sponsor_other_drug) == bm._RANK_SPONSOR_ONLY
    drug_cand = _cand(cik="874015", drug="olezarsen", pdufa="2026-06-30")
    assert _match_rank(t, drug_cand) == bm._RANK_SPONSOR_AND_DRUG


def test_select_match_prefers_drug_over_first_same_cik():
    """The core fix: with two same-CIK candidates, the olezarsen truth row binds
    to the olezarsen candidate, NOT whichever same-CIK candidate comes first."""
    t = _truth(cik="874015", ticker="IONS", drug="olezarsen", true_pdufa_date="2026-06-30")
    # order deliberately puts the WRONG-drug same-CIK candidate first
    cands = [
        _cand(cik="874015", ticker="IONS", drug="zilganersen", pdufa="2026-09-22"),
        _cand(cik="874015", ticker="IONS", drug="olezarsen", pdufa="2026-06-30"),
    ]
    chosen = _select_match(t, cands, set())
    assert chosen is cands[1]  # the olezarsen candidate, not the first same-CIK one


def test_multidrug_sponsor_both_rows_score_exact():
    """Two IONS truth rows + two IONS candidates -> each binds to its own drug and
    both score date-exact (the pre-fix scorer collapsed them and missed one)."""
    truth = [
        _truth(cik="874015", ticker="IONS", drug="olezarsen",
               true_pdufa_date="2026-06-30", status="pending"),
        _truth(cik="874015", ticker="IONS", drug="zilganersen",
               true_pdufa_date="2026-09-22", status="pending"),
    ]
    cands = [
        _cand(cik="874015", ticker="IONS", drug="zilganersen", pdufa="2026-09-22"),
        _cand(cik="874015", ticker="IONS", drug="olezarsen", pdufa="2026-06-30"),
    ]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.surfaced == 2
    assert r.date_exact == 2           # BOTH exact (pre-fix: 1 exact + 1 84-day miss)
    assert r.date_exact_rate == 1.0


def test_sponsor_only_match_still_binds_when_truth_has_no_drug():
    """A sponsor-only match is legitimate when the truth row carries no drug name
    (the disambiguation guard must not drop these)."""
    truth = [_truth(cik="874015", true_pdufa_date="2026-06-30", status="pending")]  # no drug
    cands = [_cand(cik="874015", drug="olezarsen", pdufa="2026-06-30")]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.surfaced == 1
    assert r.date_exact == 1


def test_sponsor_only_match_binds_when_single_candidate_for_sponsor():
    """If the truth row has a drug but the sponsor has exactly ONE candidate (whose
    drug name didn't parse / differs), we still bind it (unambiguous) rather than
    drop a real surfacing."""
    truth = [_truth(cik="874015", drug="olezarsen", true_pdufa_date="2026-06-30",
                    status="pending")]
    cands = [_cand(cik="874015", drug=None, pdufa="2026-06-30")]  # drug didn't parse
    chosen = _select_match(truth[0], cands, set())
    assert chosen is cands[0]


def test_sponsor_only_refused_when_multiple_candidates_and_no_drug_match():
    """If the truth row HAS a drug and the sponsor has MULTIPLE candidates but NONE
    match that drug, refuse to bind a wrong-drug candidate (the exact mis-score)."""
    t = _truth(cik="874015", drug="olezarsen", true_pdufa_date="2026-06-30")
    cands = [
        _cand(cik="874015", drug="zilganersen", pdufa="2026-09-22"),
        _cand(cik="874015", drug="pelacarsen", pdufa="2026-11-01"),
    ]
    assert _select_match(t, cands, set()) is None  # not surfaced -> no phantom date-miss


def test_used_id_tiebreak_prefers_distinct_candidates_for_same_sponsor():
    """When a drug-less 2-row sponsor has TWO equally-ranked sponsor-only candidates,
    the used-id tiebreak hands each truth row a DISTINCT candidate (so they don't both
    collapse onto the same one) — exercising the unused-first sort key."""
    truth = [
        _truth(cik="500", true_pdufa_date="2026-07-01", status="pending"),
        _truth(cik="500", true_pdufa_date="2026-07-01", status="pending"),
    ]
    cands = [
        _cand(cik="500", drug="x", pdufa="2026-07-01"),
        _cand(cik="500", drug="y", pdufa="2026-07-01"),
    ]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.surfaced == 2
    # both candidates got used (distinct binding), not one candidate twice
    surfaced_recs = [rec for rec in r.per_truth if rec["surfaced"]]
    assert len(surfaced_recs) == 2


def test_single_candidate_drugless_sponsor_binds_honestly():
    """Edge case: ONE candidate, TWO drug-less same-sponsor truth rows. With no drug
    to disambiguate and only one candidate, both rows bind it — the honest 'we found
    this sponsor' answer (this is benign; the real cohort carries drugs on every
    multi-row sponsor so this case doesn't arise in the gate)."""
    truth = [
        _truth(cik="500", true_pdufa_date="2026-07-01", status="pending"),
        _truth(cik="500", true_pdufa_date="2026-07-01", status="pending"),
    ]
    cands = [_cand(cik="500", pdufa="2026-07-01")]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.surfaced == 2  # both bind the single candidate (no drug to split them)


# ===========================================================================
# FIX 2 — corrected false-positive metric (truth-covered in-window slice). The
# bug: 74 emitted dates scored against 37 truth rows counted real out-of-truthset
# catalysts as FPs (-> ~0.66). Corrected: only emitted in-window dates for
# truth-COVERED sponsors that don't bind a truth row are FPs.
# ===========================================================================

def test_fp_excludes_sponsor_absent_from_truthset():
    """An emitted in-window date for a sponsor NOT in the truth set is a real catalyst
    out of scope — NOT a false positive."""
    truth = [_truth(cik="100", ticker="AAAA", drug="alpha",
                    true_pdufa_date="2026-07-01", status="pending")]
    cands = [
        _cand(cik="100", ticker="AAAA", drug="alpha", pdufa="2026-07-01"),   # covered + matches
        _cand(cik="999", ticker="ZZZZ", drug="omega", pdufa="2026-08-01"),   # NOT in truth set
    ]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.fp_eval_pool == 1          # only the covered-sponsor candidate is in the pool
    assert r.fp_contradictions == 0     # and it correctly matched -> no FP
    assert r.false_pos_rate == 0.0
    # the raw proxy still flags the out-of-truthset catalyst (transparency only)
    assert r.false_positives == 1
    assert r.false_pos_rate_raw == round(1 / 2, 3)


def test_fp_counts_phantom_for_covered_sponsor():
    """An emitted in-window date for a COVERED sponsor that binds no truth row (a
    phantom application for that sponsor) IS a false positive."""
    truth = [_truth(cik="100", ticker="AAAA", drug="alpha",
                    true_pdufa_date="2026-07-01", status="pending")]
    cands = [
        _cand(cik="100", ticker="AAAA", drug="alpha", pdufa="2026-07-01"),     # matches truth
        _cand(cik="100", ticker="AAAA", drug="phantom", pdufa="2026-08-15"),   # covered, no truth -> FP
    ]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.fp_eval_pool == 2
    assert r.fp_contradictions == 1
    assert r.false_pos_rate == round(1 / 2, 3)
    assert r.fp_examples and r.fp_examples[0]["drug"] == "phantom"


def test_fp_excludes_out_of_window_dates_for_covered_sponsor():
    """Even for a covered sponsor, an OUT-OF-WINDOW emitted date is not in the FP pool
    (the gate is about the in-window product universe)."""
    truth = [_truth(cik="100", ticker="AAAA", drug="alpha",
                    true_pdufa_date="2026-07-01", status="pending")]
    cands = [
        _cand(cik="100", ticker="AAAA", drug="alpha", pdufa="2026-07-01"),    # in window, matches
        _cand(cik="100", ticker="AAAA", drug="future", pdufa="2027-09-01"),   # out of window
    ]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.fp_eval_pool == 1          # the 2027 date is excluded from the pool
    assert r.fp_contradictions == 0
    assert r.false_pos_rate == 0.0


def test_fp_rate_zero_when_pool_empty():
    """No covered-sponsor in-window emitted dates -> FP rate guarded to 0.0."""
    truth = [_truth(cik="100", ticker="AAAA", true_pdufa_date="2026-07-01", status="pending")]
    cands = [_cand(cik="999", ticker="ZZZZ", pdufa="2026-08-01")]  # uncovered only
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.fp_eval_pool == 0
    assert r.false_pos_rate == 0.0


def test_fp_matches_by_ticker_when_cik_absent_on_candidate():
    """Coverage membership works on ticker too (candidate may lack a CIK)."""
    truth = [_truth(ticker="AAAA", drug="alpha", true_pdufa_date="2026-07-01", status="pending")]
    cands = [
        _cand(ticker="AAAA", drug="alpha", pdufa="2026-07-01"),     # covered by ticker, matches
        _cand(ticker="AAAA", drug="phantom", pdufa="2026-08-15"),   # covered by ticker, phantom -> FP
    ]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.fp_eval_pool == 2
    assert r.fp_contradictions == 1
    assert r.false_pos_rate == round(1 / 2, 3)


def test_fp_dedups_duplicate_8ks_about_one_application():
    """The SAME application disclosed across multiple 8-Ks (identical sponsor + date,
    drug didn't parse so the surrogate is date-keyed) collapses to ONE pool entry —
    the live VRDN '06-30 filed 4x' inflation. The date matches the sponsor's truth
    date, so it is corroborating, NOT a false positive."""
    truth = [_truth(cik="1590750", ticker="VRDN", drug="Veligrotug",
                    true_pdufa_date="2026-06-30", status="pending")]
    # four duplicate candidates: same sponsor + same date, drug unparsed
    cands = [_cand(cik="1590750", ticker="VRDN", drug=None, pdufa="2026-06-30")
             for _ in range(4)]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.fp_eval_pool == 1          # 4 duplicate 8-Ks -> ONE distinct (sponsor,date)
    assert r.fp_contradictions == 0     # 06-30 IS the VRDN truth date -> corroborating
    assert r.false_pos_rate == 0.0


def test_fp_corroborating_date_not_fp_even_when_unbound():
    """An emitted in-window date that MATCHES one of the sponsor's truth dates is not an
    FP, even if drug-disambiguation left it unbound (e.g. the truth row carried a drug
    the candidate couldn't parse). FP requires CONTRADICTION, not merely 'didn't bind'."""
    truth = [_truth(cik="100", ticker="AAAA", drug="alpha",
                    true_pdufa_date="2026-07-01", status="pending")]
    # candidate has the right date but no drug -> may not bind, but date corroborates
    cands = [_cand(cik="100", ticker="AAAA", drug=None, pdufa="2026-07-01")]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.fp_eval_pool == 1
    assert r.fp_contradictions == 0     # date matches sponsor truth date -> not a contradiction
    assert r.false_pos_rate == 0.0


def test_fp_distinct_real_dates_for_multidrug_sponsor_both_corroborate():
    """A multi-drug sponsor with TWO genuine truth dates: two distinct emitted dates that
    each match one of the sponsor's truth dates are BOTH corroborating (IONS olezarsen
    06-30 + zilganersen 09-22)."""
    truth = [
        _truth(cik="874015", ticker="IONS", drug="olezarsen",
               true_pdufa_date="2026-06-30", status="pending"),
        _truth(cik="874015", ticker="IONS", drug="zilganersen",
               true_pdufa_date="2026-09-22", status="pending"),
    ]
    cands = [
        _cand(cik="874015", ticker="IONS", drug="olezarsen", pdufa="2026-06-30"),
        _cand(cik="874015", ticker="IONS", drug="zilganersen", pdufa="2026-09-22"),
    ]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.fp_eval_pool == 2
    assert r.fp_contradictions == 0     # both dates are real IONS truth dates
    assert r.false_pos_rate == 0.0


def test_fp_extra_date_for_multidrug_sponsor_is_contradiction():
    """For the same multi-drug sponsor, an emitted date that is NOT any of the sponsor's
    truth dates is a genuine contradiction (a phantom third catalyst)."""
    truth = [
        _truth(cik="874015", ticker="IONS", drug="olezarsen",
               true_pdufa_date="2026-06-30", status="pending"),
        _truth(cik="874015", ticker="IONS", drug="zilganersen",
               true_pdufa_date="2026-09-22", status="pending"),
    ]
    cands = [
        _cand(cik="874015", ticker="IONS", drug="olezarsen", pdufa="2026-06-30"),
        _cand(cik="874015", ticker="IONS", drug="phantom", pdufa="2026-08-01"),  # not a truth date
    ]
    r = score_against_truth(truth, cands, WINDOW, TODAY)
    assert r.fp_eval_pool == 2
    assert r.fp_contradictions == 1
    assert r.fp_examples[0]["pdufa_date"] == "2026-08-01"
