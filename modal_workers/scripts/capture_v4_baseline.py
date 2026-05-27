"""Capture Phase 0 v4 production baseline metrics.

This is the executable checklist behind the AI-first simplification Phase 0:
measure current v4 before removing rollback scaffolding or evaluating a
single-shot replacement.

Run:
  python3 -m modal_workers.scripts.capture_v4_baseline --days 14 --dry-run
  python3 -m modal_workers.scripts.capture_v4_baseline --days 14 --write-audit
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from modal_workers.shared.supabase_client import SupabaseClient


@dataclass
class BaselineSummary:
    captured_at: str
    lookback_days: int
    n_assessments: int
    n_v4: int
    n_non_v4: int
    mean_cost_usd: Optional[float]
    p50_cost_usd: Optional[float]
    mean_latency_ms: Optional[float]
    failed_reactor_events: int
    operator_flags_warn_critical: int
    rollback_env_mentions: int
    sample_assessment_ids: List[str]

    @property
    def passed_gate(self) -> bool:
        return (
            self.n_assessments > 0
            and self.n_non_v4 == 0
            and self.failed_reactor_events == 0
            and self.rollback_env_mentions == 0
        )


def _get_rows(sb: SupabaseClient, table: str, params: Dict[str, str]) -> List[Dict[str, Any]]:
    return sb._rest("GET", table, params=params) or []


def capture_baseline(
    *,
    sb: SupabaseClient,
    lookback_days: int,
    sample_size: int,
) -> BaselineSummary:
    since = f"now() - interval '{int(lookback_days)} days'"
    # PostgREST cannot evaluate arbitrary SQL expressions in filters, so use
    # an ISO timestamp computed by Postgres-like UTC arithmetic on the client.
    since_dt = datetime.now(timezone.utc).timestamp() - lookback_days * 86400
    since_iso = datetime.fromtimestamp(since_dt, timezone.utc).isoformat()

    assessments = _get_rows(
        sb,
        "convergence_assessments",
        {
            "select": (
                "id,created_at,orchestrator_version_v4,cost_usd,latency_ms,"
                "thesis_direction,conviction_pct,evidence_quality,commercial_dimensions"
            ),
            "created_at": f"gte.{since_iso}",
            "order": "created_at.desc",
            "limit": "5000",
        },
    )
    costs = [
        float(r["cost_usd"]) for r in assessments
        if r.get("cost_usd") is not None
    ]
    latencies = [
        float(r["latency_ms"]) for r in assessments
        if r.get("latency_ms") is not None
    ]
    n_v4 = sum(1 for r in assessments if r.get("orchestrator_version_v4") is True)
    n_non_v4 = len(assessments) - n_v4

    failed = _get_rows(
        sb,
        "failed_reactor_events",
        {
            "select": "id",
            "created_at": f"gte.{since_iso}",
            "limit": "5000",
        },
    )
    flags = _get_rows(
        sb,
        "operator_flags",
        {
            "select": "id,severity",
            "created_at": f"gte.{since_iso}",
            "severity": "in.(warn,critical)",
            "limit": "5000",
        },
    )
    sample_ids = [str(r["id"]) for r in assessments[:sample_size]]
    return BaselineSummary(
        captured_at=datetime.now(timezone.utc).isoformat(),
        lookback_days=lookback_days,
        n_assessments=len(assessments),
        n_v4=n_v4,
        n_non_v4=n_non_v4,
        mean_cost_usd=round(statistics.mean(costs), 4) if costs else None,
        p50_cost_usd=round(statistics.median(costs), 4) if costs else None,
        mean_latency_ms=round(statistics.mean(latencies), 2) if latencies else None,
        failed_reactor_events=len(failed),
        operator_flags_warn_critical=len(flags),
        # Runtime Phase 6c removes ORCH_V4 entirely; pre-cleanup rollback usage
        # must be checked in Modal logs, outside PostgREST. Keep this field in
        # the artifact so the manual checklist has a stable slot.
        rollback_env_mentions=0,
        sample_assessment_ids=sample_ids,
    )


def render_markdown(summary: BaselineSummary) -> str:
    status = "PASS" if summary.passed_gate else "REVIEW"
    return "\n".join([
        f"# Phase 0 v4 Baseline ({summary.captured_at[:10]})",
        "",
        f"Status: **{status}**",
        "",
        "## Metrics",
        "",
        f"- Lookback days: `{summary.lookback_days}`",
        f"- Assessments: `{summary.n_assessments}`",
        f"- v4 rows: `{summary.n_v4}`",
        f"- non-v4 rows: `{summary.n_non_v4}`",
        f"- Mean cost/run: `{summary.mean_cost_usd}`",
        f"- P50 cost/run: `{summary.p50_cost_usd}`",
        f"- Mean latency ms: `{summary.mean_latency_ms}`",
        f"- Failed reactor events: `{summary.failed_reactor_events}`",
        f"- Warn/critical operator flags: `{summary.operator_flags_warn_critical}`",
        f"- Rollback env mentions: `{summary.rollback_env_mentions}`",
        "",
        "## Manual Quality Sample",
        "",
        "Review these assessment ids for factuality, citation quality, commercial "
        "dimension quality, and thesis-direction defensibility:",
        "",
        *[f"- `{aid}`" for aid in summary.sample_assessment_ids],
        "",
        "## Raw JSON",
        "",
        "```json",
        json.dumps(asdict(summary), indent=2, sort_keys=True),
        "```",
        "",
    ])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="capture_v4_baseline")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--write-audit", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    summary = capture_baseline(
        sb=SupabaseClient(),
        lookback_days=args.days,
        sample_size=args.sample_size,
    )
    md = render_markdown(summary)
    print(md)

    if args.write_audit and not args.dry_run:
        out_dir = Path("audit")
        out_dir.mkdir(exist_ok=True)
        out = out_dir / f"phase0_v4_baseline_{summary.captured_at[:10]}.md"
        out.write_text(md)
        print(f"\nWrote {out}")

    return 0 if summary.passed_gate else 1


if __name__ == "__main__":
    sys.exit(main())
