"""
legal_enricher.py — deterministic severity x likelihood enrichment for
litigation/regulatory signals.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).parent.parent
SIGNAL_LOG = REPO / "signals" / "signal_log.json"
WORKING = REPO / "working"
WORKING.mkdir(exist_ok=True)

SEVERITY_LABELS = {1: "Negligible", 2: "Low", 3: "Moderate", 4: "High", 5: "Critical"}
LIKELIHOOD_LABELS = {1: "Remote", 2: "Unlikely", 3: "Possible", 4: "Likely", 5: "Almost Certain"}


def _color_for_score(score: int) -> str:
    if score <= 4:
        return "GREEN"
    if score <= 9:
        return "YELLOW"
    if score <= 15:
        return "ORANGE"
    return "RED"


SEVERITY_BOOST_PATTERNS = [
    (re.compile(r"\b(fraud|criminal|indict|felony|insider trading|market manipulation|ponzi|conspirac)", re.I), 2),
    (re.compile(r"\b(class action|securities|disgorge|cease and desist|injunction|consent decree|monetary relief|civil penalt|settle|enforcement action|investigation|subpoena)", re.I), 1),
    (re.compile(r"\b(patent infringement|preliminary injunction|itc 337|exclusion order)", re.I), 1),
]
LIKELIHOOD_ADJUST_PATTERNS = [
    (re.compile(r"\b(settled|consent|judgment entered|verdict|final order|order of dismissal|conviction)", re.I), +2),
    (re.compile(r"\b(motion to dismiss denied|summary judgment|trial set|trial commenc|appeal pending)", re.I), +1),
    (re.compile(r"\b(complaint filed|preliminary|initial pleading|motion to dismiss filed|temporary restraining)", re.I), -1),
]

BASELINE_BY_TYPE: Dict[tuple, tuple] = {
    ("sec_enforcement_scanner", "litigation_release"): (4, 5),
    ("sec_enforcement_scanner", "administrative_proceeding"): (3, 5),
    ("courtlistener_scanner", "securities_class_action"): (4, 3),
    ("courtlistener_scanner", "antitrust"): (4, 3),
    ("courtlistener_scanner", "patent_infringement"): (3, 3),
    ("courtlistener_scanner", "contract_dispute"): (2, 3),
    ("bse_nse_scanner", "pending_litigation"): (2, 3),
    ("cvm_scanner", "litigation_event"): (2, 3),
    ("hkex_scanner", "litigation_event"): (2, 3),
    ("kind_scanner", "litigation_event"): (3, 3),
    ("bmv_scanner", "litigation_event"): (2, 3),
    ("lse_rns_scanner", "litigation"): (2, 3),
}
BASELINE_BY_TYPE_ONLY: Dict[str, tuple] = {
    "litigation_release": (4, 5),
    "administrative_proceeding": (3, 5),
    "securities_class_action": (4, 3),
    "antitrust": (4, 3),
    "patent_infringement": (3, 3),
    "contract_dispute": (2, 3),
    "pending_litigation": (2, 3),
    "litigation_event": (2, 3),
    "regulatory_investigation": (4, 4),
    "fda_warning_letter": (3, 4),
}

REGULATIONS_BY_SCANNER: Dict[str, List[str]] = {
    "sec_enforcement_scanner": [
        "Securities Act of 1933",
        "Securities Exchange Act of 1934",
        "Investment Advisers Act of 1940",
        "Sarbanes-Oxley Act of 2002",
    ],
    "courtlistener_scanner": [
        "Federal Rules of Civil Procedure",
        "Securities Exchange Act of 1934 (if NOS 850)",
        "Sherman Antitrust Act (if NOS 410)",
        "Patent Act 35 USC §271 (if NOS 830/835)",
    ],
    "bse_nse_scanner": [
        "SEBI (Listing Obligations and Disclosure Requirements) Regulations 2015",
        "SEBI (Substantial Acquisition of Shares and Takeovers) Regulations 2011",
    ],
    "cvm_scanner": [
        "Lei nº 6.404/76 (Brazilian Corporations Act)",
        "Instrução CVM 358/2002 (Material Facts)",
    ],
    "hkex_scanner": [
        "HKEX Main Board Listing Rules ch.13 (Disclosure of Information)",
        "Securities and Futures Ordinance (Cap. 571)",
    ],
    "kind_scanner": [
        "Korea Capital Markets Act (자본시장법)",
        "FSC Disclosure Regulations",
    ],
    "bmv_scanner": [
        "Ley del Mercado de Valores",
        "Disposiciones de Carácter General CNBV",
    ],
    "lse_rns_scanner": [
        "UK Market Abuse Regulation (MAR)",
        "FCA Listing Rules ch.9 (Continuing Obligations)",
    ],
}


def _regulations_for(signal: dict) -> List[str]:
    out = list(REGULATIONS_BY_SCANNER.get(signal.get("upstream_scanner", ""), []))
    text = " ".join(filter(None, [signal.get("headline") or "", signal.get("summary") or ""])).lower()
    if "fda" in text or "drug" in text or "medical device" in text:
        out.append("Federal Food, Drug, and Cosmetic Act (21 USC §301)")
    if "data breach" in text or "personal information" in text or "gdpr" in text:
        out.append("GDPR (EU) 2016/679 / state breach notification statutes")
    if "antitrust" in text or "monopol" in text or "cartel" in text:
        out.append("Sherman Antitrust Act + Clayton Act")
    if not out:
        out.append("(no specific regulation surfaced; review case docs)")
    return out


def _baseline(signal: dict) -> tuple:
    scanner = signal.get("upstream_scanner") or signal.get("scanner_source") or ""
    signal_type = signal.get("signal_type") or ""
    if (scanner, signal_type) in BASELINE_BY_TYPE:
        return BASELINE_BY_TYPE[(scanner, signal_type)]
    if signal_type in BASELINE_BY_TYPE_ONLY:
        return BASELINE_BY_TYPE_ONLY[signal_type]
    return (3, 3)


def _apply_keyword_boosts(signal: dict, base_severity: int, base_likelihood: int) -> tuple:
    text = " ".join(filter(None, [signal.get("headline") or "", signal.get("summary") or ""]))
    severity = base_severity
    for pattern, boost in SEVERITY_BOOST_PATTERNS:
        if pattern.search(text):
            severity = min(5, severity + boost)
            break
    likelihood = base_likelihood
    for pattern, adjust in LIKELIHOOD_ADJUST_PATTERNS:
        if pattern.search(text):
            likelihood = max(1, min(5, likelihood + adjust))
            break
    return severity, likelihood


def _explanation(signal: dict, severity: int, likelihood: int, regulations: List[str]) -> str:
    scanner = signal.get("upstream_scanner") or "unknown_source"
    signal_type = signal.get("signal_type") or "unknown_type"
    score = severity * likelihood
    color = _color_for_score(score)
    headline = (signal.get("headline") or "")[:140]
    return (
        f"Source: {scanner} / {signal_type}. "
        f"Severity {severity} ({SEVERITY_LABELS[severity]}) x Likelihood {likelihood} ({LIKELIHOOD_LABELS[likelihood]}) "
        f"= score {score} → {color}. "
        f"Subject: {headline} "
        f"Regulations in scope: {', '.join(regulations[:3])}{'…' if len(regulations) > 3 else ''}"
    )


def enrich_litigation_signal(signal: dict) -> dict:
    base_severity, base_likelihood = _baseline(signal)
    severity, likelihood = _apply_keyword_boosts(signal, base_severity, base_likelihood)
    regulations = _regulations_for(signal)
    score = severity * likelihood
    patch = {
        "severity_tier": severity,
        "severity_label": SEVERITY_LABELS[severity],
        "likelihood_tier": likelihood,
        "likelihood_label": LIKELIHOOD_LABELS[likelihood],
        "risk_score": score,
        "risk_color": _color_for_score(score),
        "regulations": regulations,
        "explanation": _explanation(signal, severity, likelihood, regulations),
        "enriched_at": datetime.now(timezone.utc).isoformat(),
        "framework": "legal:legal-risk-assessment severity×likelihood",
    }
    signal["legal_enrichment"] = patch
    return patch


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
    except Exception:
        pass
    os.replace(str(tmp), str(path))


def enrich_signal_log(path: Optional[Path] = None) -> Dict[str, Any]:
    path = path or SIGNAL_LOG
    if not path.exists():
        return {"status": "no_log", "enriched": 0}
    data = json.loads(path.read_text(encoding="utf-8"))
    litigation_signals = [signal for signal in data if signal.get("scoring_profile") == "litigation"]
    enriched = 0
    color_counts = {"GREEN": 0, "YELLOW": 0, "ORANGE": 0, "RED": 0}
    for signal in litigation_signals:
        patch = enrich_litigation_signal(signal)
        enriched += 1
        color_counts[patch["risk_color"]] += 1
    _atomic_write(path, json.dumps(data, indent=2, default=str, ensure_ascii=False))
    today = datetime.now(timezone.utc).date().isoformat()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "framework": "legal:legal-risk-assessment + legal:compliance-check",
        "total_litigation_signals": len(litigation_signals),
        "enriched": enriched,
        "by_color": color_counts,
        "top_red_signals": [
            {
                "signal_id": signal.get("signal_id"),
                "ticker": signal.get("ticker"),
                "headline": (signal.get("headline") or "")[:160],
                "risk_score": signal["legal_enrichment"]["risk_score"],
                "color": signal["legal_enrichment"]["risk_color"],
            }
            for signal in sorted(litigation_signals, key=lambda item: item["legal_enrichment"]["risk_score"], reverse=True)[:10]
        ],
    }
    out = WORKING / f"legal_enrichment_report_{today}.json"
    _atomic_write(out, json.dumps(report, indent=2, ensure_ascii=False))
    report["report_path"] = str(out)
    report["status"] = "ok"
    return report


def summarize_legal_desk(window_days: int = 1, max_items: int = 12) -> List[Dict[str, Any]]:
    if not SIGNAL_LOG.exists():
        return []
    data = json.loads(SIGNAL_LOG.read_text(encoding="utf-8"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    items = []
    for signal in data:
        if signal.get("scoring_profile") != "litigation":
            continue
        if "legal_enrichment" not in signal:
            enrich_litigation_signal(signal)
        source_date = signal.get("source_date") or signal.get("scan_date")
        try:
            ts = datetime.fromisoformat(str(source_date).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
        except Exception:
            continue
        enrichment = signal["legal_enrichment"]
        items.append(
            {
                "signal_id": signal.get("signal_id"),
                "ticker": signal.get("ticker"),
                "company": signal.get("company_name_en"),
                "headline": (signal.get("headline") or "")[:200],
                "scanner": signal.get("upstream_scanner"),
                "signal_type": signal.get("signal_type"),
                "source_date": source_date,
                "filing_url": signal.get("filing_url"),
                "severity": enrichment["severity_label"],
                "likelihood": enrichment["likelihood_label"],
                "risk_score": enrichment["risk_score"],
                "color": enrichment["risk_color"],
                "regulations": enrichment["regulations"][:3],
                "explanation": enrichment["explanation"],
            }
        )
    items.sort(key=lambda item: item["risk_score"], reverse=True)
    return items[:max_items]


def _cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--desk", action="store_true", help="Print legal-desk JSON for last 24h.")
    parser.add_argument("--window-days", type=int, default=1, help="Window for --desk (default 1).")
    parser.add_argument("--signal-id", default=None, help="Enrich only the matching signal_id and print result.")
    args = parser.parse_args()
    if args.signal_id:
        data = json.loads(SIGNAL_LOG.read_text(encoding="utf-8"))
        signal = next((item for item in data if item.get("signal_id") == args.signal_id), None)
        if not signal:
            print(json.dumps({"error": "signal_id not found"}))
            return
        print(json.dumps(enrich_litigation_signal(signal), indent=2, ensure_ascii=False))
        return
    if args.desk:
        print(json.dumps(summarize_legal_desk(window_days=args.window_days), indent=2, ensure_ascii=False))
        return
    report = enrich_signal_log()
    print(json.dumps({key: value for key, value in report.items() if key != "top_red_signals"}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
