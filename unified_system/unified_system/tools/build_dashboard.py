"""build_dashboard.py — generates unified_system/DASHBOARD.html snapshot."""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO.parent
SIGNALS = REPO / "signals"
WORKING = REPO / "working"
CANDIDATES_DIR = REPO / "candidates"
CURATED_RATIONALES = CANDIDATES_DIR / "_curated_rationales.json"
DASHBOARD = ROOT / "DASHBOARD.html"
REPORTING_ROOT = ROOT / "reporting"


def _load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def _latest_matching(pattern):
    matches = sorted(glob.glob(str(WORKING / pattern)))
    return Path(matches[-1]) if matches else None


def _load_curated_rationales():
    return _load_json(CURATED_RATIONALES, default={}) or {}


def _classify_curated(curated):
    draft_tickers = set()
    draft_details = {}
    if not isinstance(curated, dict):
        return draft_tickers, draft_details
    for ticker, rationale in curated.items():
        if ticker == "_archived" or not isinstance(rationale, dict):
            continue
        is_draft = bool(rationale.get("_draft"))
        if not is_draft:
            one = str(rationale.get("one_liner") or "")
            hyp = str(rationale.get("hypothesis") or "")
            if "[DRAFT" in one or "[DRAFT" in hyp or "[TODO" in one or "[TODO" in hyp:
                is_draft = True
        if is_draft:
            draft_tickers.add(ticker.upper())
            draft_details[ticker.upper()] = {
                "one_liner": rationale.get("one_liner") or "",
                "profile": rationale.get("_draft_profile") or "",
                "generated_at": (rationale.get("_draft_generated_at") or "")[:10],
            }
    return draft_tickers, draft_details


def _extract_deep_thesis(curated):
    out = {}
    if not isinstance(curated, dict):
        return out
    for ticker, rationale in curated.items():
        if ticker == "_archived" or not isinstance(rationale, dict):
            continue
        thesis = rationale.get("deep_thesis")
        if not isinstance(thesis, dict) or not thesis.get("schema_version"):
            continue
        sit = str(thesis.get("situation") or "").strip()
        first_para = next((p for p in sit.split("\n\n") if p.strip()), "")
        out[ticker.upper()] = {
            "confidence": str(thesis.get("confidence") or "").upper(),
            "direction": str(thesis.get("directional_view") or "").upper(),
            "situation_excerpt": first_para[:300],
            "analysed_at": (rationale.get("_thesis_analyst_at") or "")[:10],
        }
    return out


