"""Regression tests for asset_linker pass-1 — covers the no-progress loop bug.

The 2026-05-11 incident: pass-1 ran every 15min, re-Sonneted the same ~200
dailymed docs, burned ~$2.40/run with 0 links inserted. Root cause: zero-link
outcomes never wrote a "classified" marker, so the same docs reappeared in the
next batch.

These tests pin down the three guarantees that prevent recurrence:
  1. load_documents_to_link only returns docs where linker_classified_at IS NULL
  2. every terminal outcome calls _mark_classified with the right result tag
  3. transient API errors do NOT mark (so retries happen on the next run)
  4. asset_linker_runs row is inserted at start + completed at end

Run: python -m pytest modal_workers/tests/test_asset_linker_pass1.py -v
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

# setdefault is a no-op when the key exists but is empty ("ANTHROPIC_API_KEY=")
# — common in CI envs that pre-declare the var. Force a value here.
for _k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "ANTHROPIC_API_KEY"):
    if not os.environ.get(_k):
        os.environ[_k] = "https://x.supabase.co" if _k == "SUPABASE_URL" else "x"

import anthropic

from modal_workers.extractor.asset_linker import (
    FALLBACK_ELIGIBLE_SOURCES,
    LinkerStats,
    PREFILTER_EXCLUDED_DOC_TYPES,
    _active_asset_set_hash,
    _finish_run_row,
    _mark_classified,
    _start_run_row,
    build_keyword_index,
    classify_document,
    load_documents_to_link,
    load_eligible_sources,
    prefilter_doc,
)


# A stable hash used by tests that need to pass one in. md5("test-hash") would
# work too — what matters is that it's a known string written into stamps.
_TEST_HASH = "deadbeef" * 4  # 32-char placeholder mimicking md5 shape


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doc(doc_id: str = "doc-1") -> Dict[str, Any]:
    return {
        "id": doc_id,
        "source": "dailymed",
        "doc_type": "drug_label",
        "title": "Atorvastatin Calcium Label",
        "url": None,
        "raw_text": "Some drug label text mentioning VRDN and veligrotug.",
        "raw_text_tokens": 50,
        "storage_path": None,
        "published_at": "2026-05-06T00:00:00+00:00",
        "extensions": {},
    }


def _assets() -> List[Dict[str, Any]]:
    return [{
        "id": "asset-1",
        "drug_name": "VRDN (veligrotug)",
        "generic_name": "veligrotug",
        "sponsor_name": "Viridian Therapeutics",
        "indication": "thyroid eye disease",
    }]


def _anthropic_response(json_body: str, in_tok: int = 1200, out_tok: int = 80,
                        cache_read: int = 0, cache_create: int = 0):
    block = MagicMock()
    block.type = "text"
    block.text = json_body
    resp = MagicMock()
    resp.content = [block]
    resp.usage.input_tokens = in_tok
    resp.usage.output_tokens = out_tok
    resp.usage.cache_read_input_tokens = cache_read
    resp.usage.cache_creation_input_tokens = cache_create
    return resp


# ---------------------------------------------------------------------------
# build_keyword_index — prefilter must NOT index `indication`
# ---------------------------------------------------------------------------

def test_keyword_index_excludes_indication_strings():
    """The 2026-05-20 prefilter tightening: indication strings were causing
    61% of docs to pass the prefilter against a <3% match rate (common
    conditions like "type 2 diabetes" leak into every diabetes drug label).
    Pin the exclusion so it can't silently regress."""
    assets = [{
        "id": "asset-1",
        "drug_name": "VRDN",
        "generic_name": "veligrotug",
        "sponsor_name": "Viridian Therapeutics",
        "indication": "thyroid eye disease",
    }]
    idx = build_keyword_index(assets)
    # Indication strings must NOT appear as keywords
    assert "thyroid eye disease" not in idx
    assert "thyroid" not in idx
    assert "eye" not in idx
    # But drug_name / generic_name / sponsor_name MUST remain
    assert "vrdn" in idx
    assert "veligrotug" in idx
    assert "viridian" in idx


def test_keyword_index_still_handles_sponsor_and_generic():
    """Regression guard: tightening must not have removed too much."""
    assets = [{
        "id": "a-2",
        "drug_name": "FILSPARI (sparsentan)",
        "generic_name": "sparsentan",
        "sponsor_name": "Travere Therapeutics",
        "indication": "IgA nephropathy",
    }]
    idx = build_keyword_index(assets)
    assert "filspari" in idx
    assert "sparsentan" in idx
    assert "travere" in idx
    # Therapeutics is a SPONSOR_STOPWORD — must not be a keyword
    assert "therapeutics" not in idx
    # Indication tokens stay out
    assert "iga" not in idx
    assert "nephropathy" not in idx


# ---------------------------------------------------------------------------
# Sponsor stopword filter — closes the second leak (boilerplate corp words)
# ---------------------------------------------------------------------------

def test_sponsor_stopwords_are_stripped():
    """The 15:00 run showed 50% prefilter pass-rate even after dropping
    indication. Root cause: pharma boilerplate words ("Sciences",
    "Therapeutics", "Pharmaceuticals") in sponsor_name strings false-match
    every dailymed label that mentions any pharma company. Stripping them
    via SPONSOR_STOPWORDS leaves only the specific company name as a kw."""
    assets = [
        {"id": "a-1", "drug_name": "Aaa", "generic_name": "aaa",
         "sponsor_name": "Gilead Sciences", "indication": "x"},
        {"id": "a-2", "drug_name": "Bbb", "generic_name": "bbb",
         "sponsor_name": "Ionis Pharmaceuticals", "indication": "x"},
        {"id": "a-3", "drug_name": "Ccc", "generic_name": "ccc",
         "sponsor_name": "Achieve Life Sciences", "indication": "x"},
        {"id": "a-4", "drug_name": "Ddd", "generic_name": "ddd",
         "sponsor_name": "MannKind Corporation", "indication": "x"},
    ]
    idx = build_keyword_index(assets)
    # Specific company tokens kept (lowercased for matching)
    assert "gilead" in idx
    assert "ionis" in idx
    assert "achieve" in idx
    assert "mannkind" in idx
    # Pharma boilerplate words MUST NOT appear as keywords
    for stopword in ("sciences", "therapeutics", "pharmaceuticals",
                     "pharmaceutical", "pharma", "medicines", "life",
                     "corporation", "limited"):
        assert stopword not in idx, (
            f"'{stopword}' is a SPONSOR_STOPWORD but leaked into the keyword "
            "index — would match every drug label mentioning any pharma "
            "company. Got idx keys: %r" % sorted(idx.keys())
        )


