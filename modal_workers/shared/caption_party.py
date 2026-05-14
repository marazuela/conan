"""Extract a corporate party name from a court-case caption.

Used by courtlistener_scanner to pass a CLEAN company name to
`entity_hints.name` rather than the full caption. Passing the full caption
(e.g., "People of the State of California v. REV Group Inc") creates junk
entities that never resolve to FIGI/ticker — see the 2026-04-23 log review.

Two outputs:
  name        str   — the extracted corporate party, or "" if extraction failed
  confidence  float — 0.0 to 1.0; higher = cleaner extraction. Scanners pass
                      this to `raw_data.party_resolution_confidence` so the
                      litigation.party_confidence_cap catches low-confidence rows.

Confidence scale:
  1.0  "In re X Stockholders/Derivative Litigation" — unambiguous corporate target
  0.9  "X Corp. v. Y Corp." where exactly one side has a corporate suffix
  0.7  "Individual Name v. X Corp." — individual-vs-corporate after cleanup
  0.5  "X Corp. v. Y Corp." both sides corporate (ambiguous)
  0.3  no corporate suffix found but non-trivial party survived cleanups
  0.0  extraction failed / empty / only individuals

Design notes:
  - Heuristic-only (no NER). Good enough for the ~90% of captions with a
    corporate suffix; the 10% long tail is caught by the confidence score and
    the rubric's party_confidence_cap.
  - Government-plaintiff prefixes are stripped so the defendant shows through:
    "People of The State of California v. REV Group Inc" → "REV Group Inc" (0.7).
  - Returns the cleaned name including its suffix so downstream SEC-company
    lookups can match "Tesla, Inc." ↔ "Tesla, Inc." cleanly.
"""

from __future__ import annotations

import re
from typing import Tuple


# Corporate entity suffixes — the "is this a company?" signal.
_ENTITY_SUFFIX_RE = re.compile(
    r"\b(Inc\.?|Corp\.?|Corporation|LLC|L\.L\.C\.?|Ltd\.?|LP|L\.P\.?|"
    r"Company|Co\.?|Holdings?|Partners|Trust|Bank|N\.A\.?|N\.V\.?|"
    r"PLC|S\.A\.?|AG|GmbH|AB|SE|B\.V\.?|S\.p\.A\.?|S\.r\.l\.?)\b",
    re.IGNORECASE,
)

# Government-plaintiff prefixes we strip so the corporate defendant survives.
# Each is evaluated as a leading match only (^...).
_GOV_PLAINTIFF_PATTERNS = [
    re.compile(r"^people\s+of\s+(the\s+)?state\s+of\s+[A-Za-z .]+?\s+v\.?\s+",
               re.IGNORECASE),
    re.compile(r"^people\s+v\.?\s+", re.IGNORECASE),
    re.compile(r"^state\s+of\s+[A-Za-z .]+?\s+v\.?\s+", re.IGNORECASE),
    re.compile(r"^commonwealth\s+of\s+[A-Za-z .]+?\s+v\.?\s+", re.IGNORECASE),
    re.compile(r"^united\s+states(\s+of\s+america)?\s+v\.?\s+", re.IGNORECASE),
    re.compile(r"^u\.?s\.?a?\.?\s+v\.?\s+", re.IGNORECASE),
    re.compile(r"^sec\s+v\.?\s+", re.IGNORECASE),
    re.compile(r"^s\.e\.c\.\s+v\.?\s+", re.IGNORECASE),
    re.compile(r"^ftc\s+v\.?\s+", re.IGNORECASE),
    re.compile(r"^f\.t\.c\.\s+v\.?\s+", re.IGNORECASE),
    re.compile(r"^eeoc\s+v\.?\s+", re.IGNORECASE),
    re.compile(r"^doj\s+v\.?\s+", re.IGNORECASE),
    re.compile(r"^cfpb\s+v\.?\s+", re.IGNORECASE),
    re.compile(r"^nlrb\s+v\.?\s+", re.IGNORECASE),
]

# Chancery-specific caption idioms we strip to isolate the corporate party.
_CAPTION_CLEANUPS = [
    # "In re X Stockholders Litigation" → "X"
    re.compile(r"^in\s+re\s+", re.IGNORECASE),
    (re.compile(r"\bstockholders?\s+litigation\b", re.IGNORECASE), " "),
    (re.compile(r"\bderivative\s+litigation\b", re.IGNORECASE), " "),
    (re.compile(r"\bsecurities\s+litigation\b", re.IGNORECASE), " "),
    (re.compile(r"\bappraisal\s+(proceedings?|of)\b", re.IGNORECASE), " "),
]

