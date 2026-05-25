"""WI-6 — Q2 sample-balance audit.

Port of v2_skills/audit-historical-sample-balance. Runs on the cohort of
eval_harness rows where q1_verdict='clean' and computes Herfindahl-style
concentration across 5 axes:

  1. HIT/MISS ratio       — direction-aligned outcome distribution
  2. Time concentration   — Herfindahl on year buckets of reference_assessment_date
  3. Sector concentration — Herfindahl on fda_assets.indication
  4. Sponsor concentration — Herfindahl on fda_assets.sponsor_name
  5. Survivorship         — share of issuer_status IN ('delisted','acquired','bankrupt')

Verdict ladder:
  - any axis fail → fail
  - 1-2 axes warn → pass_with_warnings
  - all axes pass → pass

Gate integration: nightly_calibration_refit reads the latest audit row and,
when internal_config.q2_gate_mode='required', refuses curve promotion if
verdict='fail'. Default 'warn' shadows the audit without blocking.

Run locally:
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python3 -m modal_workers.scripts.audit_sample_balance \\
        --profile binary_catalyst --apply
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError  # noqa: E402

logger = logging.getLogger(__name__)

# Thresholds — keep in lockstep with the plan's WI-6 table.
HIT_MISS_WARN_LOW, HIT_MISS_WARN_HIGH = 0.30, 0.70
HIT_MISS_FAIL_LOW, HIT_MISS_FAIL_HIGH = 0.20, 0.80
HERFINDAHL_WARN = 0.25
HERFINDAHL_FAIL = 0.40
SURVIVORSHIP_WARN_PCT = 0.05  # <5% delisted+acquired+bankrupt → warn
SURVIVORSHIP_FAIL_N_FLOOR = 50  # below this n, fail-on-zero isn't safe

Status = Literal["pass", "warn", "fail"]
Verdict = Literal["pass", "pass_with_warnings", "fail"]

DELISTED_STATUSES = ("delisted", "acquired", "bankrupt")


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------


@dataclass
class AxisResult:
    value: float
    threshold_warn: Any
    threshold_fail: Any
    status: Status

    def as_dict(self) -> Dict[str, Any]:
        return {
            "value": self.value,
            "threshold_warn": self.threshold_warn,
            "threshold_fail": self.threshold_fail,
            "status": self.status,
        }


@dataclass
class Q2Verdict:
    cohort_hash: str
    cohort_size: int
    verdict: Verdict
    axes: Dict[str, AxisResult] = field(default_factory=dict)
    phase5_triggers: List[str] = field(default_factory=list)
    audit_date: str = field(
        default_factory=lambda: datetime.now(timezone.utc).date().isoformat()
    )

    def as_db_row(self) -> Dict[str, Any]:
        return {
            "cohort_hash": self.cohort_hash,
            "cohort_size": self.cohort_size,
            "audit_date": self.audit_date,
            "verdict": self.verdict,
            "axes": {k: v.as_dict() for k, v in self.axes.items()},
            "phase5_triggers": self.phase5_triggers,
        }


# ---------------------------------------------------------------------------
# Pure helpers — no I/O. Tests drive these.
# ---------------------------------------------------------------------------


def compute_cohort_hash(pairs: Iterable[Tuple[str, str]]) -> str:
    """sha256 over sorted '<asset_id>|<ref_date>' lines, truncated to 16 hex
    chars. Deterministic per training pool.

    Inputs are (asset_id, reference_assessment_date) tuples; the helper
    sorts them before hashing so order of arrival doesn't matter.
    """
    lines = sorted(f"{a}|{r}" for a, r in pairs)
    joined = "\n".join(lines).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:16]


def herfindahl(values: Iterable[Any]) -> float:
    """Standard HHI on category counts: sum of squared shares. 0 = perfectly
    diverse, 1 = single-category dominance. NULL/empty values are dropped.
    """
    counts = Counter(v for v in values if v is not None and v != "")
    n = sum(counts.values())
    if n == 0:
        return 0.0
    return sum((c / n) ** 2 for c in counts.values())


def _hit_miss_axis(*, n_hits: int, n_total: int) -> AxisResult:
    if n_total == 0:
        # No data → fail loudly (downstream gate will catch).
        return AxisResult(
            value=0.0, threshold_warn=(HIT_MISS_WARN_LOW, HIT_MISS_WARN_HIGH),
            threshold_fail=(HIT_MISS_FAIL_LOW, HIT_MISS_FAIL_HIGH),
            status="fail",
        )
    ratio = n_hits / n_total
    if ratio < HIT_MISS_FAIL_LOW or ratio > HIT_MISS_FAIL_HIGH:
        status: Status = "fail"
    elif ratio < HIT_MISS_WARN_LOW or ratio > HIT_MISS_WARN_HIGH:
        status = "warn"
    else:
        status = "pass"
    return AxisResult(
        value=round(ratio, 4),
        threshold_warn=(HIT_MISS_WARN_LOW, HIT_MISS_WARN_HIGH),
        threshold_fail=(HIT_MISS_FAIL_LOW, HIT_MISS_FAIL_HIGH),
        status=status,
    )


def _herfindahl_axis(values: Iterable[Any]) -> AxisResult:
    h = herfindahl(values)
    if h > HERFINDAHL_FAIL:
        status: Status = "fail"
    elif h > HERFINDAHL_WARN:
        status = "warn"
    else:
        status = "pass"
    return AxisResult(
        value=round(h, 4),
        threshold_warn=HERFINDAHL_WARN,
        threshold_fail=HERFINDAHL_FAIL,
        status=status,
    )


def _survivorship_axis(
    *, n_delisted_etc: int, n_total: int,
) -> AxisResult:
    if n_total == 0:
        return AxisResult(
            value=0.0, threshold_warn=SURVIVORSHIP_WARN_PCT,
            threshold_fail=0.0, status="fail",
        )
    pct = n_delisted_etc / n_total
    if pct == 0 and n_total > SURVIVORSHIP_FAIL_N_FLOOR:
        # Zero delisted rows in a cohort large enough that we'd expect some
        # — strong survivorship-bias signal.
        status: Status = "fail"
    elif pct < SURVIVORSHIP_WARN_PCT:
        status = "warn"
    else:
        status = "pass"
    return AxisResult(
        value=round(pct, 4),
        threshold_warn=SURVIVORSHIP_WARN_PCT,
        threshold_fail=0.0,
        status=status,
    )


def assemble_q2_verdict(
    *,
    cohort_pairs: List[Tuple[str, str]],
    n_hits: int,
    n_total: int,
    years: List[Any],
    sectors: List[Any],
    sponsors: List[Any],
    n_delisted_etc: int,
) -> Q2Verdict:
    """Pure verdict assembly from cohort-level aggregates."""
    axes = {
        "hit_miss_ratio": _hit_miss_axis(n_hits=n_hits, n_total=n_total),
        "time_concentration": _herfindahl_axis(years),
        "sector_concentration": _herfindahl_axis(sectors),
        "sponsor_concentration": _herfindahl_axis(sponsors),
        "survivorship": _survivorship_axis(
            n_delisted_etc=n_delisted_etc, n_total=n_total,
        ),
    }
    statuses = [a.status for a in axes.values()]
    if "fail" in statuses:
        verdict: Verdict = "fail"
    elif statuses.count("warn") >= 1:
        verdict = "pass_with_warnings"
    else:
        verdict = "pass"

    triggers: List[str] = []
    if axes["hit_miss_ratio"].status == "fail":
        triggers.append("rebalance_hit_miss_distribution")
    if axes["time_concentration"].status == "fail":
        triggers.append("broaden_time_coverage")
    if axes["sector_concentration"].status == "fail":
        triggers.append("broaden_sector_coverage")
    if axes["sponsor_concentration"].status == "fail":
        triggers.append("broaden_sponsor_coverage")
    if axes["survivorship"].status == "fail":
        triggers.append("run_survivorship_audit")

    return Q2Verdict(
        cohort_hash=compute_cohort_hash(cohort_pairs),
        cohort_size=n_total,
        verdict=verdict,
        axes=axes,
        phase5_triggers=triggers,
    )


# ---------------------------------------------------------------------------
# DB-driven audit_cohort — orchestrates fetches + verdict assembly.
# ---------------------------------------------------------------------------


def audit_cohort(
    sb: SupabaseClient,
    *,
    profile: str = "binary_catalyst",
) -> Q2Verdict:
    """Pull q1_verdict='clean' rows for the given profile and compute the
    Q2 verdict. Profile defaults to binary_catalyst since v1 only audits
    that profile (the survivorship axis doesn't generalize).
    """
    rows = _load_clean_eval_harness_rows(sb, profile=profile)
    cohort_pairs: List[Tuple[str, str]] = []
    n_hits = 0
    years: List[Any] = []
    sectors: List[Any] = []
    sponsors: List[Any] = []
    n_delisted_etc = 0

    for r in rows:
        asset_id = r.get("asset_id")
        ref_date = r.get("reference_assessment_date")
        if not asset_id or not ref_date:
            continue
        cohort_pairs.append((str(asset_id), str(ref_date)))
        if _row_is_hit(r):
            n_hits += 1
        years.append(str(ref_date)[:4])
        fa = r.get("fda_assets") or {}
        sectors.append(fa.get("indication"))
        sponsors.append(fa.get("sponsor_name"))
        if r.get("issuer_status") in DELISTED_STATUSES:
            n_delisted_etc += 1

    return assemble_q2_verdict(
        cohort_pairs=cohort_pairs,
        n_hits=n_hits,
        n_total=len(cohort_pairs),
        years=years,
        sectors=sectors,
        sponsors=sponsors,
        n_delisted_etc=n_delisted_etc,
    )


def _row_is_hit(r: Dict[str, Any]) -> bool:
    """Read the HIT bool out of realized_outcome_data. Match the convention
    used by label_forward_returns / nightly_calibration_refit."""
    ro = r.get("realized_outcome_data") or {}
    if isinstance(ro, dict):
        return ro.get("hit") is True
    return False


# ---------------------------------------------------------------------------
# DB fetch + persist helpers — small surface for test monkeypatching.
# ---------------------------------------------------------------------------


def _load_clean_eval_harness_rows(
    sb: SupabaseClient, *, profile: str,
) -> List[Dict[str, Any]]:
    """Pull q1_verdict='clean' rows joined to fda_assets for sector/sponsor.

    Profile filter applied via realized_outcome_data->>'profile' since the
    eval_harness schema doesn't carry a top-level profile column.
    """
    query = (
        sb.from_("eval_harness")
        .select(
            "id, asset_id, reference_assessment_date, "
            "realized_outcome_data, issuer_status, "
            "fda_assets!inner(indication, sponsor_name)"
        )
        .eq("q1_verdict", "clean")
    )
    result = query.execute()
    rows = result.data or []
    return [r for r in rows if _row_matches_profile(r, profile)]


def _row_matches_profile(r: Dict[str, Any], profile: str) -> bool:
    ro = r.get("realized_outcome_data") or {}
    if isinstance(ro, dict):
        return ro.get("profile") == profile
    return False


def persist_q2_verdict(sb: SupabaseClient, verdict: Q2Verdict) -> None:
    """ON CONFLICT (cohort_hash, audit_date) DO UPDATE — re-runs on the same
    day overwrite the prior verdict so dashboards always see the latest."""
    sb.from_("eval_sample_balance_audits").upsert(
        verdict.as_db_row(),
        on_conflict="cohort_hash,audit_date",
    ).execute()


def read_q2_gate_mode(sb: SupabaseClient) -> str:
    """Read internal_config.q2_gate_mode. Returns 'warn' on missing config
    (matches the post-migration default).
    """
    result = (
        sb.from_("internal_config")
        .select("value")
        .eq("key", "q2_gate_mode")
        .maybe_single()
        .execute()
    )
    value = (result.data or {}).get("value") or "warn"
    value = value.strip().lower()
    if value not in ("off", "warn", "required"):
        return "warn"
    return value


# ---------------------------------------------------------------------------
# Gate-into-nightly_calibration_refit helper.
# ---------------------------------------------------------------------------


def evaluate_q2_gate(
    sb: SupabaseClient, *, profile: str = "binary_catalyst",
) -> Dict[str, Any]:
    """Run the cohort audit and return a routing dict for the calibration refit:

      {
        "verdict": "pass" | "pass_with_warnings" | "fail",
        "gate_mode": "off" | "warn" | "required",
        "blocks_promotion": bool,
      }

    Caller in nightly_calibration_refit reads `blocks_promotion`: when True,
    set GateEvaluation.gate_reason='q2_failed' and skip curve promotion.
    """
    gate_mode = read_q2_gate_mode(sb)
    if gate_mode == "off":
        return {
            "verdict": None, "gate_mode": "off", "blocks_promotion": False,
        }
    verdict = audit_cohort(sb, profile=profile)
    persist_q2_verdict(sb, verdict)
    blocks = (gate_mode == "required" and verdict.verdict == "fail")
    return {
        "verdict": verdict.verdict,
        "gate_mode": gate_mode,
        "blocks_promotion": blocks,
        "cohort_hash": verdict.cohort_hash,
        "cohort_size": verdict.cohort_size,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", default="binary_catalyst")
    p.add_argument("--apply", action="store_true",
                   help="Persist the verdict row. Default is dry-run.")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    sb = SupabaseClient()
    verdict = audit_cohort(sb, profile=args.profile)
    print(f"[Q2 audit] cohort={verdict.cohort_size} hash={verdict.cohort_hash} "
          f"verdict={verdict.verdict} triggers={verdict.phase5_triggers}")
    for axis, ar in verdict.axes.items():
        print(f"  {axis}: value={ar.value} status={ar.status}")
    if args.apply:
        persist_q2_verdict(sb, verdict)
        print("[Q2 audit] persisted")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
