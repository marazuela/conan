"""
Tests for candidate_gate — preservation parity with tools/candidate_gate.py.

These assertions lock in the v1 thesis-quality rules (D-008): char-count minimums,
boilerplate regex, date-format parser. Any rule change requires a gate_version bump
and an explicit spec.md §12 note.

Run: python -m pytest modal_workers/tests/test_candidate_gate.py -v
"""
from __future__ import annotations

import pytest

from modal_workers.shared.candidate_gate import (
    BOILERPLATE_PATTERNS,
    MIN_FIELD_CHARS,
    REQUIRED_FIELDS,
    assess_thesis,
    assess_thesis_v2,
    render_candidate_markdown,
    render_candidate_markdown_v2,
)


VALID_THESIS = {
    "situation": "RPAY board adopted a 12.5% poison pill one day after Forager disclosed a 12.9% stake via 13D/A amendment number two, tying the trigger precisely to Forager's current position.",
    "why_underpriced": "Market priced the 8-K as routine defensive housekeeping at a flat $3.07 close. Tight custom triggers like this historically precede proxy contest or strategic review filings, producing 30-60% returns over 6-18 months in comparable precedents.",
    "next_catalyst": "Forager SC 13D/A Amendment No. 3 response filing within weeks.",
    "next_catalyst_date": "2026-05-27",
    "kill_conditions": "Forager reduces its stake or the board announces stake settlement on terms that limit dilution headroom.",
}


class TestAssessThesisHappyPath:
    def test_full_valid_thesis_passes(self):
        ok, reasons = assess_thesis(VALID_THESIS)
        assert ok is True
        assert reasons == []

    def test_iso_date_accepted(self):
        t = dict(VALID_THESIS, next_catalyst_date="2026-07-01")
        ok, reasons = assess_thesis(t)
        assert ok is True, reasons

    def test_quarter_band_date_accepted(self):
        t = dict(VALID_THESIS, next_catalyst_date="Q2 2026")
        ok, reasons = assess_thesis(t)
        assert ok is True, reasons

    def test_half_band_date_accepted(self):
        t = dict(VALID_THESIS, next_catalyst_date="H2 2026")
        ok, reasons = assess_thesis(t)
        assert ok is True, reasons

    def test_month_year_date_accepted(self):
        for mo in ("January", "Jan", "July 2026", "Dec 2026"):
            cd = mo if mo.endswith("2026") else f"{mo} 2026"
            t = dict(VALID_THESIS, next_catalyst_date=cd)
            ok, reasons = assess_thesis(t)
            assert ok is True, f"{cd}: {reasons}"

    def test_early_mid_late_date_accepted(self):
        for band in ("early 2026", "mid 2026", "late 2026"):
            t = dict(VALID_THESIS, next_catalyst_date=band)
            ok, reasons = assess_thesis(t)
            assert ok is True, f"{band}: {reasons}"


