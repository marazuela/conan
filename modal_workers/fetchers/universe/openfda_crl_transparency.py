"""FDA CRL Transparency-dump fetcher (Complete Response Letters).

Source: the openFDA Transparency CRL dataset — the FDA's own bulk export of
released Complete Response Letters (and a handful of other letter types). This is
the trustworthy CRL ledger that the A0 score-cohort study (Light-v4 Phase-1
parallel track) validates the M14 adjusted scorer against, and the future
Phase-3 *resolved-outcome* source.

Index resolution (do NOT hardcode the partition URL — read it so a future
re-partition does not silently break us):

    https://api.fda.gov/download.json
        -> results.transparency.crl.export_date      (provenance)
        -> results.transparency.crl.partitions[i].file   (the .json.zip URL)

Each partition is a ``.json.zip`` whose root JSON is ``{"meta": {...},
"results": [ ... ]}``. Per-record fields (verified 2026-06-01 export, 439 recs):
    letter_date (MM/DD/YYYY), letter_year (str), letter_type, approval_status,
    file_name, application_number (LIST, e.g. ["NDA 215344"], ["BLA 761385"]),
    company_name, company_rep, company_address, approver_name, approver_title,
    approver_center (list), text (OCR body).

This module only needs the READ path for A0: fetch -> resolve -> download ->
unzip -> parse -> persist a frozen raw snapshot under ``data/a0/``. It writes
NOTHING to Supabase. It is intentionally self-contained and is NOT wired into
any cron — A0 is an offline study.

Run locally (read-only; writes a local snapshot):

    python3 -m modal_workers.fetchers.universe.openfda_crl_transparency \
        --out-dir data/a0

If a cached unzipped export already exists (e.g. the 2026-06-01 export at
``/tmp/crl_unzipped/transparency-crl-0001-of-0001.json``) pass
``--cached-json <path>`` to reuse it offline and skip the network entirely.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

logger = logging.getLogger(__name__)

DOWNLOAD_INDEX = "https://api.fda.gov/download.json"

# Defensive parse thresholds — fail loud if the FDA changes the schema so we
# never silently validate a model against a truncated / re-shaped export.
_MIN_RESULTS = 400
_MIN_COMPLETE_RESPONSE = 400


# --------------------------------------------------------------------------- #
# index resolution
# --------------------------------------------------------------------------- #
def resolve_crl_partition(index: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the CRL dataset descriptor from the openFDA download index.

    Returns ``{"export_date": str, "total_records": int, "partitions":
    [{"file": url, ...}]}`` for ``results.transparency.crl``. Raises if the
    path is missing (schema drift / dataset renamed)."""
    transparency = (index.get("results") or {}).get("transparency") or {}
    crl = transparency.get("crl")
    if not crl:
        raise RuntimeError(
            "download.json has no results.transparency.crl — the dataset path "
            "moved or the index shape changed; investigate before trusting it."
        )
    partitions = crl.get("partitions") or []
    if not partitions:
        raise RuntimeError("transparency.crl has no partitions[]")
    return crl


# --------------------------------------------------------------------------- #
# fetch + parse
# --------------------------------------------------------------------------- #
def _http_get_json(url: str, *, timeout_s: float = 30.0) -> Dict[str, Any]:
    import requests  # local import so the cached path needs no network dep

    r = requests.get(url, timeout=timeout_s)
    r.raise_for_status()
    return r.json()


def _http_get_bytes(url: str, *, timeout_s: float = 120.0) -> bytes:
    import requests

    r = requests.get(url, timeout=timeout_s)
    r.raise_for_status()
    return r.content


