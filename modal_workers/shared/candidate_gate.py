"""
Candidate gate — v2 port of tools/candidate_gate.py.

Preserves v1 thesis-quality rules VERBATIM (PRD §6 + D-008):
  - REQUIRED_FIELDS: situation, why_underpriced, next_catalyst, next_catalyst_date,
    kill_conditions.
  - MIN_FIELD_CHARS: situation ≥80, why_underpriced ≥100, next_catalyst ≥40,
    kill_conditions ≥60 (non-whitespace chars).
  - BOILERPLATE_PATTERNS: the 6 regexes that mark a thesis as scanner-generated filler.
  - next_catalyst_date parser: ISO, quarter/half/month band, or month+year.

What changed vs v1:
  - No filesystem side effects. `assess_thesis` is pure. `render_candidate_markdown`
    returns a string; the caller (thesis_writer in Modal, or the dashboard) is
    responsible for persisting to `candidates` table + Storage.
  - `promote_candidate` is NOT ported — v2 routes promotion through thesis_writer
    which owns the DB upsert. The gate validates; thesis_writer persists.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

REQUIRED_FIELDS = [
    "situation",
    "why_underpriced",
    "next_catalyst",
    "next_catalyst_date",
    "kill_conditions",
]

MIN_FIELD_CHARS = {
    "situation": 80,
    "why_underpriced": 100,
    "next_catalyst": 40,
    "kill_conditions": 60,
}

BOILERPLATE_PATTERNS = [
    r"scanner\s+classified\s+signal_type",
    r"tdnet\s+filed\s+\w+\s+for",
    r"auto[-\s]generated\s+by",
    r"placeholder\s+thesis",
    r"no\s+thesis\s+yet",
    r"to\s+be\s+researched",
]

_BOILERPLATE_RE = re.compile("|".join(BOILERPLATE_PATTERNS), re.IGNORECASE)


def _non_ws_len(s: str) -> int:
    return len(re.sub(r"\s+", "", s or ""))


def assess_thesis(thesis: Optional[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """Return (ok, reasons). reasons is a list of why the thesis failed.

    Verbatim port of tools/candidate_gate.py::assess_thesis — any behavior change
    here breaks the v1→v2 preservation covenant and must be flagged in spec.md §12.
    """
    reasons: List[str] = []

    if not isinstance(thesis, dict):
        return False, ["thesis is missing or not a dict"]

    for field in REQUIRED_FIELDS:
        val = thesis.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            reasons.append(f"missing required field: {field}")
            continue
        if isinstance(val, str):
            min_chars = MIN_FIELD_CHARS.get(field, 0)
            if min_chars and _non_ws_len(val) < min_chars:
                reasons.append(
                    f"{field}: too short ({_non_ws_len(val)} chars, need >= {min_chars})"
                )
            if _BOILERPLATE_RE.search(val):
                reasons.append(f"{field}: matches scanner boilerplate pattern")

    cat_date = thesis.get("next_catalyst_date")
    if isinstance(cat_date, str) and cat_date.strip():
        cd = cat_date.strip()
        iso_ok = re.match(r"^\d{4}-\d{2}-\d{2}", cd)
        band_ok = re.match(r"^(Q[1-4]|H[12]|early|mid|late)\s+\d{4}", cd, re.IGNORECASE)
        month_ok = re.match(
            r"^(January|February|March|April|May|June|July|August|"
            r"September|October|November|December|Jan|Feb|Mar|Apr|Jun|"
            r"Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+\d{4}",
            cd, re.IGNORECASE,
        )
        if not (iso_ok or band_ok or month_ok):
            reasons.append("next_catalyst_date: not an ISO date or recognizable range")

    return (len(reasons) == 0), reasons


def render_candidate_markdown(
    signal: Dict[str, Any],
    thesis: Dict[str, Any],
    *,
    band: str,
    scoring_profile: Optional[str],
    entity: Optional[Dict[str, Any]] = None,
) -> str:
    """Render the canonical dossier markdown. Returns string; caller persists.

    `signal` fields consumed: signal_id, scanner_id/scanner_name (either), signal_type,
        source_url, source_date, scan_date, score_with_bonus or score.
    `entity` (optional): name, primary_ticker, primary_mic. When absent, falls back to
        hints embedded in signal.raw_payload.
    `thesis` fields consumed: the 5 REQUIRED_FIELDS plus optional timeline, sources.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ticker = (entity or {}).get("primary_ticker") \
        or signal.get("ticker_local") or signal.get("ticker") or "UNK"
    mic = (entity or {}).get("primary_mic") or signal.get("mic") or "UNK"
    company = (entity or {}).get("name") \
        or signal.get("company_name_en") or signal.get("company_name_local") or "Unknown"

    score = signal.get("score_with_bonus") or signal.get("score_total") or signal.get("score")
    sig_id = signal.get("signal_id") or ""
    scanner = signal.get("scanner_name") or signal.get("scanner") or ""
    signal_type = signal.get("signal_type") or ""
    sig_date = signal.get("source_date") or signal.get("scan_date") or ""
    src_url = signal.get("source_url") or ""

    frontmatter = "\n".join([
        "---",
        f"ticker_local: {_yaml_scalar(ticker)}",
        f"mic: {_yaml_scalar(mic)}",
        f"company: {_yaml_scalar(company)}",
        f"scoring_profile: {_yaml_scalar(scoring_profile or 'unclassified')}",
        f"band: {_yaml_scalar(band)}",
        f"score: {_yaml_scalar(score)}",
        f"signal_id: {_yaml_scalar(sig_id)}",
        f"scanner: {_yaml_scalar(scanner)}",
        f"signal_type: {_yaml_scalar(signal_type)}",
        f"signal_date: {_yaml_scalar(sig_date)}",
        f"candidate_created: {_yaml_scalar(today)}",
        "gate_version: 2",
        "authored_by: claude_thesis_writer",
        "---",
    ])

    body = [
        f"# {ticker}.{mic} — {company}",
        "",
        f"**Band**: {band}   **Score**: {score}   **Profile**: {scoring_profile or 'unclassified'}",
        "",
        "## Situation",
        "",
        (thesis.get("situation") or "").strip(),
        "",
        "## Why this is under-priced",
        "",
        (thesis.get("why_underpriced") or "").strip(),
        "",
        "## Next catalyst",
        "",
        f"- **Date**: {(thesis.get('next_catalyst_date') or '').strip()}",
        f"- **Event**: {(thesis.get('next_catalyst') or '').strip()}",
        "",
        "## Kill conditions",
        "",
        (thesis.get("kill_conditions") or "").strip(),
        "",
    ]

    if thesis.get("timeline"):
        body += ["## Timeline", "", str(thesis["timeline"]).strip(), ""]
    if thesis.get("sources"):
        body += ["## Sources", ""]
        sources = thesis["sources"]
        if isinstance(sources, list):
            body += [f"- {s}" for s in sources]
        else:
            body.append(str(sources))
        body.append("")
    if src_url:
        body += [f"Primary source: {src_url}", ""]

    return frontmatter + "\n\n" + "\n".join(body)