class TestAssessThesisRejection:
    def test_non_dict_rejected(self):
        ok, reasons = assess_thesis(None)
        assert ok is False
        assert reasons == ["thesis is missing or not a dict"]

    def test_missing_required_field(self):
        t = {k: v for k, v in VALID_THESIS.items() if k != "kill_conditions"}
        ok, reasons = assess_thesis(t)
        assert ok is False
        assert any("kill_conditions" in r for r in reasons)

    def test_empty_string_rejected(self):
        t = dict(VALID_THESIS, situation="")
        ok, reasons = assess_thesis(t)
        assert ok is False
        assert any("situation" in r for r in reasons)

    def test_too_short_situation(self):
        t = dict(VALID_THESIS, situation="Too short.")
        ok, reasons = assess_thesis(t)
        assert ok is False
        assert any("situation" in r and "too short" in r for r in reasons)

    def test_too_short_why_underpriced(self):
        t = dict(VALID_THESIS, why_underpriced="A" * 50)  # non-ws char count = 50 < 100
        ok, reasons = assess_thesis(t)
        assert ok is False
        assert any("why_underpriced" in r for r in reasons)

    def test_too_short_next_catalyst(self):
        t = dict(VALID_THESIS, next_catalyst="Date")
        ok, reasons = assess_thesis(t)
        assert ok is False
        assert any("next_catalyst" in r and "too short" in r for r in reasons)

    def test_too_short_kill_conditions(self):
        t = dict(VALID_THESIS, kill_conditions="Stock goes up")
        ok, reasons = assess_thesis(t)
        assert ok is False
        assert any("kill_conditions" in r for r in reasons)

    def test_boilerplate_scanner_classified(self):
        t = dict(VALID_THESIS,
                 situation="Scanner classified signal_type as activist_13d and filed accordingly for the RPAY ticker with the normalized metadata tagged.")
        ok, reasons = assess_thesis(t)
        assert ok is False
        assert any("boilerplate" in r for r in reasons)

    def test_boilerplate_tdnet_filed(self):
        t = dict(VALID_THESIS,
                 why_underpriced="TDnet filed disclosure for Toyota and the market has priced in the standard governance filing already, leaving no edge beyond reading the regulatory tape.")
        ok, reasons = assess_thesis(t)
        assert ok is False
        assert any("boilerplate" in r for r in reasons)

    def test_boilerplate_auto_generated(self):
        t = dict(VALID_THESIS,
                 kill_conditions="This thesis is auto-generated by the scanner pipeline and should be reviewed manually.")
        ok, reasons = assess_thesis(t)
        assert ok is False
        assert any("boilerplate" in r for r in reasons)

    def test_boilerplate_no_thesis_yet(self):
        t = dict(VALID_THESIS,
                 situation="No thesis yet — this is a placeholder until research lands. Added for completeness of the row.")
        ok, reasons = assess_thesis(t)
        assert ok is False
        assert any("boilerplate" in r for r in reasons)

    def test_invalid_date_format(self):
        t = dict(VALID_THESIS, next_catalyst_date="soon-ish")
        ok, reasons = assess_thesis(t)
        assert ok is False
        assert any("next_catalyst_date" in r for r in reasons)


class TestSchemaConstants:
    def test_required_fields_unchanged(self):
        assert REQUIRED_FIELDS == [
            "situation", "why_underpriced", "next_catalyst",
            "next_catalyst_date", "kill_conditions",
        ]

    def test_min_chars_unchanged(self):
        assert MIN_FIELD_CHARS == {
            "situation": 80, "why_underpriced": 100,
            "next_catalyst": 40, "kill_conditions": 60,
        }

    def test_boilerplate_pattern_count(self):
        assert len(BOILERPLATE_PATTERNS) == 6


VALID_V2_EXTRAS = {
    "steelman": (
        "The opposing view: the poison pill is purely defensive boilerplate. Forager has "
        "acquired 12.9% in a 26% total return environment [inferred]; most 13D filers never "
        "force a sale. Comparable pills (Masonite, ChannelAdvisor) triggered proxy fights but "
        "did not always close above the pill price. Without a named credible buyer, board "
        "capitulation could arrive through cosmetic governance reform, not premium M&A [speculated]."
    ),
    "web_research": [
        {"url": "https://www.sec.gov/Archives/edgar/data/1720592/000114036126002340/form8k.htm",
         "retrieved_at": "2026-04-20", "lean": "strengthening",
         "finding": "8-K Item 3.03 confirms 12.5% trigger, one day after the 13D/A. Novel language tracks the filer precisely."},
        {"url": "https://www.insideractivism.com/reports/2024_13D_outcomes_sub_500mm.pdf",
         "retrieved_at": "2026-04-20", "lean": "weakening",
         "finding": "Only 28% of sub-$500mm 13D campaigns led to a premium outcome; the median took 14 months."},
        {"url": "https://seekingalpha.com/article/rpay-valuation-nov",
         "retrieved_at": "2026-04-20", "lean": "neutral",
         "finding": "Sell-side models RPAY at 6.2x forward EBITDA, roughly in line with peers; no extreme discount."},
    ],
    "structured_kill_conditions": [
        {"id": "K1",
         "description": "Forager reduces 13D stake below 10% without a replacement activist on the cap table",
         "observable": {"source_type": "edgar_13d_amendment", "search_pattern": "Forager Fund, L.P."},
         "date_bound": "2026-09-30"},
        {"id": "K2",
         "description": "RPAY announces governance reform settlement with existing board seats unchanged",
         "observable": {"source_type": "edgar_8k", "search_pattern": "cooperation agreement"}},
        {"id": "K3",
         "description": "Stock closes below $2.40 for five consecutive trading days without further 13D activity",
         "observable": {"source_type": "marketdata_close", "url_pattern_hint": "nasdaq.com/market-activity/stocks/rpay"}},
    ],
}


