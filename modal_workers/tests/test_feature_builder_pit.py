"""Unit tests for the shared PIT feature builder (no DB, no network).

Covers Phase 1 §7.2 (parity vs feature_assembly), §7.3 (no-look-ahead), §7.4
(scorer-input-key conformance). All offline: a fake Drugs@FDA stub + an injected
8-K counter + the param-aware FakeClient from test_fda_crl_feature_assembly stand
in for the live sources.
"""

from __future__ import annotations

import inspect
from datetime import date

import pytest

from modal_workers.shared import feature_builder_pit as fb
from modal_workers.bc_score._m14 import feature_assembly as fa
from modal_workers.bc_score._m14 import nda_scorer


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class FakeClient:
    """Param-aware fake SupabaseClient — applies top-level ``eq.`` filters and
    ignores select/order/and/or (mirrors test_fda_crl_feature_assembly)."""

    def __init__(self, tables):
        self.tables = tables
        self.calls = []

    def _rest(self, method, table, params=None):
        self.calls.append((method, table, dict(params or {})))
        out = []
        for r in self.tables.get(table, []):
            keep = True
            for k, v in (params or {}).items():
                if k in ("select", "order", "and", "or", "limit", "offset"):
                    continue
                if isinstance(v, str) and v.startswith("eq.") and str(r.get(k)) != v[3:]:
                    keep = False
                    break
            if keep:
                out.append(r)
        return out


class FakeDrugsFDA:
    """Stand-in for ``DrugsFDA`` — serves canned drugsfda records by appno and
    by sponsor_name, so the PIT builder's submission/prior-filings reads run
    offline. Records are the openFDA ``drug/drugsfda.json`` ``results[]`` shape
    (``submissions[]`` with compact ``YYYYMMDD`` dates)."""

    def __init__(self, by_appno=None, by_sponsor=None):
        self._by_appno = by_appno or {}
        self._by_sponsor = by_sponsor or {}

    def application(self, appno):
        return self._by_appno.get(fb._norm(appno))

    def sponsor_applications(self, sponsor_name):
        return self._by_sponsor.get(fb._firm_norm(sponsor_name), [])


# --------------------------------------------------------------------------- #
# §7.2 — feature parity vs assemble_nda_features
# --------------------------------------------------------------------------- #
def _fa_tables():
    # fda_application_submissions for assemble_nda_features: an ORIG for the
    # subject (priority/class) + two prior ORIGs for the sponsor (n_prior=2).
    return {
        fa.SUBMISSIONS: [
            {"application_number": "NDA021436", "submission_type": "ORIG1",
             "submission_class_code": "TYPE 3", "review_priority": "PRIORITY",
             "ticker": "ABBV", "sponsor_name": "AbbVie Inc",
             "submission_status_date": "2018-01-01"},
            {"application_number": "NDA111111", "submission_type": "ORIG1",
             "ticker": "ABBV", "sponsor_name": "AbbVie Inc",
             "submission_status_date": "2016-01-01"},
            {"application_number": "NDA222222", "submission_type": "ORIG1",
             "ticker": "ABBV", "sponsor_name": "AbbVie Inc",
             "submission_status_date": "2015-01-01"},
        ],
        fa.INSPECTIONS: [],  # dropped for v1 on both sides -> absent
        fa.WARNING_LETTERS: [],  # empty -> 0 on both sides
        fa.DOCUMENTS: [
            {"id": "d1", "source": "edgar", "doc_type": "8-K", "entity_id": "e-1",
             "published_at": "2024-09-01"},
            {"id": "d2", "source": "edgar", "doc_type": "8-K", "entity_id": "e-1",
             "published_at": "2024-08-15"},
        ],
    }


def _dfda_mirror():
    # SAME data as _fa_tables, in drugsfda shape, for the PIT builder.
    return FakeDrugsFDA(
        by_appno={
            "NDA021436": {
                "application_number": "NDA021436",
                "sponsor_name": "AbbVie Inc",
                "submissions": [
                    {"submission_type": "ORIG", "submission_number": "1",
                     "submission_class_code": "TYPE 3", "review_priority": "PRIORITY",
                     "submission_status_date": "20180101"},
                ],
            },
        },
        by_sponsor={
            "abbvie inc": [
                {"application_number": "NDA021436", "submissions": [
                    {"submission_type": "ORIG", "submission_number": "1",
                     "submission_status_date": "20180101"}]},
                {"application_number": "NDA111111", "submissions": [
                    {"submission_type": "ORIG", "submission_number": "1",
                     "submission_status_date": "20160101"}]},
                {"application_number": "NDA222222", "submissions": [
                    {"submission_type": "ORIG", "submission_number": "1",
                     "submission_status_date": "20150101"}]},
            ],
        },
    )


