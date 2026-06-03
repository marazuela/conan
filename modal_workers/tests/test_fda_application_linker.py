"""Unit tests for the FDA application-number linker (no network, no DB)."""

from __future__ import annotations

import re

import pytest

from modal_workers.fetchers.universe import fda_application_linker as lk


def _make_get(by_drug):
    """Fake openFDA get: maps the quoted drug name in `search` -> results list."""
    def _get(path, params):
        m = re.search(r'"([^"]+)"', params.get("search", ""))
        drug = lk._norm_name(m.group(1)) if m else ""
        return {"results": list(by_drug.get(drug, []))}
    return _get


def _app(appno, sponsor, brand=None, generic=None):
    return {
        "application_number": appno,
        "sponsor_name": sponsor,
        "openfda": {"brand_name": [brand] if brand else [], "generic_name": [generic] if generic else []},
        "products": [{"brand_name": brand}] if brand else [],
    }


class FakeClient:
    def __init__(self, assets):
        self.assets = assets
        self.patches = []

    def _rest(self, method, table, params=None):
        return list(self.assets)

    def _rest_with_retry(self, method, table, params=None, json_body=None, prefer=None):
        self.patches.append({"method": method, "table": table, "params": params, "body": json_body})
        return [{"id": "patched"}]


# --------------------------------------------------------------------------- #
def test_appl_type():
    assert lk._appl_type("BLA761360") == "BLA"
    assert lk._appl_type("NDA215358") == "NDA"
    assert lk._appl_type("215358") is None


def test_resolve_exact_single_brand_match():
    get = _make_get({"augtyro": [_app("NDA215358", "Bristol-Myers Squibb", brand="Augtyro", generic="repotrectinib")]})
    res = lk.resolve_application_number({"drug_name": "Augtyro", "sponsor_name": "Zai Lab"}, get=get)
    assert res == {"application_number": "NDA215358", "application_type": "NDA", "match_method": "name_exact_single"}


def test_resolve_dev_code_no_match_returns_none():
    get = _make_get({})  # AXS-05 isn't an openFDA brand/generic name
    assert lk.resolve_application_number({"drug_name": "AXS-05", "sponsor_name": "Axsome"}, get=get) is None


def test_resolve_ambiguous_without_sponsor_overlap_returns_none():
    apps = [_app("NDA111", "Alpha Pharma", generic="metformin"),
            _app("NDA222", "Beta Labs", generic="metformin")]
    get = _make_get({"metformin": apps})
    res = lk.resolve_application_number({"drug_name": "metformin", "sponsor_name": "Gamma Inc"}, get=get)
    assert res is None  # two matches, sponsor can't disambiguate -> refuse to guess


def test_resolve_ambiguous_disambiguated_by_sponsor():
    apps = [_app("NDA111", "Alpha Pharma", generic="metformin"),
            _app("NDA222", "Beta Labs", generic="metformin")]
    get = _make_get({"metformin": apps})
    res = lk.resolve_application_number({"drug_name": "metformin", "sponsor_name": "Alpha Pharmaceuticals Inc"}, get=get)
    assert res["application_number"] == "NDA111"
    assert res["match_method"] == "name_plus_sponsor"


def test_link_dry_run_matches_but_writes_nothing():
    assets = [
        {"id": "a1", "drug_name": "Augtyro", "sponsor_name": "BMS", "application_number": ""},
        {"id": "a2", "drug_name": "AXS-05", "sponsor_name": "Axsome", "application_number": ""},
        {"id": "a3", "drug_name": "Keytruda", "sponsor_name": "Merck", "application_number": "BLA125514"},  # already linked
    ]
    get = _make_get({"augtyro": [_app("NDA215358", "BMS", brand="Augtyro")]})
    client = FakeClient(assets)
    summary = lk.link_application_numbers(client, dry_run=True, get=get)
    assert summary["scanned"] == 2  # a3 already has an appno -> not a target
    assert summary["matched"] == 1  # only Augtyro resolves
    assert summary["written"] == 0
    assert client.patches == []


def test_link_commit_writes_resolved_only():
    assets = [
        {"id": "a1", "drug_name": "Augtyro", "sponsor_name": "BMS", "application_number": ""},
        {"id": "a2", "drug_name": "AXS-05", "sponsor_name": "Axsome", "application_number": ""},
    ]
    get = _make_get({"augtyro": [_app("NDA215358", "BMS", brand="Augtyro")]})
    client = FakeClient(assets)
    summary = lk.link_application_numbers(client, dry_run=False, get=get)
    assert summary["matched"] == 1 and summary["written"] == 1
    assert len(client.patches) == 1
    p = client.patches[0]
    assert p["method"] == "PATCH" and p["params"]["id"] == "eq.a1"
    assert p["body"] == {"application_number": "NDA215358", "application_type": "NDA"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
