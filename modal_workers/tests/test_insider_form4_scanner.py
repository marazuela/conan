"""
Tests for insider_form4_scanner.

Covers:
  - _classify_role (C-suite / VP / director / 10% / minor)
  - _reporter_normalized (affiliate dedup: first-token for orgs, full name for people)
  - _parse_form4 XML parsing:
      * multi-reporter filings
      * discretionary code filter (P/S only; M/G/A/F dropped)
      * 10b5-1 detection via attribute AND via footnote reference
      * missing price handled without crash
  - dim_estimator Form 4 branch:
      * 3+ C-suite cluster → crowding_intensity=5
      * 2 C-suite + 1 VP → 4
      * VPs/directors only → 3
      * solo C-suite buy → 3 (not 1 — solo signal type)
  - scan() end-to-end with mocked EFTS + primary-doc fetches
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from modal_workers.scanners import insider_form4_scanner as ifs
from modal_workers.shared.dim_estimator import (
    estimate_dimensions,
    project_short_positioning_heuristic,
)
from modal_workers.shared.scanner_base import Signal


# ---------------------------------------------------------------------------
# _classify_role
# ---------------------------------------------------------------------------

class TestClassifyRole:
    def test_csuite_via_officer_title(self):
        assert ifs._classify_role(False, True, False, "Chief Executive Officer") == "csuite"
        assert ifs._classify_role(False, True, False, "CFO") == "csuite"
        assert ifs._classify_role(True, True, False, "President") == "csuite"

    def test_vp_via_officer_title(self):
        assert ifs._classify_role(False, True, False, "Senior Vice President") == "vp"
        assert ifs._classify_role(False, True, False, "EVP, Engineering") == "vp"
        assert ifs._classify_role(False, True, False, "General Counsel") == "vp"

    def test_officer_without_recognizable_title_falls_to_vp(self):
        # Conservative default so we don't under-classify real officers.
        assert ifs._classify_role(False, True, False, "Strategic Advisor") == "vp"
        assert ifs._classify_role(False, True, False, None) == "vp"

    def test_director_only(self):
        assert ifs._classify_role(True, False, False, None) == "director_only"

    def test_ten_percent_only(self):
        assert ifs._classify_role(False, False, True, None) == "ten_percent_only"
        # 10% overrides director if both flags set.
        assert ifs._classify_role(True, False, True, None) == "ten_percent_only"

    def test_minor(self):
        assert ifs._classify_role(False, False, False, None) == "minor"


# ---------------------------------------------------------------------------
# _reporter_normalized (affiliate dedup)
# ---------------------------------------------------------------------------

class TestReporterNormalized:
    def test_citadel_affiliates_collapse(self):
        a = ifs._reporter_normalized("Citadel Advisors LLC")
        b = ifs._reporter_normalized("Citadel Americas LLC")
        c = ifs._reporter_normalized("Citadel Capital")
        assert a == b == c == "citadel"

    def test_blackrock_affiliates_collapse(self):
        assert ifs._reporter_normalized("BlackRock, Inc.") == "blackrock"
        assert ifs._reporter_normalized("BlackRock Fund Advisors") == "blackrock"

    def test_person_name_preserved(self):
        # Individual reporters stay distinct even if they share first token.
        a = ifs._reporter_normalized("John Smith")
        b = ifs._reporter_normalized("John Doe")
        assert a != b
        assert a == "john smith"
        assert b == "john doe"

    def test_empty_string_returns_empty(self):
        assert ifs._reporter_normalized("") == ""


# ---------------------------------------------------------------------------
# _parse_form4 XML
# ---------------------------------------------------------------------------

def _make_form4_xml(
    *,
    issuer_cik: str = "0000320193",
    issuer_name: str = "Apple Inc.",
    reporter_cik: str = "0001111111",
    reporter_name: str = "Cook Timothy D",
    is_director: bool = False,
    is_officer: bool = True,
    is_ten_percent: bool = False,
    officer_title: str = "Chief Executive Officer",
    txn_date: str = "2026-04-15",
    txn_code: str = "P",
    shares: str = "1000",
    price: str = "150.00",
    ad: str = "A",
    is_10b5_1_attr: bool = False,
    footnote_10b5_1: bool = False,
) -> bytes:
    plan_el = "<rule10b5-1>1</rule10b5-1>" if is_10b5_1_attr else ""
    fn_ref = '<footnoteId id="F1"/>' if footnote_10b5_1 else ""
    fn_block = '<footnote id="F1">Transaction made under Rule 10b5-1 plan.</footnote>' if footnote_10b5_1 else ""
    xml = f"""<?xml version="1.0"?>