def parse_zip_bytes(zip_bytes: bytes) -> Dict[str, Any]:
    """Unzip the partition (single JSON inside) and return the parsed root dict.

    Defensive: loads the exact member name from the archive (not a filesystem
    glob, which can pick up a stale file), and validates the root shape."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        if not names:
            raise RuntimeError(f"zip has no .json member; members={zf.namelist()}")
        with zf.open(names[0]) as fh:
            root = json.load(fh)
    _assert_root_shape(root)
    return root


def _assert_root_shape(root: Dict[str, Any]) -> None:
    if not isinstance(root, dict) or "meta" not in root or "results" not in root:
        raise RuntimeError(
            "CRL dump root must have keys {'meta','results'}; "
            f"got keys={list(root.keys()) if isinstance(root, dict) else type(root)}"
        )
    results = root.get("results") or []
    if len(results) < _MIN_RESULTS:
        raise RuntimeError(
            f"CRL dump has only {len(results)} results (< {_MIN_RESULTS}); "
            "refusing to trust a truncated export."
        )
    n_cr = sum(1 for r in results if r.get("letter_type") == "COMPLETE RESPONSE")
    if n_cr < _MIN_COMPLETE_RESPONSE:
        raise RuntimeError(
            f"CRL dump has only {n_cr} COMPLETE RESPONSE records "
            f"(< {_MIN_COMPLETE_RESPONSE}); schema/letter_type may have shifted."
        )


def load_cached_json(path: Path) -> Dict[str, Any]:
    """Load an already-unzipped CRL JSON snapshot and validate its shape."""
    root = json.loads(Path(path).read_text(encoding="utf-8"))
    _assert_root_shape(root)
    return root


def fetch_crl_dump(
    *,
    cached_json: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return ``{"export_date", "source_url", "root"}`` for the CRL dump.

    If ``cached_json`` is provided and exists, load it offline (no network).
    Otherwise resolve the partition from the live download index and download.
    """
    if cached_json and Path(cached_json).exists():
        logger.info("loading cached CRL dump from %s", cached_json)
        root = load_cached_json(Path(cached_json))
        export_date = (root.get("meta") or {}).get("last_updated") or "cached"
        return {"export_date": str(export_date), "source_url": str(cached_json), "root": root}

    logger.info("resolving CRL partition from %s", DOWNLOAD_INDEX)
    index = _http_get_json(DOWNLOAD_INDEX)
    crl = resolve_crl_partition(index)
    export_date = str(crl.get("export_date") or "unknown")
    part_url = crl["partitions"][0]["file"]
    logger.info("CRL export_date=%s partition=%s", export_date, part_url)
    zip_bytes = _http_get_bytes(part_url)
    root = parse_zip_bytes(zip_bytes)
    return {"export_date": export_date, "source_url": part_url, "root": root}


# --------------------------------------------------------------------------- #
# snapshot persistence
# --------------------------------------------------------------------------- #
def _safe_export_tag(export_date: str) -> str:
    """Filesystem-safe tag from an export_date / last_updated string."""
    return "".join(c if c.isalnum() else "_" for c in str(export_date))[:32] or "unknown"


def write_snapshot(
    fetched: Dict[str, Any],
    out_dir: Path,
    *,
    write_parquet: bool = True,
) -> Dict[str, Path]:
    """Persist the raw records to ``data/a0/crl_transparency_raw_<tag>.{json,parquet}``.

    The JSON is the authoritative frozen snapshot (so the cohort is reproducible
    even after FDA updates the dump in place). Parquet is a convenience mirror;
    if pyarrow is unavailable it is skipped without failing."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    root = fetched["root"]
    results: List[Dict[str, Any]] = root.get("results") or []
    tag = _safe_export_tag(fetched["export_date"])

    written: Dict[str, Path] = {}
    json_path = out_dir / f"crl_transparency_raw_{tag}.json"
    json_path.write_text(
        json.dumps(
            {
                "_provenance": {
                    "export_date": fetched["export_date"],
                    "source_url": fetched["source_url"],
                    "n_records": len(results),
                },
                "meta": root.get("meta"),
                "results": results,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    written["json"] = json_path
    logger.info("wrote %d raw CRL records -> %s", len(results), json_path)

    if write_parquet:
        try:
            import pandas as pd  # noqa: WPS433

            # application_number is a list — store as JSON string so parquet is
            # rectangular and the cohort builder re-parses it identically.
            flat = []
            for r in results:
                row = dict(r)
                an = row.get("application_number")
                row["application_number"] = json.dumps(an) if an is not None else None
                ac = row.get("approver_center")
                row["approver_center"] = json.dumps(ac) if ac is not None else None
                flat.append(row)
            pq_path = out_dir / f"crl_transparency_raw_{tag}.parquet"
            pd.DataFrame(flat).to_parquet(pq_path, index=False)
            written["parquet"] = pq_path
            logger.info("wrote parquet mirror -> %s", pq_path)
        except Exception as exc:  # noqa: BLE001 — parquet is optional
            logger.warning("parquet mirror skipped (%s)", exc)

    return written


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=_REPO_ROOT / "data" / "a0")
    parser.add_argument(
        "--cached-json",
        type=Path,
        default=None,
        help="Path to an already-unzipped CRL JSON to reuse offline.",
    )
    parser.add_argument("--no-parquet", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    fetched = fetch_crl_dump(cached_json=args.cached_json)
    n = len(fetched["root"].get("results") or [])
    written = write_snapshot(fetched, args.out_dir, write_parquet=not args.no_parquet)
    print(
        json.dumps(
            {
                "export_date": fetched["export_date"],
                "source_url": fetched["source_url"],
                "n_records": n,
                "written": {k: str(v) for k, v in written.items()},
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
