"""Golden-vector tests for openfigi_resolver.normalize_ticker.

These vectors encode the load-bearing JP 5-char alphanumeric fix (Q-003 / Q-016) and its
negation outside JP MICs. Any future refactor of normalize_ticker must keep these passing
byte-for-byte — this is the preservation covenant from PRD §6.

Run: python -m pytest modal_workers/tests/test_openfigi_normalize.py -v
"""
from __future__ import annotations

import pytest

from modal_workers.shared.openfigi_resolver import normalize_ticker


# ----------------------------------------------------------------------
# JP 5-char alphanumeric fix: must strip trailing '0' when len==5 AND position 3 is a
# letter AND position 4 is '0' AND the MIC is a known JP MIC (or unspecified).
# ----------------------------------------------------------------------

@pytest.mark.parametrize("ticker,mic,expected", [
    # Canonical Q-003 cases — the two tickers that motivated the fix.
    ("469A0", "XTKS", "469A"),
    ("364A0", "XTKS", "364A"),
    # Other JP MICs covered by the fix.
    ("469A0", "XJPX", "469A"),
    ("469A0", "XSAP", "469A"),
    ("469A0", "XNGO", "469A"),
    ("469A0", "XFKA", "469A"),
    # MIC unspecified — fix still applies (v1 behavior: conservative strip).
    ("469A0", None, "469A"),
    # Lowercase input normalizes to uppercase before the pattern check.
    ("469a0", "XTKS", "469A"),
])
def test_jp_alphanumeric_5char_fix(ticker, mic, expected):
    assert normalize_ticker(ticker, mic) == expected


# ----------------------------------------------------------------------
# Negative cases: the fix must NOT apply.
# ----------------------------------------------------------------------

@pytest.mark.parametrize("ticker,mic,expected", [
    # Non-JP MICs — no trim (even when pattern matches).
    ("469A0", "XNYS", "469A0"),
    ("469A0", "XLON", "469A0"),
    ("469A0", "XHKG", "469A0"),
    # Pattern doesn't match — position 3 not a letter.
    ("46900", "XTKS", "46900"),
    # Pattern doesn't match — position 4 not '0'.
    ("469A1", "XTKS", "469A1"),
    # Wrong length — 4-char ticker untouched.
    ("7203", "XTKS", "7203"),
    # Wrong length — 6-char ticker untouched.
    ("12345A", "XTKS", "12345A"),
])
def test_jp_fix_does_not_overreach(ticker, mic, expected):
    assert normalize_ticker(ticker, mic) == expected


# ----------------------------------------------------------------------
# Canonical non-JP cases — used in the v1 CLI self-test at openfigi_resolver.py:330-333.
# ----------------------------------------------------------------------

def test_us_ticker_passthrough():
    assert normalize_ticker("AAPL", None) == "AAPL"
    assert normalize_ticker("aapl", None) == "AAPL"  # uppercased


def test_jp_numeric_4char_passthrough():
    assert normalize_ticker("7203", "XTKS") == "7203"


# ----------------------------------------------------------------------
# Edge cases.
# ----------------------------------------------------------------------

def test_empty_input_returns_unchanged():
    assert normalize_ticker("", None) == ""
    assert normalize_ticker("", "XTKS") == ""


def test_whitespace_stripped():
    assert normalize_ticker("  AAPL  ", None) == "AAPL"
    assert normalize_ticker("  469A0  ", "XTKS") == "469A"