<ownershipDocument>
  <issuer>
    <issuerCik>{issuer_cik}</issuerCik>
    <issuerName>{issuer_name}</issuerName>
    <issuerTradingSymbol>AAPL</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>{reporter_cik}</rptOwnerCik>
      <rptOwnerName>{reporter_name}</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>{'1' if is_director else '0'}</isDirector>
      <isOfficer>{'1' if is_officer else '0'}</isOfficer>
      <isTenPercentOwner>{'1' if is_ten_percent else '0'}</isTenPercentOwner>
      <officerTitle>{officer_title or ''}</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>{txn_date}</value></transactionDate>
      <transactionCoding>
        <transactionCode>{txn_code}</transactionCode>
        {plan_el}
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{shares}</value></transactionShares>
        <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      {fn_ref}
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  <footnotes>
    {fn_block}
  </footnotes>
</ownershipDocument>"""
    return xml.encode("utf-8")


class TestParseForm4:
    def test_basic_csuite_purchase(self):
        xml = _make_form4_xml()
        txns = ifs._parse_form4(xml, accession="0001-25-000001",
                                filing_url="http://x", file_date="2026-04-16")
        assert len(txns) == 1
        t = txns[0]
        assert t.issuer_cik == "0000320193"
        assert t.role == "csuite"
        assert t.txn_code == "P"
        assert t.shares == 1000.0
        assert t.price_per_share == 150.0
        assert t.value_usd == 150_000.0
        assert t.is_10b5_1 is False

    def test_10b5_1_attribute_detected(self):
        xml = _make_form4_xml(txn_code="S", ad="D", is_10b5_1_attr=True)
        txns = ifs._parse_form4(xml, accession="x", filing_url="", file_date="")
        assert len(txns) == 1
        assert txns[0].is_10b5_1 is True

    def test_10b5_1_via_footnote_detected(self):
        xml = _make_form4_xml(txn_code="S", ad="D", footnote_10b5_1=True)
        txns = ifs._parse_form4(xml, accession="x", filing_url="", file_date="")
        assert len(txns) == 1
        assert txns[0].is_10b5_1 is True

    def test_non_discretionary_codes_dropped(self):
        # Code M = option exercise, not discretionary → dropped.
        xml = _make_form4_xml(txn_code="M", ad="A")
        txns = ifs._parse_form4(xml, accession="x", filing_url="", file_date="")
        assert txns == []

        # Code G = gift → dropped.
        xml = _make_form4_xml(txn_code="G", ad="D")
        txns = ifs._parse_form4(xml, accession="x", filing_url="", file_date="")
        assert txns == []

    def test_missing_price_handled(self):
        xml = _make_form4_xml(price="")
        txns = ifs._parse_form4(xml, accession="x", filing_url="", file_date="")
        assert len(txns) == 1
        assert txns[0].price_per_share is None
        assert txns[0].value_usd is None

    def test_director_classification(self):
        xml = _make_form4_xml(is_director=True, is_officer=False, officer_title="")
        txns = ifs._parse_form4(xml, accession="x", filing_url="", file_date="")
        assert txns[0].role == "director_only"

    def test_ten_percent_holder_classification(self):
        xml = _make_form4_xml(is_officer=False, is_ten_percent=True, officer_title="")
        txns = ifs._parse_form4(xml, accession="x", filing_url="", file_date="")
        assert txns[0].role == "ten_percent_only"

    def test_malformed_xml_returns_empty(self):
        assert ifs._parse_form4(b"not xml", accession="x", filing_url="", file_date="") == []


# ---------------------------------------------------------------------------
# dim_estimator Form 4 branch
# ---------------------------------------------------------------------------

class TestForm4DimEstimator:
    """Form 4 cluster heuristic — exercised via `project_short_positioning_heuristic`.

    The public `estimate_dimensions("short_positioning", ...)` is now
    `_estimate_none` (signals emit unscored, AI fills via signal_resolver), but
    the heuristic remains live for ESMA + Form 4 internal top_signal_limit
    ranking. These tests lock its calibration for that ranking surface.
    """

    def _payload(self, **kw) -> dict:
        base = {
            "insider_cluster": True,
            "direction": "buy",
            "holder_count": 0,
            "c_suite_count": 0,
            "vp_count": 0,
            "director_only_count": 0,
            "ten_percent_holder_count": 0,
            "earliest_txn_date": "2026-04-10",
            "latest_txn_date": "2026-04-15",
            "total_value_usd": 100_000,
            "market_cap": 1_000_000_000,
            "adv_usd": 5_000_000,
        }
        base.update(kw)
        return base

    def test_public_estimator_returns_none_even_with_rich_form4_payload(self):
        """Form 4 payloads must NOT produce heuristic dims through the public path —
        signals emit unscored and AI fills the dims."""
        est = estimate_dimensions("short_positioning", self._payload(
            holder_count=3, c_suite_count=3))
        assert est is None

    def test_three_csuite_cluster_scores_5(self):
        est = project_short_positioning_heuristic(self._payload(
            holder_count=3, c_suite_count=3))
        assert est is not None
        assert est.dimensions["crowding_intensity"] == 5

    def test_two_csuite_plus_vp_scores_4(self):
        est = project_short_positioning_heuristic(self._payload(
            holder_count=3, c_suite_count=2, vp_count=1))
        assert est is not None
        assert est.dimensions["crowding_intensity"] == 4

    def test_vp_director_cluster_no_csuite_scores_3(self):
        est = project_short_positioning_heuristic(self._payload(
            holder_count=3, vp_count=2, director_only_count=1))
        assert est is not None
        assert est.dimensions["crowding_intensity"] == 3

    def test_two_minor_insiders_scores_2(self):
        est = project_short_positioning_heuristic(self._payload(
            holder_count=2))
        assert est is not None
        assert est.dimensions["crowding_intensity"] == 2

    def test_solo_csuite_scores_3_not_2(self):
        """Solo C-suite buy routed to ten_percent / c_suite signal_type upstream;
        ensure rubric still produces a meaningful (not minimum) tier so the
        emitted single-holder signal ranks above noise in top_signal_limit."""
        est = project_short_positioning_heuristic(self._payload(
            holder_count=1, c_suite_count=1))
        assert est is not None
        assert est.dimensions["crowding_intensity"] == 3

    def test_ten_percent_holder_counts_as_csuite_tier(self):
        est = project_short_positioning_heuristic(self._payload(
            holder_count=3, ten_percent_holder_count=3))
        assert est is not None
        assert est.dimensions["crowding_intensity"] == 5

    def test_size_vs_float_by_percentage(self):
        # 1% of $1B market cap = $10M → tier 5
        est = project_short_positioning_heuristic(self._payload(
            holder_count=2, c_suite_count=2,
            total_value_usd=10_000_000, market_cap=1_000_000_000))
        assert est.dimensions["size_vs_float"] == 5

        # 0.1% = $1M → tier 3
        est = project_short_positioning_heuristic(self._payload(
            holder_count=2, c_suite_count=2,
            total_value_usd=1_000_000, market_cap=1_000_000_000))
        assert est.dimensions["size_vs_float"] == 3

    def test_defaulted_dims_flag_requires_resolution(self):
        # catalyst_proximity + historical_analog are unknown from Form 4 alone.
        est = project_short_positioning_heuristic(self._payload(
            holder_count=2, c_suite_count=2))
        assert est is not None
        assert "catalyst_proximity" in est.defaulted_dims
        assert "historical_analog" in est.defaulted_dims
        assert est.requires_resolution is True

    def test_empty_cluster_returns_none(self):
        # All zeros → not a real cluster.
        payload = {"insider_cluster": True, "direction": "buy"}
        assert project_short_positioning_heuristic(payload) is None


# ---------------------------------------------------------------------------
# _form4_trend_tier (recency)
# ---------------------------------------------------------------------------

class TestForm4TrendTier:
    def test_rapid_recent_cluster_is_5(self):
        from modal_workers.shared.dim_estimator import _form4_trend_tier
        today = datetime.now(timezone.utc)
        latest = today.strftime("%Y-%m-%d")
        earliest = (today.replace(day=max(today.day - 5, 1))).strftime("%Y-%m-%d")
        assert _form4_trend_tier(earliest, latest, holder_count=3) == 5

    def test_missing_dates_returns_none(self):
        from modal_workers.shared.dim_estimator import _form4_trend_tier
        assert _form4_trend_tier(None, None, holder_count=3) is None
        assert _form4_trend_tier("", "2026-04-15", holder_count=3) is None


# ---------------------------------------------------------------------------
# Cluster dedup / signal hash stability
# ---------------------------------------------------------------------------

class TestContentHashing:
    def test_same_accessions_produce_same_hash(self):
        h1 = ifs._content_hash("0000320193", "buy", ["a-1", "a-2"])
        h2 = ifs._content_hash("0000320193", "buy", ["a-2", "a-1"])  # reordered
        assert h1 == h2  # sorted in the helper

    def test_different_direction_produces_different_hash(self):
        h1 = ifs._content_hash("0000320193", "buy", ["a-1"])
        h2 = ifs._content_hash("0000320193", "sell", ["a-1"])
        assert h1 != h2

    def test_signal_id_stable_across_runs(self):
        s1 = ifs._signal_id("0000320193", "buy", "2026-04-10", "2026-04-15")
        s2 = ifs._signal_id("0000320193", "buy", "2026-04-10", "2026-04-15")
        assert s1 == s2


# ---------------------------------------------------------------------------
# End-to-end scan with mocked I/O
# ---------------------------------------------------------------------------

class TestScanEndToEnd:
    def _cfg(self):
        from modal_workers.shared.supabase_client import ScannerConfig
        return ScannerConfig(
            scanner_id="test-scanner-id",
            name=ifs.NAME,
            status="operational",
            geography="US",
            cadence="daily",
            default_scoring_profile="short_positioning",
            signal_type_profile_map={
                "insider_cluster_buy": "short_positioning",
                "insider_cluster_sell": "short_positioning",
                "c_suite_open_market_buy": "short_positioning",
                "ten_percent_holder_buy": "short_positioning",
            },
            endpoints={},
            timeout_soft_s=60,
            timeout_hard_s=120,
        )

    def test_scan_requires_sec_user_agent(self, monkeypatch):
        monkeypatch.delenv("SEC_USER_AGENT", raising=False)
        from modal_workers.shared.scanner_base import MissingAuthError
        with pytest.raises(MissingAuthError):
            ifs.scan(self._cfg())

    def test_scan_returns_empty_ok_on_zero_hits(self, monkeypatch):
        monkeypatch.setenv("SEC_USER_AGENT", "test@example.com")
        with patch.object(ifs, "_list_form4_filings", return_value=[]), \
             patch.object(ifs, "SupabaseClient") as mock_sc:
            mock_sc.return_value.openfigi_cache_backend.return_value = (
                MagicMock(), MagicMock())
            result = ifs.scan(self._cfg())
        assert result.status == "ok"
        assert result.signals == []
        assert result.fetched_records == 0

    def test_scan_emits_cluster_signal_on_two_csuite_buys(self, monkeypatch):
        """Two CEO+CFO purchases on same issuer in window → insider_cluster_buy."""
        monkeypatch.setenv("SEC_USER_AGENT", "test@example.com")

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        hits = [
            {"ciks": ["0000320193"], "adsh": "0001-25-000001", "file_date": today},
            {"ciks": ["0000320193"], "adsh": "0001-25-000002", "file_date": today},
        ]
        xml_ceo = _make_form4_xml(
            reporter_cik="0001", reporter_name="Cook Timothy D",
            officer_title="Chief Executive Officer",
            txn_date=today, txn_code="P", ad="A",
        )
        xml_cfo = _make_form4_xml(
            reporter_cik="0002", reporter_name="Parekh Kevan",
            officer_title="Chief Financial Officer",
            txn_date=today, txn_code="P", ad="A",
        )
        fetch_map = {
            "0001-25-000001": xml_ceo,
            "0001-25-000002": xml_cfo,
        }

        def fake_fetch(url, *, user_agent):
            for adsh, body in fetch_map.items():
                if adsh.replace("-", "") in url:
                    return body
            return None

        # Also mock CIK → ticker resolution + openfigi + market_snapshot.
        with patch.object(ifs, "_list_form4_filings", return_value=hits), \
             patch.object(ifs, "_fetch_primary_doc", side_effect=fake_fetch), \
             patch("modal_workers.scanners.edgar_filing_monitor._get_company_tickers",
                   return_value=(["AAPL"], "NASDAQ")), \
             patch("modal_workers.shared.openfigi_resolver.resolve_ticker") as mock_figi, \
             patch("modal_workers.shared.openfigi_resolver.set_cache_backend"), \
             patch("modal_workers.shared.market_snapshot.load_market_snapshot",
                   return_value={"market_cap": 3_000_000_000_000, "adv_usd": 50_000_000_000}), \
             patch.object(ifs, "SupabaseClient") as mock_sc:
            mock_figi.return_value = MagicMock(resolved=True, issuer_figi="BBG000B9XRY4")
            mock_sc.return_value.openfigi_cache_backend.return_value = (
                MagicMock(), MagicMock())
            result = ifs.scan(self._cfg())

        assert result.status == "ok"
        assert len(result.signals) == 1
        sig = result.signals[0]
        assert sig.signal_type == "insider_cluster_buy"
        assert sig.thesis_direction == "long"
        assert sig.raw_payload["c_suite_count"] == 2
        assert sig.raw_payload["holder_count"] == 2
        assert sig.raw_payload["insider_cluster"] is True
        assert set(sig.raw_payload["contributing_accessions"]) == {
            "0001-25-000001", "0001-25-000002"}

    def test_scan_drops_10b5_1_only_cluster(self, monkeypatch):
        """Two filings both marked 10b5-1 → no cluster emitted (all filtered)."""
        monkeypatch.setenv("SEC_USER_AGENT", "test@example.com")

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        hits = [
            {"ciks": ["0000320193"], "adsh": "0001-25-000010", "file_date": today},
            {"ciks": ["0000320193"], "adsh": "0001-25-000011", "file_date": today},
        ]
        xml_planned1 = _make_form4_xml(
            reporter_cik="0001", reporter_name="Cook Timothy D",
            officer_title="Chief Executive Officer",
            txn_date=today, txn_code="S", ad="D", is_10b5_1_attr=True,
        )
        xml_planned2 = _make_form4_xml(
            reporter_cik="0002", reporter_name="Parekh Kevan",
            officer_title="Chief Financial Officer",
            txn_date=today, txn_code="S", ad="D", is_10b5_1_attr=True,
        )
        fetch_map = {
            "0001-25-000010": xml_planned1,
            "0001-25-000011": xml_planned2,
        }

        def fake_fetch(url, *, user_agent):
            for adsh, body in fetch_map.items():
                if adsh.replace("-", "") in url:
                    return body
            return None

        with patch.object(ifs, "_list_form4_filings", return_value=hits), \
             patch.object(ifs, "_fetch_primary_doc", side_effect=fake_fetch), \
             patch("modal_workers.shared.openfigi_resolver.set_cache_backend"), \
             patch.object(ifs, "SupabaseClient") as mock_sc:
            mock_sc.return_value.openfigi_cache_backend.return_value = (
                MagicMock(), MagicMock())
            result = ifs.scan(self._cfg())

        assert result.status == "ok"
        assert result.signals == []
        assert result.run_metrics["10b5_1_skipped_txns"] == 2

    def test_scan_emits_solo_csuite_buy(self, monkeypatch):
        """Single CEO open-market buy → c_suite_open_market_buy signal type."""
        monkeypatch.setenv("SEC_USER_AGENT", "test@example.com")

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        hits = [
            {"ciks": ["0000320193"], "adsh": "0001-25-000020", "file_date": today},
        ]
        xml_ceo = _make_form4_xml(
            reporter_cik="0001", reporter_name="Cook Timothy D",
            officer_title="Chief Executive Officer",
            txn_date=today, txn_code="P", ad="A",
        )

        with patch.object(ifs, "_list_form4_filings", return_value=hits), \
             patch.object(ifs, "_fetch_primary_doc", return_value=xml_ceo), \
             patch("modal_workers.scanners.edgar_filing_monitor._get_company_tickers",
                   return_value=(["AAPL"], "NASDAQ")), \
             patch("modal_workers.shared.openfigi_resolver.resolve_ticker") as mock_figi, \
             patch("modal_workers.shared.openfigi_resolver.set_cache_backend"), \
             patch("modal_workers.shared.market_snapshot.load_market_snapshot",
                   return_value={}), \
             patch.object(ifs, "SupabaseClient") as mock_sc:
            mock_figi.return_value = MagicMock(resolved=False)
            mock_sc.return_value.openfigi_cache_backend.return_value = (
                MagicMock(), MagicMock())
            result = ifs.scan(self._cfg())

        assert result.status == "ok"
        assert len(result.signals) == 1
        assert result.signals[0].signal_type == "c_suite_open_market_buy"
        assert result.signals[0].thesis_direction == "long"

    def test_scan_drops_solo_minor_insider(self, monkeypatch):
        """Single director-only buy below gate → no emission."""
        monkeypatch.setenv("SEC_USER_AGENT", "test@example.com")

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        hits = [
            {"ciks": ["0000320193"], "adsh": "0001-25-000030", "file_date": today},
        ]
        xml_dir = _make_form4_xml(
            reporter_cik="0003", reporter_name="Jung Andrea",
            is_officer=False, is_director=True, officer_title="",
            txn_date=today, txn_code="P", ad="A", shares="100", price="150",
        )

        with patch.object(ifs, "_list_form4_filings", return_value=hits), \
             patch.object(ifs, "_fetch_primary_doc", return_value=xml_dir), \
             patch("modal_workers.shared.openfigi_resolver.set_cache_backend"), \
             patch.object(ifs, "SupabaseClient") as mock_sc:
            mock_sc.return_value.openfigi_cache_backend.return_value = (
                MagicMock(), MagicMock())
            result = ifs.scan(self._cfg())

        assert result.status == "ok"
        assert result.signals == []


# ---------------------------------------------------------------------------
# top_signal_limit ranking + cap (parity with esma_short_scanner; was missing
# pre 2026-04-27 — Form 4 emissions were uncapped, inflating short volume).
# ---------------------------------------------------------------------------

def _form4_signal(signal_id: str, signal_type: str, raw_payload: dict) -> Signal:
    now = datetime.now(timezone.utc)
    return Signal(
        signal_id=signal_id,
        source_content_hash=f"sha256:{signal_id}",
        source_date=now,
        scan_date=now,
        signal_type=signal_type,
        raw_payload=raw_payload,
    )


class TestForm4TopSignalLimit:
    def _cluster_payload(self, *, csuite=0, vp=0, holders=2, total_value=500_000):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return {
            "insider_cluster": True,
            "direction": "buy",
            "holder_count": holders,
            "c_suite_count": csuite,
            "vp_count": vp,
            "director_only_count": 0,
            "ten_percent_holder_count": 0,
            "earliest_txn_date": today,
            "latest_txn_date": today,
            "total_value_usd": total_value,
            "market_cap": 1_000_000_000,
        }

    def test_cap_keeps_highest_priority_signals(self):
        strong = _form4_signal(
            "strong",
            "insider_cluster_buy",
            self._cluster_payload(csuite=3, holders=3, total_value=10_000_000),
        )
        weak = _form4_signal(
            "weak",
            "c_suite_open_market_buy",
            self._cluster_payload(csuite=1, holders=1, total_value=100_000),
        )
        kept, dropped = ifs._apply_form4_top_signal_limit([strong, weak], 1)
        assert [s.signal_id for s in kept] == ["strong"]
        assert [s.signal_id for s in dropped] == ["weak"]

    def test_zero_disables_cap(self):
        a = _form4_signal("a", "insider_cluster_buy", self._cluster_payload())
        b = _form4_signal("b", "insider_cluster_sell", self._cluster_payload())
        kept, dropped = ifs._apply_form4_top_signal_limit([a, b], 0)
        assert {s.signal_id for s in kept} == {"a", "b"}
        assert dropped == []

    def test_below_cap_passes_through(self):
        a = _form4_signal("a", "insider_cluster_buy", self._cluster_payload())
        kept, dropped = ifs._apply_form4_top_signal_limit([a], 25)
        assert kept == [a]
        assert dropped == []

    def test_coerce_signal_limit_handles_garbage(self):
        # cfg.config may carry stringified or invalid values from the registry
        # JSON without crashing the run.
        assert ifs._coerce_signal_limit(None, 25) == 25
        assert ifs._coerce_signal_limit("42", 25) == 42
        assert ifs._coerce_signal_limit("nope", 25) == 25
        assert ifs._coerce_signal_limit(-1, 25) == 25
        assert ifs._coerce_signal_limit(True, 25) == 25  # booleans are not ints here
        assert ifs._coerce_signal_limit(0, 25) == 0  # explicit "disabled"
