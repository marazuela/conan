"""Unit tests for seed_fda_asset_aliases — pure-Python helpers only.

The integration paths (openFDA, ClinicalTrials.gov, Supabase upsert) require
network + service-role creds and are exercised via dry-run smoke tests against
staging, not pytest. These tests cover normalization, validity gates,
CURATED_MAP reverse-indexing, sponsor-stem derivation, and the
ClinicalTrials response parser shape.

Run: python -m pytest modal_workers/tests/test_seed_fda_asset_aliases.py -v
"""
from __future__ import annotations

from modal_workers.scripts.seed_fda_asset_aliases import (
    NCT_PATTERN,
    NORMALIZED_BLOCKLIST,
    SPONSOR_SUFFIXES,
    AliasCandidate,
    SeedStats,
    aliases_from_clinicaltrials,
    aliases_from_curated_map,
    is_valid_alias,
    make_candidate,
    normalize,
)


# ---------------------------------------------------------------------------
# normalize / is_valid_alias
# ---------------------------------------------------------------------------

def test_normalize_lowers_and_trims() -> None:
    assert normalize("  Mounjaro  ") == "mounjaro"
    assert normalize("NCT05123456") == "nct05123456"
    assert normalize("Eli Lilly and Company") == "eli lilly and company"


def test_is_valid_alias_rejects_too_short() -> None:
    assert not is_valid_alias("ab", "brand")
    assert not is_valid_alias("", "brand")
    assert is_valid_alias("abc", "brand")


def test_is_valid_alias_rejects_blocklist() -> None:
    for word in NORMALIZED_BLOCKLIST:
        assert not is_valid_alias(word, "generic"), f"should reject {word!r}"


def test_is_valid_alias_rejects_unknown_kind() -> None:
    assert not is_valid_alias("tirzepatide", "ticker"), \
        "ticker is not a valid alias_kind in fda_asset_aliases"
    assert not is_valid_alias("tirzepatide", "bogus_kind")


def test_is_valid_alias_nct_shape() -> None:
    assert is_valid_alias("nct05123456", "nct_id")
    assert not is_valid_alias("nct123", "nct_id")           # too short
    assert not is_valid_alias("ncrt05123456", "nct_id")     # misspelled prefix
    assert not is_valid_alias("nct051234567", "nct_id")     # 9-digit suffix
    assert not is_valid_alias("NCT05123456", "nct_id")      # must be lowercase


def test_make_candidate_returns_none_on_invalid() -> None:
    assert make_candidate("a", "ab", "brand", "openfda_label") is None
    assert make_candidate("a", "peptide", "drug_name", "operator") is None
    assert make_candidate("a", "Mounjaro", "brand", "openfda_label") is not None


# ---------------------------------------------------------------------------
# CURATED_MAP reverse-indexing + sponsor_stem derivation
# ---------------------------------------------------------------------------

def test_aliases_from_curated_map_emits_sponsor_alias_and_stem_for_lilly() -> None:
    asset = {
        "id": "asset-1",
        "ticker": "LLY",
        "drug_name": "tirzepatide",
        "sponsor_name": "Eli Lilly and Company",
    }
    cands = aliases_from_curated_map(asset)
    kinds = {(c.alias_normalized, c.alias_kind) for c in cands}

    # The curated key for LLY is "Eli Lilly and Company" → sponsor_alias.
    assert ("eli lilly and company", "sponsor_alias") in kinds
    # Stripping " and Company" yields "Eli Lilly" → sponsor_stem.
    assert ("eli lilly", "sponsor_stem") in kinds


def test_aliases_from_curated_map_subsidiary_to_parent() -> None:
    # "Janssen Research & Development, LLC" → ticker JNJ in CURATED_MAP.
    # An asset with ticker JNJ should pick up the Janssen alias.
    asset = {
        "id": "asset-jnj",
        "ticker": "JNJ",
        "drug_name": "exampledrug",
        "sponsor_name": "Johnson & Johnson",
    }
    cands = aliases_from_curated_map(asset)
    aliases_lower = {c.alias_normalized for c in cands}
    assert any("janssen" in a for a in aliases_lower), (
        "subsidiary Janssen should resolve to JNJ via CURATED_MAP reverse-index"
    )


def test_aliases_from_curated_map_empty_for_unknown_ticker() -> None:
    asset = {
        "id": "asset-x",
        "ticker": "ZZZZ_NOT_LISTED",
        "drug_name": "foo",
        "sponsor_name": "Foo Pharma",
    }
    cands = aliases_from_curated_map(asset)
    # Only the asset's own sponsor_name should produce a sponsor_alias
    # (no CURATED_MAP matches; sponsor_name itself is fine).
    assert all(c.source == "curated_map" for c in cands)
    sponsor_aliases = [c for c in cands if c.alias_kind == "sponsor_alias"]
    assert len(sponsor_aliases) == 1
    assert sponsor_aliases[0].alias_normalized == "foo pharma"