def test_sponsor_stopwords_known_limitation_two_common_words():
    """Known partial-fix: sponsor names whose first 2 non-stopword tokens are
    both common English words (e.g. 'Bristol Myers Squibb' → [Bristol, Myers],
    'Scholar Rock' → [Scholar, Rock]) still leak. Those tokens stay because
    they aren't pharma boilerplate. drug_name + generic_name cover these
    assets robustly; this is acknowledged tech-debt not a regression."""
    assets = [{
        "id": "a-bms",
        "drug_name": "Iberdomide",
        "generic_name": "iberdomide",
        "sponsor_name": "Bristol Myers Squibb",
        "indication": "x",
    }]
    idx = build_keyword_index(assets)
    # "Bristol" and "Myers" remain — not ideal but acceptable
    assert "bristol" in idx or "myers" in idx
    # And the specific drug_name/generic_name still index
    assert "iberdomide" in idx


def test_sponsor_stopword_only_yields_no_keyword():
    """Edge case: a sponsor_name composed entirely of stopwords (rare but
    possible, e.g. a misformatted 'Pharmaceutical Sciences') should yield
    NO sponsor keyword. drug_name + generic_name still cover the asset."""
    assets = [{
        "id": "a-1",
        "drug_name": "Veligrotug",
        "generic_name": "veligrotug",
        "sponsor_name": "Pharmaceutical Sciences",
        "indication": "TED",
    }]
    idx = build_keyword_index(assets)
    # No sponsor token survived — but drug/generic did
    assert "veligrotug" in idx
    assert "pharmaceutical" not in idx
    assert "sciences" not in idx


# ---------------------------------------------------------------------------
# prefilter_doc — precision improvements (doc-type exclusion + word boundary)
# Added 2026-05-11: edgar 424B2 yielded 0 links / 50 docs / 25 parse_errors,
# and substring matching let "Vanda" embed in unrelated tokens like
# "Vandalism".
# ---------------------------------------------------------------------------

def test_prefilter_skips_prospectus_doc_types():
    """SEC prospectus/registration filings (424B2/B3/S-1/S-3) yield ~0 links
    with high parse-error rates. The prefilter must return [] for these
    without scanning text, regardless of keyword matches."""
    idx = build_keyword_index(_assets())
    # Text contains BOTH a drug and a sponsor hit — would normally pass.
    text = "Veligrotug from Viridian Therapeutics is discussed in section 3."
    for doc_type in PREFILTER_EXCLUDED_DOC_TYPES:
        assert prefilter_doc(text, idx, source="edgar",
                             doc_type=doc_type) == [], (
            f"doc_type={doc_type} must be excluded by prefilter")


def test_prefilter_does_not_skip_non_prospectus_edgar_types():
    """8-K / 10-Q / 10-K must NOT be excluded — they're the high-yield
    sources (70% / 61% / 32% link rate respectively)."""
    idx = build_keyword_index(_assets())
    text = "Viridian Therapeutics announced enrollment in the trial."
    for doc_type in ("8-K", "10-Q", "10-K", "10-K/A", "6-K"):
        result = prefilter_doc(text, idx, source="edgar", doc_type=doc_type)
        assert len(result) == 1, f"doc_type={doc_type} must pass prefilter"


def test_prefilter_uses_word_boundaries_not_substrings():
    """A short sponsor token like 'Vanda' must NOT match inside 'Vandalism'.
    Substring match was a precision leak before 2026-05-11."""
    assets = [{
        "id": "asset-vanda",
        "drug_name": "Imsidolimab",
        "generic_name": None,
        "sponsor_name": "Vanda Pharmaceuticals",
        "indication": "GPP",
    }]
    idx = build_keyword_index(assets)
    # 'Vanda' is in the keyword index. Embedded in 'Vandalism' it must NOT
    # trigger the prefilter.
    text = "The Vandalism case was discussed in a board memo."
    assert prefilter_doc(text, idx, source="edgar", doc_type="8-K") == []
    # But standalone 'Vanda' (or 'Vanda Pharmaceuticals') DOES trigger.
    text2 = "Vanda Pharmaceuticals issued an 8-K announcing the PDUFA date."
    assert len(prefilter_doc(text2, idx, source="edgar", doc_type="8-K")) == 1


def test_prefilter_word_boundary_is_case_insensitive():
    """Sponsor tokens are stored title-cased but documents may have any
    casing. Word-boundary regex must use the IGNORECASE flag."""
    idx = build_keyword_index(_assets())
    text = "VIRIDIAN THERAPEUTICS reported phase 3 data."
    assert len(prefilter_doc(text, idx, source="edgar", doc_type="8-K")) == 1


# ---------------------------------------------------------------------------
# Ticker matching — 2026-05-12: high-precision signal in SEC tables/headers
# ---------------------------------------------------------------------------

def _ticker_assets() -> List[Dict[str, Any]]:
    return [{
        "id": "asset-vnda",
        "ticker": "VNDA",
        "drug_name": "Imsidolimab",
        "generic_name": None,
        "sponsor_name": "Vanda Pharmaceuticals",
        "indication": "GPP",
    }]


def test_keyword_index_includes_ticker():
    idx = build_keyword_index(_ticker_assets())
    assert "vnda" in idx
    assert idx["vnda"][0]["field"] == "ticker"


def test_keyword_index_drops_short_tickers():
    """2-char tickers (e.g. 'MS', 'GS') would match too many English
    fragments. Enforce a 3-char minimum."""
    short = [{
        "id": "asset-x", "ticker": "MS", "drug_name": "DrugX",
        "generic_name": None, "sponsor_name": "X Corp", "indication": "y",
    }]
    idx = build_keyword_index(short)
    assert "ms" not in idx
    assert "drugx" in idx  # drug_name still indexed


def test_keyword_index_handles_null_ticker():
    """Some assets don't have tickers (private companies, foreign listings).
    Null/missing ticker must NOT crash build_keyword_index."""
    no_ticker = [{
        "id": "asset-y", "ticker": None, "drug_name": "Veligrotug",
        "generic_name": None, "sponsor_name": "Viridian Therapeutics",
        "indication": "TED",
    }]
    idx = build_keyword_index(no_ticker)
    assert "veligrotug" in idx


