"""
ptab_v2_snapshot.py — One-shot pull of USPTO PTAB v2 proceeding list.

Rationale (per D-014 clause (d)):
  The USPTO Developer Hub PTAB v2 API decommissions 2026-04-20. PTAB v3 at
  data.uspto.gov is live but WAF-gated (Q-004). Before v2 dies we want a
  historical-baseline snapshot of PTAB proceedings so the Phase 3+ PTAB
  scanner has context even if v3 remains gated.

Scope:
  - Single-invocation script. NOT a scheduled task. NOT a recurring scanner.
  - Paginates through the PTAB v2 proceedings endpoint, writes JSONL lines
    to baselines/ptab_baseline_proceedings.json (as a JSON object with a
    schema_version wrapper + entries list — matches the other baseline
    files' shape).
  - Resume-friendly: if the output file already exists, loads the last
    `pageNumber` seen and continues from there. Safe to re-run mid-pull.

Authored offline in Session 3 (sandbox was unavailable); NOT executed
against live endpoints this session. Next session MUST run this before
2026-04-20 while v2 is still reachable.

Usage:
  python tools/ptab_v2_snapshot.py             # full pull, page size 100
  python tools/ptab_v2_snapshot.py --limit 500 # test-mode, first 500 rows only
  python tools/ptab_v2_snapshot.py --resume    # continue from last saved page
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

_HERE = Path(__file__).resolve().parent
_LITIGATION_ROOT = _HERE.parent
_OUTPUT_PATH = _LITIGATION_ROOT / "baselines" / "ptab_baseline_proceedings.json"

# D-014: v2 base URL. Documented endpoint before Developer Hub decommission.
V2_BASE_URL = "https://developer.uspto.gov/ptab-api/proceedings"

# D-015: USPTO under Developer Hub historically accepts the operational UA.
# If this fails post-2026-04-20, the endpoint is already dead and there is
# nothing to recover.
OPERATIONAL_UA = "Litigation Signal Tool contact-javiergorordo13@hotmail.com"

# Be polite. Developer Hub documented rate limit is ~1 req/sec for unauthenticated.
MIN_REQUEST_INTERVAL_SECONDS = 1.1
HTTP_TIMEOUT_SECONDS = 30
HTTP_RETRY_COUNT = 3
HTTP_RETRY_BACKOFF_SECONDS = 3.0

PAGE_SIZE = 100  # v2 maximum per page

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("ptab_v2_snapshot")


def _load_existing() -> Dict:
    """Load prior partial snapshot (resume support)."""
    if not _OUTPUT_PATH.exists():
        return {
            "_schema_version": 1,
            "_description": "PTAB v2 one-shot snapshot. Pulled before 2026-04-20 v2 decommission per D-014(d). Not a live/maintained baseline — frozen historical record.",
            "_source_endpoint": V2_BASE_URL,
            "_pulled_at": None,
            "_pull_complete": False,
            "_last_page_fetched": 0,
            "_total_records_seen": None,
            "entries": [],
        }
    try:
        return json.loads(_OUTPUT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.error("Existing snapshot file is corrupt; aborting rather than overwrite.")
        sys.exit(2)


def _atomic_save(doc: Dict) -> None:
    tmp = _OUTPUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=False), encoding="utf-8")
    tmp.replace(_OUTPUT_PATH)


def _http_get_json(url: str, params: Dict, session) -> Optional[Dict]:
    """Throttled GET with retry. Returns parsed JSON or None on terminal failure."""
    headers = {"User-Agent": OPERATIONAL_UA, "Accept": "application/json"}
    last_err = None
    for attempt in range(HTTP_RETRY_COUNT):
        try:
            time.sleep(MIN_REQUEST_INTERVAL_SECONDS)
            r = session.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
            if r.status_code == 200:
                # Content-Type guard: Developer Hub sometimes serves HTML when
                # an endpoint is being retired. Fail fast rather than cache
                # garbage.
                ctype = r.headers.get("Content-Type", "")
                if "json" not in ctype.lower():
                    raise RuntimeError(f"Non-JSON content-type: {ctype!r}; first 200 bytes: {r.text[:200]!r}")
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {r.status_code}"
                time.sleep(HTTP_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            # 404 after 2026-04-20 = endpoint is dead; 410 = gone
            if r.status_code in (404, 410):
                logger.error("Endpoint returned %s — v2 may already be decommissioned.", r.status_code)
                return None
            last_err = f"HTTP {r.status_code}: {r.text[:200]!r}"
            break
        except Exception as e:
            last_err = repr(e)
            time.sleep(HTTP_RETRY_BACKOFF_SECONDS * (attempt + 1))
    logger.error("GET %s failed after %d attempts: %s", url, HTTP_RETRY_COUNT, last_err)
    return None


def pull(limit: Optional[int] = None, resume: bool = False) -> int:
    """Pull PTAB v2 proceedings. Returns exit code."""
    try:
        import requests  # lazy so the module imports offline
    except ImportError:
        logger.error("requests not installed. Run: pip install requests --break-system-packages")
        return 2

    session = requests.Session()
    doc = _load_existing()
    if not resume:
        # Fresh run — reset cursors but keep file to let a replay notice it existed
        doc["_pulled_at"] = None
        doc["_pull_complete"] = False
        doc["_last_page_fetched"] = 0
        doc["entries"] = []

    page = doc.get("_last_page_fetched", 0)
    seen = len(doc["entries"])
    logger.info("Starting pull. Resume=%s, starting page=%d, records so far=%d",
                resume, page, seen)

    while True:
        if limit is not None and seen >= limit:
            logger.info("Hit --limit %d; stopping.", limit)
            break

        page += 1
        params = {"pageNumber": page, "pageSize": PAGE_SIZE}
        payload = _http_get_json(V2_BASE_URL, params=params, session=session)
        if payload is None:
            logger.error("Terminal failure on page %d; saving partial and exiting.", page)
            doc["_last_page_fetched"] = page - 1
            _atomic_save(doc)
            return 1

        # v2 response shape: {"results": [...], "recordTotalQuantity": N} per USPTO docs.
        # Tolerate either "results" or "proceedings" as the list key.
        batch = payload.get("results") or payload.get("proceedings") or []
        total = payload.get("recordTotalQuantity") or payload.get("totalResults")
        if total is not None:
            doc["_total_records_seen"] = total

        if not batch:
            logger.info("Empty page %d — pull complete.", page)
            doc["_pull_complete"] = True
            break

        doc["entries"].extend(batch)
        seen += len(batch)
        doc["_last_page_fetched"] = page

        # Periodic atomic save so a mid-pull crash doesn't lose everything.
        if page % 5 == 0:
            doc["_pulled_at"] = datetime.now(timezone.utc).isoformat()
            _atomic_save(doc)
            logger.info("Saved progress: %d records through page %d.", seen, page)

        if len(batch) < PAGE_SIZE:
            logger.info("Partial page (%d < %d) — pull complete.", len(batch), PAGE_SIZE)
            doc["_pull_complete"] = True
            break

    doc["_pulled_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_save(doc)
    logger.info("Final: %d records, %d pages, complete=%s",
                len(doc["entries"]), doc["_last_page_fetched"], doc["_pull_complete"])
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after this many records (test mode).")
    p.add_argument("--resume", action="store_true",
                   help="Resume from the last saved page instead of starting over.")
    args = p.parse_args()
    return pull(limit=args.limit, resume=args.resume)


if __name__ == "__main__":
    sys.exit(main())