def gather_state():
    state = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "root": str(ROOT)}
    log = _load_json(SIGNALS / "signal_log.json", default=[])
    state["signal_count"] = len(log)

    profile_counts = Counter()
    recent_by_profile = defaultdict(list)
    for sig in log:
        profile = sig.get("scoring_profile") or "unknown"
        scanner = sig.get("scanner") or sig.get("scanner_source") or sig.get("upstream_scanner") or "unknown"
        score_block = sig.get("scoring") or {}
        score = score_block.get("score_with_bonus") if isinstance(score_block, dict) else None
        if score is None and isinstance(score_block, dict):
            score = score_block.get("score")
        score = score if score is not None else sig.get("score_total") or sig.get("score")
        profile_counts[profile] += 1
        recent_by_profile[profile].append(
            {
                "ticker": sig.get("ticker") or sig.get("ticker_plus_mic") or "-",
                "scanner": scanner,
                "date": (sig.get("scan_date") or sig.get("source_date") or "")[:10],
                "score": score or 0,
            }
        )
    for profile in recent_by_profile:
        recent_by_profile[profile].sort(key=lambda row: (row["date"], row["score"] or 0), reverse=True)
        recent_by_profile[profile] = recent_by_profile[profile][:5]
    state["profiles"] = {
        profile: {"count": profile_counts[profile], "top": recent_by_profile.get(profile, [])}
        for profile in sorted(profile_counts, key=lambda key: -profile_counts[key])
        if profile != "unknown"
    }

    health_file = _latest_matching("health_report_*.json")
    if health_file:
        health = _load_json(health_file, default={})
        state["health"] = {
            "ran_at": health.get("ran_at_utc", ""),
            "max_severity": health.get("max_severity", "unknown"),
            "by_family": health.get("by_family", {}),
        }
    else:
        state["health"] = {"max_severity": "no_report", "by_family": {}}

    conv_file = _latest_matching("convergence_report_*.json")
    if conv_file:
        conv = _load_json(conv_file, default={})
        groups = conv.get("groups", [])
        groups_sorted = sorted(groups, key=lambda g: (g.get("signal_count", 0), g.get("bonus", 0)), reverse=True)[:12]
        state["convergence"] = {
            "n_groups": conv.get("n_groups", len(groups)),
            "top": [
                {
                    "issuer_key": g.get("issuer_key", "?"),
                    "count": g.get("signal_count", 0),
                    "scanners": g.get("scanners", []) or [],
                    "profiles": g.get("profiles", []) or [],
                    "type": g.get("convergence_type", ""),
                    "bonus": g.get("bonus", 0),
                    "tickers": g.get("tickers_seen", []) or [],
                }
                for g in groups_sorted
            ],
        }
    else:
        state["convergence"] = {"n_groups": 0, "top": []}

    cal = _load_json(WORKING / "catalyst_calendar.json", default={})
    state["calendar"] = {
        "window_days": cal.get("window_days", 0),
        "today": cal.get("today", ""),
        "buckets": cal.get("buckets", {}),
    }

    curated = _load_curated_rationales()
    draft_tickers, draft_details = _classify_curated(curated)
    deep_thesis_idx = _extract_deep_thesis(curated)

    cand_file = _latest_matching("candidate_monitor_report_*.json")
    if cand_file:
        cand = _load_json(cand_file, default={})
        per = cand.get("per_ticker", {}) or {}
        live_rows, draft_rows = [], []
        for ticker, value in per.items():
            dti = deep_thesis_idx.get(ticker.upper()) or {}
            snapshot = value.get("price_snapshot") or {}
            row = {
                "ticker": ticker,
                "decision": value.get("decision") or "",
                "triggers": value.get("triggers") or [],
                "price_move": snapshot.get("max_abs_move_pct"),
                "deep_confidence": dti.get("confidence") or "",
                "deep_direction": dti.get("direction") or "",
                "deep_situation": dti.get("situation_excerpt") or "",
                "deep_analysed_at": dti.get("analysed_at") or "",
            }
            if ticker.upper() in draft_tickers:
                detail = draft_details.get(ticker.upper(), {})
                row["one_liner"] = detail.get("one_liner") or ""
                row["profile"] = detail.get("profile") or ""
                row["generated_at"] = detail.get("generated_at") or ""
                draft_rows.append(row)
            else:
                live_rows.append(row)
        conf_counts = Counter()
        for row in live_rows:
            conf = row.get("deep_confidence") or ""
            if conf:
                conf_counts[conf] += 1
        state["candidates"] = {
            "tickers_checked": len(cand.get("tickers_checked", [])) if isinstance(cand.get("tickers_checked"), list) else cand.get("tickers_checked", 0),
            "per_ticker_summary": live_rows[:30],
            "deep_thesis_conf": dict(conf_counts),
            "deep_thesis_analysed": sum(1 for row in live_rows if row.get("deep_confidence")),
        }
        state["drafts"] = {
            "count": len(draft_rows),
            "rows": draft_rows[:30],
            "note": "Auto-generated stubs from thesis_draft-style flows. Excluded from executive_summary.pdf until rewritten and unmarked.",
        }
    else:
        state["candidates"] = {"tickers_checked": 0, "per_ticker_summary": [], "deep_thesis_conf": {}, "deep_thesis_analysed": 0}
        rows = [{"ticker": ticker, **detail, "decision": "", "triggers": [], "price_move": None} for ticker, detail in draft_details.items()]
        state["drafts"] = {"count": len(rows), "rows": rows[:30], "note": "Auto-generated stubs."}

    state["reports"] = {
        "dossier_count": len(list((REPORTING_ROOT / "dossiers").glob("*.pdf"))) if (REPORTING_ROOT / "dossiers").exists() else 0
    }
    return state