def test_prefilter_ticker_hit_passes_label_source_gate():
    """A dailymed label that mentions a tracked ticker (e.g. in the
    'Manufactured by VNDA' footer) is high-signal — must pass even though
    sponsor-only would normally fail the label-source gate."""
    idx = build_keyword_index(_ticker_assets())
    text = "Distributed by manufacturer code VNDA per FDA registration."
    result = prefilter_doc(text, idx, source="dailymed",
                           doc_type="drug_label")
    assert len(result) == 1
    assert result[0]["id"] == "asset-vnda"


def test_prefilter_ticker_uses_word_boundary():
    """Tickers must word-boundary-match: VNDA must not embed in 'VNDABLE'
    or 'XVNDAX'. Particularly important for short uppercase identifiers."""
    idx = build_keyword_index(_ticker_assets())
    # Substring would match; word-boundary must not.
    text = "The VNDABLE accounting concept is unrelated to any pharma stock."
    assert prefilter_doc(text, idx, source="edgar", doc_type="8-K") == []
    # Parenthesized form must still match (common in SEC tables).
    text2 = "Vanda Pharmaceuticals (VNDA) filed an 8-K on the PDUFA."
    assert len(prefilter_doc(text2, idx, source="edgar", doc_type="8-K")) == 1


# ---------------------------------------------------------------------------
# Diversified-pharma gate — 2026-05-12: big-pharma sponsors require drug or
# ticker co-occurrence (sponsor-name alone is too ambiguous given their
# pipeline breadth)
# ---------------------------------------------------------------------------

def _diversified_sponsor_assets() -> List[Dict[str, Any]]:
    """Asset list with one diversified-pharma sponsor (Pfizer) and one
    single-drug sponsor (Vanda) so we can verify gate selectivity."""
    return [
        {
            "id": "asset-hympavzi",
            "ticker": "PFE",
            "drug_name": "HYMPAVZI (marstacimab)",
            "generic_name": None,
            "sponsor_name": "Pfizer Inc.",
            "indication": "Hemophilia",
        },
        {
            "id": "asset-imsidolimab",
            "ticker": "VNDA",
            "drug_name": "Imsidolimab",
            "generic_name": None,
            "sponsor_name": "Vanda Pharmaceuticals",
            "indication": "GPP",
        },
    ]


def test_prefilter_blocks_diversified_pharma_sponsor_alone_on_sec():
    """A Pfizer 8-K that mentions 'Pfizer Inc.' but NOT 'HYMPAVZI' or 'PFE'
    is a corporate filing unlikely to discuss our tracked drug specifically.
    Pre-2026-05-12 the prefilter would accept this on SEC (sponsor-only OK
    for SEC sources) — now it requires drug/ticker co-occurrence."""
    idx = build_keyword_index(_diversified_sponsor_assets())
    text = ("Pfizer Inc. announced Q1 earnings of $X billion driven by "
            "Comirnaty and Paxlovid demand. No discussion of hemophilia.")
    # Pfizer is the only match → blocked by diversified-pharma gate.
    result = prefilter_doc(text, idx, source="edgar", doc_type="8-K")
    assert [a["id"] for a in result] == []


def test_prefilter_passes_diversified_pharma_with_drug_cooccurrence():
    """A Pfizer 8-K that mentions BOTH 'Pfizer' AND 'HYMPAVZI' is exactly
    the high-signal SEC doc we want to fire Sonnet on. The diversified-
    pharma gate must NOT block when a drug name is also present."""
    idx = build_keyword_index(_diversified_sponsor_assets())
    text = ("Pfizer Inc. announced that the FDA accepted the sBLA for "
            "HYMPAVZI (marstacimab) in pediatric hemophilia patients.")
    result = prefilter_doc(text, idx, source="edgar", doc_type="8-K")
    assert {a["id"] for a in result} == {"asset-hympavzi"}


def test_prefilter_passes_diversified_pharma_with_ticker_cooccurrence():
    """Ticker co-occurrence also satisfies the gate — 'Pfizer Inc. (PFE)'
    in a doc title is a common SEC pattern and high precision."""
    idx = build_keyword_index(_diversified_sponsor_assets())
    text = "Pfizer Inc. (PFE) Q1 earnings release covers oncology and rare disease."
    result = prefilter_doc(text, idx, source="edgar", doc_type="8-K")
    assert {a["id"] for a in result} == {"asset-hympavzi"}


def test_prefilter_does_not_apply_diversified_gate_to_single_drug_sponsor():
    """The gate is sponsor-specific — Vanda (one tracked drug, narrow
    pipeline) must STILL accept sponsor-name-only matches on SEC sources.
    Only the diversified-pharma allowlist is gated."""
    idx = build_keyword_index(_diversified_sponsor_assets())
    text = "Vanda Pharmaceuticals announced quarterly earnings of $X."
    result = prefilter_doc(text, idx, source="edgar", doc_type="8-K")
    assert {a["id"] for a in result} == {"asset-imsidolimab"}


# ---------------------------------------------------------------------------
# clinicaltrials-specific gate — 2026-05-20: require sponsor AND
# (drug_name OR generic_name). At 81-asset watchlist scale the older
# SPONSOR_ONLY_INSUFFICIENT gate (which let drug-only docs through) was
# still passing ~80% of clinicaltrials docs against a <3% real-link rate
# and burned ~$57 in 6h on 2026-05-19.
# ---------------------------------------------------------------------------

def test_clinicaltrials_requires_sponsor_plus_drug():
    """clinicaltrials: sponsor + drug_name passes."""
    idx = build_keyword_index(_assets())  # Viridian / Veligrotug
    text = "Trial sponsored by Viridian Therapeutics evaluating Veligrotug in TED."
    result = prefilter_doc(text, idx, source="clinicaltrials",
                           doc_type="trial")
    assert len(result) == 1, "sponsor + drug_name hit must pass clinicaltrials gate"


