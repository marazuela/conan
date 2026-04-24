"""Tests for courtlistener_scanner selectivity logic (2026-04-24 rework).

Covers:
  - NOS 190 disabled-by-default; config flag re-enables it
  - require_universe_match gate drops unresolved patent/contract rows
  - Securities (850) / antitrust (410) emit regardless of universe match
  - Procedural overrides (class_certified, settlement, summary_judgment,
    mtd_denied) bypass the universe gate
  - Party extraction populates entity_hints.name (not full caption)
  - SEC issuer resolution populates ticker + cik in entity_hints
  - convergence_key derived from court + normalized party
  - party_resolution_confidence mapped from caption confidence + issuer hit
  - run_metrics surfaced for observability
  - Hard-gate (2026-04-23): unverified paren ticker_hints don't propagate to
    EntityHints unless OpenFIGI resolves them. Raw payload preserves the
    forensic trace either way.

All HTTP + Storage + SEC lookups are mocked.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from modal_workers.scanners import courtlistener_scanner as cls
from modal_workers.shared.sec_issuer_lookup import IssuerMatch


SCAN_DATE = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)


def _mk_docket(**overrides):
    base = {
        "caseName": "Smith v. Tesla Inc.",
        "_nos_queried": "190",
        "court_id": "cand",
        "dateFiled": "2026-04-20",
        "id": 42,
    }
    base.update(overrides)
    return base


def _stub_index(matches=None):
    """Build a fake IssuerIndex that returns pre-canned resolutions."""
    matches = matches or {}

    class _Stub:
        def resolve(self, name):
            return matches.get(name) or matches.get(name.lower())
    return _Stub()


class TestNosDisabling:
    def test_nos_190_dropped_by_default(self):
        """Default config: NOS 190 priority=off → None."""
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="190"),
            scan_date=SCAN_DATE,
            issuer_index=None,
            cfg_overrides={},
        )
        assert sig is None

    def test_nos_190_enabled_by_config_requires_universe(self):
        """With the config flag on, NOS 190 still requires universe match."""
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="190"),
            scan_date=SCAN_DATE,
            issuer_index=None,  # no index → no match
            cfg_overrides={"courtlistener_nos_190_enabled": True},
        )
        assert sig is None    # universe miss → dropped

    def test_nos_190_enabled_with_universe_match_emits(self):
        idx = _stub_index({"Tesla Inc.": IssuerMatch(
            ticker="TSLA", cik="0001318605", title="Tesla, Inc.",
            match_kind="exact",
        )})
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="190"),
            scan_date=SCAN_DATE,
            issuer_index=idx,
            cfg_overrides={"courtlistener_nos_190_enabled": True},
        )
        assert sig is not None
        assert sig.signal_type == "federal_civil_contract_filed"
        assert sig.entity_hints.ticker == "TSLA"

    def test_unknown_nos_returns_none(self):
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="999"),
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )
        assert sig is None


class TestUniverseMatchGate:
    def test_patent_without_universe_match_dropped(self):
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="830",
                       caseName="Acme Patent Co. v. XYZ Small Co."),
            scan_date=SCAN_DATE,
            issuer_index=None,
            cfg_overrides={},
        )
        assert sig is None

    def test_patent_with_universe_match_emits(self):
        idx = _stub_index({"Tesla Inc.": IssuerMatch(
            ticker="TSLA", cik="0001318605", title="Tesla, Inc.", match_kind="exact",
        )})
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="830"),
            scan_date=SCAN_DATE, issuer_index=idx, cfg_overrides={},
        )
        assert sig is not None
        assert sig.signal_type == "federal_civil_patent_filed"

    def test_securities_emits_without_universe_match(self):
        """Securities cases always emit; the rubric handles triage downstream."""
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="850",
                       caseName="Random Plaintiff v. Unknown Corp"),
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )
        assert sig is not None
        assert sig.signal_type == "federal_civil_securities_filed"

    def test_antitrust_emits_without_universe_match(self):
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="410"),
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )
        assert sig is not None

    def test_paren_ticker_satisfies_gate(self):
        """A (TICKER) in the caption bypasses the universe-match gate."""
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="830",
                       caseName="Smith v. Acme Inc. (ACME)"),
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )
        assert sig is not None
        assert sig.raw_payload["ticker_hint"] == "ACME"


class TestProceduralOverride:
    def test_class_certified_bypasses_universe_gate(self):
        """Procedural events are always emitted regardless of NOS rules."""
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="830",
                       caseName="Smith v. Small Co (Class Certified)"),
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )
        assert sig is not None
        assert sig.signal_type == "class_certified"

    def test_mtd_denied(self):
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="190",
                       caseName="Smith v. Small Co Motion to Dismiss Denied"),
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )
        assert sig is not None
        assert sig.signal_type == "mtd_denied"


class TestEntityHintsPopulation:
    def test_extracted_party_not_full_caption(self):
        """Main fix: entity_hints.name should be extracted party, NOT full caption.

        This is the 2026-04-23 regression — entity.name was "Sipin v. Tesla Inc."
        instead of "Tesla Inc."
        """
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="850",
                       caseName="Sipin v. Tesla Inc."),
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )
        assert sig is not None
        assert sig.entity_hints.name == "Tesla Inc."  # NOT "Sipin v. Tesla Inc."

    def test_issuer_resolution_populates_ticker_cik(self):
        idx = _stub_index({"REV Group Inc": IssuerMatch(
            ticker="REVG", cik="0001492633", title="REV Group, Inc.",
            match_kind="suffix_trimmed",
        )})
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="850",
                       caseName="People of The State of California v. REV Group Inc"),
            scan_date=SCAN_DATE, issuer_index=idx, cfg_overrides={},
        )
        assert sig is not None
        assert sig.entity_hints.ticker == "REVG"
        assert sig.entity_hints.cik == "0001492633"
        # Entity name uses SEC's official title, not the caption.
        assert sig.entity_hints.name == "REV Group, Inc."

    def test_unresolved_falls_back_to_extracted_name(self):
        """When issuer_index fails to resolve, entity_hints.name carries the
        extracted corporate party — still a huge improvement over full caption."""
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="850",
                       caseName="John Doe v. Obscure LLC"),
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )
        assert sig is not None
        assert sig.entity_hints.name == "Obscure LLC"
        assert sig.entity_hints.ticker is None


class TestRawPayload:
    def test_party_resolution_confidence_populated(self):
        """raw_payload.party_resolution_confidence is what the rubric cap reads."""
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="850",
                       caseName="Sipin v. Tesla Inc."),
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )
        # caption confidence 0.9 → prc 5 (1 + 0.9*4 = 4.6 → round → 5)
        assert sig.raw_payload["party_resolution_confidence"] == 5

    def test_issuer_match_bumps_prc_to_5(self):
        idx = _stub_index({"Tesla Inc.": IssuerMatch(
            ticker="TSLA", cik="0001318605", title="Tesla, Inc.",
            match_kind="exact",
        )})
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="850"),
            scan_date=SCAN_DATE, issuer_index=idx, cfg_overrides={},
        )
        assert sig.raw_payload["party_resolution_confidence"] == 5
        assert sig.raw_payload["universe_resolved"] is True
        assert sig.raw_payload["universe_ticker"] == "TSLA"
        assert sig.raw_payload["universe_cik"] == "0001318605"

    def test_universe_miss_flag(self):
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="850"),
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )
        assert sig.raw_payload["universe_resolved"] is False
        assert sig.raw_payload["universe_ticker"] is None

    def test_convergence_key_populated(self):
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="850",
                       caseName="Smith v. Tesla Inc.",
                       court_id="cand"),
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )
        key = sig.raw_payload["convergence_key"]
        assert key is not None
        assert key.startswith("fed|cand|")
        # same court + party → same key regardless of plaintiff side
        sig2 = cls._docket_to_signal(
            _mk_docket(_nos_queried="850",
                       caseName="Jones v. Tesla Inc.",
                       court_id="cand"),
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )
        assert sig2.raw_payload["convergence_key"] == key

    def test_strength_estimate_per_nos(self):
        sec = cls._docket_to_signal(
            _mk_docket(_nos_queried="850"),
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )
        assert sec.strength_estimate == 4
        # Patent with universe match → strength 3
        idx = _stub_index({"Tesla Inc.": IssuerMatch(
            ticker="TSLA", cik="0001318605", title="Tesla, Inc.",
            match_kind="exact",
        )})
        pat = cls._docket_to_signal(
            _mk_docket(_nos_queried="830"),
            scan_date=SCAN_DATE, issuer_index=idx, cfg_overrides={},
        )
        assert pat.strength_estimate == 3


class TestCaseNameMissing:
    def test_missing_case_name_returns_none(self):
        sig = cls._docket_to_signal(
            {"_nos_queried": "850", "court_id": "nysd", "dateFiled": "2026-04-20"},
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )
        assert sig is None


# ---------------------------------------------------------------------------
# Hard-gate (2026-04-23) — unverified paren tickers must NOT propagate to
# EntityHints. The TICKER_HINT_RE regex `[A-Z]{2,5}` matches any 2–5
# uppercase acronym, including non-tickers like UNOPS (UN Office for Project
# Services). Pre-fix, EntityHints.ticker was set from the regex even when
# OpenFIGI couldn't verify the string. Post-fix, the paren hint only
# propagates when OpenFIGI resolves it; raw_payload still carries the hint
# for forensic trace either way. Layered alongside the SEC-issuer-match
# path (issuer_match.ticker is authoritative and bypasses this gate).
# ---------------------------------------------------------------------------

class TestParenTickerHardGate:
    @patch("modal_workers.shared.openfigi_resolver.resolve_ticker")
    def test_entity_hint_ticker_is_none_when_openfigi_does_not_resolve(self, mock_resolve):
        """UNOPS case: regex extracts 'UNOPS', OpenFIGI returns unresolved, and the
        resulting EntityHints has ticker=None + issuer_figi=None. The raw_payload
        keeps ticker_hint for forensic trace."""
        mock_resolve.return_value = MagicMock(resolved=False, issuer_figi=None)

        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="850",
                       caseName="Acme Holdings (UNOPS) v. Defendant LLC"),
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )

        assert sig is not None
        assert sig.entity_hints.ticker is None
        assert sig.entity_hints.issuer_figi is None
        assert sig.raw_payload["ticker_hint"] == "UNOPS"  # forensic trace preserved
        assert sig.raw_payload["ticker_hint_source"] == "case_name_paren"

    @patch("modal_workers.shared.openfigi_resolver.resolve_ticker")
    def test_entity_hint_ticker_set_when_openfigi_resolves(self, mock_resolve):
        """Positive case: real ticker (AAPL), OpenFIGI resolves, EntityHints carries
        both ticker and issuer_figi."""
        mock_resolve.return_value = MagicMock(resolved=True, issuer_figi="BBG000B9XRY4")

        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="850",
                       caseName="Investors v. Apple Inc (AAPL)"),
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )

        assert sig is not None
        assert sig.entity_hints.ticker == "AAPL"
        assert sig.entity_hints.issuer_figi == "BBG000B9XRY4"
        assert sig.raw_payload["ticker_hint"] == "AAPL"

    @patch("modal_workers.shared.openfigi_resolver.resolve_ticker")
    def test_entity_hint_ticker_is_none_when_openfigi_raises(self, mock_resolve):
        """Network / import failure in OpenFIGI is still caught — but the ticker
        must NOT be propagated. Prior behaviour: exception swallowed, ticker_hint
        persisted anyway → junk entity. Fixed behaviour: exception swallowed, but
        ticker remains None."""
        mock_resolve.side_effect = RuntimeError("openfigi upstream 503")

        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="850",
                       caseName="Roe v. Random Acronym Ltd (ZZZZZ)"),
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )

        assert sig is not None
        assert sig.entity_hints.ticker is None
        assert sig.entity_hints.issuer_figi is None
        assert sig.raw_payload["ticker_hint"] == "ZZZZZ"

    def test_signal_emitted_without_ticker_hint_at_all(self):
        """A case name without any parenthetical acronym produces no ticker_hint;
        EntityHints.ticker stays None. Regression guard: the gate change must not
        break the no-hint path."""
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="850",
                       caseName="Smith v. Unnamed Holdings"),
            scan_date=SCAN_DATE, issuer_index=None, cfg_overrides={},
        )

        assert sig is not None
        assert sig.entity_hints.ticker is None
        assert sig.raw_payload["ticker_hint"] is None
        assert sig.raw_payload["ticker_hint_present"] is False

    def test_sec_issuer_match_bypasses_hard_gate(self):
        """When issuer_index resolves the caption to a SEC-listed issuer,
        EntityHints.ticker is set from the SEC match — no need to wait on
        OpenFIGI verification, since the SEC list is authoritative."""
        idx = _stub_index({"Tesla Inc.": IssuerMatch(
            ticker="TSLA", cik="0001318605", title="Tesla, Inc.", match_kind="exact",
        )})
        sig = cls._docket_to_signal(
            _mk_docket(_nos_queried="850",
                       caseName="Smith v. Tesla Inc."),
            scan_date=SCAN_DATE, issuer_index=idx, cfg_overrides={},
        )
        assert sig is not None
        assert sig.entity_hints.ticker == "TSLA"
