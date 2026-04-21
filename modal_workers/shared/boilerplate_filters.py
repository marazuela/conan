"""
boilerplate_filters — per-exchange regex drop-lists.

Verbatim port of tools/boilerplate_filters.py. Used by non-US scanners to drop
routine filings (voting results, auditor-change notices, PDMR shareholdings,
etc.) that carry no structural signal.

Usage:
    from modal_workers.shared.boilerplate_filters import is_boilerplate
    if is_boilerplate("LSE", headline):
        continue
"""

from __future__ import annotations

import re

_PATTERNS: dict[str, list[str]] = {
    "LSE": [
        r"^total\s+voting\s+rights",
        r"^holding\s+\(s\)\s+in\s+company",
        r"^director\s*/\s*pdmr\s+shareholding",
        r"^notification\s+of\s+major\s+interest\s+in\s+shares",
        r"^additional\s+listing",
        r"^block\s+listing\s+six\s+monthly\s+return",
        r"^transaction\s+in\s+own\s+shares",
        r"^director\s+/\s+pdmr\s+shareholding\s*$",
        r"^annual\s+information\s+update",
        r"^issue\s+of\s+equity",
        r"^result\s+of\s+agm",
        r"^result\s+of\s+meeting",
        r"^replacement",
        r"^amendment\s+to",
    ],
    "TDnet": [
        r"自己株式の取得状況",
        r"役員の異動",
        r"定款の一部変更",
        r"コーポレートガバナンスに関する報告書",
        r"独立役員届出書",
    ],
    "ASX": [
        r"^change\s+of\s+director'?s?\s+interest\s+notice",
        r"^initial\s+director's?\s+interest\s+notice",
        r"^final\s+director's?\s+interest\s+notice",
        r"^notification\s+of\s+dividend",
        r"^update\s*-\s*dividend",
        r"^becoming\s+a\s+substantial\s+holder",
        r"^ceasing\s+to\s+be\s+a\s+substantial\s+holder",
        r"^change\s+of\s+substantial\s+holding",
        r"^trading\s+halt\s+-\s+",
        r"^pause\s+in\s+trading",
        r"^notification\s+regarding\s+unquoted\s+securities",
    ],
    "SEDAR": [
        r"^news\s+release\s+-\s+voting\s+results",
        r"^notice\s+of\s+annual\s+meeting",
        r"^form\s+13-502f1",
        r"^certificate\s+of\s+filing",
    ],
    "HKEx": [
        r"next\s+day\s+disclosure\s+return",
        r"monthly\s+return",
        r"notification\s+regarding\s+change\s+of\s+directors?'?\s+information",
        r"list\s+of\s+directors\s+and\s+their\s+roles",
        r"change\s+in\s+the\s+information\s+of\s+directors",
    ],
    "KIND": [
        r"감사보고서 제출",
        r"사업보고서\s+\(일반\)",
        r"주주총회소집결의",
    ],
    "BSE_NSE": [
        r"disclosure\s+under\s+regulation\s+30\s*\(\s*loss\s+of\s+certificate\s*\)",
        r"disclosure\s+under\s+reg(ulation)?\s+30\s*-?\s*press\s+release",
        r"compliance\s+report\s+under\s+regulation\s+7\s*\(\s*3\s*\)",
        r"statement\s+of\s+investor\s+complaints",
        r"shareholding\s+pattern",
    ],
    "CVM": [
        r"^ata\s+de\s+reuni[aã]o\s+do\s+conselho\s+de\s+administra[cç][aã]o",
        r"aviso\s+aos\s+acionistas\s*-\s*juros",
        r"^edital\s+de\s+convoca[cç][aã]o",
    ],
    "BMV": [
        r"^asamblea\s+de\s+accionistas\s*-\s*convocatoria",
        r"^informaci[oó]n\s+corporativa\s*-\s*cambio\s+de\s+auditor",
        r"^eventos\s+corporativos\s*-\s*pago\s+de\s+dividendos",
    ],
}

_COMPILED: dict[str, list[re.Pattern]] = {
    key: [re.compile(p, re.IGNORECASE | re.UNICODE) for p in patterns]
    for key, patterns in _PATTERNS.items()
}


def is_boilerplate(exchange_key: str, headline: str) -> bool:
    if not headline:
        return False
    patterns = _COMPILED.get(exchange_key)
    if not patterns:
        return False
    return any(p.search(headline) for p in patterns)


def list_patterns(exchange_key: str) -> list[str]:
    return list(_PATTERNS.get(exchange_key, []))