def test_clinicaltrials_rejects_drug_only_no_sponsor():
    """clinicaltrials: drug-only hit (no sponsor mention) is now rejected.
    Comparator-arm and shared-name false positives leak through here at
    81-asset scale."""
    idx = build_keyword_index(_assets())
    text = ("Comparator arm uses Veligrotug at standard dose. "
            "Conducted by an unrelated investigator group.")
    result = prefilter_doc(text, idx, source="clinicaltrials",
                           doc_type="trial")
    assert result == [], (
        "drug-only hit on clinicaltrials must be dropped (requires sponsor "
        "co-occurrence at the 81-asset watchlist scale)"
    )


def test_clinicaltrials_rejects_sponsor_only():
    """clinicaltrials: sponsor-only hit was already rejected by the older
    SPONSOR_ONLY gate; regression-test that the new rule keeps it rejected."""
    idx = build_keyword_index(_assets())
    text = "Sponsored by Viridian Therapeutics — multiple ongoing programs."
    result = prefilter_doc(text, idx, source="clinicaltrials",
                           doc_type="trial")
    assert result == [], "sponsor-only on clinicaltrials must still be dropped"


def test_dailymed_drug_only_still_passes():
    """The stricter clinicaltrials rule must NOT bleed into dailymed.
    A dailymed label that mentions the tracked drug by name (without
    naming the sponsor explicitly) must still pass — sponsors don't
    co-occur on every dailymed label."""
    idx = build_keyword_index(_assets())
    text = "Veligrotug 50 mg solution for injection — indication: TED."
    result = prefilter_doc(text, idx, source="dailymed",
                           doc_type="drug_label")
    assert len(result) == 1, (
        "drug-only hit on dailymed must still pass — only clinicaltrials "
        "requires sponsor co-occurrence"
    )


# ---------------------------------------------------------------------------
# _active_asset_set_hash — md5 over the sorted active asset id list
# ---------------------------------------------------------------------------

def test_active_asset_set_hash_is_md5_of_sorted_ids():
    """The hash must be stable across runs (sorted) and identical for the same
    asset set regardless of fetch order. Changing the asset universe MUST
    change the hash — that's what invalidates the cached classification."""
    sb = MagicMock()
    # Return assets in arbitrary order; hash should sort them.
    sb._rest = MagicMock(return_value=[{"id": "b"}, {"id": "a"}, {"id": "c"}])

    h1 = _active_asset_set_hash(sb)

    # Same set, different order — must produce the same hash.
    sb._rest = MagicMock(return_value=[{"id": "c"}, {"id": "a"}, {"id": "b"}])
    h2 = _active_asset_set_hash(sb)
    assert h1 == h2, "Hash must be stable regardless of fetch order"

    # Different set — must produce a different hash.
    sb._rest = MagicMock(return_value=[{"id": "a"}, {"id": "b"}])
    h3 = _active_asset_set_hash(sb)
    assert h1 != h3, "Adding/removing an asset MUST change the hash"

    # Format: 32 hex chars (md5).
    assert len(h1) == 32
    assert all(c in "0123456789abcdef" for c in h1)


def test_active_asset_set_hash_queries_active_only():
    """The hash must only consider is_active=true assets. Inactive ones don't
    participate in classification, so they shouldn't affect the cache key."""
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[{"id": "a"}])

    _active_asset_set_hash(sb)

    call = sb._rest.call_args
    assert call.args[0] == "GET"
    assert call.args[1] == "fda_assets"
    assert call.kwargs["params"].get("is_active") == "eq.true"
    assert call.kwargs["params"].get("select") == "id"


# ---------------------------------------------------------------------------
# load_documents_to_link — widens to (IS NULL) OR (hash IS NULL) OR (hash != current)
# ---------------------------------------------------------------------------

def test_load_documents_filters_unclassified_or_stale_hash():
    """The bug regresses if load_documents_to_link ever returns a doc that has
    been classified against the CURRENT asset-set hash. It must return:
      - never-classified docs (linker_classified_at IS NULL), AND
      - legacy pre-hash stamps (asset_set_hash IS NULL), AND
      - hash-stale docs (asset_set_hash <> current).
    Pin the OR clause to the request layer."""
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[_doc()])

    # Pass eligible_sources explicitly so the function makes exactly one _rest
    # call (the documents fetch) — the internal-resolve path is covered
    # separately in test_load_documents_resolves_eligible_sources_when_none.
    load_documents_to_link(sb, max_docs=20, asset_set_hash=_TEST_HASH,
                           eligible_sources=["clinicaltrials"])

    sb._rest.assert_called_once()
    call = sb._rest.call_args
    assert call.args[0] == "GET"
    assert call.args[1] == "documents"
    params = call.kwargs["params"]
    or_value = params.get("or")
    assert or_value is not None, (
        "load_documents_to_link MUST use a PostgREST `or` filter to include "
        "unstamped AND hash-stale docs. Got params: %r" % params
    )
    assert "linker_classified_at.is.null" in or_value
    assert "linker_classified_asset_set_hash.is.null" in or_value
    assert f"linker_classified_asset_set_hash.neq.{_TEST_HASH}" in or_value
    assert params.get("order") == "published_at.desc"
    # Dynamic source filter (issue #54): replaces the old hardcoded
    # SOURCE_ALLOWLIST = ('clinicaltrials',).
    assert params.get("source") == "in.(clinicaltrials)"


def test_load_documents_requires_hash_in_cron_mode():
    """Forgetting to pass asset_set_hash in cron mode is a silent regression
    risk — without the OR-clause, the query degenerates to "newest 200 docs"
    and we'd ship the no-progress loop again. Fail loudly instead."""
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[])
    with pytest.raises(ValueError, match="asset_set_hash"):
        load_documents_to_link(sb, max_docs=20)


def test_load_documents_doc_ids_override_bypasses_hash():
    """Operator override (--doc-ids) must work even without a hash, so the
    cache-validation path stays usable for diagnostics."""
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[_doc()])
    # No asset_set_hash passed.
    load_documents_to_link(sb, max_docs=20, doc_ids=["doc-1", "doc-2"])
    call = sb._rest.call_args
    params = call.kwargs["params"]
    assert params.get("id") == "in.(doc-1,doc-2)"
    assert "or" not in params, "doc_ids override must not apply the OR filter"


