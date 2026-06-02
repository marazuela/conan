"""Unit tests for the FDA CRL feature-assembly layer (no DB, no network).

A small param-aware fake client stands in for SupabaseClient: it applies the
top-level ``eq.`` filters the assembly code uses (application_number, ticker,
sponsor_ticker, entity_id, source, doc_type) and ignores select/order/and/or.
That is enough to exercise the per-feature queries and the routing.
"""

from __future__ import annotations

from datetime import date

import pytest

from modal_workers.shared.fda_crl import feature_assembly as fa
from modal_workers.shared.fda_crl import router


class FakeClient:
    def __init__(self, tables):
        self.tables = tables
        self.calls = []

    def _rest(self, method, table, params=None):
        self.calls.append((method, table, dict(params or {})))
        rows = self.tables.get(table, [])
        out = []
        for r in rows:
            keep = True
            for k, v in (params or {}).items():
                if k in ("select", "order", "and", "or", "limit", "offset"):
                    continue
                if isinstance(v, str) and v.startswith("eq."):
                    if str(r.get(k)) != v[len("eq.") :]:
                        keep = False
                        break
            if keep:
                out.append(r)
        return out


# --------------------------------------------------------------------------- #
def test_appl_is_bla():
    assert fa.appl_is_bla("BLA125514") == 1
    assert fa.appl_is_bla("NDA021436") == 0
    assert fa.appl_is_bla("125514") is None
    assert fa.appl_is_bla(None) is None


def test_build_catalyst_routes_original_from_orig_submission():
    asset = {"application_number": "NDA021436", "extensions": {}}
    event = {"extensions": {}}
    sub = {"submission_type": "ORIG", "submission_class_code": "TYPE 1"}
    cat = fa.build_catalyst(asset, event, submission=sub)
    assert cat["application_type"] == "NDA"
    assert router.classify_scope(cat)["scope"] == router.ORIGINAL


def test_event_extensions_win_over_orig_submission_for_supplements():
    # A supplement PDUFA: event extensions say SUPPL/EFFICACY; the ORIG row must
    # not override it back to 'original'.
    asset = {"application_number": "NDA021436", "extensions": {}}
    event = {"extensions": {"submission_type": "SUPPL", "submission_class_code": "EFFICACY - NEW INDICATION"}}
    sub = {"submission_type": "ORIG", "submission_class_code": "TYPE 1"}
    cat = fa.build_catalyst(asset, event, submission=sub)
    assert cat["submission_type"] == "SUPPL"
    assert router.classify_scope(cat)["scope"] == router.EFFICACY_SUPPLEMENT


def _full_tables():
    return {
        fa.SUBMISSIONS: [
            {"application_number": "NDA021436", "submission_type": "ORIG", "submission_class_code": "TYPE 1",
             "review_priority": "STANDARD", "ticker": "ABBV", "submission_status_date": "2018-01-01"},
            {"application_number": "NDA111111", "submission_type": "ORIG", "ticker": "ABBV",
             "submission_status_date": "2016-01-01"},
            {"application_number": "NDA222222", "submission_type": "ORIG", "ticker": "ABBV",
             "submission_status_date": "2015-01-01"},
        ],
        fa.INSPECTIONS: [
            {"inspection_id": "i1", "sponsor_ticker": "ABBV", "inspection_end_date": "2022-03-01"},
            {"inspection_id": "i2", "sponsor_ticker": "ABBV", "inspection_end_date": "2021-06-01"},
        ],
        fa.WARNING_LETTERS: [],  # no warning letters
        fa.DOCUMENTS: [
            {"id": "d1", "source": "edgar", "doc_type": "8-K", "entity_id": "e-1", "published_at": "2024-09-01"},
            {"id": "d2", "source": "edgar", "doc_type": "8-K", "entity_id": "e-1", "published_at": "2024-08-15"},
        ],
    }


def _full_asset():
    return {
        "application_number": "NDA021436",
        "ticker": "ABBV",
        "sponsor_name": "AbbVie Inc",
        "entity_id": "e-1",
        "extensions": {"designations": {"breakthrough": True, "fast_track": False, "accelerated_approval": False}},
    }


def test_assemble_nda_features_full_coverage():
    client = FakeClient(_full_tables())
    asset = _full_asset()
    event = {"event_date": "2024-12-01", "extensions": {}}
    feats = fa.assemble_nda_features(client, asset, event)

    assert feats["is_bla"] == 0
    assert feats["ApplType"] == "NDA"
    assert feats["priority"] == 0  # STANDARD
    assert feats["SubmissionClassCode"] == "TYPE 1"
    assert feats["n_prior_filings"] == 2  # NDA111111 + NDA222222, excluding self
    assert feats["n_drug_inspections_5y_fix"] == 2
    assert feats["sponsor_has_warning"] == 0
    assert feats["has_bt"] == 1
    assert feats["has_ft"] == 0
    assert feats["has_aa"] == 0
    assert feats["n_8ks_30_180_clean"] == 2
    assert feats["cycle_type"] == "first_cycle_orig"
    assert feats["_coverage"] == 1.0  # all 10 high-signal keys present


def test_assemble_nda_degrades_without_data():
    client = FakeClient({})  # empty DB
    asset = {"application_number": "BLA125514", "ticker": None, "sponsor_name": None, "extensions": {}}
    event = {"event_date": "2024-12-01", "extensions": {}}
    feats = fa.assemble_nda_features(client, asset, event)
    # is_bla still derivable from the application_number prefix
    assert feats["is_bla"] == 1
    # nothing else sourced -> low coverage, but no crash
    assert feats["_coverage"] < 0.2
    assert "n_prior_filings" not in feats


def test_score_catalyst_crl_original_returns_calibrated_risk():
    client = FakeClient(_full_tables())
    asset = _full_asset()
    event = {"event_date": "2024-12-01", "extensions": {}}
    out = fa.score_catalyst_crl(client, asset, event)
    assert out["crl_scope"] == router.ORIGINAL
    assert isinstance(out["crl_risk"], float)
    assert 0.0 <= out["crl_risk"] <= 1.0
    assert out["crl_percentile"] is None
    assert out["crl_feature_coverage"] == 1.0


def test_score_catalyst_crl_efficacy_supplement_is_rank_only():
    client = FakeClient(_full_tables())
    asset = _full_asset()
    event = {"event_date": "2024-12-01",
             "extensions": {"submission_type": "SUPPL", "submission_class_code": "EFFICACY - NEW INDICATION"}}
    out = fa.score_catalyst_crl(client, asset, event)
    assert out["crl_scope"] == router.EFFICACY_SUPPLEMENT
    assert out["crl_risk"] is None  # never surface the uncalibrated sNDA prob
    assert out["crl_percentile"] is not None
    assert 0.0 <= out["crl_percentile"] <= 100.0


def test_score_catalyst_crl_refuses_biosimilar():
    client = FakeClient(_full_tables())
    asset = {"application_number": "BLA761234", "ticker": "X", "sponsor_name": "Y",
             "extensions": {"is_biosimilar": True}}
    event = {"event_date": "2024-12-01", "extensions": {"is_biosimilar": True}}
    out = fa.score_catalyst_crl(client, asset, event)
    assert out["crl_scope"] == router.REFUSED
    assert out["crl_risk"] is None
    assert out["crl_refusal_reason"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
