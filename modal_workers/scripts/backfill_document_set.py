"""Backfill eval_harness.document_set via 4-adapter window sweep.

Phase 0 close-out — D4. Curated rows have document_set=[] because the
ingestion adapters hadn't been wired when the rows landed. This pass runs
each adapter against the asset's relevant window
[reference_assessment_date - 30d, reference_assessment_date] and collects
every touched document UUID (new inserts AND dedup hits) into the row's
document_set array.

Adapters used:
  - edgar.ingest_keyword_search(query=drug_brand, forms="8-K,10-Q,10-K,S-1,424B*")
  - federal_register.ingest_keyword_search(query=drug_brand)
  - openfda.ingest_drugsfda_approvals(application_search=appl_num)
  - clinicaltrials.ingest_search(query_term=drug_brand)

extracted_facts and asset_documents stay empty — Phase 1 extractor will fill
those. document_set IDs alone are sufficient for the Phase 0 replay loader
to materialize a per-case corpus.

Run:
  python3 -m modal_workers.scripts.backfill_document_set [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set

from modal_workers.ingestion import (
    clinicaltrials_ingest,
    edgar_ingest,
    federal_register_ingest,
    openfda_ingest,
)
from modal_workers.shared.document_writer import DocumentWriter, WriteResult
from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)


EDGAR_FORMS_FOR_BACKFILL = "8-K,10-Q,10-K,S-1,424B1,424B2,424B3,424B4,424B5,424B7,6-K"


class RecordingDocumentWriter(DocumentWriter):
    """DocumentWriter wrapper that records every doc id it touches.

    The adapters internally only surface `was_new=True` ids in IngestRunResult.
    For document_set backfill we need both new AND dedup-hit ids — they're all
    relevant documents in the asset's window."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.touched_ids: List[str] = []

    def write_document(self, *args: Any, **kwargs: Any) -> WriteResult:  # type: ignore[override]
        result = super().write_document(*args, **kwargs)
        if result.document_id:
            self.touched_ids.append(result.document_id)
        return result

    def reset(self) -> None:
        self.touched_ids = []


@dataclass
class RowStats:
    eval_id: str
    drug: Optional[str] = None
    ticker: Optional[str] = None
    cik: Optional[str] = None
    appl: Optional[str] = None
    window_start: Optional[date] = None
    window_end: Optional[date] = None
    edgar_ids: List[str] = field(default_factory=list)
    federal_register_ids: List[str] = field(default_factory=list)
    openfda_ids: List[str] = field(default_factory=list)
    clinicaltrials_ids: List[str] = field(default_factory=list)
    skipped_reason: Optional[str] = None
    error: Optional[str] = None


@dataclass
class Stats:
    rows_seen: int = 0
    rows_already_filled: int = 0
    rows_skipped_no_drug: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0
    errors: int = 0
    total_doc_ids_collected: int = 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def resolve_cik_for_asset(asset_id: str, client: SupabaseClient) -> Optional[str]:
    """Walk fda_assets -> entities -> entity_identifiers to find the CIK."""
    rows = client._rest(
        "GET", "fda_assets",
        params={"id": f"eq.{asset_id}", "select": "entity_id,extensions", "limit": "1"},
    ) or []
    if not rows:
        return None
    asset = rows[0]
    # Some 8K-derived rows store the cik in extensions.
    ext = asset.get("extensions") or {}
    if ext.get("source") == "edgar_8k_crl":
        # The cik isn't on fda_assets but lives on the eval_harness row's
        # realized_outcome_data.edgar_cik. Caller passes it via the eval row instead.
        pass
    entity_id = asset.get("entity_id")
    if not entity_id:
        return None
    ident = client._rest(
        "GET", "entity_identifiers",
        params={
            "entity_id": f"eq.{entity_id}",
            "id_type": "eq.cik",
            "select": "id_value",
            "limit": "1",
        },
    ) or []
    if ident:
        return (ident[0].get("id_value") or "").lstrip("0") or "0"
    return None