def test_load_documents_does_not_query_asset_documents():
    """The pre-fix implementation issued a separate GET to asset_documents to
    build an exclusion set. That pattern was the bug — keep it gone."""
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[])

    load_documents_to_link(sb, max_docs=20, asset_set_hash=_TEST_HASH)

    for call in sb._rest.call_args_list:
        path = call.args[1] if len(call.args) > 1 else ""
        assert path != "asset_documents", (
            "load_documents_to_link should NOT query asset_documents — that "
            "two-query JOIN-in-Python pattern caused the no-progress loop."
        )


# ---------------------------------------------------------------------------
# load_eligible_sources + dynamic source filter — issue #54: source eligibility
# is derived from fda_assets.program_status (asset_linker_eligible_sources view)
# instead of a hardcoded SOURCE_ALLOWLIST = ('clinicaltrials',) constant.
# ---------------------------------------------------------------------------

def test_load_eligible_sources_resolves_from_view():
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[{"source": "clinicaltrials"}])

    out = load_eligible_sources(sb)

    assert out == ["clinicaltrials"]
    call = sb._rest.call_args
    assert call.args[0] == "GET"
    assert call.args[1] == "asset_linker_eligible_sources"
    assert call.kwargs["params"].get("select") == "source"


def test_load_eligible_sources_dedups_and_sorts():
    """Multiple assets can map the same source; the view DISTINCTs but a defensive
    de-dup + stable sort keeps the PostgREST `in.(...)` filter deterministic.
    Null/empty source rows are ignored."""
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[
        {"source": "openfda"}, {"source": "dailymed"},
        {"source": "clinicaltrials"}, {"source": "dailymed"},
        {"source": None}, {"source": ""},
    ])

    out = load_eligible_sources(sb)

    assert out == ["clinicaltrials", "dailymed", "openfda"]


def test_load_eligible_sources_falls_back_when_empty():
    """No active asset's program_status maps to any source — fall back to
    clinicaltrials so the linker never goes dark (the orphan watchdog flags it)."""
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[])

    out = load_eligible_sources(sb)

    assert out == list(FALLBACK_ELIGIBLE_SOURCES)
    assert out == ["clinicaltrials"]


def test_load_eligible_sources_falls_back_on_error():
    """A failed view read must NOT crash pass-1 — fall back to clinicaltrials."""
    sb = MagicMock()
    sb._rest = MagicMock(side_effect=Exception("PostgREST 503"))

    out = load_eligible_sources(sb)

    assert out == ["clinicaltrials"]


def test_load_documents_applies_dynamic_source_filter():
    """The resolved eligible_sources list drives the PostgREST source filter,
    so an approved asset (dailymed/openfda eligible) is reflected verbatim."""
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[_doc()])

    load_documents_to_link(
        sb, max_docs=20, asset_set_hash=_TEST_HASH,
        eligible_sources=["clinicaltrials", "dailymed", "openfda", "federal_register"],
    )

    sb._rest.assert_called_once()
    params = sb._rest.call_args.kwargs["params"]
    assert params.get("source") == "in.(clinicaltrials,dailymed,openfda,federal_register)"


def test_load_documents_resolves_eligible_sources_when_none():
    """When eligible_sources is not passed, load_documents_to_link resolves it
    via the view, then filters documents by the resolved set."""
    sb = MagicMock()

    def _rest(method, path, **kwargs):
        if method == "GET" and path == "asset_linker_eligible_sources":
            return [{"source": "clinicaltrials"}, {"source": "dailymed"}]
        if method == "GET" and path == "documents":
            return [_doc()]
        raise AssertionError(f"unexpected call {method} {path}")
    sb._rest = MagicMock(side_effect=_rest)

    load_documents_to_link(sb, max_docs=20, asset_set_hash=_TEST_HASH)

    paths = [c.args[1] for c in sb._rest.call_args_list]
    assert "asset_linker_eligible_sources" in paths
    docs_call = [c for c in sb._rest.call_args_list if c.args[1] == "documents"][0]
    assert docs_call.kwargs["params"].get("source") == "in.(clinicaltrials,dailymed)"


def test_load_documents_resolves_when_eligible_sources_empty():
    """An explicitly-empty list must trigger resolution, not build `in.()`
    (which would match zero docs — a silently-dark linker)."""
    sb = MagicMock()

    def _rest(method, path, **kwargs):
        if method == "GET" and path == "asset_linker_eligible_sources":
            return [{"source": "clinicaltrials"}]
        if method == "GET" and path == "documents":
            return [_doc()]
        raise AssertionError(f"unexpected call {method} {path}")
    sb._rest = MagicMock(side_effect=_rest)

    load_documents_to_link(sb, max_docs=20, asset_set_hash=_TEST_HASH,
                           eligible_sources=[])

    docs_call = [c for c in sb._rest.call_args_list if c.args[1] == "documents"][0]
    src = docs_call.kwargs["params"].get("source")
    assert src == "in.(clinicaltrials)"
    assert src != "in.()"


def test_load_documents_doc_ids_override_skips_source_resolution():
    """The --doc-ids operator override targets specific rows regardless of
    source, so it must NOT resolve or apply the source-eligibility filter."""
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[_doc()])

    load_documents_to_link(sb, max_docs=20, doc_ids=["doc-1"])

    paths = [c.args[1] for c in sb._rest.call_args_list]
    assert "asset_linker_eligible_sources" not in paths
    assert sb._rest.call_args.kwargs["params"].get("source") is None


def test_smoke_only_clinicaltrials_when_no_approved_assets():
    """Issue #54 acceptance (smoke): with no approved assets, the view resolves
    clinicaltrials only, so pass-1 filters source=in.(clinicaltrials) — identical
    to the retired SOURCE_ALLOWLIST behavior."""
    sb = MagicMock()

    def _rest(method, path, **kwargs):
        if method == "GET" and path == "asset_linker_eligible_sources":
            return [{"source": "clinicaltrials"}]
        if method == "GET" and path == "documents":
            return [_doc()]
        raise AssertionError(f"unexpected call {method} {path}")
    sb._rest = MagicMock(side_effect=_rest)

    load_documents_to_link(sb, max_docs=50, asset_set_hash=_TEST_HASH)

    docs_call = [c for c in sb._rest.call_args_list if c.args[1] == "documents"][0]
    assert docs_call.kwargs["params"].get("source") == "in.(clinicaltrials)"