def test_aliases_from_curated_map_no_ticker_no_output() -> None:
    asset = {
        "id": "asset-noticker",
        "ticker": None,
        "drug_name": "foo",
        "sponsor_name": "Foo Inc.",
    }
    assert aliases_from_curated_map(asset) == []


def test_sponsor_suffix_ordering_longer_first() -> None:
    # Regression: " Pharmaceuticals, Inc." must precede " Inc." in SPONSOR_SUFFIXES
    # so "Foo Pharmaceuticals, Inc." reduces to "Foo", not "Foo Pharmaceuticals".
    longer_idx = SPONSOR_SUFFIXES.index(" Pharmaceuticals, Inc.")
    shorter_idx = SPONSOR_SUFFIXES.index(" Inc.")
    assert longer_idx < shorter_idx


# ---------------------------------------------------------------------------
# ClinicalTrials.gov response parser
# ---------------------------------------------------------------------------

class _FakeSession:
    """Minimal stand-in for requests.Session — returns a baked response."""
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self._status_code = status_code

    def get(self, *_args, **_kwargs):  # noqa: ANN
        return _FakeResponse(self._payload, self._status_code)


class _FakeResponse:
    def __init__(self, payload, status_code):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


def test_aliases_from_clinicaltrials_picks_nct_and_otherNames() -> None:
    asset = {
        "id": "asset-orforglipron",
        "ticker": "LLY",
        "drug_name": "orforglipron",
        "sponsor_name": "Eli Lilly and Company",
    }
    payload = {
        "studies": [
            {
                "protocolSection": {
                    "identificationModule": {"nctId": "NCT05923203"},
                    "armsInterventionsModule": {
                        "interventions": [
                            {
                                "name": "Orforglipron",
                                "otherNames": ["LY3502970", "GLP-1 small molecule"],
                            },
                            {
                                # different drug in same trial — must NOT yield
                                # its code names as aliases for orforglipron.
                                "name": "Semaglutide",
                                "otherNames": ["Ozempic", "Rybelsus"],
                            },
                        ]
                    },
                }
            },
            {
                "protocolSection": {
                    "identificationModule": {"nctId": "NCT06000000"},
                    "armsInterventionsModule": {
                        "interventions": [
                            {"name": "Orforglipron 36mg", "otherNames": ["LY3502970"]},
                        ]
                    },
                }
            },
        ]
    }
    session = _FakeSession(payload)
    stats = SeedStats()

    cands = aliases_from_clinicaltrials(asset, session, stats)

    by_kind = {}
    for c in cands:
        by_kind.setdefault(c.alias_kind, set()).add(c.alias_normalized)

    # NCT IDs picked up
    assert "nct05923203" in by_kind.get("nct_id", set())
    assert "nct06000000" in by_kind.get("nct_id", set())

    # Code names from matching-intervention otherNames picked up
    assert "ly3502970" in by_kind.get("code", set())
    assert "glp-1 small molecule" in by_kind.get("code", set())

    # Code names from OTHER drugs in the same multi-arm trial must be excluded
    assert "ozempic" not in by_kind.get("code", set()), (
        "must not steal a competitor intervention's otherNames into our asset"
    )
    assert "rybelsus" not in by_kind.get("code", set())


def test_aliases_from_clinicaltrials_handles_empty_response() -> None:
    asset = {
        "id": "asset-x",
        "ticker": "XXX",
        "drug_name": "fakedrug",
        "sponsor_name": "FakeCo",
    }
    session = _FakeSession({"studies": []})
    cands = aliases_from_clinicaltrials(asset, session, SeedStats())
    assert cands == []


def test_aliases_from_clinicaltrials_no_drug_no_call() -> None:
    asset = {"id": "a", "ticker": "X", "drug_name": None, "generic_name": None,
             "sponsor_name": "FakeCo"}
    stats = SeedStats()
    cands = aliases_from_clinicaltrials(asset, _FakeSession({"studies": []}), stats)
    assert cands == []
    # No API calls made if drug name is missing
    assert stats.api_calls == 0


# ---------------------------------------------------------------------------
# NCT_PATTERN regex
# ---------------------------------------------------------------------------

def test_nct_pattern_matches_only_normalized_form() -> None:
    assert NCT_PATTERN.match("nct05123456")
    assert not NCT_PATTERN.match("NCT05123456")  # must be lowercase
    assert not NCT_PATTERN.match("nct051234")    # too short
    assert not NCT_PATTERN.match("nct051234567") # too long
    assert not NCT_PATTERN.match("nctxxxxxxxx")  # non-numeric
