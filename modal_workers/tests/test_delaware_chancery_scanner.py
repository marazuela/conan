"""
Tests for delaware_chancery_scanner.

Covers:
  - _parse_opinions_html — table parsing + case-number / date / caption extraction
  - _classify_caption — all 6 signal types + generic fallback
  - _extract_party_name — Chancery caption idioms (In re X Stockholders Litigation,
    Smith v. ABC Inc., etc.)
  - _content_hash / _signal_id — dedup stability on (case_number, signal_type, date)
  - scan() end-to-end with mocked opinions fetch + graceful degradation when
    the page is unreachable or CourtConnect is stubbed
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests

from modal_workers.scanners import delaware_chancery_scanner as dcs


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

# Mirrors the real courts.delaware.gov opinions-index column layout:
#   cell[0] caption | cell[1] date | cell[2] C.A. No. ... | cell[3] court
#   cell[4] type    | cell[5] judge | cell[6] opinion_title (with PDF link)
_OPINIONS_HTML_BASIC = """<!doctype html>
<html><body>
<table>
  <thead><tr>
    <th>Case</th><th>Date</th><th>C.A. No.</th><th>Court</th>
    <th>Type</th><th>Judge</th><th>Opinion</th>
  </tr></thead>
  <tbody>
    <tr>
      <td>In re Acme Corp. Stockholders Litigation</td>
      <td>2026-04-15</td>
      <td>C.A. No. 2026-0123-AGB</td>
      <td>Court of Chancery</td>
      <td>Civil</td>
      <td>Bouchard A.</td>
      <td><a href="/Opinions/Download.aspx?id=12345">Memorandum Opinion on Motion to Dismiss</a></td>
    </tr>
    <tr>
      <td>Smith v. Widget Holdings, Inc.</td>
      <td>4/10/2026</td>
      <td>C.A. No. 2026-0099-LWW</td>
      <td>Court of Chancery</td>
      <td>Civil</td>
      <td>Will L.</td>
      <td><a href="https://courts.delaware.gov/opinions/download/abc">Letter Report Denying Inspection of Books and Records</a></td>
    </tr>
    <tr>
      <td>In re XYZ Co. Appraisal Proceedings</td>
      <td>2026-03-02</td>
      <td>C.A. No. 2026-0045-JTL</td>
      <td>Court of Chancery</td>
      <td>Civil</td>
      <td>Laster J.T.</td>
      <td><a href="/Opinions/Download.aspx?id=99999">Post-Trial Opinion</a></td>
    </tr>
  </tbody>
