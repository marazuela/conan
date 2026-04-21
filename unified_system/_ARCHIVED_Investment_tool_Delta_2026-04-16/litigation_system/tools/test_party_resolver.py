"""
test_party_resolver.py — Offline unit tests for Stage 1 normalization.

Runs without requests, rapidfuzz, or network access. Exercises every code
path in `normalize_party()` and the private classification helpers. Failures
print the failing case name, expected value, and actual value so a fresh
session can tell at a glance which invariant regressed.

Stage 2 smoke tests live in a separate file (test_party_resolver_live.py)
that requires network access and is NOT run here.

Usage:
  python tools/test_party_resolver.py

Exit code 0 on all pass, 1 on any failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Let the test file run either from litigation_system/ or from the project root.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import party_resolver as pr  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal test harness (no pytest dependency — avoids pip in sandbox-cold sessions)
# ---------------------------------------------------------------------------

_FAILURES = []
_PASSED = 0


def _check(label, actual, expected):
    global _PASSED
    if actual == expected:
        _PASSED += 1
        return True
    _FAILURES.append((label, expected, actual))
    return False


def _check_truthy(label, value):
    global _PASSED
    if value:
        _PASSED += 1
        return True
    _FAILURES.append((label, "truthy", value))
    return False


# ---------------------------------------------------------------------------
# Corporate-suffix stripping
# ---------------------------------------------------------------------------

def test_corporate_suffixes():
    # Simple Inc.
    r = pr.normalize_party("Apple Inc.")
    _check("apple-inc normalized_name", r.normalized_name, "apple")
    _check("apple-inc party_class", r.party_class, "corporate_entity")
    _check_truthy("apple-inc stripped non-empty", r.stripped_suffixes)

    # LLC variants
    r = pr.normalize_party("Acme Widgets L.L.C.")
    _check("acme-llc normalized_name", r.normalized_name, "acme widgets")
    r = pr.normalize_party("Acme Widgets LLC")
    _check("acme-llc2 normalized_name", r.normalized_name, "acme widgets")

    # Corporation
    r = pr.normalize_party("Ford Motor Corporation")
    _check("ford-corp normalized_name", r.normalized_name, "ford motor")

    # Stacked suffixes (resolver should strip multiple passes up to its cap)
    r = pr.normalize_party("Acme Holdings, Inc.")
    _check("acme-holdings-inc normalized_name", r.normalized_name, "acme")
    _check_truthy("acme-holdings stripped count >= 2", len(r.stripped_suffixes) >= 2)

    # European forms
    _check("gmbh", pr.normalize_party("Siemens GmbH").normalized_name, "siemens")
    _check("ag", pr.normalize_party("Bayer AG").normalized_name, "bayer")
    _check("plc", pr.normalize_party("BP PLC").normalized_name, "bp")
    _check("sa", pr.normalize_party("LVMH S.A.").normalized_name, "lvmh")

    # No suffix — stays as-is, class=unknown or individual
    r = pr.normalize_party("Berkshire Hathaway")
    _check("no-suffix party_class not corporate",
           r.party_class in {"individual", "unknown"}, True)
    _check("no-suffix stripped empty", r.stripped_suffixes, [])


# ---------------------------------------------------------------------------
# Government classification
# ---------------------------------------------------------------------------

def test_government():
    r = pr.normalize_party("United States of America")
    _check("usa party_class", r.party_class, "government")

    r = pr.normalize_party("Securities and Exchange Commission")
    _check("sec party_class", r.party_class, "government")

    r = pr.normalize_party("Federal Trade Commission")
    _check("ftc party_class", r.party_class, "government")

    r = pr.normalize_party("Department of Justice")
    _check("doj party_class", r.party_class, "government")

    r = pr.normalize_party("State of California")
    _check("ca party_class", r.party_class, "government")


# ---------------------------------------------------------------------------
# Individual classification
# ---------------------------------------------------------------------------

def test_individuals():
    r = pr.normalize_party("John Q. Smith, an individual")
    _check("individual-explicit class", r.party_class, "individual")

    r = pr.normalize_party("Jane Doe, Defendant")
    _check("individual-role class", r.party_class, "individual")

    # Bare two-name heuristic
    r = pr.normalize_party("Elon Musk")
    _check("bare-name class", r.party_class, "individual")


# ---------------------------------------------------------------------------
# Unicode & whitespace normalization
# ---------------------------------------------------------------------------

def test_unicode_and_whitespace():
    # Fullwidth chars and extra whitespace
    r = pr.normalize_party("  \u0041pple\u3000Inc. ")
    _check("fullwidth normalized", r.normalized_name, "apple")

    # Accented name — should fold via NFKC (accents retained but consistent form)
    r = pr.normalize_party("Nestle S.A.")
    _check("nestle lowercased", r.normalized_name, "nestle")


# ---------------------------------------------------------------------------
# Signal raw_data projection
# ---------------------------------------------------------------------------

def test_signal_raw_data_projection():
    res = pr.Resolution(
        raw_name="Apple Inc.",
        normalized_name="apple",
        party_class="corporate_entity",
        method="sec_edgar_exact",
        confidence=0.95,
        cik="0000320193",
        ticker="AAPL",
        issuer_figi="BBG000B9XRY4",
        issuer_name="APPLE INC",
        resolved_at="2026-04-14T13:00:00Z",
    )
    rd = res.as_signal_raw_data()
    _check("signal party_raw_name", rd["party_raw_name"], "Apple Inc.")
    _check("signal resolution_method", rd["resolution_method"], "sec_edgar_exact")
    _check("signal confidence", rd["resolution_confidence"], 0.95)


# ---------------------------------------------------------------------------
# Holdings caveat — regression guard
# ---------------------------------------------------------------------------

def test_holdings_caveat():
    # D-003 caveat: stripping "Holdings" can cause false matches. Confirm
    # the resolver records what was stripped so callers can down-weight.
    r = pr.normalize_party("Berkshire Hathaway Holdings")
    _check("holdings-recorded",
           any("holdings" in s.lower() for s in r.stripped_suffixes),
           True)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def main():
    test_corporate_suffixes()
    test_government()
    test_individuals()
    test_unicode_and_whitespace()
    test_signal_raw_data_projection()
    test_holdings_caveat()

    total = _PASSED + len(_FAILURES)
    print(f"\n{_PASSED}/{total} checks passed")
    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILURES:")
        for label, expected, actual in _FAILURES:
            print(f"  - {label}")
            print(f"      expected: {expected!r}")
            print(f"      actual:   {actual!r}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