def render_html(state):
    payload = json.dumps(state, default=str, ensure_ascii=False)
    ran = state.get("generated_at", "")
    signal_count = state.get("signal_count", 0)
    max_sev = state.get("health", {}).get("max_severity", "?")
    sev_color = {"red": "#c0392b", "yellow": "#d4a418", "green": "#2d8f3f", "info": "#4a6da7", "no_report": "#888"}.get(max_sev, "#888")
    conv_n = state.get("convergence", {}).get("n_groups", 0)
    dossier_count = state.get("reports", {}).get("dossier_count", 0)

    def esc(value):
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    profile_cards_html = ""
    for profile, pdata in state.get("profiles", {}).items():
        rows = ""
        for sig in pdata["top"]:
            score = sig.get("score")
            score_txt = f"{score:.1f}" if isinstance(score, (int, float)) and score else "-"
            rows += (
                "<tr><td class=\"mono\">"
                + esc(sig["ticker"])
                + "</td><td>"
                + esc(sig["scanner"])
                + "</td><td>"
                + esc(sig["date"])
                + "</td><td class=\"right\">"
                + score_txt
                + "</td></tr>"
            )
        profile_cards_html += (
            "<div class=\"card\"><div class=\"card-head\"><span class=\"card-title\">"
            + esc(profile)
            + "</span><span class=\"pill\">"
            + str(pdata["count"])
            + "</span></div><table class=\"mini\"><thead><tr><th>ticker</th><th>scanner</th><th>date</th><th class=\"right\">score</th></tr></thead><tbody>"
            + (rows or "<tr><td colspan=4 class=\"muted\">no recent signals</td></tr>")
            + "</tbody></table></div>"
        )

    conv_rows = ""
    for group in state.get("convergence", {}).get("top", []):
        tickers = ", ".join(group["tickers"][:3]) or group["issuer_key"]
        scanners = ", ".join(group["scanners"][:4]) or "-"
        profiles = ", ".join(group["profiles"][:3]) or "-"
        conv_rows += (
            "<tr><td class=\"mono\">"
            + esc(tickers)
            + "</td><td class=\"right\">"
            + str(group["count"])
            + "</td><td>"
            + esc(scanners)
            + "</td><td>"
            + esc(profiles)
            + "</td><td>"
            + esc(group["type"])
            + "</td><td class=\"right\">"
            + str(group["bonus"])
            + "</td></tr>"
        )

    health_rows = ""
    for family, findings in sorted((state.get("health", {}).get("by_family", {}) or {}).items()):
        items = findings if isinstance(findings, list) else []
        severity = "green"
        if items:
            order = {"red": 3, "yellow": 2, "green": 1, "info": 0}
            severity = max((item.get("severity", "info") for item in items if isinstance(item, dict)), key=lambda s: order.get(s, 0), default="info")
        dot = {"red": "#c0392b", "yellow": "#d4a418", "green": "#2d8f3f", "info": "#4a6da7"}.get(severity, "#888")
        note = ""
        if items:
            first = items[0]
            note = first.get("msg", "") if isinstance(first, dict) else str(first)
        health_rows += "<tr><td><span class=\"dot\" style=\"background:" + dot + "\"></span>" + esc(family) + "</td><td>" + esc(severity) + "</td><td class=\"small\">" + esc((note or "")[:140]) + "</td></tr>"

    bucket_order = [
        ("overdue", "Overdue"),
        ("this_week", "This week"),
        ("next_30_days", "Next 30 days"),
        ("30_to_90_days", "30-90 days"),
        ("90_plus_days", "90+ days"),
    ]
    cal_html = ""
    for bucket_key, label in bucket_order:
        entries = state.get("calendar", {}).get("buckets", {}).get(bucket_key, []) or []
        if not entries:
            continue
        items = ""
        for entry in entries[:8]:
            ticker = entry.get("ticker") or "-"
            when = entry.get("date_iso") or ""
            kind = entry.get("event_type") or ""
            items += "<li><span class=\"mono\">" + esc(ticker) + "</span> - " + esc(when) + " <span class=\"small\">(" + esc(kind) + ")</span></li>"
        cal_html += "<div class=\"bucket\"><h4>" + esc(label) + " <span class=\"pill\">" + str(len(entries)) + "</span></h4><ul>" + (items or "<li class=\"muted\">-</li>") + "</ul></div>"
    if not cal_html:
        cal_html = "<p class=\"muted\">No catalysts in the current calendar.</p>"

    def _conf_badge(conf):
        if not conf:
            return "<span class=\"badge-conf badge-none\">no thesis</span>"
        cls = {"HIGH": "badge-high", "MEDIUM": "badge-med", "LOW": "badge-low"}.get(conf.upper(), "badge-none")
        return "<span class=\"badge-conf " + cls + "\">" + esc(conf) + "</span>"

    def _dir_badge(direction):
        if not direction:
            return ""
        cls = {"LONG": "badge-long", "SHORT": "badge-short"}.get(direction.upper(), "badge-none")
        return "<span class=\"badge-dir " + cls + "\">" + esc(direction) + "</span>"

    cand_rows = ""
    for cand in state.get("candidates", {}).get("per_ticker_summary", [])[:20]:
        price = cand.get("price_move")
        price_txt = f"{price:+.1f}%" if isinstance(price, (int, float)) else "-"
        triggers = ", ".join(cand.get("triggers", [])[:3]) or "-"
        sit = (cand.get("deep_situation") or "").strip()
        sit_excerpt = sit.split("\n")[0][:200] if sit else "-"
        cand_rows += (
            "<tr><td class=\"mono\">"
            + esc(cand["ticker"])
            + "</td><td>"
            + _conf_badge(cand.get("deep_confidence") or "")
            + " "
            + _dir_badge(cand.get("deep_direction") or "")
            + "</td><td class=\"small\">"
            + esc(sit_excerpt)
            + "</td><td>"
            + esc(cand.get("decision") or "-")
            + "</td><td>"
            + esc(triggers)
            + "</td><td class=\"right\">"
            + price_txt
            + "</td></tr>"
        )

    cand_state = state.get("candidates", {}) or {}
    deep_n = cand_state.get("deep_thesis_analysed", 0)
    deep_conf = cand_state.get("deep_thesis_conf", {}) or {}
    deep_line = f"{deep_n} live candidates have engine deep theses ({', '.join(f'{k}: {v}' for k, v in sorted(deep_conf.items()))})" if deep_n else "no live candidates have engine deep theses yet"

    drafts_state = state.get("drafts", {}) or {}
    draft_count = drafts_state.get("count", 0)
    draft_rows_html = ""
    for draft in drafts_state.get("rows", []) or []:
        one = draft.get("one_liner") or "-"
        if one.startswith("[DRAFT"):
            idx = one.find("]")
            if idx > 0:
                one = one[idx + 1 :].strip()
        draft_rows_html += (
            "<tr><td class=\"mono\">"
            + esc(draft["ticker"])
            + " <span class=\"badge-draft\">DRAFT</span></td><td>"
            + esc(draft.get("profile") or "-")
            + "</td><td>"
            + esc(draft.get("generated_at") or "-")
            + "</td><td class=\"small\">"
            + esc(one[:200])
            + "</td></tr>"
        )
    drafts_panel_html = ""
    if draft_count > 0:
        drafts_panel_html = (
            "<section class=\"panel drafts\"><h2>Drafts pending curation <span class=\"muted\">"
            + str(draft_count)
            + " auto-generated stubs - NOT in executive_summary.pdf</span></h2><div class=\"note-draft\">"
            + esc(drafts_state.get("note", ""))
            + "</div><table class=\"full\"><thead><tr><th>ticker</th><th>profile</th><th>generated</th><th>headline / draft one-liner</th></tr></thead><tbody>"
            + draft_rows_html
            + "</tbody></table></section>"
        )

    css = (
        ":root{color-scheme:light}*{box-sizing:border-box}"
        "body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f7f7f5;color:#1b1b1a;font-size:14px;line-height:1.45}"
        ".top{padding:20px 28px;background:#fff;border-bottom:1px solid #e3e3de;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:16px}"
        ".top h1{margin:0;font-size:20px;font-weight:600;letter-spacing:-0.3px}.top h1 .muted{color:#888;font-weight:400;font-size:13px;margin-left:8px}"
        ".kpis{display:flex;gap:24px;flex-wrap:wrap}.kpi{display:flex;flex-direction:column}.kpi .v{font-size:18px;font-weight:600}.kpi .l{font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.5px}"
        ".links{display:flex;gap:12px;flex-wrap:wrap}.links a{color:#1a4d8f;text-decoration:none;font-size:13px;border:1px solid #c8d4e5;padding:6px 12px;border-radius:5px;background:#f0f5fb}"
        ".links a:hover{background:#e1edf9}.sev-dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle}"
        "main{padding:20px 28px;display:grid;grid-template-columns:1fr;gap:20px}section.panel{background:#fff;border:1px solid #e3e3de;border-radius:8px;padding:18px}"
        "section.panel h2{margin:0 0 12px 0;font-size:15px;font-weight:600;letter-spacing:-0.2px}section.panel h2 .muted{color:#888;font-weight:400;font-size:12px;margin-left:8px}"
        ".grid{display:grid;gap:16px}.grid.profiles{grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}.grid.halves{grid-template-columns:1fr 1fr}@media(max-width:760px){.grid.halves{grid-template-columns:1fr}}"
        ".card{background:#fafaf7;border:1px solid #e8e8e2;border-radius:6px;padding:12px}.card-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}.card-title{font-weight:600;font-size:13px}"
        ".pill{background:#1b1b1a;color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;font-weight:500}table{width:100%;border-collapse:collapse}"
        "table.mini th,table.mini td{padding:4px 6px;font-size:12px;border-bottom:1px solid #efefe9;text-align:left}table.full th,table.full td{padding:6px 10px;font-size:13px;border-bottom:1px solid #efefe9;text-align:left}"
        "th{color:#666;font-weight:500;text-transform:uppercase;font-size:11px;letter-spacing:.3px}.right{text-align:right}.mono{font-family:'SF Mono',Consolas,Menlo,monospace;font-size:12px}.small{font-size:11px;color:#666}.muted{color:#888}"
        ".dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px;vertical-align:middle}.buckets{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}.bucket{background:#fafaf7;border:1px solid #e8e8e2;border-radius:6px;padding:10px}.bucket h4{margin:0 0 6px 0;font-size:12px;font-weight:600}.bucket ul{margin:0;padding-left:18px;font-size:12px}.bucket li{margin-bottom:2px}"
        ".badge-draft{display:inline-block;background:#d4a418;color:#fff;font-size:9px;font-weight:700;letter-spacing:.5px;padding:1px 6px;border-radius:3px;margin-left:6px;vertical-align:middle}.panel.drafts{border-left:3px solid #d4a418}.note-draft{background:#fff8e6;border:1px solid #e5d089;color:#6b5300;font-size:12px;padding:8px 12px;border-radius:4px;margin-bottom:10px}"
        ".badge-conf{display:inline-block;font-size:9px;font-weight:700;letter-spacing:.5px;padding:2px 6px;border-radius:3px;color:#fff;vertical-align:middle}.badge-high{background:#2d8f3f}.badge-med{background:#d4a418}.badge-low{background:#c0392b}.badge-none{background:#aaa}.badge-dir{display:inline-block;font-size:9px;font-weight:700;letter-spacing:.5px;padding:2px 6px;border-radius:3px;color:#fff;margin-left:4px;vertical-align:middle}.badge-long{background:#1a4d8f}.badge-short{background:#7a3b8f}"
        ".deep-note{background:#eef3fb;border:1px solid #c8d4e5;color:#1a4d8f;font-size:11px;padding:6px 10px;border-radius:4px;margin-bottom:10px}.footer{padding:16px 28px;color:#666;font-size:11px;text-align:center;border-top:1px solid #e3e3de}"
    )

    cal_today = esc(state.get("calendar", {}).get("today", ""))
    cal_window = state.get("calendar", {}).get("window_days", 0)

    html = (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Conan - Dashboard</title><style>" + css + "</style></head><body>"
        "<div class=\"top\"><div><h1>Conan Dashboard <span class=\"muted\">generated " + esc(ran) + "</span></h1></div>"
        "<div class=\"kpis\">"
        "<div class=\"kpi\"><span class=\"v\">" + str(signal_count) + "</span><span class=\"l\">Signals</span></div>"
        "<div class=\"kpi\"><span class=\"v\">" + str(conv_n) + "</span><span class=\"l\">Convergence groups</span></div>"
        "<div class=\"kpi\"><span class=\"v\"><span class=\"sev-dot\" style=\"background:" + sev_color + "\"></span>" + esc(max_sev) + "</span><span class=\"l\">Scanner health</span></div>"
        "<div class=\"kpi\"><span class=\"v\">" + str(dossier_count) + "</span><span class=\"l\">Dossiers</span></div>"
        "<div class=\"kpi\"><span class=\"v\">" + str(draft_count) + "</span><span class=\"l\">Drafts pending</span></div>"
        "<div class=\"kpi\"><span class=\"v\">" + str(deep_n) + "</span><span class=\"l\">Deep theses</span></div>"
        "</div><div class=\"links\">"
        "<a href=\"reporting/summary/executive_summary.pdf\" target=\"_blank\">Executive summary</a>"
        "<a href=\"reporting/dossiers/\" target=\"_blank\">Dossiers folder</a>"
        "</div></div><main>"
        "<section class=\"panel\"><h2>Active signals by scoring profile <span class=\"muted\">(top 5 recent per profile)</span></h2><div class=\"grid profiles\">"
        + profile_cards_html
        + "</div></section><div class=\"grid halves\">"
        "<section class=\"panel\"><h2>Top convergence groups <span class=\"muted\">("
        + str(conv_n)
        + " total)</span></h2><table class=\"full\"><thead><tr><th>tickers</th><th class=\"right\">signals</th><th>scanners</th><th>profiles</th><th>type</th><th class=\"right\">bonus</th></tr></thead><tbody>"
        + (conv_rows or "<tr><td colspan=6 class=\"muted\">no convergence groups</td></tr>")
        + "</tbody></table></section>"
        "<section class=\"panel\"><h2>Scanner health <span class=\"muted\">by family</span></h2><table class=\"full\"><thead><tr><th>family</th><th>severity</th><th>note</th></tr></thead><tbody>"
        + (health_rows or "<tr><td colspan=3 class=\"muted\">no health report found</td></tr>")
        + "</tbody></table></section></div>"
        "<section class=\"panel\"><h2>Catalyst calendar <span class=\"muted\">window "
        + str(cal_window)
        + "d, today "
        + cal_today
        + "</span></h2><div class=\"buckets\">"
        + cal_html
        + "</div></section>"
        "<section class=\"panel\"><h2>Candidate queue <span class=\"muted\">live candidates only - drafts shown separately below</span></h2>"
        "<div class=\"deep-note\">Engine deep-thesis: "
        + deep_line
        + ". Confidence = evidence quality (HIGH/MED/LOW). Direction = engine-inferred bias (LONG/SHORT).</div>"
        "<table class=\"full\"><thead><tr><th>ticker</th><th>engine thesis</th><th>situation (excerpt)</th><th>decision</th><th>triggers</th><th class=\"right\">price move</th></tr></thead><tbody>"
        + (cand_rows or "<tr><td colspan=6 class=\"muted\">no live candidates under active monitoring</td></tr>")
        + "</tbody></table></section>"
        + drafts_panel_html
        + "</main><div class=\"footer\">Regenerated by <span class=\"mono\">unified_system/tools/build_dashboard.py</span>. "
        "Data sources: signal_log.json, convergence_report_*.json, health_report_*.json, catalyst_calendar.json, candidate_monitor_report_*.json, candidates/_curated_rationales.json</div>"
        "<script id=\"dashboard-state\" type=\"application/json\">"
        + payload
        + "</script></body></html>"
    )
    return html


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args()
    state = gather_state()
    html = render_html(state)
    if args.stdout:
        sys.stdout.write(html)
        return
    DASHBOARD.parent.mkdir(parents=True, exist_ok=True)
    with open(DASHBOARD, "w", encoding="utf-8") as handle:
        handle.write(html)
    size_kb = DASHBOARD.stat().st_size // 1024
    print(f"Wrote {DASHBOARD} ({size_kb} KB)")
    print(f"  signals: {state['signal_count']}")
    print(f"  profiles: {list(state['profiles'].keys())}")
    print(f"  convergence groups: {state['convergence']['n_groups']}")
    print(f"  health: {state['health']['max_severity']}")
    print(f"  live candidates: {len(state.get('candidates', {}).get('per_ticker_summary', []))}")
    print(f"  drafts pending: {state.get('drafts', {}).get('count', 0)}")
    print(f"  deep theses: {state.get('candidates', {}).get('deep_thesis_analysed', 0)} ({state.get('candidates', {}).get('deep_thesis_conf', {})})")


if __name__ == "__main__":
    main()
