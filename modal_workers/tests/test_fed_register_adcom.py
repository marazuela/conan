"""Tests for fed_register_adcom fetcher (catalyst_universe writer).

Pure-Python: the upstream helper (`fetch_advisory_committee_meetings`) and
`upsert_catalyst_universe_row` are stubbed via monkeypatch. Asserts the
non-meeting filter, the upsert counts envelope, and the row shape mapped
into catalyst_universe.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List

import pytest

from modal_workers.fetchers.universe import fed_register_adcom as M
from modal_workers.shared.fda_advisory_calendar import Meeting


def _meeting(
    *,
    meeting_date: str | None,
    title: str,
    committee: str | None = "ODAC",
    drug_candidates: List[str] | None = None,
    agenda_excerpt: str = "",
    publication_date: str = "2026-04-15",
    source_url: str = "https://www.federalregister.gov/d/test",
    abstract: str = "",
) -> Meeting:
    return Meeting(
        publication_date=publication_date,
        meeting_date=meeting_date,
        title=title,
        abstract=abstract,
        committee=committee,
        source_url=source_url,
        drug_candidates=drug_candidates or [],
        agenda_excerpt=agenda_excerpt,
    )


class _StubClient:
    """Stub SupabaseClient — fed_register_adcom doesn't call any client REST
    method directly; it delegates to upsert_catalyst_universe_row, which is
    monkeypatched in each test."""


@pytest.fixture
def captured_upserts(monkeypatch):
    """Replace upsert_catalyst_universe_row with a recorder. Returns the list
    that captures every call's kwargs."""
    calls: List[Dict[str, Any]] = []

    def fake_upsert(_client, **kwargs):
        calls.append(kwargs)
        return "catalyst-id-stub"

    monkeypatch.setattr(M, "upsert_catalyst_universe_row", fake_upsert)
    return calls


def _patch_helper(monkeypatch, meetings: List[Meeting]) -> None:
    def fake_fetch(**_):
        return meetings
    monkeypatch.setattr(M, "fetch_advisory_committee_meetings", fake_fetch)


# ---------------------------------------------------------------------------
# Envelope counts
# ---------------------------------------------------------------------------

def test_fetch_skips_meeting_without_date(monkeypatch, captured_upserts):
    _patch_helper(monkeypatch, [
        _meeting(meeting_date=None, title="Some procedural notice"),
    ])
    res = M.fetch(_StubClient(), start_date=date(2026, 4, 1), end_date=date(2026, 5, 1))
    assert res["fetched"] == 1
    assert res["upserted"] == 0
    assert res["skipped"] == 1
    assert res["errors"] == []
    assert captured_upserts == []


def test_fetch_skips_renewal_charter_titles(monkeypatch, captured_upserts):
    _patch_helper(monkeypatch, [
        _meeting(meeting_date="2026-06-01",
                 title="Blood Products Advisory Committee; Renewal"),
        _meeting(meeting_date="2026-06-15",
                 title="Advisory Committee Charter Withdrawal"),
        _meeting(meeting_date="2026-07-01",
                 title="Agency Information Collection Activities"),
        _meeting(meeting_date="2026-07-15",
                 title="ChemoCentryx, Inc.; Proposal To Withdraw Approval ...; "
                        "Opportunity for a Hearing"),
    ])
    res = M.fetch(_StubClient(), start_date=date(2026, 4, 1), end_date=date(2026, 5, 1))
    assert res["fetched"] == 4
    assert res["upserted"] == 0
    assert res["skipped"] == 4
    assert captured_upserts == []