def _v2_thesis_with_tags() -> dict:
    # Inject reasoning tags across situation / why_underpriced / steelman so the
    # reasoning_tag_coverage check (>=5 tags, >=1 [verified]) passes.
    #
    # deepcopy is required because VALID_V2_EXTRAS carries shared list values
    # (web_research, structured_kill_conditions). Previously tests that mutated
    # t["web_research"][0] = {...} leaked into subsequent callers, producing
    # order-dependent failures once enough tests exercised the list.
    import copy
    base = {**VALID_THESIS, **copy.deepcopy(VALID_V2_EXTRAS)}
    base["situation"] = (
        "RPAY's board adopted a 12.5% poison pill [verified from 8-K Item 3.03] one day after "
        "Forager disclosed a 12.9% stake via 13D/A Amendment No. 2 [verified]. "
        "The trigger is calibrated precisely below Forager's current position [inferred]."
    )
    base["why_underpriced"] = (
        "Market closed RPAY flat at $3.07 on the 8-K day [verified from Nasdaq close]. "
        "Custom triggers tied to a specific filer historically precede proxy fights [inferred] "
        "and the ~$268M market cap keeps the name off most analyst screens [speculated]."
    )
    return base


class TestAssessThesisV2HappyPath:
    def test_valid_v2_thesis_passes(self):
        ok, reasons = assess_thesis_v2(_v2_thesis_with_tags())
        assert ok is True, reasons