# ---------------------------------------------------------------------------
# v2 gate — extends v1 with steelman, web_research, reasoning-tag coverage, and
# structured kill conditions. Used by the Claude thesis_writer pipeline (§6.1 step 8b,
# §7.4). v1 `assess_thesis` remains importable for historical-dossier import (§9.3).
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"\[(verified|inferred|speculated)\]", re.IGNORECASE)

# Heuristics that flag a sentence as "load-bearing" per spec.md §7.1 assess_thesis_v2:
#   - contains any decimal number
#   - contains a 2+ capitalized-word proper-noun phrase
#   - contains a quarter/half date band (e.g. "Q2 2026")
#   - contains an ISO-like date "YYYY-MM-DD"
_LOAD_BEARING_RE = re.compile(
    r"\d+\.?\d*|"
    r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+|"
    r"\b(?:Q[1-4]|H[12])\s+\d{4}\b|"
    r"\b\d{4}-\d{2}-\d{2}\b"
)

_VALID_LEANS = {"strengthening", "weakening", "neutral"}

# URL schemes that are safe to store in `candidates.dossier_markdown`. Dashboard
# renders the markdown-as-HTML, so a `javascript:`/`data:`/`vbscript:` URL in
# web_research entries becomes an XSS vector on click or on render. Whitelist
# the two schemes that make sense for research citations: http and https.
_SAFE_URL_SCHEMES = ("http://", "https://")


