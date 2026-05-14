"""Tests for the fed_register_adcom fetcher.

The fetcher is a thin wrapper around fda_advisory_calendar.fetch_advisory_committee_meetings.
We mock that helper directly with controlled Meeting fixtures, then assert
on the mapped catalyst_universe rows we asked upsert_catalyst_universe_row
to write.

Why this fetcher exists: regulatory_history.py's MCP tools query
catalyst_universe WHERE catalyst_type='adcomm' and got zero rows before
this producer landed. See migration
supabase/migrations/20260527010000_catalyst_universe_adcomm_enum.sql.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from modal_workers.fetchers.universe import fed_register_adcom as mod
from modal_workers.shared.fda_advisory_calendar import Meeting


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk_meeting(
    *,
    meeting_date: str | None = "2026-06-15",
    publication_date: str = "2026-05-01",
    committee: str | None = "ODAC",
    title: str = "Notice of meeting",
    abstract: str = "Meeting on June 15, 2026 to discuss XYZ.",
    drug_candidates: List[str] | None = None,
    source_url: str = "https://www.federalregister.gov/d/2026-99999",
) -> Meeting:
    return Meeting(
        publication_date=publication_date,
        meeting_date=meeting_date,
        title=title,
        abstract=abstract,
        committee=committee,
        source_url=source_url,
        drug_candidates=drug_candidates or [],
    )


class _UpsertRecorder:
    """Captures every upsert_catalyst_universe_row call so tests can
    assert on the rows the fetcher would write. Replaces the real helper
    via monkeypatch."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, client, **kwargs) -> str:
        self.calls.append(kwargs)
        return f"row-{len(self.calls)}"


@pytest.fixture
def fake_client():
    """Return a MagicMock standing in for SupabaseClient."""
    return MagicMock()


@pytest.fixture
def recorder(monkeypatch):
    """Replace upsert_catalyst_universe_row with a recording stub so
    fetch() never reaches Supabase. Returns the recorder for assertions."""
    rec = _UpsertRecorder()
    monkeypatch.setattr(mod, "upsert_catalyst_universe_row", rec)
    return rec


def _patch_meetings(monkeypatch, meetings: List[Meeting]) -> None:
    """Monkeypatch fetch_advisory_committee_meetings to return our fixture."""
    monkeypatch.setattr(
        mod, "fetch_advisory_committee_meetings",
        lambda **_kwargs: meetings,
    )


# ---------------------------------------------------------------------------
# row mapping
# ---------------------------------------------------------------------------

def test_row_mapping_basic(monkeypatch, fake_client, recorder):
    """A single notice with a clean meeting_date produces one upserted row
    with the expected enum values + raw_payload contents."""
    _patch_meetings(monkeypatch, [_mk_meeting(
        meeting_date="2026-07-10",
        publication_date="2026-05-20",
        committee="CRDAC",
        title="Notice of CRDAC meeting on Drug-X",
        abstract="The CRDAC will meet on July 10, 2026.",
        drug_candidates=["Drug-X"],
        source_url="https://www.federalregister.gov/d/2026-77777",
    )])

    result = mod.fetch(
        fake_client,
        start_date=date(2026, 4, 21),
        end_date=date(2026, 5, 21),
    )

    assert result["fetched"] == 1
    assert result["upserted"] == 1
    assert result["skipped"] == 0
    assert result["errors"] == []
    assert len(recorder.calls) == 1

    call = recorder.calls[0]
    assert call["profile"] == "binary_catalyst"
    assert call["catalyst_type"] == "adcomm"
    assert call["catalyst_date"] == "2026-07-10"
    assert call["source_feed"] == "federal_register_adcom"
    assert call["ticker"] is None
    assert call["entity_id"] is None
    assert call["material_outcome"] == "unclear"
    assert call["source_url"] == "https://www.federalregister.gov/d/2026-77777"

    payload = call["raw_payload"]
    assert payload["committee"] == "CRDAC"
    assert payload["drug_candidates"] == ["Drug-X"]
    assert payload["publication_date"] == "2026-05-20"
    assert "CRDAC" in payload["title"]
    assert "July 10, 2026" in payload["abstract"]


def test_drug_candidates_passed_through_payload(monkeypatch, fake_client, recorder):
    """Multiple drug_candidates from the helper land verbatim in raw_payload
    so a downstream entity_linker pass can read them."""
    _patch_meetings(monkeypatch, [_mk_meeting(
        drug_candidates=["Drug-A", "Drug-B", "Drug-C"],
    )])
    mod.fetch(fake_client, start_date=date.today() - timedelta(days=30),
              end_date=date.today())
    assert recorder.calls[0]["raw_payload"]["drug_candidates"] == [
        "Drug-A", "Drug-B", "Drug-C",
    ]