</table>
</body></html>"""


class TestParseOpinionsHtml:
    def test_parses_three_rows(self):
        opinions = dcs._parse_opinions_html(_OPINIONS_HTML_BASIC)
        assert len(opinions) == 3

    def test_case_number_extracted(self):
        opinions = dcs._parse_opinions_html(_OPINIONS_HTML_BASIC)
        case_numbers = [op.case_number for op in opinions]
        assert "2026-0123-AGB" in case_numbers
        assert "2026-0099-LWW" in case_numbers
        assert "2026-0045-JTL" in case_numbers

    def test_date_formats_normalized(self):
        opinions = dcs._parse_opinions_html(_OPINIONS_HTML_BASIC)
        dates = sorted(op.release_date for op in opinions)
        assert dates == ["2026-03-02", "2026-04-10", "2026-04-15"]

    def test_caption_extracted(self):
        opinions = dcs._parse_opinions_html(_OPINIONS_HTML_BASIC)
        captions = [op.case_caption for op in opinions]
        assert any("Acme Corp." in c for c in captions)
        assert any("Widget Holdings" in c for c in captions)
        assert any("Appraisal" in c for c in captions)

    def test_pdf_links_resolved(self):
        opinions = dcs._parse_opinions_html(_OPINIONS_HTML_BASIC)
        for op in opinions:
            assert op.opinion_url.startswith("https://")

    def test_empty_html_returns_empty(self):
        assert dcs._parse_opinions_html("") == []
        assert dcs._parse_opinions_html("<html></html>") == []

    def test_rows_without_case_number_skipped(self):
        html = """<table><tr><td>2026-04-15</td><td>Random Matter</td></tr></table>"""
        assert dcs._parse_opinions_html(html) == []

    def test_case_number_extracted_from_ca_no_prefix(self):
        """Real HTML wraps the number as 'C.A. No. 2026-0123-AGB'."""
        opinions = dcs._parse_opinions_html(_OPINIONS_HTML_BASIC)
        assert all(
            re.match(r"^\d{4}-\d{3,5}-[A-Z]{2,4}$", op.case_number)
            for op in opinions
        )

    def test_caption_distinct_from_opinion_title(self):
        """cell[0] caption ≠ cell[6] opinion title. Both are captured separately
        so classification can match either surface."""
        opinions = dcs._parse_opinions_html(_OPINIONS_HTML_BASIC)
        widget = next(op for op in opinions if "Widget" in op.case_caption)
        assert widget.case_caption == "Smith v. Widget Holdings, Inc."
        assert "Books and Records" in widget.opinion_title
        assert widget.case_caption != widget.opinion_title

    def test_classification_uses_opinion_title_when_caption_is_neutral(self):
        """Widget case caption is generic 'Smith v. Widget' — matter-type must
        come from the opinion_title 'Letter Report Denying Inspection of Books
        and Records'."""
        opinions = dcs._parse_opinions_html(_OPINIONS_HTML_BASIC)
        widget = next(op for op in opinions if "Widget" in op.case_caption)
        assert widget.matter_type == "books_and_records"

    def test_opinion_title_skips_court_and_type_boilerplate(self):
        """When opinion_title is a short word like 'Opinion' (cell[6]), the
        parser must skip the longer 'Court of Chancery' / 'Civil' /
        '<Name> V.C.' metadata cells rather than grabbing those instead.
        Regression: live data showed 'Masimo Corporation v. Kiani' getting
        'Court of Chancery' as its opinion_title before this fix."""
        html = """<table><tr>
          <td>Masimo Corporation v. Joe E. Kiani</td>
          <td>04/21/2026</td>
          <td>C.A. No. 2024-1086-NAC</td>
          <td>Court of Chancery</td>
          <td>Civil</td>
          <td>Cook V.C.</td>
          <td><a href="/x">Opinion</a></td>
        </tr></table>"""
        ops = dcs._parse_opinions_html(html)
        assert len(ops) == 1
        assert ops[0].opinion_title == "Opinion"
        assert ops[0].case_caption == "Masimo Corporation v. Joe E. Kiani"


# ---------------------------------------------------------------------------
# _looks_like_caption (disambiguates real captions from opinion titles)
# ---------------------------------------------------------------------------

class TestLooksLikeCaption:
    def test_versus_marker_accepted(self):
        assert dcs._looks_like_caption("Smith v. Widget Holdings, Inc.") is True
        assert dcs._looks_like_caption("XYZ Corp. v. Smith") is True

    def test_in_re_prefix_accepted(self):
        assert dcs._looks_like_caption("In re Acme Corp. Stockholders Litigation") is True
        assert dcs._looks_like_caption("In Re XYZ Co. Appraisal Proceedings") is True

    def test_entity_suffix_accepted(self):
        assert dcs._looks_like_caption("Some Random Acme Corp. Matter") is True
        assert dcs._looks_like_caption("Widget Holdings Litigation") is True

    def test_opinion_titles_rejected(self):
        """Opinion titles should NOT look like captions."""
        assert dcs._looks_like_caption(
            "Letter Report Denying Inspection of Books and Records") is False
        assert dcs._looks_like_caption("Memorandum Opinion") is False
        assert dcs._looks_like_caption("Post-Trial Opinion") is False

    def test_short_non_caption_rejected(self):
        assert dcs._looks_like_caption("") is False
        assert dcs._looks_like_caption("Civil") is False
        assert dcs._looks_like_caption("2026-04-15") is False


