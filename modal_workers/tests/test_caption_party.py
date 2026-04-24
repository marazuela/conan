"""Tests for shared caption_party.extract_corporate_party.

Real-world captions drawn from the 2026-04-23 courtlistener log review +
Chancery opinion fixtures. Each case documents the expected confidence tier.
"""
from __future__ import annotations

import pytest

from modal_workers.shared.caption_party import extract_corporate_party


class TestExtractCorporateParty:
    # ----- empty / degenerate -------------------------------------------

    @pytest.mark.parametrize("bad", ["", "   ", None])
    def test_empty_input_zero_confidence(self, bad):
        name, conf = extract_corporate_party(bad or "")
        assert name == ""
        assert conf == 0.0

    # ----- In re ... Litigation: confidence 1.0 ------------------------

    @pytest.mark.parametrize("caption,expected_name", [
        ("In re Acme Corp. Stockholders Litigation", "Acme Corp."),
        ("In re XYZ Co. Derivative Litigation", "XYZ Co."),
        ("In re Widget Holdings, Inc. Securities Litigation", "Widget Holdings, Inc."),
        ("In Re Masimo Corporation Stockholders Litigation", "Masimo Corporation"),
    ])
    def test_in_re_litigation_top_tier(self, caption, expected_name):
        name, conf = extract_corporate_party(caption)
        assert expected_name in name
        assert conf == 1.0

    def test_in_re_appraisal(self):
        name, conf = extract_corporate_party("In re XYZ Co. Appraisal Proceedings")
        assert "XYZ Co." in name
        assert conf == 1.0

    # ----- X Corp v. Y Corp: both corporate, defendant wins 0.5 --------

    def test_both_corporate_returns_defendant(self):
        name, conf = extract_corporate_party("Apple Inc. v. Samsung Electronics Co.")
        assert "Samsung" in name
        assert conf == 0.5

    # ----- Individual v. Corp / Corp v. Individual: 0.9 ---------------

    @pytest.mark.parametrize("caption,expected", [
        ("Smith v. Widget Holdings, Inc.", "Widget Holdings, Inc."),
        ("John Doe v. Acme Corp.", "Acme Corp."),
        ("XYZ Corp. v. Smith", "XYZ Corp."),
        ("Tesla Inc. v. John Doe, et al.", "Tesla Inc."),
    ])
    def test_individual_vs_corporate(self, caption, expected):
        name, conf = extract_corporate_party(caption)
        assert name == expected
        assert conf == 0.9

    # ----- Government plaintiff stripped: 0.7 --------------------------

    def test_people_of_state_prefix_stripped(self):
        # Exact case from the 2026-04-23 log flood.
        name, conf = extract_corporate_party(
            "People of The State of California v. REV Group Inc"
        )
        assert name == "REV Group Inc"
        assert conf == 0.7

    @pytest.mark.parametrize("caption,expected", [
        ("United States v. Microsoft Corp.", "Microsoft Corp."),
        ("USA v. Widget Inc.", "Widget Inc."),
        ("SEC v. Acme Holdings LLC", "Acme Holdings LLC"),
        ("FTC v. Facebook, Inc.", "Facebook, Inc."),
        ("EEOC v. Tesla Inc.", "Tesla Inc."),
        ("State of Delaware v. Acme Corp.", "Acme Corp."),
        ("Commonwealth of Pennsylvania v. REV Group Inc", "REV Group Inc"),
    ])
    def test_government_plaintiffs_stripped(self, caption, expected):
        name, conf = extract_corporate_party(caption)
        assert name == expected
        assert conf == 0.7

    def test_sec_dotted_variant(self):
        name, conf = extract_corporate_party("S.E.C. v. Tesla Inc.")
        assert name == "Tesla Inc."
        assert conf == 0.7

    # ----- et al / ", and others" cleanup -------------------------------

    @pytest.mark.parametrize("caption,expected", [
        ("Smith v. Tesla Inc., et al.", "Tesla Inc."),
        ("Smith v. Tesla Inc., et al", "Tesla Inc."),
        ("Doe v. Acme Corp., and others", "Acme Corp."),
        ("DONALD BALL v TESLA INC., ET AL", "TESLA INC."),  # real probe
    ])
    def test_et_al_stripped(self, caption, expected):
        name, conf = extract_corporate_party(caption)
        assert name == expected
        assert conf >= 0.7

    # ----- Individual v. Individual: low confidence --------------------

    def test_individual_vs_individual(self):
        name, conf = extract_corporate_party("Smith v. Jones")
        assert conf <= 0.3

    # ----- Real flood samples (2026-04-23 logs) -----------------------

    @pytest.mark.parametrize("caption,must_contain,min_conf", [
        ("Sipin v. Tesla Inc.", "Tesla Inc.", 0.7),
        ("ContentNexus LLC v. Wipro, LLC", "Wipro", 0.5),
        ("CITY OF ALLENTOWN v. REV GROUP, INC.", "REV GROUP, INC.", 0.7),
        ("BJ's Wholesale Club, Inc. v. Agri Stats, Inc.", "Agri Stats, Inc.", 0.5),
        ("Target Corporation v. Agri Stats, Inc.", "Agri Stats, Inc.", 0.5),
        ("Clark v. Wallbox N.V.", "Wallbox N.V.", 0.7),
        ("Unified Fire Authority v. REV Group Inc", "REV Group Inc", 0.3),
    ])
    def test_real_flood_samples(self, caption, must_contain, min_conf):
        name, conf = extract_corporate_party(caption)
        assert must_contain in name, f"got {name!r} from {caption!r}"
        assert conf >= min_conf

    # ----- Case in which only non-corporate survives ------------------

    def test_purely_procedural_no_suffix(self):
        name, conf = extract_corporate_party("PetVet Operating, LLC v. Hutchison")
        # PetVet is an LLC — should come through as 0.9 (corp vs individual)
        assert "PetVet" in name
        assert conf == 0.9

    # ----- vs / V. / vS variants --------------------------------------

    @pytest.mark.parametrize("sep", ["v.", "v", "V.", "VS.", "vs", "Vs."])
    def test_separator_variants(self, sep):
        caption = f"Smith {sep} Acme Corp."
        name, conf = extract_corporate_party(caption)
        assert "Acme Corp" in name
        assert conf == 0.9

    # ----- Whitespace tolerance ---------------------------------------

    def test_extra_whitespace(self):
        name, conf = extract_corporate_party("   Smith   v.   Tesla Inc.   ")
        assert name == "Tesla Inc."
        assert conf == 0.9