# ---------------------------------------------------------------------------
# date-parse fallthrough
# ---------------------------------------------------------------------------

def test_notices_without_meeting_date_are_skipped(monkeypatch, fake_client, recorder):
    """A notice whose abstract has no parseable date is counted as
    `skipped`, not errored, and never upserted."""
    _patch_meetings(monkeypatch, [
        _mk_meeting(meeting_date=None, title="Procedural notice"),
        _mk_meeting(meeting_date="2026-08-01", title="Real meeting"),
    ])

    result = mod.fetch(
        fake_client,
        start_date=date.today() - timedelta(days=30),
        end_date=date.today(),
    )

    assert result["fetched"] == 2
    assert result["upserted"] == 1
    assert result["skipped"] == 1
    assert result["errors"] == []
    # Only the second notice produced an upsert.
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["catalyst_date"] == "2026-08-01"


def test_no_parseable_meetings_returns_zero(monkeypatch, fake_client, recorder):
    """All notices lack meeting_date → fetcher returns clean envelope
    without raising or recording any upserts."""
    _patch_meetings(monkeypatch, [
        _mk_meeting(meeting_date=None),
        _mk_meeting(meeting_date=None),
        _mk_meeting(meeting_date=None),
    ])
    result = mod.fetch(
        fake_client,
        start_date=date.today() - timedelta(days=30),
        end_date=date.today(),
    )
    assert result == {
        "fetched": 3,
        "upserted": 0,
        "skipped": 3,
        "errors": [],
        "window": {
            "start": (date.today() - timedelta(days=30)).isoformat(),
            "end": date.today().isoformat(),
        },
    }
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# idempotency / dedupe relies on upsert helper. We can't unit-test the
# DB unique key, but we can assert the call shape is consistent across
# repeated invocations so the upsert key inputs are stable.
# ---------------------------------------------------------------------------

def test_dedupe_on_rerun(monkeypatch, fake_client, recorder):
    """Calling fetch twice with the same Meeting fixtures produces calls
    with identical dedupe-key fields (source_feed + catalyst_type +
    ticker + catalyst_date). The DB ON CONFLICT clause makes the second
    write a no-op idempotently — we just verify the inputs match."""
    meetings = [_mk_meeting(meeting_date="2026-09-12", source_url="https://x/1"),
                _mk_meeting(meeting_date="2026-10-04", source_url="https://x/2")]
    _patch_meetings(monkeypatch, meetings)

    r1 = mod.fetch(fake_client,
                   start_date=date.today() - timedelta(days=30),
                   end_date=date.today())
    r2 = mod.fetch(fake_client,
                   start_date=date.today() - timedelta(days=30),
                   end_date=date.today())

    assert r1["upserted"] == r2["upserted"] == 2

    # Same dedupe keys across both runs.
    def _key(c):
        return (c["source_feed"], c["catalyst_type"], c.get("ticker"),
                c["catalyst_date"])
    keys_run1 = [_key(c) for c in recorder.calls[:2]]
    keys_run2 = [_key(c) for c in recorder.calls[2:]]
    assert keys_run1 == keys_run2


# ---------------------------------------------------------------------------
# upstream helper failure handling
# ---------------------------------------------------------------------------

def test_helper_exception_returns_error_envelope(monkeypatch, fake_client, recorder):
    """If fetch_advisory_committee_meetings raises (Federal Register down
    or cache corrupted), the fetcher captures the error at the top level
    and returns an envelope with no partial writes."""
    def _boom(**_kw):
        raise RuntimeError("federal register 503")
    monkeypatch.setattr(mod, "fetch_advisory_committee_meetings", _boom)

    result = mod.fetch(
        fake_client,
        start_date=date.today() - timedelta(days=30),
        end_date=date.today(),
    )

    assert result["fetched"] == 0
    assert result["upserted"] == 0
    assert result["skipped"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["phase"] == "fetch_advisory_committee_meetings"
    assert "federal register 503" in result["errors"][0]["error"]
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# dry-run never writes
# ---------------------------------------------------------------------------

def test_dry_run_does_not_call_upsert(monkeypatch, fake_client, recorder):
    """dry_run=True counts upserts but never calls the recorder. This
    matches the existing fda_adcomm_pdufa.py contract."""
    _patch_meetings(monkeypatch, [_mk_meeting(meeting_date="2026-09-01")])
    result = mod.fetch(
        fake_client,
        start_date=date.today() - timedelta(days=30),
        end_date=date.today(),
        dry_run=True,
    )
    assert result["upserted"] == 1
    assert recorder.calls == []
