"""
Candidate Gate — authoritative promotion rule.

Enforces the non-negotiable rule: NO signal becomes a candidate (either
Immediate or Watchlist band) without a written rationale thesis that
explains (a) the situation, (b) why it is under-priced, (c) the next
catalyst + date, (d) named kill conditions.

Per Pedro's directive (2026-04-17):
  "How is it possible that we have a candidate and not a thesis on why
   it is a candidate? then how has it been classified as a candidate?
   this makes no sense."

Any scanner / finalizer that wants to promote a signal to candidates/
MUST route through `promote_candidate()` in this module. Direct writes
to candidates/ bypassing this gate are a bug.

Functions
---------
assess_thesis(thesis) -> (ok, reasons)
    Validates a thesis dict against minimum-quality requirements.

promote_candidate(signal, thesis, *, band="watchlist", scoring_profile=None)
    -> dict {"status": "promoted"|"rejected", "path": str, "reasons": list}
    Promotes if thesis passes; otherwise appends to the rejection log.

extract_existing_thesis(md_path) -> dict|None
    Pulls thesis fields out of an existing candidate .md file for audits.

CLI
---
  python candidate_gate.py --audit
      Scan candidates/ and candidates/watchlist/ for files missing a thesis.
      Prints a summary + writes working/thesis_gate_audit_<date>.json.

  python candidate_gate.py --demote-stubs
      Moves candidates/watchlist/*.json entries lacking a thesis to
      candidates/rejected_pending_thesis/ with a YYYY-MM-DD prefix.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).parent.parent
CANDIDATES_DIR = REPO / "candidates"
WATCHLIST_DIR = CANDIDATES_DIR / "watchlist"
REJECTED_DIR = CANDIDATES_DIR / "rejected_pending_thesis"
WORKING = REPO / "working"
WORKING.mkdir(exist_ok=True)

# --------------------------------------------------------------------
# Thesis quality rules
# --------------------------------------------------------------------

# A valid thesis must include each of these fields as non-empty, non-boilerplate
# prose. "Boilerplate" = scanner-generated filler like "Scanner classified
# signal_type=..." or "TDnet filed X for Y".
REQUIRED_FIELDS = [
    "situation",          # what happened — the specific fact pattern
    "why_underpriced",    # rationale — why the market hasn't priced it in
    "next_catalyst",      # the next event that could trigger realization
    "next_catalyst_date", # absolute date or tight range
    "kill_conditions",    # explicit triggers that invalidate the thesis
]

# Minimum prose length per field (characters of non-whitespace content).
MIN_FIELD_CHARS = {
    "situation": 80,
    "why_underpriced": 100,
    "next_catalyst": 40,
    "kill_conditions": 60,
}

# Phrases that mark a thesis as auto-generated scanner output (not real analysis).
BOILERPLATE_PATTERNS = [
    r"scanner\s+classified\s+signal_type",
    r"tdnet\s+filed\s+\w+\s+for",
    r"auto[-\s]generated\s+by",
    r"placeholder\s+thesis",
    r"no\s+thesis\s+yet",
    r"to\s+be\s+researched",
]

BOILERPLATE_RE = re.compile("|".join(BOILERPLATE_PATTERNS), re.IGNORECASE)


def _non_ws_len(s: str) -> int:
    return len(re.sub(r"\s+", "", s or ""))


def assess_thesis(thesis: Optional[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """Return (ok, reasons). reasons is a list of why the thesis failed."""
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
            if BOILERPLATE_RE.search(val):
                reasons.append(f"{field}: matches scanner boilerplate pattern")

    # Date sanity
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


# --------------------------------------------------------------------
# Candidate file writer
# --------------------------------------------------------------------

def _slug(s: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").strip().lower())
    s = s.strip("-")
    return s[:maxlen] or "unnamed"


def _candidate_filename(signal: Dict[str, Any], scoring_profile: Optional[str]) -> str:
    """Canonical filename: <TICKER>_<MIC>_<slug>.md"""
    ticker = signal.get("ticker_local") or signal.get("ticker") or "UNK"
    mic = signal.get("mic") or "UNK"
    tag_parts = []
    if scoring_profile:
        tag_parts.append(scoring_profile)
    headline = ((signal.get("raw_data") or {}).get("headline")
                or signal.get("signal_type") or "candidate")
    tag_parts.append(_slug(headline, 32))
    tag = "-".join(tag_parts)[:60]
    return f"{ticker}_{mic}_{tag}.md"


def _render_candidate_md(
    signal: Dict[str, Any],
    thesis: Dict[str, Any],
    *,
    band: str,
    scoring_profile: Optional[str],
) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    src_url = signal.get("source_url") or ""
    ticker = signal.get("ticker_local") or "UNK"
    mic = signal.get("mic") or "UNK"
    company = (signal.get("company_name_en")
               or signal.get("company_name_local")
               or "Unknown")
    score = signal.get("score_total")
    sig_id = signal.get("signal_id") or ""
    scanner = signal.get("scanner") or ""
    signal_type = signal.get("signal_type") or ""
    sig_date = signal.get("source_date") or signal.get("scan_date") or ""

    frontmatter_lines = [
        "---",
        f"ticker_local: {ticker}",
        f"mic: {mic}",
        f"company: \"{company}\"",
        f"scoring_profile: {scoring_profile or 'unclassified'}",
        f"band: {band}",
        f"score_total: {score}",
        f"signal_id: {sig_id}",
        f"scanner: {scanner}",
        f"signal_type: {signal_type}",
        f"signal_date: {sig_date}",
        f"candidate_created: {today}",
        f"gate_version: 1",
        "---",
    ]
    fm = "\n".join(frontmatter_lines)

    body_parts = [
        f"# {ticker}.{mic} — {company}",
        "",
        f"**Band**: {band}   **Score**: {score}   **Profile**: {scoring_profile or 'unclassified'}",
        "",
        "## Situation",
        "",
        thesis.get("situation", "").strip(),
        "",
        "## Why this is under-priced",
        "",
        thesis.get("why_underpriced", "").strip(),
        "",
        "## Next catalyst",
        "",
        f"- **Date**: {thesis.get('next_catalyst_date', '').strip()}",
        f"- **Event**: {thesis.get('next_catalyst', '').strip()}",
        "",
        "## Kill conditions",
        "",
        thesis.get("kill_conditions", "").strip(),
        "",
    ]

    # Optional fields
    if thesis.get("timeline"):
        body_parts += ["## Timeline", "", thesis["timeline"].strip(), ""]
    if thesis.get("sources"):
        body_parts += ["## Sources", ""]
        if isinstance(thesis["sources"], list):
            for s in thesis["sources"]:
                body_parts.append(f"- {s}")
        else:
            body_parts.append(thesis["sources"])
        body_parts.append("")
    if src_url:
        body_parts += [f"Primary source: {src_url}", ""]

    return fm + "\n\n" + "\n".join(body_parts)


# --------------------------------------------------------------------
# Public: promote_candidate
# --------------------------------------------------------------------

def promote_candidate(
    signal: Dict[str, Any],
    thesis: Optional[Dict[str, Any]] = None,
    *,
    band: str = "watchlist",
    scoring_profile: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Gate a signal into candidates/.

    Returns a result dict:
      {"status": "promoted", "path": "...", "band": ...} on success.
      {"status": "rejected", "reasons": [...], "rejection_path": "..."} on failure.

    Rejected signals are appended to working/rejected_promotions_<today>.json
    so they remain visible for follow-up research instead of being silently
    dropped.
    """
    if band not in ("immediate", "watchlist"):
        return {"status": "rejected", "reasons": [f"invalid band: {band}"]}

    ok, reasons = assess_thesis(thesis)
    if not ok:
        return _log_rejection(signal, reasons, band, scoring_profile, thesis, dry_run)

    CANDIDATES_DIR.mkdir(exist_ok=True)
    filename = _candidate_filename(signal, scoring_profile)
    out_path = CANDIDATES_DIR / filename
    body = _render_candidate_md(
        signal, thesis, band=band, scoring_profile=scoring_profile
    )

    if dry_run:
        return {"status": "promoted", "path": str(out_path), "band": band, "dry_run": True}

    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, out_path)
    return {"status": "promoted", "path": str(out_path), "band": band}