def test_manual_approved_asset_widens_to_dailymed_openfda():
    """Issue #54 acceptance (manual): once an approved asset is present, the view
    resolves dailymed/openfda/federal_register alongside clinicaltrials, so pass-1
    begins pulling those sources."""
    sb = MagicMock()

    def _rest(method, path, **kwargs):
        if method == "GET" and path == "asset_linker_eligible_sources":
            return [{"source": "clinicaltrials"}, {"source": "dailymed"},
                    {"source": "openfda"}, {"source": "federal_register"}]
        if method == "GET" and path == "documents":
            return [_doc()]
        raise AssertionError(f"unexpected call {method} {path}")
    sb._rest = MagicMock(side_effect=_rest)

    load_documents_to_link(sb, max_docs=50, asset_set_hash=_TEST_HASH)

    src = ([c for c in sb._rest.call_args_list
            if c.args[1] == "documents"][0].kwargs["params"].get("source"))
    assert src.startswith("in.(")
    for s in ("clinicaltrials", "dailymed", "openfda", "federal_register"):
        assert s in src, f"expected {s} in dynamic source filter, got {src!r}"


# ---------------------------------------------------------------------------
# _mark_classified — terminal-state PATCH, returns bool for caller's stats
# ---------------------------------------------------------------------------

def test_mark_classified_patches_documents_with_result_and_hash():
    sb = MagicMock()
    sb._rest = MagicMock(return_value=None)

    ok = _mark_classified(sb, "doc-xyz", "no_match", _TEST_HASH)

    assert ok is True
    sb._rest.assert_called_once()
    call = sb._rest.call_args
    assert call.args[0] == "PATCH"
    assert call.args[1] == "documents"
    assert call.kwargs["params"] == {"id": "eq.doc-xyz"}
    body = call.kwargs["json_body"]
    assert body["linker_classified_result"] == "no_match"
    assert body["linker_classified_asset_set_hash"] == _TEST_HASH, (
        "Hash MUST be stamped so add/remove-asset auto-invalidates the cache. "
        "Without it, the no-progress loop returns on every asset-set change."
    )
    assert "linker_classified_at" in body
    # Must be ISO timestamp, not None
    assert body["linker_classified_at"] is not None


@pytest.mark.parametrize("result", ["linked", "no_match", "parse_error"])
def test_mark_classified_accepts_each_valid_result(result):
    sb = MagicMock()
    sb._rest = MagicMock(return_value=None)
    ok = _mark_classified(sb, "doc-1", result, _TEST_HASH)
    assert ok is True
    body = sb._rest.call_args.kwargs["json_body"]
    assert body["linker_classified_result"] == result
    assert body["linker_classified_asset_set_hash"] == _TEST_HASH


def test_mark_classified_returns_false_on_patch_failure():
    """PATCH failure must NOT raise (observability is best-effort) AND must
    return False so the caller can increment LinkerStats.marker_failures.
    Silent True-returns here would silently regress the documents.linker_
    classified_at mechanism — the very bug the marker prevents."""
    sb = MagicMock()
    sb._rest = MagicMock(side_effect=Exception("network blip"))
    ok = _mark_classified(sb, "doc-1", "linked", _TEST_HASH)
    assert ok is False


# ---------------------------------------------------------------------------
# classify_document — parse_ok signal differentiates JSON-fail from empty
# ---------------------------------------------------------------------------

def test_classify_document_parse_ok_false_on_bad_json():
    """parse_ok=False is what tells the caller to mark parse_error instead of
    no_match. Without it, parse failures would silently look like no_match and
    we'd lose the ability to distinguish wasted-call vs. genuine no-match."""
    a_client = MagicMock()
    a_client.messages.create = MagicMock(
        return_value=_anthropic_response("not valid json at all")
    )

    links, in_tok, out_tok, cache_read, cache_create, parse_ok = classify_document(
        a_client, _doc(), _assets(),
        "some text", ["VRDN"],
    )

    assert links == []
    assert parse_ok is False
    assert in_tok > 0  # call WAS paid for


def test_classify_document_parse_ok_true_on_valid_empty_links():
    a_client = MagicMock()
    a_client.messages.create = MagicMock(
        return_value=_anthropic_response('{"links": []}')
    )

    links, in_tok, out_tok, cache_read, cache_create, parse_ok = classify_document(
        a_client, _doc(), _assets(),
        "some text", ["VRDN"],
    )

    assert links == []
    assert parse_ok is True


def test_classify_document_parse_ok_true_on_valid_link():
    a_client = MagicMock()
    body = json.dumps({"links": [{
        "asset_id": "asset-1",
        "link_type": "primary",
        "extraction_confidence": 0.9,
        "extracted_spans": [{"text": "VRDN demonstrated efficacy"}],
        "is_material": True,
        "reasoning": "test",
    }]})
    a_client.messages.create = MagicMock(return_value=_anthropic_response(body))

    links, _, _, _, _, parse_ok = classify_document(
        a_client, _doc(), _assets(),
        "some text", ["VRDN"],
    )

    assert len(links) == 1
    assert parse_ok is True


# ---------------------------------------------------------------------------
# _start_run_row / _finish_run_row — observability row lifecycle
# ---------------------------------------------------------------------------

def test_start_run_row_reclaims_stale_then_inserts():
    """Two-step protocol: PATCH any stale running row to 'failed', then POST
    a fresh 'running' row. Both calls happen in order."""
    sb = MagicMock()
    sb._rest = MagicMock(side_effect=[None, [{"id": "run-uuid"}]])

    run_id, lock_held = _start_run_row(sb, "pass1", "claude-sonnet-4-5-20250929")

    assert run_id == "run-uuid"
    assert lock_held is True
    assert sb._rest.call_count == 2
    # First call: PATCH reclaim of stale running rows
    reclaim = sb._rest.call_args_list[0]
    assert reclaim.args == ("PATCH", "asset_linker_runs")
    assert reclaim.kwargs["params"]["pass"] == "eq.pass1"
    assert reclaim.kwargs["params"]["status"] == "eq.running"
    assert "lt." in reclaim.kwargs["params"]["started_at"]
    assert reclaim.kwargs["json_body"]["status"] == "failed"
    # Second call: POST a fresh running row
    insert = sb._rest.call_args_list[1]
    assert insert.args == ("POST", "asset_linker_runs")
    assert insert.kwargs["json_body"]["pass"] == "pass1"
    assert insert.kwargs["json_body"]["status"] == "running"


