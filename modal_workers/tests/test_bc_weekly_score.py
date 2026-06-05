"""Unit tests for the bc_weekly_score worker (no DB, no network).

A capturing FakeClient records the upsert POSTs so we can assert the write
contract (on_conflict targets, scorer_name literal, model_version->scorer_version
rename, float casts, scored_at idempotency stamp, features_id linkage, pdufa_date
carry-forward, NULL-not-False). The Drugs@FDA + EFTS sources are injected so the
worker runs fully offline. Covers Phase 1 §7.5–§7.12.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from modal_workers.bc_score import run_weekly as rw
from modal_workers.shared.feature_builder_pit import DrugsFDA


# --------------------------------------------------------------------------- #
# capturing fake client
# --------------------------------------------------------------------------- #
class CaptureClient:
    """Records POSTs (the write contract) and serves GET rows for the universe
    read + the warning-letter query. Applies the top-level ``eq.``/``in.``/
    ``not.is.null`` filters the worker uses; ignores select/order."""

    def __init__(self, universe_rows, *, warning_rows=None, refresh_raises=False):
        self._universe = universe_rows
        self._warning = warning_rows or []
        self.posts = []          # (path, json_body, prefer)
        self.rpc_calls = []
        self.refresh_raises = refresh_raises
        self._next_feature_id = 1000

    # --- reads ---
    def _rest(self, method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "bc_application_features":
            return self._filter(self._universe, params)
        if method == "GET" and path == "fda_warning_letters":
            return self._filter(self._warning, params)
        if method == "POST" and path == "rpc/bc_refresh_candidates":
            self.rpc_calls.append(path)
            if self.refresh_raises:
                raise RuntimeError("refresh boom")
            return None
        # POSTs to tables go through _rest_with_retry; if any lands here, record it.
        if method == "POST":
            return self._record_post(path, json_body, prefer)
        return []

    def _rest_with_retry(self, method, path, *, params=None, json_body=None, prefer=None, attempts=3, backoff_s=0.25):
        if method == "POST":
            return self._record_post(path, json_body, prefer)
        return self._rest(method, path, params=params, json_body=json_body, prefer=prefer)

    def _record_post(self, path, json_body, prefer):
        self.posts.append((path, json_body, prefer))
        if path.startswith("bc_pipeline_runs") and "return=representation" in (prefer or ""):
            return [{"id": "run-1"}]
        if path.startswith("bc_application_features") and "return=representation" in (prefer or ""):
            fid = f"feat-{self._next_feature_id}"
            self._next_feature_id += 1
            return [{"id": fid}]
        return None

    @staticmethod
    def _filter(rows, params):
        out = []
        for r in rows:
            keep = True
            for k, v in (params or {}).items():
                if k in ("select", "order", "limit", "offset"):
                    continue
                if not isinstance(v, str):
                    continue
                if v == "not.is.null":
                    if r.get(k) is None:
                        keep = False
                        break
                elif v.startswith("eq.") and str(r.get(k)) != v[3:]:
                    keep = False
                    break
                elif v.startswith("in.("):
                    allowed = v[4:-1].split(",")
                    if str(r.get(k)) not in allowed:
                        keep = False
                        break
            if keep:
                out.append(r)
        return out


class FakeDrugsFDA:
    def __init__(self, by_appno=None, by_sponsor=None):
        self._by_appno = by_appno or {}
        self._by_sponsor = by_sponsor or {}

    def application(self, appno):
        from modal_workers.shared.feature_builder_pit import _norm
        return self._by_appno.get(_norm(appno))

    def sponsor_applications(self, sponsor_name):
        from modal_workers.shared.feature_builder_pit import _firm_norm
        return self._by_sponsor.get(_firm_norm(sponsor_name), [])


_NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)


def _universe_row(appno, cik, sponsor, appl_type, pdufa, **kw):
    base = {
        "id": f"uf-{cik}", "sponsor_cik": cik, "sponsor_name": sponsor,
        "application_number": appno, "appl_type": appl_type, "pdufa_date": pdufa,
        "is_biosimilar_bla": False, "has_bt": None, "has_ft": None, "has_aa": None,
        "submission_date": None, "snapshot_date": "2026-06-04",
        "built_at": "2026-06-04T12:00:00+00:00",
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# §7.5 — write-contract
# --------------------------------------------------------------------------- #
def test_write_contract_apply():
    rows = [
        _universe_row("NDA021937", "882095", "GILEAD SCIENCES, INC.  (GILD)", "NDA",
                      "2026-08-27", has_aa=True),
        _universe_row("EDGAR8K:78003:d20260817", "78003", "PFIZER INC  (PFE)", "BLA",
                      "2026-08-17"),
    ]
    dfda = FakeDrugsFDA(by_appno={"NDA021937": {
        "application_number": "NDA021937",
        "submissions": [{"submission_type": "ORIG", "submission_number": "1",
                         "review_priority": "PRIORITY", "submission_class_code": "TYPE 1",
                         "submission_status_date": "20260401"}],
    }})
    client = CaptureClient(rows)
    res = rw.run_weekly(client, apply=True, dfda=dfda,
                        eight_k_counter=lambda c, r: 1, user_agent="ua x@y.com", _now=_NOW)

    assert res["status"] == "succeeded"
    feat_posts = [p for p in client.posts if p[0].startswith("bc_application_features")]
    score_posts = [p for p in client.posts if p[0].startswith("bc_rubric_scores")]
    assert len(feat_posts) == 2 and len(score_posts) == 2

    # on_conflict targets (the live composite UNIQUEs)
    assert all("on_conflict=sponsor_cik,application_number,snapshot_date" in p[0] for p in feat_posts)
    assert all("on_conflict=application_number,scored_at,scorer_name" in p[0] for p in score_posts)
    # features upsert returns representation (needed for features_id FK)
    assert all("return=representation" in p[2] for p in feat_posts)

    # the score body for GILEAD
    gild_score = next(p[1][0] for p in score_posts if p[1][0]["application_number"] == "NDA021937")
    assert gild_score["scorer_name"] == "M14_adjusted"               # matches matview literal
    assert gild_score["scorer_version"] == rw.NDA_MODEL_VERSION       # model_version -> scorer_version
    assert isinstance(gild_score["p_crl"], float)                    # PERSISTED + float-cast
    assert gild_score["risk_band"] in ("low", "moderate", "elevated", "high")
    assert isinstance(gild_score["oof_percentile_rank"], float)
    assert gild_score["features_id"] == "feat-1000"                  # linked to the feature row
    # all names in this run share ONE scored_at, anchored to the SCORED SNAPSHOT
    # (2026-06-04 @ 00:00 UTC) — NOT now() — so a same-snapshot re-run merges in place.
    expected_stamp = datetime(2026, 6, 4, tzinfo=timezone.utc).isoformat()
    assert {p[1][0]["scored_at"] for p in score_posts} == {expected_stamp}

    # the feature body for GILEAD carries forward pdufa_date + sets M14 cols
    gild_feat = next(p[1][0] for p in feat_posts if p[1][0]["application_number"] == "NDA021937")
    assert gild_feat["pdufa_date"] == "2026-08-27"                   # NOT blanked (matview G3)
    assert gild_feat["snapshot_date"] == "2026-06-04"               # SAME as Phase-0 snapshot
    assert gild_feat["cycle_type"] == "first_cycle_orig"
    assert gild_feat["review_priority"] == "PRIORITY"               # sourced from drugsfda ORIG
    assert gild_feat["submission_class_code"] == "TYPE 1"
    assert gild_feat["feature_quality"] in ("standard", "low")
    assert gild_feat["has_aa"] is True                              # carried from Phase-0 row
    # NULL-not-False for absent/unsourced
    assert gild_feat["sponsor_has_warning"] is None
    assert gild_feat["n_drug_inspections_5y_fix"] is None

    # matview refreshed once
    assert client.rpc_calls == ["rpc/bc_refresh_candidates"]


def test_dry_run_writes_nothing():
    rows = [_universe_row("NDA021937", "882095", "GILEAD SCIENCES, INC.  (GILD)", "NDA", "2026-08-27")]
    client = CaptureClient(rows)
    res = rw.run_weekly(client, apply=False, dfda=FakeDrugsFDA(),
                        eight_k_counter=lambda c, r: 0, _now=_NOW)
    assert res["status"] == "succeeded"
    assert client.posts == []          # no upserts
    assert client.rpc_calls == []      # no matview refresh
    assert res["scored"][0].risk_band is not None  # but it DID score


# --------------------------------------------------------------------------- #
# §7.6 — confidence_flag CHECK-conformance (the deployed regex)
# --------------------------------------------------------------------------- #
_CONF_FLAG_RE = re.compile(
    r"^(standard|low_confidence|no_edgar_signal|refused|"
    r"synthetic_or_unverified_submission_id|probability_extrapolation|low_confidence_sponsor|"
    r"moderate_confidence_no_edgar_signal)(;.*)?$"
)


def test_confidence_flag_conformance():
    from modal_workers.bc_score._m14 import score_nda
    cases = [
        {"is_bla": 1, "priority": 1, "n_prior_filings": 5, "n_8ks_30_180_clean": 4},  # standard
        {"is_bla": 0, "n_prior_filings": 0, "n_8ks_30_180_clean": 4},                  # low_confidence_sponsor
        {"is_bla": 0, "n_prior_filings": 0, "n_8ks_30_180_clean": 0},                  # low_conf;no_edgar
        {"is_bla": 1, "priority": 0, "n_prior_filings": 0, "n_8ks_30_180_clean": 0, "has_aa": 0},  # +extrapolation maybe
    ]
    for feats in cases:
        out = score_nda(dict(feats))
        flag = out["confidence_flag"]
        assert _CONF_FLAG_RE.match(flag), f"flag {flag!r} violates the deployed CHECK regex"
    # refused row
    refused = score_nda({"cycle_type": "supplemental"})
    assert _CONF_FLAG_RE.match(refused["confidence_flag"])  # == 'refused'


# --------------------------------------------------------------------------- #
# §7.7 — refusal path (does not abort the run)
# --------------------------------------------------------------------------- #
def test_refusal_path_persists_and_counts():
    rows = [
        _universe_row("BLA761234", "111", "Bios Co  (BIO)", "BLA", "2026-09-01",
                      is_biosimilar_bla=True),                                  # -> refused
        _universe_row("NDA021937", "882095", "GILEAD  (GILD)", "NDA", "2026-08-27"),  # -> scores
    ]
    client = CaptureClient(rows)
    res = rw.run_weekly(client, apply=True, dfda=FakeDrugsFDA(),
                        eight_k_counter=lambda c, r: 0, _now=_NOW)
    assert res["stats"]["n_refused"] == 1
    assert res["stats"]["n_scored"] == 2  # both rows produced a row (one refused)
    refused_score = next(p[1][0] for p in client.posts
                         if p[0].startswith("bc_rubric_scores") and p[1][0]["application_number"] == "BLA761234")
    assert refused_score["p_crl"] is None
    assert refused_score["risk_band"] is None
    assert refused_score["refusal_reason"]               # set
    assert res["status"] == "succeeded"                  # run NOT aborted


# --------------------------------------------------------------------------- #
# §7.8 — missing-appno (surrogate) degradation: still scores, flagged low
# --------------------------------------------------------------------------- #
def test_surrogate_appno_scores_low_coverage():
    # The common live case: a surrogate appno with NO designations -> only is_bla
    # (+ sourced 8-K) cover the 8 v1-kept keys -> coverage 0.25 -> 'low'.
    rows = [_universe_row("EDGAR8K:1603454:d20260717", "1603454",
                          "Celcuity Inc.  (CELC)", "NDA", "2026-07-17")]
    client = CaptureClient(rows)
    res = rw.run_weekly(client, apply=True, dfda=FakeDrugsFDA(),
                        eight_k_counter=lambda c, r: 2, _now=_NOW)
    rec = res["scored"][0]
    assert rec.scored is True
    assert rec.coverage == 0.25                # is_bla + n_8ks = 2/8
    assert rec.feature_quality == "low"        # low coverage (no substrate for surrogate)
    assert rec.risk_band is not None           # still ranks
    assert rec.review_priority is None         # not sourceable
    feat = next(p[1][0] for p in client.posts if p[0].startswith("bc_application_features"))
    assert feat["feature_quality"] == "low"
    assert res["stats"]["per_name"]["EDGAR8K:1603454:d20260717"]["coverage"] == 0.25


# --------------------------------------------------------------------------- #
# §7.9 — empty universe -> succeeded, n_processed=0, reason=empty_universe
# --------------------------------------------------------------------------- #
def test_empty_universe_is_honest_succeeded():
    client = CaptureClient([])  # nothing with a pdufa_date
    res = rw.run_weekly(client, apply=True, dfda=FakeDrugsFDA(), eight_k_counter=lambda c, r: 0, _now=_NOW)
    assert res["status"] == "succeeded"
    assert res["stats"]["n_in_universe"] == 0
    assert res["stats"]["reason"] == "empty_universe"
    # bc_pipeline_runs opened+closed, but no table upserts / refresh
    assert not any(p[0].startswith("bc_rubric_scores") for p in client.posts)
    assert client.rpc_calls == []


# --------------------------------------------------------------------------- #
# §7.10 — fail-loud: a per-name crash -> status=partial, others persist, run closes
# --------------------------------------------------------------------------- #
def test_per_name_crash_is_partial(monkeypatch):
    rows = [
        _universe_row("NDA021937", "882095", "GILEAD  (GILD)", "NDA", "2026-08-27"),
        _universe_row("NDA050090", "1", "Acme  (ACM)", "NDA", "2026-09-10"),
    ]
    client = CaptureClient(rows)
    real_build = rw.build_features_pit

    def boom_on_acme(client_, **kw):
        if kw.get("application_number") == "NDA050090":
            raise ValueError("synthetic build failure")
        return real_build(client_, **kw)

    monkeypatch.setattr(rw, "build_features_pit", boom_on_acme)
    res = rw.run_weekly(client, apply=True, dfda=FakeDrugsFDA(),
                        eight_k_counter=lambda c, r: 0, _now=_NOW)
    assert res["status"] == "partial"
    assert res["stats"]["n_failed"] == 1
    assert res["stats"]["n_scored"] == 1
    # GILEAD score still persisted
    assert any(p[1][0]["application_number"] == "NDA021937"
               for p in client.posts if p[0].startswith("bc_rubric_scores"))


def test_matview_refresh_failure_is_partial():
    rows = [_universe_row("NDA021937", "882095", "GILEAD  (GILD)", "NDA", "2026-08-27")]
    client = CaptureClient(rows, refresh_raises=True)
    res = rw.run_weekly(client, apply=True, dfda=FakeDrugsFDA(),
                        eight_k_counter=lambda c, r: 0, _now=_NOW)
    assert res["status"] == "partial"                      # scores written, view lagged
    assert res["stats"]["matview_refreshed"] is False
    assert any(p[0].startswith("bc_rubric_scores") for p in client.posts)  # score DID persist


# --------------------------------------------------------------------------- #
# §7.11 — percentile reference (monotone, anchored to the vendored CSV)
# --------------------------------------------------------------------------- #
def test_percentile_reference_monotone():
    ref = rw._load_nda_locked2025_reference()
    assert len(ref) > 50
    from modal_workers.bc_score._m14 import to_percentile
    lo = to_percentile(0.01, reference=ref)
    mid = to_percentile(0.12, reference=ref)
    hi = to_percentile(0.35, reference=ref)
    assert 0.0 <= lo <= mid <= hi <= 100.0   # higher p_crl -> higher (riskier) percentile
    assert hi > 80.0                          # 0.35 is near the top of the locked-2025 set


# --------------------------------------------------------------------------- #
# idempotency stamp: a second same-run-timestamp apply re-POSTs the SAME scored_at
# (so the live UNIQUE merges in place rather than forking history)
# --------------------------------------------------------------------------- #
def test_idempotent_scored_at_stamp_is_per_snapshot():
    # Two runs at DIFFERENT wall-clock times but the SAME scored snapshot must
    # produce the SAME scored_at (anchored to the snapshot, not now()), so the
    # live UNIQUE merges in place rather than forking a second weekly row.
    rows = [_universe_row("NDA021937", "882095", "GILEAD  (GILD)", "NDA", "2026-08-27")]
    now1 = datetime(2026, 6, 5, 8, 0, 0, tzinfo=timezone.utc)
    now2 = datetime(2026, 6, 5, 14, 30, 0, tzinfo=timezone.utc)  # 6.5h later, same snapshot
    c1 = CaptureClient(rows)
    rw.run_weekly(c1, apply=True, dfda=FakeDrugsFDA(), eight_k_counter=lambda c, r: 0, _now=now1)
    c2 = CaptureClient(rows)
    rw.run_weekly(c2, apply=True, dfda=FakeDrugsFDA(), eight_k_counter=lambda c, r: 0, _now=now2)
    s1 = next(p[1][0]["scored_at"] for p in c1.posts if p[0].startswith("bc_rubric_scores"))
    s2 = next(p[1][0]["scored_at"] for p in c2.posts if p[0].startswith("bc_rubric_scores"))
    # snapshot_date on the universe rows is 2026-06-04 -> stamp = that date @ 00:00 UTC
    assert s1 == s2 == datetime(2026, 6, 4, tzinfo=timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