def collect_existing_ids_in_window(
    *,
    source: str,
    window_start: date,
    window_end: date,
    extensions_filter: Optional[Dict[str, str]] = None,
    title_ilike: Optional[str] = None,
    client: SupabaseClient,
) -> List[str]:
    """Pull doc ids from the documents table within [window_start, window_end]
    that match an extensions or title filter. Used to recover dedup-hit ids
    that aren't surfaced by the adapter's IngestRunResult."""
    params: Dict[str, str] = {
        "select": "id",
        "source": f"eq.{source}",
        "published_at": f"gte.{window_start.isoformat()}",
        "and": f"(published_at.lte.{(window_end + timedelta(days=1)).isoformat()})",
        "limit": "200",
    }
    if title_ilike:
        params["title"] = f"ilike.*{title_ilike}*"
    if extensions_filter:
        for k, v in extensions_filter.items():
            params[f"extensions->>{k}"] = f"eq.{v}"
    try:
        rows = client._rest("GET", "documents", params=params) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("documents lookup failed for source=%s: %s", source, exc)
        return []
    return [r["id"] for r in rows if r.get("id")]


# ---------------------------------------------------------------------------
# per-row backfill
# ---------------------------------------------------------------------------

def backfill_row(
    eval_row: Dict[str, Any],
    *,
    writer: RecordingDocumentWriter,
    client: SupabaseClient,
    sec_user_agent: str,
) -> RowStats:
    eval_id = eval_row["id"]
    asset = eval_row.get("fda_assets") or {}
    asset_id = asset.get("id") or eval_row.get("asset_id")
    drug = asset.get("drug_name")
    ticker = asset.get("ticker")
    appl = (asset.get("application_number") or "").strip()

    outcome_data = eval_row.get("realized_outcome_data") or {}
    # Prefer the Phase 0 holdout shape's explicit approval_or_crl_date; fall
    # back to the eval_harness.reference_assessment_date column which is the
    # canonical "what date is this event" anchor for D-116-labelled phase4b
    # rows (whose realized_outcome_data carries anchor_date, not
    # approval_or_crl_date).
    resolution_iso = (
        outcome_data.get("approval_or_crl_date")
        or eval_row.get("reference_assessment_date")
    )
    if not resolution_iso:
        return RowStats(eval_id=eval_id, drug=drug, ticker=ticker,
                        skipped_reason="no resolution date")
    try:
        resolution_d = datetime.strptime(resolution_iso, "%Y-%m-%d").date()
    except ValueError:
        return RowStats(eval_id=eval_id, drug=drug, ticker=ticker,
                        skipped_reason="unparseable resolution date")

    # Reference window: 30 days before resolution, ending at resolution date.
    window_start = resolution_d - timedelta(days=30)
    window_end = resolution_d

    if not drug or len(drug) < 3:
        return RowStats(eval_id=eval_id, ticker=ticker, appl=appl,
                        window_start=window_start, window_end=window_end,
                        skipped_reason="drug_name missing or too short")

    cik: Optional[str] = outcome_data.get("edgar_cik")
    if not cik and asset_id:
        cik = resolve_cik_for_asset(asset_id, client)

    out = RowStats(eval_id=eval_id, drug=drug, ticker=ticker, cik=cik,
                   appl=appl, window_start=window_start, window_end=window_end)

    # ----- EDGAR -----
    writer.reset()
    try:
        edgar_ingest.ingest_keyword_search(
            f'"{drug}"', since=window_start, until=window_end,
            forms=EDGAR_FORMS_FOR_BACKFILL, user_agent=sec_user_agent,
            size=50, writer=writer,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("edgar ingest failed for %s: %s", drug, exc)
    out.edgar_ids = list(writer.touched_ids)
    # Recover prior-run dedup hits via documents table by ticker/cik
    if cik:
        out.edgar_ids = list(set(out.edgar_ids) | set(collect_existing_ids_in_window(
            source="edgar", window_start=window_start, window_end=window_end,
            title_ilike=drug, client=client,
        )))

    # ----- Federal Register -----
    writer.reset()
    try:
        federal_register_ingest.ingest_keyword_search(
            drug, since=window_start, until=window_end, max_pages=2,
            per_page=50, writer=writer,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("federal_register ingest failed for %s: %s", drug, exc)
    out.federal_register_ids = list(writer.touched_ids)
    out.federal_register_ids = list(
        set(out.federal_register_ids) | set(collect_existing_ids_in_window(
            source="federal_register", window_start=window_start, window_end=window_end,
            title_ilike=drug, client=client,
        ))
    )

    # ----- openFDA drugsfda by application_number (digits only) -----
    if appl and appl.isdigit():
        writer.reset()
        try:
            openfda_ingest.ingest_drugsfda_approvals(
                application_search=appl, since=window_start - timedelta(days=365),
                until=window_end, page_limit=50, max_pages=2, writer=writer,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("openfda ingest failed for appl %s: %s", appl, exc)
        out.openfda_ids = list(writer.touched_ids)
        out.openfda_ids = list(
            set(out.openfda_ids) | set(collect_existing_ids_in_window(
                source="openfda", window_start=window_start - timedelta(days=365),
                window_end=window_end, extensions_filter={"application_number": appl},
                client=client,
            ))
        )

    # ----- ClinicalTrials.gov -----
    writer.reset()
    try:
        clinicaltrials_ingest.ingest_search(
            drug, page_size=20, max_pages=2, writer=writer,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("clinicaltrials ingest failed for %s: %s", drug, exc)
    out.clinicaltrials_ids = list(writer.touched_ids)
    # ClinicalTrials docs aren't time-bounded — accept any matching trial as
    # context for the asset (replay reads them as background context).
    return out


def merge_document_set(existing: List[str], new: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for d in (existing or []) + (new or []):
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="backfill_document_set")
    p.add_argument("--limit", type=int, default=200,
                   help="Max eval_harness rows to backfill in one run")
    p.add_argument("--dry-run", action="store_true",
                   help="Run adapters and collect ids but don't PATCH")
    p.add_argument("--skip-already-filled", action="store_true", default=True,
                   help="Skip rows where document_set is non-empty")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    client = SupabaseClient()
    writer = RecordingDocumentWriter()
    stats = Stats()

    sec_user_agent = os.environ.get(
        "SEC_USER_AGENT",
        "Conan/1.0 (FDA orchestrator; https://github.com/marazuela/conan)",
    )

    rows = client._rest(
        "GET", "eval_harness",
        params={
            "select": "id,asset_id,reference_assessment_date,document_set,"
                      "realized_outcome,realized_outcome_data,"
                      "fda_assets(id,ticker,drug_name,application_number,extensions)",
            "limit": str(args.limit),
        },
    ) or []
    stats.rows_seen = len(rows)
    logger.info("Inspecting %d eval_harness rows", len(rows))

    for r in rows:
        existing_set = r.get("document_set") or []
        if args.skip_already_filled and existing_set:
            stats.rows_already_filled += 1
            continue

        per_row = backfill_row(r, writer=writer, client=client,
                               sec_user_agent=sec_user_agent)
        if per_row.skipped_reason:
            logger.info("skip %s: %s", per_row.eval_id[:8], per_row.skipped_reason)
            stats.rows_skipped_no_drug += 1
            continue

        merged_ids = merge_document_set(
            existing_set,
            per_row.edgar_ids + per_row.federal_register_ids
            + per_row.openfda_ids + per_row.clinicaltrials_ids,
        )
        stats.total_doc_ids_collected += len(merged_ids) - len(existing_set or [])

        logger.info(
            "%s drug=%s ticker=%s window=%s..%s edgar=%d fr=%d openfda=%d "
            "ct=%d total=%d",
            per_row.eval_id[:8], per_row.drug, per_row.ticker,
            per_row.window_start, per_row.window_end,
            len(per_row.edgar_ids), len(per_row.federal_register_ids),
            len(per_row.openfda_ids), len(per_row.clinicaltrials_ids),
            len(merged_ids),
        )

        if args.dry_run:
            continue

        if not merged_ids:
            stats.rows_unchanged += 1
            continue

        try:
            client._rest(
                "PATCH", "eval_harness",
                params={"id": f"eq.{per_row.eval_id}"},
                json_body={"document_set": merged_ids},
                prefer="return=minimal",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("PATCH eval_harness failed for %s: %s",
                           per_row.eval_id, exc)
            stats.errors += 1
            continue
        stats.rows_updated += 1

    logger.info(
        "document_set backfill summary: rows=%d already_filled=%d "
        "skipped_no_drug=%d updated=%d unchanged=%d errors=%d "
        "total_ids_added=%d",
        stats.rows_seen, stats.rows_already_filled, stats.rows_skipped_no_drug,
        stats.rows_updated, stats.rows_unchanged, stats.errors,
        stats.total_doc_ids_collected,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
