"""WI-2 follow-up — refresh class-peer base rates for the BC pre-gate.

Reads `fda_regulatory_events` joined to `fda_assets`, buckets resolved
approval / CRL / withdrawal decisions by (moa_canonical, indication), and
upserts (n_approvals, n_crls, approval_rate, Wilson CI) into
`fda_class_precedent_base_rates`.

The reactor's `bc-pregate.ts` reads this table at gate time to fill the
`class_precedent` input (was stubbed to 0 in v1). When the table is seeded
the pre-gate composite max climbs from 10 → 15; operator should bump
`internal_config.bc_pregate_threshold` from 6 to 9 in the same flip.

Pedro 2026-05-25 — table keyed by (moa_canonical, indication). v1
normalization is `LOWER(TRIM(...))` on the raw fda_assets columns; v2 will
swap in ChEMBL-normalized MoA without a schema change.

Run locally:
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python3 -m modal_workers.scripts.bc_class_precedent_refresher --apply
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_YEARS = 10
WILSON_Z_95 = 1.959963984540054  # exact 95% normal-quantile

APPROVAL_TYPES = ("approval",)
CRL_TYPES = ("crl", "presumed_crl", "withdrawal")
DECISION_TYPES = APPROVAL_TYPES + CRL_TYPES


# ---------------------------------------------------------------------------
# Pure aggregation + Wilson CI
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassKey:
    moa_canonical: str
    indication: str


@dataclass
class BaseRateRow:
    moa_canonical: str
    indication: str
    n_approvals: int
    n_crls: int
    approval_rate: Optional[float]
    ci_low: Optional[float]
    ci_high: Optional[float]
    lookback_years: int
    source: str = "fda_regulatory_events"

    def as_db_row(self) -> Dict[str, Any]:
        return {
            "moa_canonical": self.moa_canonical,
            "indication": self.indication,
            "n_approvals": self.n_approvals,
            "n_crls": self.n_crls,
            "approval_rate": self.approval_rate,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "lookback_years": self.lookback_years,
            "source": self.source,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }


def normalize_class_field(value: Any) -> str:
    """v1 canonicalization: lowercase + collapse whitespace. Returns '' when
    the input is None/empty so the caller can skip the row cleanly.
    """
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().lower().split())


def wilson_ci(successes: int, n: int, z: float = WILSON_Z_95) -> Tuple[Optional[float], Optional[float]]:
    """Wilson score interval for a binomial proportion. Returns (low, high) in
    [0, 1]. Returns (None, None) when n == 0 — the caller decides how to
    represent "no data".
    """
    if n <= 0:
        return (None, None)
    p_hat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2 * n)) / denom
    half = (z * math.sqrt(p_hat * (1 - p_hat) / n + z2 / (4 * n * n))) / denom
    low = max(0.0, center - half)
    high = min(1.0, center + half)
    return (round(low, 4), round(high, 4))


def bucket_decisions(
    rows: Iterable[Dict[str, Any]],
) -> Dict[ClassKey, Dict[str, int]]:
    """Group raw fda_regulatory_events+fda_assets rows by (moa, indication)
    and count approvals vs. CRL-class outcomes. Pure — drives unit tests.
    """
    buckets: Dict[ClassKey, Dict[str, int]] = {}
    for row in rows:
        asset = row.get("fda_assets") or {}
        if not isinstance(asset, dict):
            continue
        moa = normalize_class_field(asset.get("mechanism"))
        ind = normalize_class_field(asset.get("indication"))
        if not moa or not ind:
            continue
        event_type = row.get("event_type")
        if event_type not in DECISION_TYPES:
            continue
        key = ClassKey(moa_canonical=moa, indication=ind)
        slot = buckets.setdefault(key, {"approvals": 0, "crls": 0})
        if event_type in APPROVAL_TYPES:
            slot["approvals"] += 1
        else:
            slot["crls"] += 1
    return buckets


def build_base_rate_rows(
    buckets: Dict[ClassKey, Dict[str, int]],
    *,
    lookback_years: int = DEFAULT_LOOKBACK_YEARS,
) -> List[BaseRateRow]:
    """Convert bucket counts into BaseRateRow records ready for upsert.
    Empty buckets (n_approvals + n_crls == 0) are skipped — the reactor
    treats absent rows as class_precedent=0 already.
    """
    out: List[BaseRateRow] = []
    for key, counts in buckets.items():
        approvals = counts["approvals"]
        crls = counts["crls"]
        total = approvals + crls
        if total == 0:
            continue
        rate = round(approvals / total, 4)
        ci_low, ci_high = wilson_ci(approvals, total)
        out.append(
            BaseRateRow(
                moa_canonical=key.moa_canonical,
                indication=key.indication,
                n_approvals=approvals,
                n_crls=crls,
                approval_rate=rate,
                ci_low=ci_low,
                ci_high=ci_high,
                lookback_years=lookback_years,
            )
        )
    return out


# ---------------------------------------------------------------------------
# DB I/O — small surface so tests monkeypatch each one.
# ---------------------------------------------------------------------------


def fetch_decisions(
    sb: SupabaseClient,
    *,
    lookback_years: int = DEFAULT_LOOKBACK_YEARS,
    today: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Pull resolved approval/CRL/withdrawal events joined to their asset's
    (mechanism, indication). Lookback window is N years (default 10),
    inclusive on both ends.
    """
    anchor = today or datetime.now(timezone.utc).date()
    floor = anchor - timedelta(days=lookback_years * 365 + 2)  # +2 for leap padding
    in_clause = "(" + ",".join(DECISION_TYPES) + ")"
    rows = sb._rest_with_retry(
        "GET",
        "fda_regulatory_events",
        params={
            "select": "event_type,event_date,event_status,fda_assets!inner(mechanism,indication)",
            "event_type": f"in.{in_clause}",
            "event_status": "eq.resolved",
            "event_date": f"gte.{floor.isoformat()}",
        },
    ) or []
    return rows