# ---------------------------------------------------------------------------
# Caption classifier
# ---------------------------------------------------------------------------

class TestClassifyCaption:
    def test_appraisal(self):
        matter, sig = dcs._classify_caption("In re XYZ Corp. Appraisal Proceedings")
        assert sig == "chancery_appraisal_filed"
        assert matter == "appraisal"

    def test_revlon(self):
        matter, sig = dcs._classify_caption("In re Acme Corp. Revlon Claims")
        assert sig == "chancery_revlon_claim_filed"
        assert matter == "revlon"

    def test_books_and_records(self):
        matter, sig = dcs._classify_caption("Smith v. ABC Inc. — Books and Records Demand")
        assert sig == "chancery_books_and_records_demand"
        assert matter == "books_and_records"

    def test_section_220(self):
        matter, sig = dcs._classify_caption("Doe v. Widget Co. (DGCL 220 Action)")
        assert sig == "chancery_books_and_records_demand"

    def test_motion_to_expedite(self):
        matter, sig = dcs._classify_caption("In re Target Corp. Motion to Expedite")
        assert sig == "chancery_motion_to_expedite_granted"

    def test_injunction_merger(self):
        matter, sig = dcs._classify_caption(
            "Stockholders v. Buyer Inc. — Preliminary Injunction Granted")
        assert sig == "chancery_injunction_granted_blocking_deal"

    def test_fallback_generic(self):
        matter, sig = dcs._classify_caption("In re Misc Corp. Matter")
        assert sig == "chancery_opinion_released"
        assert matter == "other"

    def test_empty_caption(self):
        matter, sig = dcs._classify_caption("")
        assert sig == "chancery_opinion_released"


# ---------------------------------------------------------------------------
# Party name extraction
# ---------------------------------------------------------------------------

class TestExtractPartyName:
    def test_in_re_stockholders_litigation(self):
        assert "Acme Corp" in dcs._extract_party_name(
            "In re Acme Corp. Stockholders Litigation")

    def test_versus_with_company_on_right(self):
        assert "Widget Holdings" in dcs._extract_party_name(
            "Smith v. Widget Holdings, Inc.")

    def test_versus_with_company_on_left(self):
        # When the company is the plaintiff.
        result = dcs._extract_party_name("XYZ Corp. v. Smith")
        assert "XYZ" in result

    def test_derivative_suffix_stripped(self):
        assert "Beta" in dcs._extract_party_name(
            "In re Beta Inc. Derivative Litigation")

    def test_empty_returns_empty(self):
        assert dcs._extract_party_name("") == ""


# ---------------------------------------------------------------------------
# Dedup hashes
# ---------------------------------------------------------------------------

class TestDedupHashing:
    def test_same_inputs_same_hash(self):
        a = dcs._content_hash("2026-0123-AGB", "chancery_appraisal_filed", "2026-04-15")
        b = dcs._content_hash("2026-0123-AGB", "chancery_appraisal_filed", "2026-04-15")
        assert a == b

    def test_different_signal_type_different_hash(self):
        a = dcs._content_hash("2026-0123-AGB", "chancery_appraisal_filed", "2026-04-15")
        b = dcs._content_hash("2026-0123-AGB", "chancery_opinion_released", "2026-04-15")
        assert a != b

    def test_signal_id_stable(self):
        a = dcs._signal_id("2026-0123-AGB", "chancery_appraisal_filed", "2026-04-15")
        b = dcs._signal_id("2026-0123-AGB", "chancery_appraisal_filed", "2026-04-15")
        assert a == b
        assert a.startswith("chancery_")


# ---------------------------------------------------------------------------
# End-to-end scan with mocked HTTP
# ---------------------------------------------------------------------------

