"""Route an FDA catalyst to the correct CRL model (or refuse).

Scope decision from application_type + drugsfda submission_type /
submission_class_code:

  original            -> NDA model   (first-cycle original NDA/BLA, non-biosimilar)
  efficacy_supplement -> sNDA model  (efficacy supplement: new indication/dosing/
                                      population, confirmatory, accel-approval)
  refused             -> neither     (biosimilar BLA, resubmission, CMC/labeling/
                                      facility supplement, or unclassifiable)

The refusal set encodes the two models' documented scope limits: the NDA model
excludes supplements / resubmissions / biosimilar BLAs; the sNDA model targets
only efficacy-failure CRLs (CMC / labeling / facility supplements are negatives,
hence out of scope for *risk* prediction here).
"""

from __future__ import annotations

from typing import Optional

ORIGINAL = "original"
EFFICACY_SUPPLEMENT = "efficacy_supplement"
REFUSED = "refused"


def _norm(value: object) -> str:
    return str(value or "").strip().upper().replace("-", " ").replace("_", " ")


# submission_class_code substrings that mark an *efficacy* supplement.
_EFFICACY_CLASS_TOKENS = (
    "EFFICACY",
    "NEW INDICATION",
    "TYPE 6",  # openFDA "TYPE 6 - NEW INDICATION"
    "NEW PATIENT POPULATION",
    "NEW DOSING",
    "NEW DOSE",
)
# submission_class_code substrings that are explicitly NOT efficacy.
_NON_EFFICACY_CLASS_TOKENS = (
    "LABELING",
    "MANUF",  # MANUF (CMC)
    "CMC",
    "CHEMISTRY",
    "FACILITY",
    "REMS",
)


def classify_scope(catalyst: dict) -> dict:
    """Return {'scope': <str>, 'reason': <str|None>} for a catalyst.

    Recognized keys (all optional; routing degrades on missing data):
      application_type      'NDA'|'BLA'|'sNDA'|'sBLA' ...
      submission_type       'ORIG'|'SUPPL'|'RESUBMISSION' (drugsfda)
      submission_class_code drugsfda class, e.g. 'TYPE 6 - NEW INDICATION'
      is_biosimilar         truthy -> refuse
      is_resubmission       truthy -> refuse
      cycle_type            'first_cycle_orig' hints original
    """
    appl = _norm(catalyst.get("application_type") or catalyst.get("ApplType"))
    sub_type = _norm(catalyst.get("submission_type"))
    sub_class = _norm(catalyst.get("submission_class_code") or catalyst.get("SubmissionClassCode"))
    cycle = _norm(catalyst.get("cycle_type"))

    if _truthy(catalyst.get("is_biosimilar")) or "BIOSIMILAR" in sub_class or "BIOSIMILAR" in appl:
        return _ref("biosimilar_bla_out_of_scope")
    if _truthy(catalyst.get("is_resubmission")) or "RESUBMISSION" in sub_type:
        return _ref("resubmission_out_of_scope")

    is_supplement = (
        sub_type == "SUPPL"
        or appl.startswith("SNDA")
        or appl.startswith("SBLA")
        or appl in ("SNDA", "SBLA")
    )
    is_original = (
        sub_type in ("ORIG", "ORIG 1", "ORIGINAL")
        or cycle == "FIRST CYCLE ORIG"
        or (not is_supplement and appl in ("NDA", "BLA"))
    )

    if is_supplement:
        if any(tok in sub_class for tok in _NON_EFFICACY_CLASS_TOKENS):
            return _ref("non_efficacy_supplement_out_of_scope")
        if any(tok in sub_class for tok in _EFFICACY_CLASS_TOKENS):
            return {"scope": EFFICACY_SUPPLEMENT, "reason": None}
        # Supplement of unknown class: cannot confirm efficacy target.
        return _ref("supplement_class_unclassified")

    if is_original:
        return {"scope": ORIGINAL, "reason": None}

    return _ref("unclassifiable_catalyst")


def _ref(reason: str) -> dict:
    return {"scope": REFUSED, "reason": reason}


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return _norm(value) in {"1", "TRUE", "T", "YES", "Y"}
    return bool(value)
