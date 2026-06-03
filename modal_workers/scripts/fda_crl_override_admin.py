"""Record-and-activate the CRL rubric's fair_probability override (Seam 2).

The override ships OFF (shadow). This flips it via `fda_model_versions`
(scope ``fda_crl_override``) rather than a bare env flag, so every cutover is
logged + reversible:

    python -m modal_workers.scripts.fda_crl_override_admin --status
    python -m modal_workers.scripts.fda_crl_override_admin --enable v1 --notes "shadow verdict=go"
    python -m modal_workers.scripts.fda_crl_override_admin --disable

`enable()` supersedes any active override version, then inserts a new active one
(`effective_at=now`). `disable()` supersedes the active one — scoring reverts to
the base-rate path instantly. `FDA_CRL_OVERRIDE_ENABLED` still wins as an explicit
force-on / kill-switch when set. The bridge reads this state once per run via
`_resolve_crl_override_enabled`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

SCOPE = "fda_crl_override"
logger = logging.getLogger("fda_crl_override_admin")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def status(client: Any) -> Dict[str, Any]:
    rows = client._rest(
        "GET", "fda_model_versions",
        params={"scope": f"eq.{SCOPE}", "superseded_at": "is.null", "effective_at": "not.is.null",
                "select": "version,effective_at,notes", "order": "effective_at.desc", "limit": 1},
    ) or []
    return {"enabled": bool(rows), "active": rows[0] if rows else None}


def _supersede_active(client: Any, now_iso: str) -> None:
    client._rest_with_retry(
        "PATCH", "fda_model_versions",
        params={"scope": f"eq.{SCOPE}", "superseded_at": "is.null", "effective_at": "not.is.null"},
        json_body={"superseded_at": now_iso},
        prefer="return=minimal",
    )


def enable(client: Any, version: str, *, notes: Optional[str] = None,
           created_by: str = "fda_crl_override_admin", now_iso: Optional[str] = None) -> Dict[str, Any]:
    now_iso = now_iso or _now_iso()
    _supersede_active(client, now_iso)  # one active override at a time
    rows = client._rest_with_retry(
        "POST", "fda_model_versions",
        json_body=[{"version": version, "scope": SCOPE, "effective_at": now_iso,
                    "created_by": created_by, "notes": notes}],
        prefer="return=representation",
    )
    return {"enabled": True, "version": version, "row": (rows or [None])[0]}


def disable(client: Any, *, now_iso: Optional[str] = None) -> Dict[str, Any]:
    _supersede_active(client, now_iso or _now_iso())
    return {"enabled": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true")
    group.add_argument("--enable", metavar="VERSION")
    group.add_argument("--disable", action="store_true")
    parser.add_argument("--notes")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")

    from modal_workers.shared.supabase_client import SupabaseClient

    client = SupabaseClient(
        url=os.environ.get("SUPABASE_URL"),
        service_key=os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY"),
    )
    if args.status:
        out = status(client)
    elif args.enable:
        out = enable(client, args.enable, notes=args.notes)
    else:
        out = disable(client)
    logger.info("crl override:\n%s", json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
