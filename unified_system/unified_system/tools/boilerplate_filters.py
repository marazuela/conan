"""
Boilerplate filters for Tool 2 (Non-US Discovery System).

Per-exchange regex lists of filing headlines / types that should NEVER survive
Stage 1 triage. These are housekeeping items that contain no structural signal.

Usage:
    from tools.boilerplate_filters import is_boilerplate
    if is_boilerplate("LSE", headline):
        continue  # skip
"""

from __future__ import annotations

import re

# Each value is a list of compiled regex patterns (case-insensitive).
# Headlines matching ANY pattern are dropped.
_PATTERNS: dict[str, list[str]] = {
    "LSE": [
        r"^total\s+voting\s+rights",
        r"^holding\s+\(s\)\s+in\s+company",       # some automated holdings filings
        r"^director\s*/\s*pdmr\s+shareholding",    # routine PDMR (unless flagged explicitly elsewhere)
        r"^notification\s+of\s+major\s+interest\s+in\s+shares",  # routine
        r"^additional\s+listing",
        r"^block\s+listing\s+six\s+monthly\s+return",
        r"^transaction\s+in\s+own\s+shares",       # routine buyback execution notices (keep buyback initiation)
        r"^director\s+/\s+pdmr\s+shareholding\s*$",
        r"^annual\s+information\s+update",
        r"^issue\s+of\s+equity",                   # routine share issuance admin
        r"^result\s+of\s+agm",
        r"^result\s+of\s+meeting",
        r"^replacement",
        r"^amendment\s+to",
    ],
    "TDnet": [
        r"自己株式の取得状況",                 # buyback execution report (routine)
        r"役員の異動",                          # routine director change (absent major-governance context)
        r"定款の一部変更",                      # articles of incorporation minor amendment
        r"コーポレートガバナンスに関する報告書",  # routine CG report update
        r"独立役員届出書",                      # independent director notice
    ],
    "ASX": [
        r"^change\s+of\s+director'?s?\s+interest\s+notice",
        r"^initial\s+director's?\s+interest\s+notice",
        r"^final\s+director's?\s+interest\s+notice",
        r"^notification\s+of\s+dividend",
        r"^update\s*-\s*dividend",
        r"^becoming\s+a\s+substantial\s+holder",    # handled by separate 603 signal
        r"^ceasing\s+to\s+be\s+a\s+substantial\s+holder",
        r"^change\s+of\s+substantial\s+holding",    # handled by 604 signal explicitly
        r"^trading\s+halt\s+-\s+",                   # routine halts pending announcement
        r"^pause\s+in\s+trading",
        r"^notification\s+regarding\s+unquoted\s+securities",
    ],
    "SEDAR": [
        r"^news\s+release\s+-\s+voting\s+results",
        r"^notice\s+of\s+annual\s+meeting",
        r"^form\s+13-502f1",                    # participation fee form
        r"^certificate\s+of\s+filing",
    ],
    "HKEx": [
        r"next\s+day\s+disclosure\s+return",     # routine DDR
        r"monthly\s+return",                      # monthly movements
        r"notification\s+regarding\s+change\s+of\s+directors?'?\s+information",
        r"list\s+of\s+directors\s+and\s+their\s+roles",
        r"change\s+in\s+the\s+information\s+of\s+directors",
    ],
    "KIND": [
        r"감사보고서 제출",                     # audit report submission (standalone)
        r"사업보고서\s+\(일반\)",                 # routine annual business report
        r"주주총회소집결의",                     # shareholder meeting convening resolution (routine)
    ],
    "BSE_NSE": [
        r"disclosure\s+under\s+regulation\s+30\s*\(\s*loss\s+of\s+certificate\s*\)",
        r"disclosure\s+under\s+reg(ulation)?\s+30\s*-?\s*press\s+release",  # press releases often just restate earnings
        r"compliance\s+report\s+under\s+regulation\s+7\s*\(\s*3\s*\)",
        r"statement\s+of\s+investor\s+complaints",
        r"shareholding\s+pattern",                # quarterly shareholding pattern is routine
    ],
    "CVM": [
        r"^ata\s+de\s+reuni[aã]o\s+do\s+conselho\s+de\s+administra[cç][aã]o",  # board meeting minutes (routine)
        r"aviso\s+aos\s+acionistas\s*-\s*juros",                                 # interest-on-equity notices
        r"^edital\s+de\s+convoca[cç][aã]o",                                        # meeting convocation (routine)
    ],
    "BMV": [
        r"^asamblea\s+de\s+accionistas\s*-\s*convocatoria",                       # meeting call (routine)
        r"^informaci[oó]n\s+corporativa\s*-\s*cambio\s+de\s+auditor",              # auditor change
        r"^eventos\s+corporativos\s*-\s*pago\s+de\s+dividendos",                   # routine dividend payment notice
    ],
}

# Compiled at import time for speed
_COMPILED: dict[str, list[re.Pattern]] = {
    key: [re.compile(p, re.IGNORECASE | re.UNICODE) for p in patterns]
    for key, patterns in _PATTERNS.items()
}


def is_boilerplate(exchange_key: str, headline: str) -> bool:
    """
    Returns True if the headline matches any boilerplate pattern for this exchange.

    `exchange_key` should be one of: LSE, TDnet, ASX, SEDAR, HKEx, KIND, BSE_NSE, CVM, BMV.
    Unknown exchange_keys return False (fail-open — better to let a signal through
    than drop it based on an unregistered filter list).
    """
    if not headline:
        return False
    patterns = _COMPILED.get(exchange_key)
    if not patterns:
        return False
    return any(p.search(headline) for p in patterns)


def list_patterns(exchange_key: str) -> list[str]:
    """Return raw pattern strings for inspection/debugging."""
    return list(_PATTERNS.get(exchange_key, []))


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        exch = sys.argv[1]
        hdl = sys.argv[2]
        print(f"{exch}: {hdl!r} -> boilerplate={is_boilerplate(exch, hdl)}")
    else:
        for exch in _PATTERNS:
            print(f"{exch}: {len(_PATTERNS[exch])} patterns")

# --- END OF FILE ---