def _log_rejection(
    signal: Dict[str, Any],
    reasons: List[str],
    band: str,
    scoring_profile: Optional[str],
    thesis: Optional[Dict[str, Any]],
    dry_run: bool,
) -> Dict[str, Any]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = WORKING / f"rejected_promotions_{today}.json"
    entry = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "signal_id": signal.get("signal_id"),
        "ticker_plus_mic": signal.get("ticker_plus_mic"),
        "company": signal.get("company_name_en") or signal.get("company_name_local"),
        "scanner": signal.get("scanner"),
        "signal_type": signal.get("signal_type"),
        "score_total": signal.get("score_total"),
        "intended_band": band,
        "scoring_profile": scoring_profile,
        "rejection_reasons": reasons,
        "thesis_provided": thesis,
        "source_url": signal.get("source_url"),
    }

    if dry_run:
        return {"status": "rejected", "reasons": reasons, "rejection_path": str(log_path), "dry_run": True}

    existing: List[Dict[str, Any]] = []
    if log_path.exists():
        try:
            existing = json.loads(log_path.read_text(encoding="utf-8"))
        except Exception:
            existing = []
    existing.append(entry)
    tmp = log_path.with_suffix(log_path.suffix + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    os.replace(tmp, log_path)

    return {"status": "rejected", "reasons": reasons, "rejection_path": str(log_path)}


# --------------------------------------------------------------------
# Extract thesis from existing .md (for audits)
# --------------------------------------------------------------------

SECTION_MAP = {
    "situation": [
        r"##\s*(?:\d+[\.\)]\s*)?Situation(?:\s+summary)?\b",
        r"##\s*(?:\d+[\.\)]\s*)?Transaction\b",
        r"##\s*(?:\d+[\.\)]\s*)?Company Overview\b",
        r"##\s*(?:\d+[\.\)]\s*)?Signal Evidence\b",
        r"##\s*(?:\d+[\.\)]\s*)?Thesis(?:\s+Statement)?\b",
        r"##\s*(?:\d+[\.\)]\s*)?TL;DR\b",
        r"##\s*(?:\d+[\.\)]\s*)?Headline Thesis\b",
    ],
    "why_underpriced": [
        r"##\s*(?:\d+[\.\)]\s*)?Why this is under-priced\b",
        r"##\s*(?:\d+[\.\)]\s*)?Why this is non-obvious[^\n]*",
        r"##\s*(?:\d+[\.\)]\s*)?Why under-priced\b",
        r"##\s*(?:\d+[\.\)]\s*)?Deep Dive(?:\s+Findings)?\b",
        r"##\s*(?:\d+[\.\)]\s*)?Commercial Setup\b",
        r"##\s*(?:\d+[\.\)]\s*)?Clinical Evidence Base\b",
        r"##\s*(?:\d+[\.\)]\s*)?Thesis(?:\s+Statement)?\b",
    ],
    "next_catalyst": [
        r"##\s*(?:\d+[\.\)]\s*)?Next catalyst\b",
        r"##\s*(?:\d+[\.\)]\s*)?Catalyst map\b",
        r"##\s*(?:\d+[\.\)]\s*)?Catalysts\b",
        r"##\s*(?:\d+[\.\)]\s*)?PDUFA Day Protocol[^\n]*",
        r"##\s*(?:\d+[\.\)]\s*)?Catalysts? & Timeline\b",
    ],
    "kill_conditions": [
        r"##\s*(?:\d+[\.\)]\s*)?Kill[-\s]conditions[^\n]*",
        r"##\s*(?:\d+[\.\)]\s*)?Kill Conditions[^\n]*",
    ],
}


def extract_existing_thesis(md_text: str) -> Dict[str, str]:
    """Best-effort thesis extraction from a candidate .md file for audits."""
    out: Dict[str, str] = {}
    # strip YAML frontmatter
    body = md_text
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end >= 0:
            body = body[end + 4:].lstrip("\n")

    for field, patterns in SECTION_MAP.items():
        for pat in patterns:
            m = re.search(pat + r"[^\n]*\n+(.+?)(?=\n##|\n---|\Z)",
                          body, re.DOTALL | re.IGNORECASE)
            if m:
                out[field] = m.group(1).strip()
                break
    return out


# --------------------------------------------------------------------
# Audit + demote CLI helpers
# --------------------------------------------------------------------

def audit() -> Dict[str, Any]:
    """Scan candidates/ for files missing a valid thesis."""
    results = {"rich": [], "missing": [], "summary": {}}

    md_files = sorted([p for p in CANDIDATES_DIR.glob("*.md")])
    for p in md_files:
        text = p.read_text(encoding="utf-8", errors="replace")
        thesis = extract_existing_thesis(text)
        # Audit mode: accept an ISO date anywhere inside the catalyst body,
        # or a rough-band like "Q2 2026", since the template encodes dates
        # inside the catalyst map rather than in a dedicated field.
        cat_body = thesis.get("next_catalyst", "") or ""
        iso_match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", cat_body)
        band_match = re.search(r"\b(?:Q[1-4]|H[12])\s+\d{4}\b", cat_body, re.IGNORECASE)
        month_match = re.search(
            r"\b(?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December|Jan|Feb|Mar|Apr|Jun|"
            r"Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+\d{4}\b",
            cat_body, re.IGNORECASE,
        )
        synth_date = (iso_match.group(0) if iso_match
                      else (band_match.group(0) if band_match
                      else (month_match.group(0) if month_match else "")))
        ok, reasons = assess_thesis({
            "situation": thesis.get("situation", ""),
            "why_underpriced": thesis.get("why_underpriced", ""),
            "next_catalyst": cat_body,
            "next_catalyst_date": synth_date,
            "kill_conditions": thesis.get("kill_conditions", ""),
        })
        entry = {"file": p.name, "reasons": reasons, "fields_found": list(thesis.keys())}
        (results["rich"] if ok else results["missing"]).append(entry)

    # JSON stubs in watchlist — never have a thesis by construction
    json_stubs = sorted([p for p in WATCHLIST_DIR.glob("*.json")]) if WATCHLIST_DIR.exists() else []
    results["watchlist_json_stubs"] = [p.name for p in json_stubs]

    results["summary"] = {
        "md_total": len(md_files),
        "md_with_thesis": len(results["rich"]),
        "md_missing_thesis": len(results["missing"]),
        "json_stubs": len(json_stubs),
    }
    return results


def demote_stubs() -> Dict[str, Any]:
    """Move watchlist/*.json entries lacking a thesis to rejected_pending_thesis/."""
    REJECTED_DIR.mkdir(exist_ok=True)
    moved = []
    if not WATCHLIST_DIR.exists():
        return {"moved": [], "note": "watchlist dir does not exist"}
    for p in sorted(WATCHLIST_DIR.glob("*.json")):
        dest = REJECTED_DIR / p.name
        os.replace(p, dest)
        moved.append({"from": str(p), "to": str(dest)})
    return {"moved": moved, "count": len(moved)}


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Candidate promotion gate")
    ap.add_argument("--audit", action="store_true",
                    help="Audit candidates/ for files missing a thesis")
    ap.add_argument("--demote-stubs", action="store_true",
                    help="Move watchlist/*.json entries without theses to rejected_pending_thesis/")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.audit:
        result = audit()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out = WORKING / f"thesis_gate_audit_{today}.json"
        out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result["summary"], indent=2))
        print(f"Full report: {out}")
        return

    if args.demote_stubs:
        if args.dry_run:
            stubs = sorted(WATCHLIST_DIR.glob("*.json")) if WATCHLIST_DIR.exists() else []
            print(f"Would move {len(stubs)} stub(s) to {REJECTED_DIR}")
            for p in stubs:
                print(f"  {p.name}")
            return
        result = demote_stubs()
        print(f"Moved {result.get('count', 0)} stub(s) to {REJECTED_DIR}")
        return

    ap.print_help()


if __name__ == "__main__":
    main()

# --- END OF FILE ---