class TestAssessThesisV2Rejections:
    def test_missing_steelman(self):
        t = {**_v2_thesis_with_tags()}
        del t["steelman"]
        ok, reasons = assess_thesis_v2(t)
        assert ok is False
        assert any("steelman" in r and "missing" in r for r in reasons)

    def test_short_steelman(self):
        t = dict(_v2_thesis_with_tags(), steelman="Short steelman.")
        ok, reasons = assess_thesis_v2(t)
        assert ok is False
        assert any("steelman" in r and "too short" in r for r in reasons)

    def test_web_research_under_three_entries(self):
        t = _v2_thesis_with_tags()
        t["web_research"] = t["web_research"][:2]
        ok, reasons = assess_thesis_v2(t)
        assert ok is False
        assert any("web_research" in r and ">=3" in r for r in reasons)

    def test_web_research_all_strengthening(self):
        t = _v2_thesis_with_tags()
        t["web_research"] = [
            {**e, "lean": "strengthening"} for e in t["web_research"]
        ]
        ok, reasons = assess_thesis_v2(t)
        assert ok is False
        assert any("steelman-in-practice" in r for r in reasons)

    def test_web_research_bad_date(self):
        t = _v2_thesis_with_tags()
        t["web_research"][0] = {**t["web_research"][0], "retrieved_at": "yesterday"}
        ok, reasons = assess_thesis_v2(t)
        assert ok is False
        assert any("retrieved_at" in r for r in reasons)

    def test_web_research_invalid_lean(self):
        t = _v2_thesis_with_tags()
        t["web_research"][0] = {**t["web_research"][0], "lean": "supportive"}
        ok, reasons = assess_thesis_v2(t)
        assert ok is False
        assert any("lean must be one of" in r for r in reasons)

    def test_web_research_rejects_javascript_url(self):
        t = _v2_thesis_with_tags()
        t["web_research"][0] = {**t["web_research"][0], "url": "javascript:alert(1)"}
        ok, reasons = assess_thesis_v2(t)
        assert ok is False
        assert any("http:// or https://" in r for r in reasons)

    def test_web_research_rejects_data_url(self):
        t = _v2_thesis_with_tags()
        t["web_research"][0] = {
            **t["web_research"][0],
            "url": "data:text/html,<script>alert(1)</script>",
        }
        ok, reasons = assess_thesis_v2(t)
        assert ok is False
        assert any("http:// or https://" in r for r in reasons)

    def test_web_research_rejects_file_url(self):
        t = _v2_thesis_with_tags()
        t["web_research"][0] = {**t["web_research"][0], "url": "file:///etc/passwd"}
        ok, reasons = assess_thesis_v2(t)
        assert ok is False
        assert any("http:// or https://" in r for r in reasons)

    def test_web_research_accepts_mixed_case_https(self):
        # Protocol schemes are case-insensitive per RFC 3986.
        t = _v2_thesis_with_tags()
        t["web_research"][0] = {
            **t["web_research"][0],
            "url": "HTTPS://example.com/filing?id=1",
        }
        ok, _ = assess_thesis_v2(t)
        assert ok is True

    def test_reasoning_tag_coverage_too_few_tags(self):
        t = _v2_thesis_with_tags()
        # Strip all reasoning tags.
        t["situation"] = t["situation"].replace("[verified from 8-K Item 3.03]", "").replace("[verified]", "").replace("[inferred]", "")
        t["why_underpriced"] = t["why_underpriced"].replace("[verified from Nasdaq close]", "").replace("[inferred]", "").replace("[speculated]", "")
        t["steelman"] = t["steelman"].replace("[inferred]", "").replace("[speculated]", "")
        ok, reasons = assess_thesis_v2(t)
        assert ok is False
        assert any("reasoning_tag_coverage" in r for r in reasons)

    def test_reasoning_tag_coverage_no_verified(self):
        t = _v2_thesis_with_tags()
        # Replace every [verified] with [inferred] → still 5+ tags but 0 verified anchors.
        for field in ("situation", "why_underpriced", "steelman"):
            t[field] = t[field].replace("[verified from 8-K Item 3.03]", "[inferred]") \
                               .replace("[verified from Nasdaq close]", "[inferred]") \
                               .replace("[verified]", "[inferred]")
        ok, reasons = assess_thesis_v2(t)
        assert ok is False
        assert any("[verified] anchor" in r for r in reasons)

    def test_structured_kill_conditions_under_three(self):
        t = _v2_thesis_with_tags()
        t["structured_kill_conditions"] = t["structured_kill_conditions"][:2]
        ok, reasons = assess_thesis_v2(t)
        assert ok is False
        assert any("structured_kill_conditions" in r and ">=3" in r for r in reasons)

    def test_structured_kill_conditions_no_date_bound(self):
        t = _v2_thesis_with_tags()
        t["structured_kill_conditions"] = [
            {**e, "date_bound": None} for e in t["structured_kill_conditions"]
        ]
        ok, reasons = assess_thesis_v2(t)
        assert ok is False
        assert any("date_bound" in r for r in reasons)

    def test_structured_kill_conditions_missing_observable_pattern(self):
        t = _v2_thesis_with_tags()
        t["structured_kill_conditions"][0]["observable"] = {"source_type": "foo"}  # no search or url
        ok, reasons = assess_thesis_v2(t)
        assert ok is False
        assert any("search_pattern" in r and "url_pattern_hint" in r for r in reasons)

    def test_missing_structured_kill_conditions_entirely(self):
        t = _v2_thesis_with_tags()
        del t["structured_kill_conditions"]
        ok, reasons = assess_thesis_v2(t)
        assert ok is False
        assert any("structured_kill_conditions" in r and "missing" in r for r in reasons)

    def test_situation_why_underpriced_coherence_required(self):
        """Catches the failure mode where situation and why_underpriced each look
        plausible in isolation but describe DIFFERENT theses (no shared named
        entities). Other v1/v2 checks pass — only the coherence gate should fire."""
        t = _v2_thesis_with_tags()
        # Replace why_underpriced with a paragraph about an unrelated FDA thesis.
        # Keep length, boilerplate, and reasoning-tag coverage intact.
        t["why_underpriced"] = (
            "Pharmaceutical firms with [verified] sub-$2B market caps frequently see "
            "phase-three readouts mispriced into PDUFA dates [inferred]. Generic option-pricing "
            "doesn't capture the binary nature of approval decisions [speculated], and most "
            "sell-side coverage drops out below the analyst-screen threshold of $500M. "
            "Mean reversion takes 6-9 months across 40 historical comps."
        )
        ok, reasons = assess_thesis_v2(t)
        assert ok is False
        assert any("coherence_fail_situation_unrelated_to_underpriced" in r for r in reasons)

    def test_situation_why_underpriced_overlap_passes_coherence(self):
        """Bridge token that survives — RPAY appears in both fields → coherence passes."""
        t = _v2_thesis_with_tags()  # default fixture has RPAY + 8-K shared
        ok, reasons = assess_thesis_v2(t)
        assert ok is True, reasons
        assert not any("coherence_fail_situation_unrelated_to_underpriced" in r for r in reasons)

    def test_situation_with_no_named_entities_skips_coherence(self):
        """A degenerate situation (no extractable named entities) should not be
        rejected for coherence — other checks (length, boilerplate, reasoning tags)
        will surface the underlying quality issue with a more specific reason."""
        from modal_workers.shared.candidate_gate import _validate_situation_coherence
        reasons: list = []
        _validate_situation_coherence(
            situation="the company filed a thing yesterday with the regulator the regulator the regulator",
            why_underpriced="something completely different about pharmaceutical sector pricing",
            reasons=reasons,
        )
        assert reasons == []  # no named entities in situation → check skipped