def _yaml_scalar(v: Any) -> str:
    """Render a Python value as a safe YAML scalar.

    Third-party feeds (ESMA, EDGAR, BMV) populate entity.name, ticker_local,
    etc. with whatever the source emits — occasionally including quotes,
    newlines, or colons that corrupt the frontmatter YAML. Use PyYAML-style
    double-quoted escape rules so:
      - strings with embedded `"` get `\\"` escaped
      - newlines become `\\n`
      - backslashes become `\\\\`
      - everything is wrapped in quotes so `:` and leading `-` don't trigger
        YAML flow syntax
    None / numbers pass through as plain YAML.
    """
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def _split_sentences(s: str) -> List[str]:
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+", s or "") if x.strip()]


def _validate_web_research(web: Any, reasons: List[str]) -> None:
    if not isinstance(web, list) or len(web) < 3:
        got = len(web) if isinstance(web, list) else 0
        reasons.append(f"web_research: need >=3 entries (got {got})")
        return
    non_strengthening = 0
    for i, entry in enumerate(web):
        if not isinstance(entry, dict):
            reasons.append(f"web_research[{i}]: not an object")
            continue
        url = entry.get("url")
        ret = entry.get("retrieved_at")
        finding = entry.get("finding")
        lean = entry.get("lean")
        if not isinstance(url, str) or not url.strip():
            reasons.append(f"web_research[{i}]: missing url")
        else:
            lowered = url.strip().lower()
            if not any(lowered.startswith(s) for s in _SAFE_URL_SCHEMES):
                reasons.append(
                    f"web_research[{i}]: url must start with http:// or https:// "
                    f"(got scheme {lowered.split(':', 1)[0]!r})"
                )
        if not isinstance(ret, str) or not re.match(r"^\d{4}-\d{2}-\d{2}", ret):
            reasons.append(f"web_research[{i}]: retrieved_at must be ISO-8601 date")
        if not isinstance(finding, str) or _non_ws_len(finding) < 40:
            got = _non_ws_len(finding) if isinstance(finding, str) else 0
            reasons.append(f"web_research[{i}]: finding too short ({got} chars, need >=40)")
        if lean not in _VALID_LEANS:
            reasons.append(f"web_research[{i}]: lean must be one of {sorted(_VALID_LEANS)}")
        elif lean != "strengthening":
            non_strengthening += 1
    if non_strengthening < 1:
        reasons.append("web_research: need >=1 entry with lean != 'strengthening' (steelman-in-practice)")


def _validate_reasoning_tags(situation: str, why_underpriced: str, steelman: str,
                             reasons: List[str]) -> None:
    body = " ".join([situation or "", why_underpriced or "", steelman or ""])
    tags = _TAG_RE.findall(body)
    total = len(tags)
    verified = sum(1 for t in tags if t.lower() == "verified")
    if total < 5:
        reasons.append(
            f"reasoning_tag_coverage: need >=5 [verified]/[inferred]/[speculated] tags "
            f"across situation+why_underpriced+steelman (got {total})"
        )
    if verified < 1:
        reasons.append("reasoning_tag_coverage: need >=1 [verified] anchor")

    # Count untagged sentences that carry load-bearing claims.
    violations = 0
    for sentence in _split_sentences(body):
        if _TAG_RE.search(sentence):
            continue
        if _LOAD_BEARING_RE.search(sentence):
            violations += 1
    if violations > 2:
        reasons.append(
            f"reasoning_tag_coverage: {violations} load-bearing claim sentences untagged (limit 2)"
        )


