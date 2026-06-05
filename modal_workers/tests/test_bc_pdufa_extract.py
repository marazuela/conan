"""Unit tests for modal_workers.shared.bc_pdufa_extract (pure parser, no I/O).

Coverage (per spike spec §5.4 / §6 step 1):
  - one fixture per accepted date format (long, abbreviated, numeric, ISO)
  - each designation (BT / FT / AA) -> True; absence -> None (not False)
  - a decoy date (date present but NOT near a PDUFA anchor) -> None
  - a no-match body (no PDUFA token at all) -> None
  - context-anchoring: a far-away date is rejected, a near one accepted
  - appl_type hint keyword derivation
"""

from __future__ import annotations

from modal_workers.shared.bc_pdufa_extract import (
    PdufaExtract,
    extract_appl_type_hint,
    extract_designations,
    extract_drug_name,
    extract_pdufa,
    extract_pdufa_date,
)


# ---------------------------------------------------------------------------
# Date format coverage — each must normalize to the same ISO date.
# ---------------------------------------------------------------------------

def test_date_long_month_form():
    text = "The Company announced that the FDA assigned a PDUFA goal date of January 5, 2026 for its NDA."
    assert extract_pdufa_date(text) == "2026-01-05"


def test_date_long_abbreviated_month():
    text = "FDA set the PDUFA action date for Jan 5, 2026 following acceptance."
    assert extract_pdufa_date(text) == "2026-01-05"


def test_date_long_no_comma():
    text = "the PDUFA goal date is March 12 2026 per the agency."
    assert extract_pdufa_date(text) == "2026-03-12"


def test_date_sept_abbreviation():
    # "Sept" is not a strptime month token; the normalizer maps it to "Sep".
    text = "FDA assigned a PDUFA target action date of Sept. 30, 2026."
    assert extract_pdufa_date(text) == "2026-09-30"


def test_date_numeric_mmddyyyy():
    text = "The PDUFA goal date is 01/05/2026 according to the FDA."
    assert extract_pdufa_date(text) == "2026-01-05"


def test_date_numeric_single_digit():
    text = "PDUFA action date: 3/5/2026 (priority review)."
    assert extract_pdufa_date(text) == "2026-03-05"


def test_date_iso_form():
    text = "The PDUFA goal date (2026-01-05) was confirmed in the FDA acceptance letter."
    assert extract_pdufa_date(text) == "2026-01-05"


# ---------------------------------------------------------------------------
# Context anchoring — decoy date and no-match.
# ---------------------------------------------------------------------------

def test_decoy_date_far_from_anchor_rejected():
    # A date that appears with no PDUFA token within ~200 chars is NOT taken,
    # even though another PDUFA sentence (with no date) exists elsewhere.
    filler = "x" * 400
    text = (
        "The board declared a dividend payable on February 14, 2026 to shareholders."
        + filler
        + "Separately, the company noted a PDUFA milestone but did not disclose a date."
    )
    assert extract_pdufa_date(text) is None


def test_no_pdufa_token_returns_none():
    text = "The Company reported quarterly revenue of $50 million for the period ended December 31, 2025."
    assert extract_pdufa_date(text) is None


def test_anchor_within_window_accepted_far_rejected():
    # Date 1 is far from any anchor (dividend context); date 2 is right after a
    # PDUFA anchor. The anchored one wins.
    text = (
        "A dividend was paid on June 1, 2025. " + ("y" * 300) +
        " The FDA set a PDUFA goal date of August 20, 2026."
    )
    assert extract_pdufa_date(text) == "2026-08-20"


def test_empty_and_none_safe():
    assert extract_pdufa_date("") is None
    assert extract_pdufa_date(None) is None  # type: ignore[arg-type]
    assert extract_pdufa(None) == PdufaExtract()


# ---------------------------------------------------------------------------
# Designations — present -> True, absent -> None (never False).
# ---------------------------------------------------------------------------

def test_designation_breakthrough():
    bt, ft, aa = extract_designations(
        "The therapy received Breakthrough Therapy designation from the FDA."
    )
    assert bt is True
    assert ft is None  # not mentioned -> unknown, NOT False
    assert aa is None


def test_designation_fast_track():
    bt, ft, aa = extract_designations(
        "FDA granted Fast Track status to the program in 2025."
    )
    assert ft is True
    assert bt is None
    assert aa is None


def test_designation_accelerated_approval():
    bt, ft, aa = extract_designations(
        "The application is being reviewed under the Accelerated Approval pathway."
    )
    assert aa is True
    assert bt is None
    assert ft is None


def test_designation_all_three_present():
    text = (
        "The drug holds Breakthrough Therapy designation and Fast Track status, "
        "and the BLA was submitted under Accelerated Approval."
    )
    bt, ft, aa = extract_designations(text)
    assert (bt, ft, aa) == (True, True, True)


def test_designation_none_present_all_unknown():
    bt, ft, aa = extract_designations(
        "The FDA set a PDUFA goal date of January 5, 2026 for the NDA."
    )
    assert (bt, ft, aa) == (None, None, None)


def test_designation_btd_acronym():
    bt, _ft, _aa = extract_designations("The program (BTD) advanced to filing.")
    assert bt is True


# ---------------------------------------------------------------------------
# appl_type hint
# ---------------------------------------------------------------------------

def test_appl_type_bla_precedence():
    assert extract_appl_type_hint(
        "The Biologics License Application (BLA) references the prior NDA."
    ) == "BLA"


def test_appl_type_nda():
    assert extract_appl_type_hint("The New Drug Application was accepted.") == "NDA"


def test_appl_type_none_when_absent():
    assert extract_appl_type_hint("The company reported earnings.") is None


# ---------------------------------------------------------------------------
# Top-level extract_pdufa — end to end on a realistic snippet.
# ---------------------------------------------------------------------------

def test_extract_pdufa_full_realistic():
    text = (
        "Acme Therapeutics, Inc. (ACME) today announced that the U.S. Food and Drug "
        "Administration has accepted for filing its New Drug Application for relacorilant "
        "and assigned a PDUFA goal date of January 5, 2026. The application was granted "
        "Breakthrough Therapy designation and Fast Track status."
    )
    out = extract_pdufa(text)
    assert out.pdufa_date_iso == "2026-01-05"
    assert out.drug_name == "relacorilant"  # INN-suffix 'corilant'
    assert out.has_bt is True
    assert out.has_ft is True
    assert out.has_aa is None  # not mentioned
    assert out.appl_type_hint == "NDA"


def test_drug_name_rejects_exhibit_residue():
    # "EX-99" / "EX-99.1" is 8-K exhibit residue, not a drug. Must be rejected.
    assert extract_drug_name("Exhibit EX-99.1 to the Current Report on Form 8-K.") is None
    assert extract_drug_name("See EX-99 for the press release.") is None


def test_drug_name_rejects_concept_cept_false_positive():
    # "concept" ends in the INN suffix 'cept' but is not a drug.
    assert extract_drug_name("This concept was discussed at the meeting.") is None


def test_drug_name_real_code_still_extracted():
    assert extract_drug_name("The Company reported on VK2735 in the study.") == "VK2735"


def test_extract_pdufa_code_drug_and_bla():
    text = (
        "Biotech Co (BIO) announced FDA acceptance of its Biologics License Application "
        "for VK2735 with a PDUFA action date of 09/30/2026 under Accelerated Approval."
    )
    out = extract_pdufa(text)
    assert out.pdufa_date_iso == "2026-09-30"
    assert out.drug_name == "VK2735"
    assert out.appl_type_hint == "BLA"
    assert out.has_aa is True
