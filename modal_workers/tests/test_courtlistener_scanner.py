"""Tests for courtlistener_scanner._docket_to_signal FIGI hard-gate.

The parenthetical ticker regex `[A-Z]{2,5}` matches any 2-5 uppercase acronym,
including non-tickers like UNOPS (UN Office for Project Services). Entities
were being populated with regex-matched junk because the scanner emitted
EntityHints.ticker=ticker_hint even when OpenFIGI couldn't verify the string
was a real issuer identifier. The hard-gate added 2026-04-23 only propagates
ticker_hint to EntityHints when OpenFIGI resolves — otherwise the entity row
gets primary_ticker=NULL. The raw_payload still carries ticker_hint for
forensic trace.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from modal_workers.scanners import courtlistener_scanner as cls


_SCAN_DATE = datetime(2026, 4, 23, tzinfo=timezone.utc)


def _docket(case_name: str, nos: str = "850") -> dict:
    return {
        "_nos_queried": nos,
        "caseName": case_name,
        "dateFiled": "2026-04-20",
        "court": "nysd",
        "docket_id": 123456,
    }


def test_ticker_hint_regex_matches_unops_from_case_name_paren():
    """Sanity: the regex DOES match UNOPS — the whole reason we need the gate."""
    assert cls._extract_ticker_hint("Acme Holdings (UNOPS) v. Defendant LLC") == "UNOPS"
    assert cls._extract_ticker_hint("Jones v. XYZ Corp.") is None  # no paren match


@patch("modal_workers.shared.openfigi_resolver.resolve_ticker")
def test_entity_hint_ticker_is_none_when_openfigi_does_not_resolve(mock_resolve):
    """UNOPS case: regex extracts 'UNOPS', OpenFIGI returns unresolved, and the
    resulting EntityHints has ticker=None + issuer_figi=None. The raw_payload
    keeps ticker_hint for forensic trace."""
    mock_resolve.return_value = MagicMock(resolved=False, issuer_figi=None)

    sig = cls._docket_to_signal(_docket("Acme Holdings (UNOPS) v. Defendant LLC"), _SCAN_DATE)

    assert sig is not None
    assert sig.entity_hints.ticker is None
    assert sig.entity_hints.issuer_figi is None
    assert sig.raw_payload["ticker_hint"] == "UNOPS"  # forensic trace preserved
    assert sig.raw_payload["ticker_hint_source"] == "case_name_paren"


@patch("modal_workers.shared.openfigi_resolver.resolve_ticker")
def test_entity_hint_ticker_set_when_openfigi_resolves(mock_resolve):
    """Positive case: real ticker (AAPL), OpenFIGI resolves, EntityHints carries
    both ticker and issuer_figi."""
    mock_resolve.return_value = MagicMock(resolved=True, issuer_figi="BBG000B9XRY4")

    sig = cls._docket_to_signal(_docket("Investors v. Apple Inc (AAPL)"), _SCAN_DATE)

    assert sig is not None
    assert sig.entity_hints.ticker == "AAPL"
    assert sig.entity_hints.issuer_figi == "BBG000B9XRY4"
    assert sig.raw_payload["ticker_hint"] == "AAPL"


@patch("modal_workers.shared.openfigi_resolver.resolve_ticker")
def test_entity_hint_ticker_is_none_when_openfigi_raises(mock_resolve):
    """Network / import failure in OpenFIGI is still caught — but the ticker
    must NOT be propagated. Prior behaviour: exception swallowed, ticker_hint
    persisted anyway → junk entity. Fixed behaviour: exception swallowed, but
    ticker remains None."""
    mock_resolve.side_effect = RuntimeError("openfigi upstream 503")

    sig = cls._docket_to_signal(_docket("Roe v. Random Acronym Ltd (ZZZZZ)"), _SCAN_DATE)

    assert sig is not None
    assert sig.entity_hints.ticker is None
    assert sig.entity_hints.issuer_figi is None
    assert sig.raw_payload["ticker_hint"] == "ZZZZZ"


def test_signal_emitted_without_ticker_hint_at_all():
    """A case name without any parenthetical acronym produces no ticker_hint;
    EntityHints.ticker stays None. Regression guard: the gate change must not
    break the no-hint path."""
    sig = cls._docket_to_signal(_docket("Smith v. Unnamed Holdings"), _SCAN_DATE)

    assert sig is not None
    assert sig.entity_hints.ticker is None
    assert sig.raw_payload["ticker_hint"] is None
    assert sig.raw_payload["ticker_hint_present"] is False