def test_fetch_upserts_real_meeting_notices(monkeypatch, captured_upserts):
    _patch_helper(monkeypatch, [
        _meeting(meeting_date="2026-04-30",
                 title="Oncologic Drugs Advisory Committee; Notice of Meeting; "
                        "Establishment of a Public Docket",
                 committee="ODAC",
                 drug_candidates=["camizestrant", "capivasertib", "truqap"]),
        _meeting(meeting_date="2026-06-18",
                 title="Vaccines and Related Biological Products Advisory Committee; "
                        "Notice of Meeting",
                 committee="VRBPAC",
                 drug_candidates=["mflusiva", "moderna"]),
    ])
    res = M.fetch(_StubClient(), start_date=date(2026, 4, 1), end_date=date(2026, 5, 1))
    assert res["fetched"] == 2
    assert res["upserted"] == 2
    assert res["skipped"] == 0
    assert len(captured_upserts) == 2


def test_fetch_dry_run_makes_no_upserts(monkeypatch, captured_upserts):
    _patch_helper(monkeypatch, [
        _meeting(meeting_date="2026-04-30",
                 title="Oncologic Drugs Advisory Committee; Notice of Meeting"),
    ])
    res = M.fetch(_StubClient(),
                  start_date=date(2026, 4, 1), end_date=date(2026, 5, 1),
                  dry_run=True)
    assert res["fetched"] == 1
    assert res["upserted"] == 1   # accounting only; no actual write
    assert captured_upserts == []


def test_fetch_helper_exception_returns_error_envelope(monkeypatch, captured_upserts):
    def broken_fetch(**_):
        raise RuntimeError("upstream blew up")
    monkeypatch.setattr(M, "fetch_advisory_committee_meetings", broken_fetch)

    res = M.fetch(_StubClient(),
                  start_date=date(2026, 4, 1), end_date=date(2026, 5, 1))
    assert res["fetched"] == 0
    assert res["upserted"] == 0
    assert res["skipped"] == 0
    assert len(res["errors"]) == 1
    assert "upstream blew up" in res["errors"][0]["error"]
    assert captured_upserts == []


# ---------------------------------------------------------------------------
# Row shape
# ---------------------------------------------------------------------------

def test_map_meeting_to_row_shape():
    m = _meeting(
        meeting_date="2026-04-30",
        title="Oncologic Drugs Advisory Committee; Notice of Meeting",
        committee="ODAC",
        drug_candidates=["camizestrant", "truqap"],
        agenda_excerpt="The committee will discuss NDA 220359...",
        publication_date="2026-03-09",
        source_url="https://www.federalregister.gov/d/2026-04497",
        abstract="The FDA announces a forthcoming meeting...",
    )
    row = M._map_meeting_to_row(m)
    assert row["profile"] == "binary_catalyst"
    assert row["catalyst_type"] == "adcomm"
    assert row["catalyst_date"] == "2026-04-30"
    assert row["source_feed"] == "federal_register_adcom"
    assert row["ticker"] is None
    assert row["entity_id"] is None
    assert row["material_outcome"] == "unclear"
    assert row["source_url"] == "https://www.federalregister.gov/d/2026-04497"
    assert row["raw_payload"]["committee"] == "ODAC"
    assert row["raw_payload"]["drug_candidates"] == ["camizestrant", "truqap"]
    assert row["raw_payload"]["publication_date"] == "2026-03-09"
    assert "NDA 220359" in row["raw_payload"]["agenda_excerpt"]


def test_fetch_passes_kwargs_to_upsert(monkeypatch, captured_upserts):
    _patch_helper(monkeypatch, [
        _meeting(meeting_date="2026-04-30",
                 title="Oncologic Drugs Advisory Committee; Notice of Meeting",
                 drug_candidates=["camizestrant"]),
    ])
    M.fetch(_StubClient(), start_date=date(2026, 4, 1), end_date=date(2026, 5, 1))
    assert len(captured_upserts) == 1
    kwargs = captured_upserts[0]
    assert kwargs["profile"] == "binary_catalyst"
    assert kwargs["catalyst_type"] == "adcomm"
    assert kwargs["catalyst_date"] == "2026-04-30"
    assert kwargs["source_feed"] == "federal_register_adcom"
    assert kwargs["material_outcome"] == "unclear"
    assert kwargs["ticker"] is None
    assert "camizestrant" in kwargs["raw_payload"]["drug_candidates"]