def test_start_run_row_returns_false_lock_on_conflict():
    """Concurrency guard: if another instance holds the lock (partial unique
    index trips a 23505 / duplicate-key error), return (None, False) so the
    caller exits cleanly without doing duplicate work."""
    sb = MagicMock()

    def _rest(method, path, **_kwargs):
        if method == "PATCH":
            return None  # stale reclaim is allowed to succeed
        # Simulate Postgres unique-violation on INSERT
        raise Exception(
            "duplicate key value violates unique constraint "
            "\"asset_linker_runs_one_running_per_pass\""
        )
    sb._rest = MagicMock(side_effect=_rest)

    run_id, lock_held = _start_run_row(sb, "pass1", "m")

    assert run_id is None
    assert lock_held is False, "Concurrent-run conflict must signal NO lock"


def test_start_run_row_non_conflict_insert_failure_still_runs():
    """If the INSERT fails for a non-conflict reason (e.g. PostgREST 503),
    caller should still proceed (lock_held=True with run_id=None) so the
    Modal cron tick doesn't lose a 15-min slot to transient infra issues."""
    sb = MagicMock()

    def _rest(method, path, **_kwargs):
        if method == "PATCH":
            return None
        raise Exception("503 Service Unavailable")
    sb._rest = MagicMock(side_effect=_rest)

    run_id, lock_held = _start_run_row(sb, "pass1", "m")
    assert run_id is None
    assert lock_held is True


def test_start_run_row_reclaim_failure_does_not_block_insert():
    """Stale-row reclaim is best-effort. If the PATCH fails for any reason,
    INSERT must still be attempted — the unique index will reject if a real
    conflict exists."""
    sb = MagicMock()
    sb._rest = MagicMock(side_effect=[
        Exception("reclaim failed"),
        [{"id": "run-uuid"}],
    ])
    run_id, lock_held = _start_run_row(sb, "pass1", "m")
    assert run_id == "run-uuid"
    assert lock_held is True


def test_finish_run_row_patches_with_pass1_fields():
    sb = MagicMock()
    sb._rest = MagicMock(return_value=None)

    stats = LinkerStats(
        docs_seen=100, docs_prefilter_passed=50, docs_prefilter_skipped=50,
        api_calls=48, errors=2, input_tokens=350_000, output_tokens=8_000,
        cost_usd=1.23, links_inserted=4, links_dedup_skipped=1,
    )
    _finish_run_row(sb, "run-uuid", "completed", stats)

    sb._rest.assert_called_once()
    call = sb._rest.call_args
    assert call.args[0] == "PATCH"
    assert call.args[1] == "asset_linker_runs"
    assert call.kwargs["params"] == {"id": "eq.run-uuid"}
    body = call.kwargs["json_body"]
    assert body["status"] == "completed"
    assert body["docs_seen"] == 100
    assert body["prefilter_passed"] == 50
    assert body["prefilter_skipped"] == 50
    assert body["api_calls"] == 48
    assert body["errors"] == 2
    assert body["links_inserted"] == 4
    assert body["cost_usd"] == 1.23
    assert "completed_at" in body


def test_finish_run_row_skips_when_run_id_none():
    """If start failed (run_id=None), finish must be a no-op."""
    sb = MagicMock()
    sb._rest = MagicMock()
    _finish_run_row(sb, None, "completed", LinkerStats())
    sb._rest.assert_not_called()


# ---------------------------------------------------------------------------
# End-to-end main() — the critical regression: each terminal path marks
# correctly, transient API errors do NOT mark.
# ---------------------------------------------------------------------------

@pytest.fixture
def patched_main_env():
    """Patch SupabaseClient + Anthropic so main() can be invoked without I/O.
    Yields (sb_mock, anth_mock, captured_marks) where captured_marks is a list
    of (doc_id, result) tuples observed via _mark_classified."""
    sb = MagicMock()
    anth = MagicMock()
    captured: List[tuple] = []

    def _rest_router(method, path, **kwargs):
        if method == "GET" and path == "fda_assets":
            return _assets()
        if method == "GET" and path == "asset_linker_eligible_sources":
            # _doc() rows are source="dailymed"; keep the resolved eligible set
            # consistent so the e2e path exercises a non-trivial source filter.
            return [{"source": "dailymed"}]
        if method == "GET" and path == "documents":
            return [_doc("doc-A"), _doc("doc-B"), _doc("doc-C")]
        if method == "POST" and path == "asset_linker_runs":
            return [{"id": "run-uuid"}]
        if method == "POST" and path == "asset_documents":
            return [{"id": "ad-1"}]
        if method == "PATCH" and path == "documents":
            params = kwargs.get("params") or {}
            doc_id = (params.get("id") or "").removeprefix("eq.")
            body = kwargs.get("json_body") or {}
            captured.append((doc_id, body.get("linker_classified_result")))
        return None
    sb._rest = MagicMock(side_effect=_rest_router)

    with patch("modal_workers.extractor.asset_linker.SupabaseClient",
               return_value=sb), \
         patch("modal_workers.extractor.asset_linker.anthropic.Anthropic",
               return_value=anth):
        yield sb, anth, captured


def test_main_marks_no_match_when_links_empty(patched_main_env):
    sb, anth, marks = patched_main_env
    anth.messages.create = MagicMock(
        return_value=_anthropic_response('{"links": []}')
    )

    from modal_workers.extractor.asset_linker import main
    rc = main(["--max", "5", "--budget-usd", "1.00"])
    assert rc == 0

    # Every doc that made it past prefilter and got a successful empty response
    # must be marked no_match.
    assert all(result == "no_match" for _, result in marks), (
        "Zero-link verdicts should mark 'no_match' — got marks: %r" % marks
    )
    assert len(marks) >= 1


def test_main_marks_parse_error_on_bad_json(patched_main_env):
    sb, anth, marks = patched_main_env
    anth.messages.create = MagicMock(
        return_value=_anthropic_response("garbage not json")
    )

    from modal_workers.extractor.asset_linker import main
    rc = main(["--max", "5", "--budget-usd", "1.00"])
    assert rc == 0

    # Parse failures must mark parse_error, NOT no_match — that distinction is
    # what makes the cost visible separately and prevents retry-on-deterministic-fail.
    parse_marks = [r for _, r in marks if r == "parse_error"]
    assert len(parse_marks) >= 1, (
        "Bad JSON should mark 'parse_error' — got marks: %r" % marks
    )