def upsert_base_rates(
    sb: SupabaseClient,
    rows: List[BaseRateRow],
    *,
    apply: bool = False,
) -> int:
    """Bulk upsert via PostgREST resolution=merge-duplicates. Returns the
    count of rows that would be / were written. Dry-run by default.
    """
    if not rows:
        return 0
    if not apply:
        return len(rows)
    payload = [r.as_db_row() for r in rows]
    sb._rest_with_retry(
        "POST",
        "fda_class_precedent_base_rates",
        params={"on_conflict": "moa_canonical,indication"},
        json_body=payload,
        prefer="resolution=merge-duplicates,return=minimal",
    )
    return len(rows)


# ---------------------------------------------------------------------------
# Top-level refresh
# ---------------------------------------------------------------------------


def refresh(
    sb: SupabaseClient,
    *,
    lookback_years: int = DEFAULT_LOOKBACK_YEARS,
    apply: bool = False,
    today: Optional[date] = None,
) -> Dict[str, int]:
    decisions = fetch_decisions(sb, lookback_years=lookback_years, today=today)
    buckets = bucket_decisions(decisions)
    rate_rows = build_base_rate_rows(buckets, lookback_years=lookback_years)
    written = upsert_base_rates(sb, rate_rows, apply=apply)
    return {
        "decisions_fetched": len(decisions),
        "class_buckets": len(buckets),
        "rate_rows": len(rate_rows),
        "written": written,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lookback-years", type=int, default=DEFAULT_LOOKBACK_YEARS)
    p.add_argument("--apply", action="store_true",
                   help="Persist rows. Default is dry-run (counts only).")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    sb = SupabaseClient()
    try:
        result = refresh(
            sb, lookback_years=args.lookback_years, apply=args.apply,
        )
    except SupabaseError as exc:
        logger.error("refresh failed: %s", exc)
        return 1
    print(f"[bc_class_precedent_refresher] {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