class TestScanEndToEnd:
    def _cfg(self, lookback_days: int = 365):
        from modal_workers.shared.supabase_client import ScannerConfig
        return ScannerConfig(
            scanner_id="test-id",
            name=dcs.NAME,
            status="operational",
            geography="US",
            cadence="daily",
            default_scoring_profile="litigation",
            signal_type_profile_map={},
            endpoints={},
            timeout_soft_s=60,
            timeout_hard_s=120,
            config={"opinions_lookback_days": lookback_days},
        )

    def test_scan_emits_signals_from_opinions(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = _OPINIONS_HTML_BASIC
        with patch("modal_workers.scanners.delaware_chancery_scanner.requests.get",
                   return_value=resp), \
             patch.object(dcs, "SupabaseClient") as mock_sc:
            mock_sc.return_value = MagicMock()
            result = dcs.scan(self._cfg(lookback_days=365))

        # Status is partial because CourtConnect stub adds a deferred warning;
        # opinions should still emit three signals.
        assert result.status in ("ok", "partial")
        assert len(result.signals) == 3
        types = sorted(s.signal_type for s in result.signals)
        # One appraisal, two generics (the others). Appraisal matches first.
        assert "chancery_appraisal_filed" in types

    def test_scan_filters_stale_opinions_by_lookback(self):
        """Opinions older than lookback_days should be dropped."""
        resp = MagicMock()
        resp.status_code = 200
        resp.text = _OPINIONS_HTML_BASIC  # contains 2026-03-02, 2026-04-10, 2026-04-15
        with patch("modal_workers.scanners.delaware_chancery_scanner.requests.get",
                   return_value=resp), \
             patch.object(dcs, "SupabaseClient") as mock_sc:
            mock_sc.return_value = MagicMock()
            # Force lookback to 1 day — everything is ancient history from any
            # realistic "today", so all three filter out.
            result = dcs.scan(self._cfg(lookback_days=1))

        assert result.run_metrics["opinions_filtered_by_date"] == 3
        assert result.signals == []

    def test_scan_graceful_degradation_on_opinions_failure(self):
        """If the opinions page is unreachable, scan returns partial with warning."""
        with patch("modal_workers.scanners.delaware_chancery_scanner.requests.get",
                   side_effect=requests.exceptions.ConnectionError("no net")), \
             patch.object(dcs, "SupabaseClient") as mock_sc:
            mock_sc.return_value = MagicMock()
            result = dcs.scan(self._cfg())

        # Opinions failed but CourtConnect stub is "deferred", not "error" —
        # we want the scanner to report error since both surfaces failed.
        assert result.status == "error"
        assert result.signals == []
        assert any("opinions" in w.lower() for w in result.warnings)

    def test_scan_handles_non_200_opinions_response(self):
        resp = MagicMock()
        resp.status_code = 503
        resp.text = ""
        with patch("modal_workers.scanners.delaware_chancery_scanner.requests.get",
                   return_value=resp), \
             patch.object(dcs, "SupabaseClient") as mock_sc:
            mock_sc.return_value = MagicMock()
            result = dcs.scan(self._cfg())

        assert result.status == "error"
        assert any("503" in w for w in result.warnings)

    def test_scan_emits_party_hints(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = _OPINIONS_HTML_BASIC
        with patch("modal_workers.scanners.delaware_chancery_scanner.requests.get",
                   return_value=resp), \
             patch.object(dcs, "SupabaseClient") as mock_sc:
            mock_sc.return_value = MagicMock()
            result = dcs.scan(self._cfg(lookback_days=365))

        # Every signal should carry a party name in entity_hints.name.
        for sig in result.signals:
            assert sig.entity_hints is not None
            assert sig.entity_hints.country == "US"
            assert sig.raw_payload["signal_category"] == "delaware_chancery"
            assert sig.raw_payload["chancery_case_number"]