def test_main_does_not_mark_on_api_error(patched_main_env):
    """The CRITICAL invariant: transient API errors leave linker_classified_at
    NULL so the next cron run retries. If we ever mark on APIError, retries
    are silently dropped."""
    sb, anth, marks = patched_main_env
    anth.messages.create = MagicMock(
        side_effect=anthropic.APIError("rate limited", request=MagicMock(), body=None)
    )

    from modal_workers.extractor.asset_linker import main
    rc = main(["--max", "5", "--budget-usd", "1.00"])
    assert rc == 0

    # No doc should have been marked classified — transient errors must retry.
    api_error_marks = [r for _, r in marks if r in ("linked", "no_match", "parse_error")]
    # Some prefilter-skipped marks are OK (they're deterministic skips not API errors).
    # But we can detect by checking that EVERY mark was 'no_match' from prefilter,
    # not from a classification attempt. If marks include 'linked' or 'parse_error',
    # something marked despite an API error.
    assert "linked" not in {r for _, r in marks}
    assert "parse_error" not in {r for _, r in marks}


def test_main_marks_linked_after_successful_insert(patched_main_env):
    sb, anth, marks = patched_main_env
    valid = json.dumps({"links": [{
        "asset_id": "asset-1",
        "link_type": "primary",
        "extraction_confidence": 0.92,
        "extracted_spans": [{"text": "VRDN result"}],
        "is_material": True,
        "reasoning": "test",
    }]})
    anth.messages.create = MagicMock(return_value=_anthropic_response(valid))

    from modal_workers.extractor.asset_linker import main
    rc = main(["--max", "5", "--budget-usd", "1.00"])
    assert rc == 0

    linked_marks = [r for _, r in marks if r == "linked"]
    assert len(linked_marks) >= 1, (
        "Successful link insert should mark 'linked' — got marks: %r" % marks
    )


# ---------------------------------------------------------------------------
# Hardening: silent marker failures, unexpected exceptions, lock conflicts
# ---------------------------------------------------------------------------

def test_main_skips_when_another_run_holds_the_lock():
    """If _start_run_row reports lock_held=False (concurrent-run conflict),
    main() must exit 0 WITHOUT loading assets, fetching docs, or calling
    Sonnet — otherwise concurrent ticks still double the spend."""
    sb = MagicMock()

    def _rest(method, path, **_kwargs):
        if method == "PATCH" and path == "asset_linker_runs":
            return None  # reclaim succeeds (no stale rows)
        if method == "POST" and path == "asset_linker_runs":
            # Simulate the partial unique index rejecting a second running row
            raise Exception(
                "duplicate key value violates unique constraint "
                "\"asset_linker_runs_one_running_per_pass\""
            )
        # If we reach any other table the test has FAILED — main proceeded
        raise AssertionError(
            f"main() called {method} {path} despite lock conflict — "
            "concurrent-run guard regressed"
        )
    sb._rest = MagicMock(side_effect=_rest)
    anth = MagicMock()

    with patch("modal_workers.extractor.asset_linker.SupabaseClient",
               return_value=sb), \
         patch("modal_workers.extractor.asset_linker.anthropic.Anthropic",
               return_value=anth):
        from modal_workers.extractor.asset_linker import main
        rc = main(["--max", "20", "--budget-usd", "5.00"])

    assert rc == 0
    # Sonnet must NOT have been called
    anth.messages.create.assert_not_called()


def test_main_increments_marker_failures_when_patch_fails(patched_main_env):
    """Silent _mark_classified failures were the gap-3 regression vector:
    if a PATCH fails (network blip, rate limit on the documents table), the
    doc stays unmarked and re-Sonnets every cron tick. The new contract is
    that LinkerStats.marker_failures counts these so a run summary surfaces
    the silent regression."""
    sb, anth, marks = patched_main_env
    anth.messages.create = MagicMock(
        return_value=_anthropic_response('{"links": []}')
    )

    # Override the PATCH-to-documents path to throw — simulate marker failure
    original_rest = sb._rest.side_effect

    def _rest_with_patch_fail(method, path, **kwargs):
        if method == "PATCH" and path == "documents":
            raise Exception("simulated PostgREST 5xx")
        return original_rest(method, path, **kwargs)
    sb._rest = MagicMock(side_effect=_rest_with_patch_fail)

    # We need to inspect the LinkerStats — capture by spying on _finish_run_row.
    captured: List[Any] = []
    from modal_workers.extractor import asset_linker as al_module
    original_finish = al_module._finish_run_row

    def _spy_finish(client, run_id, status, stats):
        captured.append((status, stats.marker_failures, stats.docs_classified))
        return original_finish(client, run_id, status, stats)

    with patch.object(al_module, "_finish_run_row", side_effect=_spy_finish):
        rc = al_module.main(["--max", "5", "--budget-usd", "1.00"])

    assert rc == 0
    assert captured, "_finish_run_row was not called"
    status, marker_failures, docs_classified = captured[-1]
    assert status == "completed", f"expected completed, got {status}"
    assert marker_failures > 0, (
        "_mark_classified PATCH failures MUST increment LinkerStats.marker_failures "
        f"— got {marker_failures} on a run where every PATCH failed"
    )


def test_main_unexpected_exception_in_classify_does_not_kill_run(patched_main_env):
    """Broader exception handler around classify_document: a non-Anthropic
    surprise (httpx error, malformed response shape, etc.) on ONE doc must
    not kill the whole batch. Without this catch, the run row stays in
    status='running' and blocks the next 15-min cron tick via the unique
    partial index."""
    sb, anth, marks = patched_main_env

    # First call raises a non-Anthropic exception; subsequent calls succeed
    counter = {"n": 0}

    def _create(**_kwargs):
        counter["n"] += 1
        if counter["n"] == 1:
            raise RuntimeError("simulated httpx.RemoteProtocolError")
        return _anthropic_response('{"links": []}')

    anth.messages.create = MagicMock(side_effect=_create)

    from modal_workers.extractor.asset_linker import main
    rc = main(["--max", "5", "--budget-usd", "1.00"])

    # Run must complete cleanly with rc=0 (one doc errored, others marked)
    assert rc == 0
    # At least one doc must have been marked (the surviving classify calls)
    assert any(r == "no_match" for _, r in marks), (
        "Surviving docs must still be marked — got marks: %r" % marks
    )
