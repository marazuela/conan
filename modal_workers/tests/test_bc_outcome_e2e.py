"""Integration test (Phase 3 §8.5) — resolved catalyst -> bc_prediction_outcomes triple.

The Phase-3 exit-gate proof "resolved catalysts land in bc_prediction_outcomes":
seed a resolved app (PDUFA = today-10, a fixture CRL match, a pre-PDUFA
bc_rubric_scores row, fixture mature price bars) -> run run_labeler(apply=True) ->
assert THREE bc_prediction_outcomes rows (regulatory_outcome='crl' lowercase,
scored_p_crl from the PRE-PDUFA score, price_return_pct for the mature horizons,
hypothesis_outcome set), idempotent on a second run, and a
bc_pipeline_runs(pipeline_name='bc_outcome_labeler') row closed 'succeeded'.

FAKE clients throughout — NO network, NO live DB, NO live LLM. A small in-memory
"DB" emulates the merge-upsert on (application_number, horizon_days) so the
idempotency + null-omitting-merge contract is exercised end-to-end.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from modal_workers.bc_outcome_labeler.run_labeler import run_labeler


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _bars(start: date, closes):
    return [
        {"t": _ms(start + timedelta(days=i)), "o": c, "h": c, "l": c, "c": c, "v": 1_000_000}
        for i, c in enumerate(closes)
    ]


class FakeMarketData:
    def __init__(self, bars):
        self._bars = bars

    def get_historical_prices(self, ticker, days):
        return list(self._bars)


class InMemoryDB:
    """Emulates the subset of the REST client the labeler uses, with a real
    merge-upsert on bc_prediction_outcomes keyed by (application_number, horizon_days)."""

    def __init__(self, *, pre_pdufa_p_crl, pre_pdufa_band):
        self.outcomes: dict[tuple, dict] = {}     # (appno, horizon) -> row
        self.pipeline_runs: list[dict] = []
        self._run_seq = 0
        self._p = pre_pdufa_p_crl
        self._band = pre_pdufa_band

    # pipeline_runs open/close
    def _rest_with_retry(self, method, path, *, json_body=None, prefer=None,
                         params=None, attempts=3, backoff_s=0.25):
        if method == "POST" and path == "bc_pipeline_runs":
            self._run_seq += 1
            rid = f"run-{self._run_seq}"
            self.pipeline_runs.append({"id": rid, "status": "running", **(json_body[0] if json_body else {})})
            return [{"id": rid}]
        if method == "PATCH" and path.startswith("bc_pipeline_runs?id=eq."):
            rid = path.split("eq.")[1]
            for r in self.pipeline_runs:
                if r["id"] == rid:
                    r.update(json_body or {})
            return []
        return []

    def select(self, table, *, params=None):
        params = params or {}
        if table == "bc_config":
            key = params.get("key", "")
            if "outcome_price_horizons" in key:
                return [{"value": [1, 7, 30]}]
            if "outcome_resolve_grace_days" in key:
                return [{"value": 14}]
            return []
        if table == "bc_rubric_scores":
            return [{"p_crl": self._p, "risk_band": self._band, "scored_at": "2026-03-01T00:00:00+00:00"}]
        if table == "bc_prediction_outcomes":
            # the labeler reads existing appnos for the stale sweep
            return [{"application_number": a} for (a, _h) in self.outcomes.keys()]
        if table == "bc_company_tradeable":
            return [{"ticker": "PRTX", "snapshot_date": "2026-06-01"}]
        if table == "bc_candidates":
            return []  # apps are injected directly in this test
        return []

    def upsert(self, table, rows, *, on_conflict=None, prefer=None):
        assert table == "bc_prediction_outcomes"
        assert on_conflict == "application_number,horizon_days"
        assert "merge-duplicates" in (prefer or "")
        for r in rows:
            key = (r["application_number"], r["horizon_days"])
            existing = self.outcomes.get(key, {"application_number": r["application_number"], "horizon_days": r["horizon_days"]})
            # merge-duplicates + null-omitting body: only the present keys are written,
            # so an existing set value is never clobbered by an omitted (null) field.
            existing.update(r)
            self.outcomes[key] = existing
        return []

    def rpc(self, name, *, body=None):
        return None


TODAY = date(2026, 3, 20)
PDUFA = TODAY - timedelta(days=10)   # resolved 10 days ago


def _seed_app():
    return {"application_number": "BLA761333", "pdufa_date": PDUFA, "ticker": "PRTX"}


def _crl_match():
    return [{"application_number": ["BLA 761333"], "letter_year": "2026"}]


def _mature_bars():
    # base = 100 (day before PDUFA); then 31 post-PDUFA days at 65 (a -35% CRL crash)
    return _bars(PDUFA - timedelta(days=1), [100.0]) + _bars(PDUFA, [65.0] * 31)


def test_resolved_catalyst_writes_three_outcome_rows():
    db = InMemoryDB(pre_pdufa_p_crl=0.42, pre_pdufa_band="low")
    out = run_labeler(
        db, apply=True, today=TODAY,
        market_data=FakeMarketData(_mature_bars()),
        crl_records=_crl_match(), submissions_by_app={},
        apps=[_seed_app()], crl_source_available=True,
    )
    assert out["status"] == "succeeded"

    # exactly 3 rows (h=1,7,30), all crl, all paired to the PRE-PDUFA p_crl
    rows = db.outcomes
    assert len(rows) == 3
    for h in (1, 7, 30):
        r = rows[("BLA761333", h)]
        assert r["regulatory_outcome"] == "crl"          # lowercase CHECK
        assert r["scored_p_crl"] == 0.42                 # the pre-PDUFA p_crl, not today's
        assert r["price_return_pct"] == pytest.approx(-35.0)
        assert r["hypothesis_outcome"] == "band_understated_risk"  # low band + crl = the costly miss

    # the pipeline run closed 'succeeded'
    run = db.pipeline_runs[-1]
    assert run["status"] == "succeeded"
    assert run["pipeline_name"] == "bc_outcome_labeler"


def test_resolved_catalyst_is_idempotent_on_rerun():
    db = InMemoryDB(pre_pdufa_p_crl=0.42, pre_pdufa_band="low")
    args = dict(
        apply=True, today=TODAY, market_data=FakeMarketData(_mature_bars()),
        crl_records=_crl_match(), submissions_by_app={}, apps=[_seed_app()],
        crl_source_available=True,
    )
    run_labeler(db, **args)
    snapshot = {k: dict(v) for k, v in db.outcomes.items()}
    run_labeler(db, **args)   # re-run
    # still exactly 3 rows, identical values (merge-upsert idempotent on the UNIQUE)
    assert len(db.outcomes) == 3
    for k, v in db.outcomes.items():
        assert v == snapshot[k]


def test_verdict_first_then_price_merges_without_clobber():
    """A first run with the verdict but IMMATURE prices writes 3 verdict rows with
    null prices (omitted); a later run with matured prices MERGES the price in
    without clobbering the verdict (the null-omitting upsert contract, §5.3)."""
    db = InMemoryDB(pre_pdufa_p_crl=0.42, pre_pdufa_band="elevated")

    # run 1: only t+1 bar exists (t+7/t+30 immature)
    immature = _bars(PDUFA - timedelta(days=1), [100.0]) + _bars(PDUFA, [80.0])  # only t+1
    run_labeler(
        db, apply=True, today=PDUFA + timedelta(days=1),
        market_data=FakeMarketData(immature),
        crl_records=_crl_match(), submissions_by_app={}, apps=[_seed_app()],
        crl_source_available=True,
    )
    r1 = db.outcomes[("BLA761333", 1)]
    r7 = db.outcomes[("BLA761333", 7)]
    assert r1["regulatory_outcome"] == "crl"
    assert r1["price_return_pct"] == pytest.approx(-20.0)
    assert r7["regulatory_outcome"] == "crl"
    assert "price_return_pct" not in r7   # immature -> omitted, not null-written

    # run 2 (later): all horizons mature now
    run_labeler(
        db, apply=True, today=TODAY,
        market_data=FakeMarketData(_mature_bars()),
        crl_records=_crl_match(), submissions_by_app={}, apps=[_seed_app()],
        crl_source_available=True,
    )
    r7b = db.outcomes[("BLA761333", 7)]
    assert r7b["regulatory_outcome"] == "crl"          # verdict NOT clobbered
    assert r7b["price_return_pct"] == pytest.approx(-35.0)  # price merged in
    # the t+1 row's already-set price is unchanged by the merge
    assert db.outcomes[("BLA761333", 1)]["regulatory_outcome"] == "crl"


def test_extended_then_terminal_overwrites_verdict():
    """A PDUFA push first logs 'extended'; when the new date resolves to a terminal
    verdict, the non-null value overwrites 'extended' on all three rows (§5.5)."""
    db = InMemoryDB(pre_pdufa_p_crl=0.30, pre_pdufa_band="elevated")
    app = {"application_number": "BLA761333", "pdufa_date": PDUFA, "ticker": "PRTX",
           "last_seen_pdufa": PDUFA - timedelta(days=60)}

    # run 1: PDUFA pushed later vs last-seen, no terminal verdict -> extended
    run_labeler(
        db, apply=True, today=TODAY,
        market_data=FakeMarketData(_mature_bars()),
        crl_records=[], submissions_by_app={}, apps=[app],
        crl_source_available=True,
    )
    assert db.outcomes[("BLA761333", 1)]["regulatory_outcome"] == "extended"

    # run 2: a CRL now resolves it -> overwrites 'extended' with the terminal 'crl'
    run_labeler(
        db, apply=True, today=TODAY,
        market_data=FakeMarketData(_mature_bars()),
        crl_records=_crl_match(), submissions_by_app={}, apps=[app],
        crl_source_available=True,
    )
    for h in (1, 7, 30):
        assert db.outcomes[("BLA761333", h)]["regulatory_outcome"] == "crl"