# Trailing noise we strip after party extraction.
_TRAILING_NOISE_RE = re.compile(
    r"(,?\s+et\s+al\.?|,?\s+and\s+others?)\s*$", re.IGNORECASE,
)


def _strip_gov_plaintiff(caption: str) -> Tuple[str, bool]:
    """Strip a government-plaintiff prefix if one leads the caption.
    Returns (stripped_caption, was_stripped)."""
    for pat in _GOV_PLAINTIFF_PATTERNS:
        m = pat.match(caption)
        if m:
            return caption[m.end():], True
    return caption, False


def _strip_trailing_noise(s: str) -> str:
    s = _TRAILING_NOISE_RE.sub("", s)
    return s.strip(" ,;:")


def _looks_like_individual_name(s: str) -> bool:
    """Heuristic: does `s` look like a person's name (not a company)?

    A person's name typically has no corporate suffix AND is 1-4 short words.
    """
    if _ENTITY_SUFFIX_RE.search(s):
        return False
    words = s.split()
    if not (1 <= len(words) <= 4):
        return False
    # Single short all-uppercase word → acronym, probably not an individual.
    if len(words) == 1 and s.isupper() and len(s) <= 4:
        return False
    # Individuals are usually Title-Case or UPPER; if any word is obviously a
    # sentence word ("Acme", "National", etc.) we don't try to distinguish.
    return all(len(w) <= 20 for w in words)


def extract_corporate_party(caption: str) -> Tuple[str, float]:
    """Extract the most likely corporate party and a confidence score.

    See module docstring for the confidence scale.
    """
    if not caption:
        return "", 0.0
    s = caption.strip()
    if not s:
        return "", 0.0

    # 1. Strip government-plaintiff prefix first (changes confidence ceiling).
    s, gov_stripped = _strip_gov_plaintiff(s)
    s = s.strip(" ,;:")

    # 2. Strip Chancery idioms (In re, Stockholders Litigation, etc.).
    # These mutate both the "In re" prefix and trailing phrases.
    had_in_re = bool(re.match(r"^in\s+re\s+", s, re.IGNORECASE))
    for rule in _CAPTION_CLEANUPS:
        if isinstance(rule, tuple):
            pat, repl = rule
            s = pat.sub(repl, s)
        else:
            s = rule.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" ,;:")

    # 3. Strip trailing "et al", ", and others".
    s = _strip_trailing_noise(s)
    if not s:
        return "", 0.0

    # 4. Try splitting on " v. " / " v " / " vs. " — prefer the corporate side.
    parts = re.split(r"\s+vs?\.?\s+", s, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2:
        left, right = parts[0].strip(" ,"), parts[1].strip(" ,")
        left = _strip_trailing_noise(left)
        right = _strip_trailing_noise(right)
        left_hit = bool(_ENTITY_SUFFIX_RE.search(left))
        right_hit = bool(_ENTITY_SUFFIX_RE.search(right))

        if left_hit and not right_hit:
            # corp v individual — corp wins, confidence 0.9 (or 0.7 if gov-strip)
            conf = 0.7 if gov_stripped else 0.9
            return left, conf
        if right_hit and not left_hit:
            # individual v corp — corp wins
            conf = 0.7 if gov_stripped else 0.9
            return right, conf
        if left_hit and right_hit:
            # Both corporate — ambiguous. Prefer defendant (right side)
            # since most consequential filings target the defendant.
            return right, 0.5
        # Neither side has a corporate suffix.
        # If one side looks like an individual name, prefer the other.
        left_is_ind = _looks_like_individual_name(left)
        right_is_ind = _looks_like_individual_name(right)
        if left_is_ind and not right_is_ind and right:
            return right, 0.3
        if right_is_ind and not left_is_ind and left:
            return left, 0.3
        # Both look individual or both non-corp — weak signal.
        return right if right else left, 0.2

    # 5. No "v." split — single name after cleanups.
    # If In re was stripped and a corporate suffix survives, high confidence.
    if had_in_re and _ENTITY_SUFFIX_RE.search(s):
        return s, 1.0
    if _ENTITY_SUFFIX_RE.search(s):
        # Gov-plaintiff stripped + only defendant surviving → medium confidence.
        return s, 0.7 if gov_stripped else 0.9
    if s and not _looks_like_individual_name(s):
        return s, 0.3
    return "", 0.0