def test_parity_pit_vs_assemble_nda_features():
    """The load-bearing test: build_features_pit == assemble_nda_features on a
    shared fixture (same submissions, same 8-K count, same designations)."""
    ref = date(2024, 12, 1)

    # assemble_nda_features path (reads fda_application_submissions + documents)
    fa_client = FakeClient(_fa_tables())
    fa_asset = {
        "application_number": "NDA021436", "ticker": "ABBV", "sponsor_name": "AbbVie Inc",
        "entity_id": "e-1",
        "extensions": {"designations": {"breakthrough": True, "fast_track": False,
                                        "accelerated_approval": False}},
    }
    fa_feats = fa.assemble_nda_features(fa_client, fa_asset, {}, ref_date=ref)

    # build_features_pit path (reads drugsfda stub + injected 8-K counter)
    pit_feats = fb.build_features_pit(
        FakeClient({"fda_warning_letters": []}),
        application_number="NDA021436",
        sponsor_cik="0001551152",
        sponsor_name="AbbVie Inc",
        appl_type="NDA",
        ref_date=ref,
        designations={"has_bt": True, "has_ft": False, "has_aa": False},
        dfda=_dfda_mirror(),
        eight_k_counter=lambda cik, r: 2,  # same as the two documents rows
    )

    # Parity on every VALUE-BEARING feature (the score inputs). Coverage is
    # compared on the v1-kept denominator below (the PIT builder intentionally
    # drops inspections for v1, so its raw _coverage denominator is 8, not 10).
    shared_keys = (
        "is_bla", "ApplType", "priority", "SubmissionClassCode", "n_prior_filings",
        "sponsor_has_warning", "has_bt", "has_ft", "has_aa", "n_8ks_30_180_clean",
        "cycle_type",
    )
    for k in shared_keys:
        assert fa_feats.get(k) == pit_feats.get(k), f"parity mismatch on {k}: fa={fa_feats.get(k)} pit={pit_feats.get(k)}"

    # concrete expected values (proves both are RIGHT, not just equal)
    assert pit_feats["is_bla"] == 0
    assert pit_feats["priority"] == 1            # PRIORITY
    assert pit_feats["SubmissionClassCode"] == "TYPE 3"
    assert pit_feats["n_prior_filings"] == 2     # NDA111111 + NDA222222
    assert pit_feats["sponsor_has_warning"] == 0  # empty table
    assert pit_feats["has_bt"] == 1
    assert pit_feats["n_8ks_30_180_clean"] == 2

    # Coverage parity on the v1-kept key set (both builders agree on which of
    # the 8 buildable keys are present): all 8 present here -> 1.0.
    fa_v1_cov = sum(1 for k in fb._V1_KEPT_COVERAGE_KEYS if k in fa_feats) / len(fb._V1_KEPT_COVERAGE_KEYS)
    assert pit_feats["_coverage"] == round(fa_v1_cov, 4) == 1.0

    # And the two feature dicts produce the SAME score (the ultimate parity).
    assert nda_scorer.score_nda(dict(pit_feats))["p_crl"] == nda_scorer.score_nda(dict(fa_feats))["p_crl"]


# --------------------------------------------------------------------------- #
# §7.3 — no-look-ahead
# --------------------------------------------------------------------------- #
def test_no_look_ahead_excludes_future_prior_filing():
    """A prior ORIG dated after ref must be excluded (n_prior counts only < ref)."""
    ref = date(2020, 1, 1)
    dfda = FakeDrugsFDA(
        by_appno={"NDA000001": {"application_number": "NDA000001", "submissions": [
            {"submission_type": "ORIG", "submission_number": "1",
             "submission_status_date": "20190101"}]}},
        by_sponsor={"acme bio": [
            {"application_number": "NDA000002", "submissions": [  # before ref -> counts
                {"submission_type": "ORIG", "submission_number": "1",
                 "submission_status_date": "20180101"}]},
            {"application_number": "NDA000003", "submissions": [  # AFTER ref -> excluded
                {"submission_type": "ORIG", "submission_number": "1",
                 "submission_status_date": "20210101"}]},
        ]},
    )
    feats = fb.build_features_pit(
        None, application_number="NDA000001", sponsor_cik="123", sponsor_name="Acme Bio",
        appl_type="NDA", ref_date=ref, dfda=dfda, eight_k_counter=lambda c, r: 0,
        enable_warning_letters=False,
    )
    assert feats["n_prior_filings"] == 1  # only NDA000002 (2018), NOT NDA000003 (2021)