# Coherence-bridging tokens between `situation` and `why_underpriced`. The
# motivating failure mode: a thesis with a coherent situation paragraph and a
# separately-coherent why_underpriced paragraph that describe DIFFERENT theses
# (e.g. takeover-target situation paired with FDA-narrative why). v2's other
# checks (length, boilerplate, reasoning-tag coverage) all pass on such drafts
# because each field is internally well-formed.
#
# Extraction targets named-entity-ish tokens that bridge the two fields:
#   1. Multi-word proper nouns ("Vanguard Total Stock Market")
#   2. ALL-CAPS acronyms 3+ chars (FDA, SEC, PDUFA), incl. SEC form codes
#   3. Mixed-case proper nouns 4+ chars (Drug, Vanguard, Texas)
#   4. SEC form codes with leading digits (8-K, 13D, 10-Q)
#   5. ISO dates and quarter bands
# Pure numbers are excluded — too noisy. Common short capitalized words
# ("The", "And") fall outside the 4+ char and ALL-CAPS gates.
_COHERENCE_TOKEN_RE = re.compile(
    r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+|"
    r"\b[A-Z]{3,}(?:-[A-Z0-9]+)?|"
    r"\b\d+-?[A-Z]+\b|"
    r"\b[A-Z][a-z]{3,}|"
    r"\b\d{4}-\d{2}-\d{2}\b|"
    r"\b(?:Q[1-4]|H[12])\s+\d{4}\b"
)


def _extract_coherence_tokens(s: str) -> set:
    if not s:
        return set()
    return {m.group(0).lower() for m in _COHERENCE_TOKEN_RE.finditer(s)}


def _validate_situation_coherence(situation: str, why_underpriced: str,
                                  reasons: List[str]) -> None:
    """Reject theses where the situation describes a different event than the
    why-underpriced paragraph references. Requires at least one coherence-
    bridging token from situation to appear in why_underpriced.

    A degenerate situation (no extractable named entities) skips this check —
    other v1/v2 validators (length, boilerplate, reasoning-tag coverage) will
    surface the underlying quality problem with a more specific reason.
    """
    sit_tokens = _extract_coherence_tokens(situation)
    if not sit_tokens:
        return
    why_tokens = _extract_coherence_tokens(why_underpriced)
    if not (sit_tokens & why_tokens):
        sample = ", ".join(sorted(sit_tokens)[:5])
        reasons.append(
            f"coherence_fail_situation_unrelated_to_underpriced: no overlapping "
            f"named entities between situation and why_underpriced "
            f"(situation tokens e.g. [{sample}])"
        )


def _validate_structured_kill(kc: Any, reasons: List[str]) -> None:
    if not isinstance(kc, list) or len(kc) < 3:
        got = len(kc) if isinstance(kc, list) else 0
        reasons.append(f"structured_kill_conditions: need >=3 entries (got {got})")
        return
    has_date_bound = False
    for i, entry in enumerate(kc):
        if not isinstance(entry, dict):
            reasons.append(f"structured_kill_conditions[{i}]: not an object")
            continue
        if not entry.get("id"):
            reasons.append(f"structured_kill_conditions[{i}]: missing id")
        desc_len = _non_ws_len(entry.get("description") or "")
        if desc_len < 40:
            reasons.append(f"structured_kill_conditions[{i}]: description too short ({desc_len}, need >=40)")
        obs = entry.get("observable")
        if not isinstance(obs, dict):
            reasons.append(f"structured_kill_conditions[{i}]: missing observable object")
        else:
            if not obs.get("source_type"):
                reasons.append(f"structured_kill_conditions[{i}]: observable.source_type required")
            if not (obs.get("search_pattern") or obs.get("url_pattern_hint")):
                reasons.append(
                    f"structured_kill_conditions[{i}]: observable needs search_pattern OR url_pattern_hint"
                )
        if entry.get("date_bound"):
            has_date_bound = True
    if not has_date_bound:
        reasons.append("structured_kill_conditions: at least one entry needs date_bound")