class TestRenderV2:
    def test_v2_render_has_new_sections(self):
        signal = {"signal_id": "x", "source_url": "https://example/", "signal_type": "activist_13d",
                  "score_with_bonus": 35.0}
        entity = {"name": "Test Co", "primary_ticker": "TST", "primary_mic": "XNAS"}
        md = render_candidate_markdown_v2(signal, _v2_thesis_with_tags(),
                                          band="immediate", scoring_profile="activist_governance",
                                          entity=entity)
        assert "## Steelman" in md
        assert "## Web research" in md
        assert "## Kill conditions (structured)" in md
        assert "## Kill conditions" in md
        assert "gate_version: 2_steelman" in md
        # Steelman section comes before structured kills.
        assert md.index("## Steelman") < md.index("## Kill conditions")


class TestRenderMarkdown:
    def test_render_includes_frontmatter_and_sections(self):
        signal = {
            "signal_id": "edgar_13d_20260416_RPAY_012",
            "source_url": "https://www.sec.gov/Archives/edgar/data/123/456",
            "signal_type": "activist_13d",
            "score_with_bonus": 35.0,
        }
        entity = {"name": "Repay Holdings Corporation", "primary_ticker": "RPAY", "primary_mic": "XNAS"}
        md = render_candidate_markdown(signal, VALID_THESIS,
                                       band="immediate", scoring_profile="activist_governance",
                                       entity=entity)
        assert md.startswith("---\n")
        # Frontmatter strings are double-quoted as of 2026 YAML-escape patch.
        assert 'ticker_local: "RPAY"' in md
        assert 'mic: "XNAS"' in md
        assert "gate_version: 2" in md
        assert "authored_by: claude_thesis_writer" in md
        assert "# RPAY.XNAS — Repay Holdings Corporation" in md
        assert "**Band**: immediate" in md
        assert "## Situation" in md
        assert "## Why this is under-priced" in md
        assert "## Next catalyst" in md
        assert "## Kill conditions" in md
        assert VALID_THESIS["situation"] in md
        assert "Primary source: https://www.sec.gov/Archives/edgar/data/123/456" in md

    def test_frontmatter_escapes_quotes_and_newlines(self):
        """Third-party feeds can surface entity names with embedded " or newlines
        (e.g., scraped from RNS headlines). Unescaped, these corrupt YAML parse
        or inject arbitrary frontmatter fields."""
        signal = {
            "signal_id": "s1",
            "signal_type": 'xss_probe',
            "source_url": "https://example.com",
            "score_with_bonus": 35.0,
        }
        entity = {
            "name": 'Evil "Corp"\nauthored_by: attacker',
            "primary_ticker": "EVIL",
            "primary_mic": "XXXX",
        }
        md = render_candidate_markdown(signal, VALID_THESIS,
                                       band="immediate", scoring_profile="activist_governance",
                                       entity=entity)
        # Extract the frontmatter block (between first two '---' lines).
        first = md.index("---\n")
        second = md.index("\n---\n", first + 4)
        fm = md[first:second]
        # Invariants that keep YAML safe even when the name contains ", \n, or : —
        #   (a) the real newline must be escaped, not written literally, so the
        #       company field is a single YAML scalar. Only one real line should
        #       start with "company:" in the frontmatter block.
        company_lines = [ln for ln in fm.split("\n") if ln.startswith("company:")]
        assert len(company_lines) == 1
        #   (b) the only authored_by: line at line-start (after splitting on real
        #       newlines) is the intended literal — attacker-injected "authored_by:
        #       attacker" is inside the escaped company scalar, not at the start
        #       of its own line.
        authored_lines = [ln for ln in fm.split("\n") if ln.startswith("authored_by:")]
        assert authored_lines == ["authored_by: claude_thesis_writer"]
        #   (c) escaped forms present: backslash-n for the newline, backslash-
        #       quote for the inner quotes. Using YAML to parse the block would
        #       confirm this, but substring checks are enough for a unit test.
        assert '\\n' in fm
        assert '\\"Corp\\"' in fm