def test_no_look_ahead_raises_on_future_warning_letter():
    """A warning letter dated > ref must trip the look-ahead assertion."""
    ref = date(2020, 1, 1)
    client = FakeClient({"fda_warning_letters": [
        {"letter_id": "w1", "issue_date": "2021-06-01", "firm_name_norm": "acme bio"},
    ]})
    with pytest.raises(AssertionError, match="look-ahead"):
        fb.build_features_pit(
            client, application_number="NDA000001", sponsor_cik="123",
            sponsor_name="Acme Bio", appl_type="NDA", ref_date=ref,
            dfda=FakeDrugsFDA(), eight_k_counter=lambda c, r: 0,
        )


def test_builder_signature_takes_no_outcome_field():
    """The builder must never accept pdufa_date / a CRL label / an outcome arg."""
    params = set(inspect.signature(fb.build_features_pit).parameters)
    for forbidden in ("pdufa_date", "crl_label", "outcome", "event_date", "letter_date"):
        assert forbidden not in params, f"builder must not take {forbidden}"


# --------------------------------------------------------------------------- #
# §7.4 — every emitted key is one the scorer recognizes (alias-drift guard)
# --------------------------------------------------------------------------- #
def test_emitted_keys_are_scorer_recognized():
    ref = date(2024, 12, 1)
    feats = fb.build_features_pit(
        None, application_number="NDA021436", sponsor_cik="1", sponsor_name="AbbVie Inc",
        appl_type="NDA", ref_date=ref, designations={"has_bt": True},
        dfda=_dfda_mirror(), eight_k_counter=lambda c, r: 2, enable_warning_letters=False,
    )
    # keys score_row reads (first-alias-wins families) + the gates.
    recognized = {
        "cycle_type", "is_biosimilar_bla", "biosimilar", "is_biosimilar",
        "is_bla", "ApplType", "appl_type",
        "priority", "ReviewPriority", "review_priority",
        "type5_or_3", "SubmissionClassCode", "submission_class",
        "n_prior_filings", "n_prior_filings_log", "sponsor_history", "n_prior_filing_events",
        "sponsor_has_warning", "sponsor_warning", "has_warning_letter",
        "n_drug_inspections_5y_fix", "n_drug_inspections_5y", "n_drug_inspections", "n_drug_inspections_log",
        "has_bt", "breakthrough", "breakthrough_therapy",
        "has_ft", "fast_track",
        "has_aa", "accelerated_approval",
        "n_8ks_30_180_clean", "n_8ks_30_180", "edgar_8k_count_30_180",
        "sponsor_has_orphan_history",
        "ctgov_failed_primary", "failed_primary",
        "ctgov_any_randomized", "ctgov_any_randomized_pre_event", "any_randomized",
    }
    builder_internal = {
        "_coverage", "_max_source_date", "_provenance", "_feature_sources",
        "_required_feature_missing_count",
    }
    for k in feats:
        if k in builder_internal:
            continue
        assert k in recognized, f"builder emitted unrecognized scorer key {k!r}"


# --------------------------------------------------------------------------- #
# surrogate appno degradation (substrate features fall absent, still scores)
# --------------------------------------------------------------------------- #
def test_surrogate_appno_degrades_gracefully():
    ref = date(2026, 3, 1)
    feats = fb.build_features_pit(
        None, application_number="EDGAR8K:874015:d20260630", sponsor_cik="874015",
        sponsor_name="IONIS PHARMACEUTICALS INC", appl_type="NDA", ref_date=ref,
        designations={"has_bt": True, "has_aa": True},
        dfda=FakeDrugsFDA(), eight_k_counter=lambda c, r: 3, enable_warning_letters=False,
    )
    # is_bla from appl_type hint; designations carried; 8-K sourced; but NO
    # priority/class/n_prior (surrogate appno has no drugsfda record).
    assert feats["is_bla"] == 0
    assert feats["ApplType"] == "NDA"
    assert feats["has_bt"] == 1
    assert feats["has_aa"] == 1
    assert feats["n_8ks_30_180_clean"] == 3
    assert "priority" not in feats
    assert "SubmissionClassCode" not in feats
    # present of the 8 v1-kept keys: is_bla, has_bt, has_aa, n_8ks = 4/8 = 0.5
    assert feats["_coverage"] == 0.5  # low coverage -> 'low' feature_quality
    assert feats["_required_feature_missing_count"] == 4
    # still scores without error
    out = nda_scorer.score_nda(dict(feats))
    assert out["risk_band"] in ("low", "moderate", "elevated", "high")
    assert out["confidence_flag"]  # non-empty


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
