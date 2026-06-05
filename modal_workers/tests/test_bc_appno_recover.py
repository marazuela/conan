"""Unit tests for modal_workers.shared.bc_appno_recover (drugsfda real-appno join).

All offline — a fake `openfda_get` returns canned Drugs@FDA payloads, so no HTTP.
Covers (per Phase-0 spec §1 appl_type recovery + §5.4):
  - ORIG NDA/BLA identity extraction + appl_type from the number prefix
  - review_priority pulled from the ORIG submission (PRIORITY/STANDARD only)
  - brand-name match selection (strongest key) vs sole-NDA/BLA selection
  - ambiguous (multiple NDA/BLA, no brand match) -> None (no guessing)
  - ANDA / malformed application_number -> rejected
  - recover_real_appno two-query flow (brand first, sponsor fallback) + miss->None
  - sponsor-suffix cleaning
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from modal_workers.shared.bc_appno_recover import (
    RecoveredAppno,
    _clean_sponsor,
    pick_orig_application,
    recover_real_appno,
)


# ---------------------------------------------------------------------------
# Fixture payload builders (shape mirrors live api.fda.gov/drug/drugsfda.json)
# ---------------------------------------------------------------------------

def _app(appno: str, *, sponsor: str = "ACME", brands: Optional[List[str]] = None,
         orig_priority: Optional[str] = None, orig_status: str = "AP",
         include_orig: bool = True) -> Dict[str, Any]:
    products = [{"brand_name": b} for b in (brands or [])]
    submissions = []
    if include_orig:
        submissions.append({
            "submission_type": "ORIG", "submission_number": "1",
            "submission_status": orig_status, "submission_class_code": "TYPE 1",
            "review_priority": orig_priority, "submission_status_date": "20240115",
        })
    # add a noise supplement submission
    submissions.append({
        "submission_type": "SUPPL", "submission_number": "5",
        "submission_status": "AP", "review_priority": "STANDARD",
    })
    return {"application_number": appno, "sponsor_name": sponsor,
            "products": products, "submissions": submissions}


# ---------------------------------------------------------------------------
# pick_orig_application — pure selection/extraction
# ---------------------------------------------------------------------------

def test_pick_brand_match_nda():
    recs = [_app("NDA215000", brands=["RELACOR"], orig_priority="PRIORITY")]
    out = pick_orig_application(recs, drug="relacor")
    assert out is not None
    assert out.application_number == "NDA215000"
    assert out.appl_type == "NDA"
    assert out.review_priority == "PRIORITY"
    assert out.match_basis == "brand"
    assert out.matched_brand == "RELACOR"


def test_pick_brand_match_bla_prefix_drives_type():
    recs = [_app("BLA761234", brands=["OPDIVO"], orig_priority="STANDARD")]
    out = pick_orig_application(recs, drug="Opdivo")
    assert out.appl_type == "BLA"
    assert out.application_number == "BLA761234"
    assert out.review_priority == "STANDARD"


def test_pick_brand_substring_either_way():
    # "opdivo" matches product "OPDIVO QVANTIG" (drug is substring of brand)
    recs = [_app("BLA761381", brands=["OPDIVO QVANTIG"])]
    out = pick_orig_application(recs, drug="opdivo")
    assert out is not None and out.application_number == "BLA761381"


def test_pick_sole_nda_when_no_brand_match():
    # one NDA/BLA, drug doesn't match any brand -> sole selection
    recs = [_app("NDA200001", brands=["SOMETHINGELSE"])]
    out = pick_orig_application(recs, drug="mydrug")
    assert out is not None
    assert out.application_number == "NDA200001"
    assert out.match_basis == "sole"


def test_pick_ambiguous_multiple_no_brand_returns_none():
    recs = [_app("NDA1", brands=["A"]), _app("NDA2", brands=["B"])]
    # drug matches neither brand and there are 2 NDA/BLA -> no confident pick
    assert pick_orig_application(recs, drug="zzz") is None


def test_pick_rejects_anda_only():
    recs = [_app("ANDA090001", brands=["GENERIC"])]
    assert pick_orig_application(recs, drug="generic") is None


def test_pick_empty_records_none():
    assert pick_orig_application([], drug="x") is None
    assert pick_orig_application([{"application_number": "ANDA1"}], drug="x") is None


def test_pick_priority_only_valid_tokens():
    # an unexpected review_priority value must NOT leak through (CHECK-safety)
    recs = [_app("NDA300000", brands=["X"], orig_priority="EXPEDITED")]
    out = pick_orig_application(recs, drug="x")
    assert out is not None
    assert out.review_priority is None  # 'EXPEDITED' not in {PRIORITY,STANDARD}


def test_pick_missing_orig_priority_none():
    recs = [_app("NDA300001", brands=["X"], include_orig=False)]
    out = pick_orig_application(recs, drug="x")
    assert out is not None
    assert out.review_priority is None  # no ORIG submission -> unknown priority


# ---------------------------------------------------------------------------
# recover_real_appno — two-query flow with a fake openfda_get (no network)
# ---------------------------------------------------------------------------

class _FakeFDA:
    """Records queries and returns a scripted body per search clause."""

    def __init__(self, brand_body=None, sponsor_body=None):
        self.brand_body = brand_body
        self.sponsor_body = sponsor_body
        self.queries: List[str] = []

    def __call__(self, path: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        search = params.get("search", "")
        self.queries.append(search)
        if "brand_name" in search:
            return self.brand_body
        if "sponsor_name" in search:
            return self.sponsor_body
        return None


def test_recover_brand_query_hits_first():
    fake = _FakeFDA(brand_body={"results": [_app("NDA215000", brands=["RELACOR"], orig_priority="PRIORITY")]})
    out = recover_real_appno("relacor", "Corcept Therapeutics, Inc.", openfda_get=fake)
    assert out is not None and out.application_number == "NDA215000"
    # brand query is issued first; sponsor fallback not needed
    assert any("brand_name" in q for q in fake.queries)
    assert not any("sponsor_name" in q for q in fake.queries)


def test_recover_falls_back_to_sponsor_when_brand_misses():
    # brand query returns nothing (drug not in drugsfda yet), sponsor query has a sole NDA
    fake = _FakeFDA(brand_body=None,
                    sponsor_body={"results": [_app("NDA400000", brands=["OTHER"])]})
    out = recover_real_appno("freshdrug", "Acme Pharmaceuticals", openfda_get=fake)
    assert out is not None and out.application_number == "NDA400000"
    assert any("brand_name" in q for q in fake.queries)
    assert any("sponsor_name" in q for q in fake.queries)


def test_recover_miss_returns_none_keeps_surrogate():
    # the realistic PENDING case: neither brand nor sponsor yields an NDA/BLA
    fake = _FakeFDA(brand_body=None, sponsor_body={"results": []})
    out = recover_real_appno("olezarsen", "Ionis Pharmaceuticals, Inc.", openfda_get=fake)
    assert out is None


def test_recover_no_drug_uses_sponsor_only():
    fake = _FakeFDA(sponsor_body={"results": [_app("BLA125554", brands=["OPDIVO"])]})
    out = recover_real_appno(None, "Bristol Myers Squibb", openfda_get=fake)
    assert out is not None and out.appl_type == "BLA"
    # with no drug, the brand query is skipped entirely
    assert all("brand_name" not in q for q in fake.queries)


def test_recover_swallows_fetch_exception():
    def boom(path, params):
        raise RuntimeError("openfda 500")
    # recovery is advisory: an exception must not propagate (keeps surrogate)
    assert recover_real_appno("x", "Y Inc.", openfda_get=boom) is None


def test_recover_returns_recoveredappno_type():
    fake = _FakeFDA(brand_body={"results": [_app("NDA1", brands=["D"])]})
    out = recover_real_appno("d", "S", openfda_get=fake)
    assert isinstance(out, RecoveredAppno)


# ---------------------------------------------------------------------------
# sponsor suffix cleaning
# ---------------------------------------------------------------------------

def test_clean_sponsor_strips_corporate_suffix():
    assert _clean_sponsor("Exelixis, Inc.") == "Exelixis"
    assert _clean_sponsor("Vera Therapeutics, Inc.") == "Vera"
    assert _clean_sponsor("Gilead Sciences, Inc.") == "Gilead Sciences"


def test_clean_sponsor_keeps_when_all_suffix():
    # don't nuke to empty
    out = _clean_sponsor("Pharmaceuticals Inc")
    assert out  # non-empty fallback