def assess_thesis_v2(thesis: Optional[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """v2 gate: v1 5-field check + steelman + web_research + reasoning-tag coverage
    + structured_kill_conditions validation.

    `thesis` shape (spec §7.1, §7.4 routine output contract):
      {
        situation, why_underpriced, next_catalyst, next_catalyst_date, kill_conditions,  # v1
        steelman,                                                                          # v2
        web_research: [{url, retrieved_at, finding, lean}],                                # v2
        structured_kill_conditions: [{id, description, observable, date_bound?}],          # v2
      }
    """
    ok_v1, reasons = assess_thesis(thesis)
    # Continue accumulating even if v1 already failed — surface all reasons in one pass so
    # the thesis_writer retry prompt can address them together (spec §7.4).

    if not isinstance(thesis, dict):
        return False, reasons  # v1 already added 'missing or not a dict'

    # steelman
    steelman = thesis.get("steelman")
    if steelman is None or (isinstance(steelman, str) and not steelman.strip()):
        reasons.append("missing required field: steelman")
    elif isinstance(steelman, str):
        if _non_ws_len(steelman) < 120:
            reasons.append(f"steelman: too short ({_non_ws_len(steelman)} chars, need >=120)")
        if _BOILERPLATE_RE.search(steelman):
            reasons.append("steelman: matches scanner boilerplate pattern")

    _validate_web_research(thesis.get("web_research"), reasons)
    _validate_reasoning_tags(
        thesis.get("situation") or "",
        thesis.get("why_underpriced") or "",
        thesis.get("steelman") or "",
        reasons,
    )
    _validate_situation_coherence(
        thesis.get("situation") or "",
        thesis.get("why_underpriced") or "",
        reasons,
    )

    if "structured_kill_conditions" in thesis:
        _validate_structured_kill(thesis.get("structured_kill_conditions"), reasons)
    else:
        reasons.append("missing required field: structured_kill_conditions")

    return (len(reasons) == 0), reasons


def render_candidate_markdown_v2(
    signal: Dict[str, Any],
    thesis: Dict[str, Any],
    *,
    band: str,
    scoring_profile: Optional[str],
    entity: Optional[Dict[str, Any]] = None,
) -> str:
    """Render a v2 dossier. First five sections match v1 byte-for-byte; adds `## Steelman`
    and `## Web research` between `## Next catalyst` and `## Kill conditions`, plus a
    structured-kills block when provided."""
    # Build the v1 body, then splice the new sections in before "## Kill conditions".
    v1_md = render_candidate_markdown(signal, thesis, band=band,
                                      scoring_profile=scoring_profile, entity=entity)

    # Bump gate_version in the frontmatter.
    v1_md = v1_md.replace("gate_version: 2", "gate_version: 2_steelman", 1)

    # Build the new sections.
    sections: List[str] = []
    steelman = (thesis.get("steelman") or "").strip()
    if steelman:
        sections += ["## Steelman", "", steelman, ""]

    web = thesis.get("web_research") or []
    if isinstance(web, list) and web:
        sections += ["## Web research", ""]
        for entry in web:
            if not isinstance(entry, dict):
                continue
            lean = entry.get("lean", "?")
            url = entry.get("url", "")
            retrieved_at = entry.get("retrieved_at", "")
            finding = (entry.get("finding") or "").strip()
            sections += [
                f"- **[{lean}]** {url} (retrieved {retrieved_at})",
                f"  {finding}",
            ]
        sections += [""]

    kc_struct = thesis.get("structured_kill_conditions") or []
    if isinstance(kc_struct, list) and kc_struct:
        sections += ["## Kill conditions (structured)", ""]
        for entry in kc_struct:
            if not isinstance(entry, dict):
                continue
            line = f"- **{entry.get('id', '?')}**: {(entry.get('description') or '').strip()}"
            obs = entry.get("observable") or {}
            obs_parts = []
            if obs.get("source_type"):
                obs_parts.append(f"source={obs['source_type']}")
            if obs.get("search_pattern"):
                obs_parts.append(f"search=`{obs['search_pattern']}`")
            if obs.get("url_pattern_hint"):
                obs_parts.append(f"url~`{obs['url_pattern_hint']}`")
            if obs_parts:
                line += f"  ({'; '.join(obs_parts)})"
            if entry.get("date_bound"):
                line += f"  [by {entry['date_bound']}]"
            sections += [line]
        sections += [""]

    if not sections:
        return v1_md

    # Splice: insert the new sections right before "## Kill conditions".
    insertion = "\n".join(sections)
    marker = "## Kill conditions"
    if marker in v1_md:
        return v1_md.replace(marker, insertion + "\n" + marker, 1)
    return v1_md + "\n\n" + insertion
