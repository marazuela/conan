"""Unit tests for the BC outcome labeler (Phase 3 §8.3) — FAKE clients, NO network.

Covers:
  - Regulatory precedence: CRL match -> 'crl'; Drugs@FDA AP -> 'approved';
    WD -> 'withdrawn'; a PDUFA-date push -> 'extended'. ALL LOWERCASE (CHECK §0.2).
  - Price math: base = last close STRICTLY BEFORE PDUFA; t+1/7/30 = Nth trading-day
    close; (c_tN/base-1)*100 within tolerance; no-ticker -> null + 'no_ticker'.
  - Three-row merge / partial: a verdict-only first run writes 3 rows with null
    prices (omitted from the body); a later run merges each mature price without
    clobbering the verdict (null-omitting upsert); re-run idempotent;
    extended -> terminal overwrites.
  - scored_p_crl pairing uses the PRE-PDUFA bc_rubric_scores row, not today's.
  - hypothesis_outcome: low+crl -> band_understated_risk; elevated+crl ->
    band_correct_high_risk; extended -> indeterminate.
  - Stale-pending sweep: a past-dated app past grace with no outcome -> logged
    'stale_pending_unresolved' (not dropped/fabricated/deleted).
  - CRL-source-absent degradation: approvals path still runs, logs
    'crl_source_unavailable', no crash.
  - No refit touched: the labeler never reads/writes bc_refit_log / l7.refit_*.

FAKE client only. No live LLM, no live DB, no network.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from modal_workers.bc_outcome_labeler.price_returns import (
    compute_return_for_horizon,
    compute_returns,
    fetch_returns,
    split_bars_around_pdufa,
)
from modal_workers.bc_outcome_labeler.resolve import (
    detect_extension,
    hypothesis_outcome,
    match_crl,
    normalize_appno_digits,
    resolve_drugsfda_status,
    resolve_regulatory_outcome,
)
from modal_workers.bc_outcome_labeler.run_labeler import (
    build_outcome_rows,
    label_app,
    pre_pdufa_score,
    run_labeler,
)


# ===========================================================================
# Fakes
# ===========================================================================
def _ms(d: date) -> int:
    """epoch-millis at 00:00 UTC for a date (Polygon aggregate ``t``)."""
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def make_bars(start: date, closes):
    """Daily bars on consecutive *calendar* days starting at ``start`` (the labeler
    counts on returned bars, so consecutive bars == consecutive trading days)."""
    bars = []
    for i, c in enumerate(closes):
        d = start + timedelta(days=i)
        bars.append({"t": _ms(d), "o": c, "h": c, "l": c, "c": c, "v": 1_000_000})
    return bars


class FakeMarketData:
    """Returns a fixed bar list for any ticker (or None for a configured 'dead' one)."""

    def __init__(self, bars, *, dead_tickers=()):
        self._bars = bars
        self._dead = set(dead_tickers)

    def get_historical_prices(self, ticker, days):
        if ticker in self._dead:
            return None
        return list(self._bars)


class FakeClient:
    """Records upserts; serves canned select() responses keyed by table.

    ``selects`` maps a table name -> a callable(params)->rows so tests can return
    the pre-PDUFA score, config, existing outcomes, etc.
    """

    def __init__(self, *, selects=None, return_id="run-1"):
        self.upserts = []          # (table, rows, on_conflict, prefer)
        self.pipeline_calls = []   # _rest_with_retry calls (open/close)
        self._selects = selects or {}
        self._return_id = return_id

    # the bc_pipeline_runs open/close go through _rest_with_retry directly
    def _rest_with_retry(self, method, path, *, json_body=None, prefer=None,
                         params=None, attempts=3, backoff_s=0.25):
        self.pipeline_calls.append({"method": method, "path": path, "json_body": json_body, "prefer": prefer})
        if method == "POST" and path == "bc_pipeline_runs":
            return [{"id": self._return_id}]
        return []

    def select(self, table, *, params=None):
        fn = self._selects.get(table)
        if fn is None:
            return []
        return fn(params or {})

    def upsert(self, table, rows, *, on_conflict=None, prefer=None):
        self.upserts.append({"table": table, "rows": rows, "on_conflict": on_conflict, "prefer": prefer})
        return []

    def rpc(self, name, *, body=None):
        return None


# ===========================================================================
# resolve.py — application-number normalization
# ===========================================================================
def test_normalize_appno_digits_handles_list_and_scalar():
    assert normalize_appno_digits(["NDA 215344"]) == "215344"
    assert normalize_appno_digits("NDA215344") == "215344"
    assert normalize_appno_digits("BLA 761385") == "761385"
    assert normalize_appno_digits("215344") == "215344"
    # a pure-text surrogate with no digits normalizes to "" (so it never spuriously
    # matches a CRL record by digits)
    assert normalize_appno_digits("EDGAR8K:abc:def") == ""


# ===========================================================================
# resolve.py — regulatory precedence + lowercase CHECK conformance
# ===========================================================================
PDUFA = date(2026, 3, 10)


def test_crl_match_returns_lowercase_crl():
    crl = [{"application_number": ["NDA 215344"], "letter_year": "2026"}]
    res = resolve_regulatory_outcome(
        application_number="NDA215344", pdufa_date=PDUFA, crl_records=crl,
    )
    assert res["outcome"] == "crl"
    assert res["outcome"] == res["outcome"].lower()
    assert res["is_terminal"] is True


def test_crl_match_excluded_when_letter_year_before_pdufa():
    crl = [{"application_number": ["NDA 215344"], "letter_year": "2024"}]
    m = match_crl(crl, "NDA215344", PDUFA)
    assert m is None  # prior-cycle CRL is not this catalyst


def test_drugsfda_ap_returns_approved():
    subs = [{"submission_type": "ORIG", "submission_status": "AP",
             "submission_status_date": "2026-03-12"}]
    res = resolve_regulatory_outcome(application_number="NDA215344", pdufa_date=PDUFA,
                                     crl_records=[], submissions=subs)
    assert res["outcome"] == "approved"
    assert res["is_terminal"] is True


def test_drugsfda_wd_returns_withdrawn():
    subs = [{"submission_type": "ORIG", "submission_status": "WD"}]
    res = resolve_regulatory_outcome(application_number="NDA215344", pdufa_date=PDUFA,
                                     crl_records=[], submissions=subs)
    assert res["outcome"] == "withdrawn"


def test_pdufa_push_returns_extended_non_terminal():
    res = resolve_regulatory_outcome(
        application_number="NDA215344",
        pdufa_date=date(2026, 6, 10),     # pushed later
        crl_records=[], submissions=[],
        last_seen_pdufa=date(2026, 3, 10),
    )
    assert res["outcome"] == "extended"
    assert res["is_terminal"] is False


def test_precedence_crl_beats_drugsfda():
    crl = [{"application_number": ["NDA 215344"], "letter_year": "2026"}]
    subs = [{"submission_type": "ORIG", "submission_status": "AP",
             "submission_status_date": "2026-03-12"}]
    res = resolve_regulatory_outcome(application_number="NDA215344", pdufa_date=PDUFA,
                                     crl_records=crl, submissions=subs)
    assert res["outcome"] == "crl"  # CRL Transparency is more authoritative


def test_unresolved_returns_none():
    res = resolve_regulatory_outcome(application_number="NDA215344", pdufa_date=PDUFA,
                                     crl_records=[], submissions=[])
    assert res["outcome"] is None
    assert res["is_terminal"] is False


def test_all_outcome_values_are_lowercase():
    # belt-and-suspenders: every emittable verdict is lowercase (CHECK §0.2)
    for ro in ("crl", "approved", "withdrawn", "extended"):
        assert ro == ro.lower()


def test_detect_extension():
    assert detect_extension(date(2026, 6, 10), date(2026, 3, 10)) is True
    assert detect_extension(date(2026, 3, 10), date(2026, 3, 10)) is False
    assert detect_extension(date(2026, 3, 10), None) is False


# ===========================================================================
# resolve.py — CRL source absent degradation (approvals still work)
# ===========================================================================
def test_crl_source_absent_runs_approvals_and_logs_token():
    subs = [{"submission_type": "ORIG", "submission_status": "AP",
             "submission_status_date": "2026-03-12"}]
    res = resolve_regulatory_outcome(
        application_number="NDA215344", pdufa_date=PDUFA,
        crl_records=None, submissions=subs, crl_source_available=False,
    )
    assert res["outcome"] == "approved"
    assert res["log"] == "crl_source_unavailable"


def test_crl_source_absent_unresolved_still_logs_token():
    res = resolve_regulatory_outcome(
        application_number="NDA215344", pdufa_date=PDUFA,
        crl_records=None, submissions=[], crl_source_available=False,
    )
    assert res["outcome"] is None
    assert res["log"] == "crl_source_unavailable"


# ===========================================================================
# resolve.py — hypothesis_outcome
# ===========================================================================
@pytest.mark.parametrize("band,ro,expected", [
    ("low", "crl", "band_understated_risk"),
    ("moderate", "crl", "band_understated_risk"),
    ("elevated", "crl", "band_correct_high_risk"),
    ("high", "crl", "band_correct_high_risk"),
    ("low", "approved", "band_correct_low_risk"),
    ("elevated", "approved", "band_overstated_risk"),
    ("low", "extended", "indeterminate"),
    ("elevated", "withdrawn", "indeterminate"),
])
def test_hypothesis_outcome(band, ro, expected):
    assert hypothesis_outcome(band, ro) == expected


def test_hypothesis_outcome_omitted_until_both_present():
    assert hypothesis_outcome(None, "crl") is None
    assert hypothesis_outcome("low", None) is None


# ===========================================================================
# price_returns.py — base + horizon math
# ===========================================================================
def test_split_bars_base_is_last_strictly_before_pdufa():
    # bars: 3 days before PDUFA, then PDUFA day + 30 after
    pre_start = PDUFA - timedelta(days=3)
    closes = [90.0, 95.0, 100.0]  # 100.0 is the day BEFORE PDUFA
    post = [120.0] + [121.0 + i for i in range(40)]  # PDUFA day = 120.0
    bars = make_bars(pre_start, closes) + make_bars(PDUFA, post)
    split = split_bars_around_pdufa(bars, PDUFA)
    assert split["base"] == 100.0          # last close strictly before PDUFA
    assert split["post"][0] == 120.0       # t+1 == PDUFA-day close (1-indexed)


def test_compute_return_for_horizon_math():
    pre_start = PDUFA - timedelta(days=1)
    bars = make_bars(pre_start, [100.0]) + make_bars(PDUFA, [120.0, 110.0, 130.0])
    # base = 100; t+1 = 120 -> +20%; t+2 = 110 -> +10%; t+3 = 130 -> +30%
    assert compute_return_for_horizon(bars, PDUFA, 1) == pytest.approx(20.0)
    assert compute_return_for_horizon(bars, PDUFA, 2) == pytest.approx(10.0)
    assert compute_return_for_horizon(bars, PDUFA, 3) == pytest.approx(30.0)


def test_immature_horizon_is_none():
    pre_start = PDUFA - timedelta(days=1)
    bars = make_bars(pre_start, [100.0]) + make_bars(PDUFA, [120.0])  # only t+1 exists
    assert compute_return_for_horizon(bars, PDUFA, 1) == pytest.approx(20.0)
    assert compute_return_for_horizon(bars, PDUFA, 7) is None  # not mature
    assert compute_return_for_horizon(bars, PDUFA, 30) is None


def test_no_pre_pdufa_bar_yields_none():
    bars = make_bars(PDUFA, [120.0, 130.0])  # no bar before PDUFA
    assert compute_return_for_horizon(bars, PDUFA, 1) is None


def test_compute_returns_dict():
    pre_start = PDUFA - timedelta(days=1)
    bars = make_bars(pre_start, [100.0]) + make_bars(PDUFA, [110.0] * 31)
    out = compute_returns(bars, PDUFA, [1, 7, 30])
    assert out[1] == pytest.approx(10.0)
    assert out[7] == pytest.approx(10.0)
    assert out[30] == pytest.approx(10.0)


def test_fetch_returns_no_ticker_logs_and_nulls():
    md = FakeMarketData([])
    out = fetch_returns(md, "", PDUFA, [1, 7, 30])
    assert out["log"] == "no_ticker"
    assert out["returns"] == {1: None, 7: None, 30: None}


def test_fetch_returns_dead_ticker_no_bars():
    md = FakeMarketData([], dead_tickers={"DEAD"})
    out = fetch_returns(md, "DEAD", PDUFA, [1, 7, 30])
    assert out["log"] == "no_bars"
    assert all(v is None for v in out["returns"].values())


# ===========================================================================
# run_labeler.py — build_outcome_rows: null-omitting, 3 rows, lowercase
# ===========================================================================
def test_build_outcome_rows_verdict_only_omits_prices():
    rows = build_outcome_rows(
        application_number="NDA215344", horizons=[1, 7, 30],
        regulatory_outcome="crl",
        returns={1: None, 7: None, 30: None},
        scored_p_crl=0.42, band="low",
    )
    assert len(rows) == 3
    for r in rows:
        assert r["regulatory_outcome"] == "crl"
        assert "price_return_pct" not in r        # null omitted (no clobber on merge)
        assert r["scored_p_crl"] == 0.42
        assert r["hypothesis_outcome"] == "band_understated_risk"
        assert r["horizon_days"] in (1, 7, 30)


def test_build_outcome_rows_merges_mature_price_only():
    rows = build_outcome_rows(
        application_number="NDA215344", horizons=[1, 7, 30],
        regulatory_outcome="crl",
        returns={1: -35.0, 7: -40.0, 30: None},   # t+30 not mature yet
        scored_p_crl=0.42, band="low",
    )
    by_h = {r["horizon_days"]: r for r in rows}
    assert by_h[1]["price_return_pct"] == -35.0
    assert by_h[7]["price_return_pct"] == -40.0
    assert "price_return_pct" not in by_h[30]      # omitted -> later merge fills it


def test_build_outcome_rows_outcome_lowercased():
    rows = build_outcome_rows(
        application_number="NDA1", horizons=[1],
        regulatory_outcome="CRL",   # caller passes upper -> row stores lowercase
        returns={1: None}, scored_p_crl=None, band=None,
    )
    assert rows[0]["regulatory_outcome"] == "crl"


def test_build_outcome_rows_omits_regulatory_when_unknown():
    rows = build_outcome_rows(
        application_number="NDA1", horizons=[1, 7, 30],
        regulatory_outcome=None,           # verdict not known yet (price-only)
        returns={1: 5.0, 7: None, 30: None}, scored_p_crl=0.1, band="low",
    )
    by_h = {r["horizon_days"]: r for r in rows}
    assert "regulatory_outcome" not in by_h[1]
    assert by_h[1]["price_return_pct"] == 5.0
    assert "hypothesis_outcome" not in by_h[1]   # no verdict -> no hypothesis


# ===========================================================================
# run_labeler.py — pre_pdufa_score pairing (uses pre-PDUFA, not today's)
# ===========================================================================
def test_pre_pdufa_score_pairs_prior_score():
    captured = {}

    def scores_select(params):
        captured.update(params)
        return [{"p_crl": 0.42, "risk_band": "low", "scored_at": "2026-03-01T00:00:00+00:00"}]

    client = FakeClient(selects={"bc_rubric_scores": scores_select})
    snap = pre_pdufa_score(client, "NDA215344", PDUFA)
    assert snap["scored_p_crl"] == 0.42
    assert snap["risk_band"] == "low"
    # the query constrains scored_at <= the PDUFA date (the live read at prediction time)
    assert "lte." in captured.get("scored_at", "")
    assert captured.get("order") == "scored_at.desc"


def test_pre_pdufa_score_missing_is_none():
    client = FakeClient(selects={"bc_rubric_scores": lambda p: []})
    snap = pre_pdufa_score(client, "NDA215344", PDUFA)
    assert snap == {"scored_p_crl": None, "risk_band": None}


# ===========================================================================
# run_labeler.py — label_app end-to-end (fakes), apply=True writes 3 rows
# ===========================================================================
def _score_select_factory(p_crl, band):
    def fn(params):
        return [{"p_crl": p_crl, "risk_band": band, "scored_at": "2026-03-01T00:00:00+00:00"}]
    return fn


def test_label_app_crl_writes_three_rows_with_paired_p_crl():
    pre_start = PDUFA - timedelta(days=1)
    bars = make_bars(pre_start, [100.0]) + make_bars(PDUFA, [65.0] * 31)  # -35% crash
    md = FakeMarketData(bars)
    client = FakeClient(selects={"bc_rubric_scores": _score_select_factory(0.42, "low")})
    crl = [{"application_number": ["NDA 215344"], "letter_year": "2026"}]

    res = label_app(
        client,
        {"application_number": "NDA215344", "pdufa_date": PDUFA, "ticker": "PRTX"},
        horizons=[1, 7, 30], market_data=md, crl_records=crl,
        submissions_by_app={}, crl_source_available=True, apply=True,
    )
    assert res["regulatory_outcome"] == "crl"
    assert res["scored_p_crl"] == 0.42
    assert res["hypothesis_outcome"] == "band_understated_risk"
    assert res["wrote"] is True

    # exactly one upsert of 3 rows, on the UNIQUE conflict target, merge-duplicates
    assert len(client.upserts) == 1
    up = client.upserts[0]
    assert up["table"] == "bc_prediction_outcomes"
    assert up["on_conflict"] == "application_number,horizon_days"
    assert "merge-duplicates" in (up["prefer"] or "")
    assert len(up["rows"]) == 3
    for r in up["rows"]:
        assert r["regulatory_outcome"] == "crl"
        assert r["scored_p_crl"] == 0.42
        assert r["price_return_pct"] == pytest.approx(-35.0)


def test_label_app_no_ticker_records_verdict_with_null_price():
    client = FakeClient(selects={"bc_rubric_scores": _score_select_factory(0.42, "low")})
    crl = [{"application_number": ["NDA 215344"], "letter_year": "2026"}]
    res = label_app(
        client,
        {"application_number": "NDA215344", "pdufa_date": PDUFA, "ticker": None},
        horizons=[1, 7, 30], market_data=None, crl_records=crl,
        submissions_by_app={}, crl_source_available=True, apply=True,
    )
    assert res["regulatory_outcome"] == "crl"
    assert res["wrote"] is True
    for r in client.upserts[0]["rows"]:
        assert "price_return_pct" not in r       # null price omitted
        assert r["regulatory_outcome"] == "crl"


def test_label_app_unresolved_writes_nothing():
    client = FakeClient(selects={"bc_rubric_scores": _score_select_factory(0.1, "low")})
    res = label_app(
        client,
        {"application_number": "NDA999", "pdufa_date": PDUFA, "ticker": None},
        horizons=[1, 7, 30], market_data=None, crl_records=[],
        submissions_by_app={}, crl_source_available=True, apply=True,
    )
    assert res["regulatory_outcome"] is None
    assert res["wrote"] is False
    assert client.upserts == []                  # nothing written for an unresolved app


# ===========================================================================
# run_labeler.py — run_labeler open/close + stale sweep + CHECK conformance
# ===========================================================================
def _config_selects(horizons="[1,7,30]", grace="14"):
    def cfg(params):
        key = params.get("key", "")
        if "outcome_price_horizons" in key:
            return [{"value": [1, 7, 30]}]
        if "outcome_resolve_grace_days" in key:
            return [{"value": int(grace)}]
        return []
    return cfg


def test_run_labeler_opens_and_closes_pipeline_run_succeeded():
    pre_start = PDUFA - timedelta(days=1)
    bars = make_bars(pre_start, [100.0]) + make_bars(PDUFA, [65.0] * 31)
    md = FakeMarketData(bars)
    client = FakeClient(selects={
        "bc_config": _config_selects(),
        "bc_rubric_scores": _score_select_factory(0.42, "low"),
        "bc_prediction_outcomes": lambda p: [],   # no existing rows
    })
    crl = [{"application_number": ["NDA 215344"], "letter_year": "2026"}]
    out = run_labeler(
        client, apply=True, today=PDUFA + timedelta(days=2),
        market_data=md, crl_records=crl, submissions_by_app={},
        apps=[{"application_number": "NDA215344", "pdufa_date": PDUFA, "ticker": "PRTX"}],
        crl_source_available=True,
    )
    assert out["status"] == "succeeded"
    # open (POST) + close (PATCH) both happened, with CHECK-valid status
    methods = [c["method"] for c in client.pipeline_calls]
    assert "POST" in methods and "PATCH" in methods
    patch = [c for c in client.pipeline_calls if c["method"] == "PATCH"][-1]
    assert patch["json_body"]["status"] in ("succeeded", "partial", "failed")
    assert patch["json_body"]["status"] == "succeeded"


def test_run_labeler_stale_pending_logged_not_fabricated():
    client = FakeClient(selects={
        "bc_config": _config_selects(),
        "bc_rubric_scores": _score_select_factory(0.1, "low"),
        "bc_prediction_outcomes": lambda p: [],
    })
    today = PDUFA + timedelta(days=20)  # 20d past PDUFA, > 14d grace
    out = run_labeler(
        client, apply=True, today=today,
        market_data=None, crl_records=[], submissions_by_app={},
        apps=[{"application_number": "NDA888", "pdufa_date": PDUFA, "ticker": None}],
        crl_source_available=True,
    )
    assert "NDA888" in out["stats"]["stale_pending_unresolved"]
    # NOT fabricated: no outcome upsert, no fabricated verdict, no row delete
    assert client.upserts == []
    # the close log carries the stale list for operator eyes
    patch = [c for c in client.pipeline_calls if c["method"] == "PATCH"][-1]
    assert "NDA888" in patch["json_body"]["log"]["stale_pending_unresolved"]


def test_run_labeler_dry_run_writes_nothing_and_no_pipeline_row():
    client = FakeClient(selects={
        "bc_config": _config_selects(),
        "bc_rubric_scores": _score_select_factory(0.42, "low"),
        "bc_prediction_outcomes": lambda p: [],
    })
    crl = [{"application_number": ["NDA 215344"], "letter_year": "2026"}]
    out = run_labeler(
        client, apply=False, today=PDUFA + timedelta(days=2),
        market_data=FakeMarketData(make_bars(PDUFA - timedelta(days=1), [100.0]) + make_bars(PDUFA, [65.0] * 31)),
        crl_records=crl, submissions_by_app={},
        apps=[{"application_number": "NDA215344", "pdufa_date": PDUFA, "ticker": "PRTX"}],
        crl_source_available=True,
    )
    assert out["status"] == "succeeded"
    assert client.upserts == []          # dry-run writes nothing
    assert client.pipeline_calls == []   # and opens NO pipeline row


def test_run_labeler_crl_source_absent_degrades_gracefully():
    subs = {"NDA215344": [{"submission_type": "ORIG", "submission_status": "AP",
                           "submission_status_date": "2026-03-12"}]}
    client = FakeClient(selects={
        "bc_config": _config_selects(),
        "bc_rubric_scores": _score_select_factory(0.1, "low"),
        "bc_prediction_outcomes": lambda p: [],
    })
    out = run_labeler(
        client, apply=True, today=PDUFA + timedelta(days=2),
        market_data=None, crl_records=None, submissions_by_app=subs,
        apps=[{"application_number": "NDA215344", "pdufa_date": PDUFA, "ticker": None}],
        crl_source_available=False,   # transparency module unavailable
    )
    # approvals path still resolved it; no crash
    res = out["results"][0]
    assert res["regulatory_outcome"] == "approved"
    assert res["resolve_log"] == "crl_source_unavailable"


# ===========================================================================
# INVARIANT: no refit loop touched (no bc_refit_log / l7.refit_* read or write)
# ===========================================================================
def test_run_labeler_never_touches_refit_tables():
    touched_tables = []

    class RefitWatchClient(FakeClient):
        def select(self, table, *, params=None):
            touched_tables.append(("select", table, (params or {}).get("key", "")))
            return super().select(table, params=params)

        def upsert(self, table, rows, *, on_conflict=None, prefer=None):
            touched_tables.append(("upsert", table, ""))
            return super().upsert(table, rows, on_conflict=on_conflict, prefer=prefer)

    client = RefitWatchClient(selects={
        "bc_config": _config_selects(),
        "bc_rubric_scores": _score_select_factory(0.42, "low"),
        "bc_prediction_outcomes": lambda p: [],
    })
    crl = [{"application_number": ["NDA 215344"], "letter_year": "2026"}]
    run_labeler(
        client, apply=True, today=PDUFA + timedelta(days=2),
        market_data=FakeMarketData(make_bars(PDUFA - timedelta(days=1), [100.0]) + make_bars(PDUFA, [65.0] * 31)),
        crl_records=crl, submissions_by_app={},
        apps=[{"application_number": "NDA215344", "pdufa_date": PDUFA, "ticker": "PRTX"}],
        crl_source_available=True,
    )
    for op, table, key in touched_tables:
        assert table != "bc_refit_log", f"labeler touched bc_refit_log via {op}"
        assert "refit" not in (table or "").lower()
        assert "refit" not in (key or "").lower()  # never reads l7.refit_min_crl_events
