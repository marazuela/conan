"""One-shot ingester: pull citation_url targets from fda_event_evidence into
the documents table.

The seed_documents_for_missing_assets script closed the gap for assets with a
trial registry footprint. But the agentic-review citations on
fda_regulatory_events still point at sponsor press releases, FDA gov pages,
and journal articles that aren't covered by any registry-API adapter. This
script attacks that residual: it walks fda_event_evidence rows, classifies
each citation_url by domain, fetches the page over HTTP, extracts title +
visible text, and writes a documents row + asset_documents primary link.

Domain whitelist (everything else is skipped):
  fda.gov                       → source=fda_advisory
  federalregister.gov           → source=federal_register
  clinicaltrials.gov            → source=clinicaltrials
  prnewswire / globenewswire /
    businesswire / stocktitan /
    ir.<sponsor>.com /          → source=press_release
    sponsor.com/(news|press-...)
  nejm.org / thelancet.com /
    jamanetwork.com /
    nature.com / cell.com /
    science.org / pubmed.*       → source=pubmed

Explicit skip list (these are "microstructure agent" citations, not primary
content): fintel.io, marketbeat.com, stockanalysis.com, benzinga.com,
nasdaq.com, dailypolitical.com, simplywall.st, finance.yahoo.com.

Fetch budget: per-URL 10s timeout, max 200KB body kept, default cap 100 URLs
per run. Failures are logged but don't abort the batch.

Idempotency:
  - documents UNIQUE(source, source_content_hash) — same body → same row.
  - asset_documents UNIQUE(asset_id, document_id, link_type) — re-runs no-op.

Run:
  SUPABASE_URL=… SUPABASE_SERVICE_ROLE_KEY=… \\
    python3 -m modal_workers.scripts.ingest_fda_event_citation_urls \\
        [--dry-run] [--limit N] [--insert-primary-links]
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from modal_workers.shared.document_writer import DocumentWriter
from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# domain classification
# ---------------------------------------------------------------------------

# Hard skip list — citations from these domains are by definition not primary
# content (they're financial / stock-analysis crawls). Logged but never fetched.
SKIP_DOMAINS = {
    "fintel.io", "marketbeat.com", "stockanalysis.com", "benzinga.com",
    "nasdaq.com", "www.nasdaq.com", "dailypolitical.com", "simplywall.st",
    "finance.yahoo.com", "cgtlive.com",
    # tradingview wraps third-party news with their own /news path — the
    # classifier matched the /news heuristic; opt out explicitly.
    "tradingview.com", "www.tradingview.com",
}

# Press-release distributors (sponsor news goes through these).
PRESS_DISTRIBUTORS = {
    "prnewswire.com", "www.prnewswire.com",
    "globenewswire.com", "www.globenewswire.com",
    "businesswire.com", "www.businesswire.com",
    "stocktitan.net", "www.stocktitan.net",
    "newswire.com", "www.newswire.com",
}

# Peer-reviewed journals — treat as `pubmed` (closest valid source CHECK
# value for narrative scientific content).
JOURNAL_DOMAINS = {
    "nejm.org", "www.nejm.org",
    "thelancet.com", "www.thelancet.com",
    "jamanetwork.com", "www.jamanetwork.com",
    "nature.com", "www.nature.com",
    "cell.com", "www.cell.com",
    "science.org", "www.science.org",
    "pubmed.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov",
    "biorxiv.org", "www.biorxiv.org",
    "medrxiv.org", "www.medrxiv.org",
}


def classify_url(url: str) -> Optional[Tuple[str, str]]:
    """Return (source, doc_type) tuple or None if we should skip the URL.

    Source must be one of the documents.source CHECK whitelist; doc_type is
    free-form text (the orchestrator and dashboard render it as-is).
    """
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None

    if host in SKIP_DOMAINS:
        return None

    # FDA gov pages — advisory, safety, etc.
    if host.endswith("fda.gov"):
        return ("fda_advisory", "fda_page")

    # Federal Register
    if host.endswith("federalregister.gov"):
        return ("federal_register", "fr_notice")

    # ClinicalTrials.gov (rarely cited directly but handle it).
    if host.endswith("clinicaltrials.gov"):
        return ("clinicaltrials", "clinical_trial")

    if host in PRESS_DISTRIBUTORS:
        return ("press_release", "press_release")

    if host in JOURNAL_DOMAINS:
        return ("pubmed", "journal_article")

    # SEC EDGAR — already covered by the edgar ingest adapter; skip to avoid
    # parsing the SEC index pages without form-type discovery.
    if host.endswith("sec.gov"):
        return None

    # Sponsor investor-relations / corporate news pages. Match the common
    # path patterns. We accept any host (ir.* or sponsor.com) as long as the
    # URL path indicates news / press / IR content.
    path = (parsed.path or "").lower()
    if host.startswith("ir."):
        return ("press_release", "press_release")
    if any(seg in path for seg in (
        "/news", "/press-release", "/press-releases", "/news-release",
        "/news-releases", "/media-centre", "/media-center", "/investor",
        "/investors",
    )):
        return ("press_release", "press_release")

    # Otherwise: not on the whitelist. Skip.
    return None


# ---------------------------------------------------------------------------
# fetch + extract
# ---------------------------------------------------------------------------

FETCH_TIMEOUT_S = 10.0
MAX_BODY_BYTES = 200 * 1024  # keep first 200 KB after HTML stripping
USER_AGENT = (
    "Conan/1.0 (FDA event citation ingester; "
    "https://github.com/marazuela/conan; respect robots.txt)"
)


@dataclass
class FetchResult:
    title: Optional[str]
    body_text: Optional[str]
    fetched_at: datetime
    error: Optional[str] = None


def fetch_and_extract(url: str, *, session: requests.Session) -> FetchResult:
    fetched_at = datetime.now(timezone.utc)
    try:
        r = session.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
            timeout=FETCH_TIMEOUT_S,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        return FetchResult(None, None, fetched_at, error=f"http_error: {exc}")

    if r.status_code >= 400:
        return FetchResult(None, None, fetched_at,
                           error=f"http_status_{r.status_code}")

    ctype = (r.headers.get("Content-Type") or "").lower()
    if "html" not in ctype and "xml" not in ctype:
        # Skip PDFs / binary — they'd need a separate parser path.
        return FetchResult(None, None, fetched_at,
                           error=f"unsupported_content_type: {ctype}")

    try:
        soup = BeautifulSoup(r.content[:MAX_BODY_BYTES * 4], "html.parser")
    except Exception as exc:  # noqa: BLE001
        return FetchResult(None, None, fetched_at, error=f"parse_error: {exc}")

    # Title: <title> > og:title > h1
    title: Optional[str] = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            title = og["content"].strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)

    # Body: strip script/style/nav, then get text.
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer",
                     "aside", "form", "iframe"]):
        tag.decompose()
    body_text = soup.get_text(" ", strip=True)
    body_text = re.sub(r"\s+", " ", body_text).strip()
    if len(body_text) > MAX_BODY_BYTES:
        body_text = body_text[:MAX_BODY_BYTES]

    if not body_text:
        return FetchResult(title, None, fetched_at, error="empty_body_after_strip")

    return FetchResult(title=title, body_text=body_text, fetched_at=fetched_at)


# ---------------------------------------------------------------------------
# evidence row enumeration
# ---------------------------------------------------------------------------

@dataclass
class CitationTarget:
    asset_id: str
    ticker: str
    drug_name: str
    citation_url: str
    evidence_id: str
    event_id: str
    event_date: Optional[str]


def find_citation_targets(client: SupabaseClient, limit: int) -> List[CitationTarget]:
    """Pull fda_event_evidence.citation_url targets for active assets, joining
    through fda_regulatory_events to fda_assets.

    We do this in three PostgREST calls and reassemble in Python — PostgREST
    nested-resource syntax would work, but the explicit form is easier to
    audit when something goes sideways.
    """
    # 1) active asset ids.
    assets = client._rest(
        "GET",
        "fda_assets",
        params={
            "is_active": "eq.true",
            "select": "id,ticker,drug_name",
            "limit": "500",
        },
    ) or []
    by_id: Dict[str, Dict[str, Any]] = {a["id"]: a for a in assets if a.get("id")}
    if not by_id:
        return []
    asset_ids = list(by_id.keys())

    # 2) fda_regulatory_events for those assets (in chunks of 100 ids).
    events: List[Dict[str, Any]] = []
    for i in range(0, len(asset_ids), 100):
        chunk = asset_ids[i:i+100]
        in_clause = ",".join(f'"{aid}"' for aid in chunk)
        rows = client._rest(
            "GET",
            "fda_regulatory_events",
            params={
                "asset_id": f"in.({in_clause})",
                "select": "id,asset_id,event_date",
            },
        ) or []
        events.extend(rows)
    if not events:
        return []
    event_by_id: Dict[str, Dict[str, Any]] = {e["id"]: e for e in events}

    # 3) evidence rows whose event_id is in the above set.
    evidence_rows: List[Dict[str, Any]] = []
    event_ids = list(event_by_id.keys())
    for i in range(0, len(event_ids), 100):
        chunk = event_ids[i:i+100]
        in_clause = ",".join(f'"{eid}"' for eid in chunk)
        rows = client._rest(
            "GET",
            "fda_event_evidence",
            params={
                "event_id": f"in.({in_clause})",
                "citation_url": "not.is.null",
                "evidence_status": "eq.active",
                "select": "id,event_id,citation_url",
                "limit": str(limit * 4),  # raw row cap, dedup happens below
            },
        ) or []
        evidence_rows.extend(rows)

    # Reassemble + dedup by citation_url (per-asset; same URL appearing on
    # multiple evidence rows for different assets keeps both entries).
    seen: Dict[Tuple[str, str], None] = {}
    out: List[CitationTarget] = []
    for ev in evidence_rows:
        url = ev.get("citation_url")
        event = event_by_id.get(ev["event_id"])
        if not url or not event:
            continue
        asset = by_id.get(event["asset_id"])
        if not asset:
            continue
        key = (asset["id"], url)
        if key in seen:
            continue
        seen[key] = None
        out.append(CitationTarget(
            asset_id=asset["id"],
            ticker=asset.get("ticker") or "",
            drug_name=asset.get("drug_name") or "",
            citation_url=url,
            evidence_id=ev["id"],
            event_id=ev["event_id"],
            event_date=event.get("event_date"),
        ))
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# document writes
# ---------------------------------------------------------------------------

def write_doc_from_citation(
    target: CitationTarget,
    classified: Tuple[str, str],
    fetched: FetchResult,
    writer: DocumentWriter,
) -> Optional[str]:
    """Write the documents row. Returns the document_id or None on error.

    source_doc_id = the URL itself (PostgREST sees this as the human-readable
    source identifier; the (source, source_content_hash) UNIQUE still gates
    duplication).
    """
    source, doc_type = classified
    if not fetched.body_text:
        return None
    published_at = (
        datetime.fromisoformat(target.event_date)
        if target.event_date else fetched.fetched_at
    )
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    try:
        result = writer.write_document(
            source=source,
            source_doc_id=target.citation_url[:255],
            doc_type=doc_type,
            raw_text=fetched.body_text,
            published_at=published_at,
            url=target.citation_url,
            title=(fetched.title or target.citation_url)[:500],
            language="en",
            extensions={
                "ingest_path": "citation_url_backfill",
                "from_evidence_id": target.evidence_id,
                "from_event_id": target.event_id,
            },
        )
        return result.document_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("write_document failed for %s: %s",
                       target.citation_url, exc)
        return None


def link_doc_to_asset(
    client: SupabaseClient,
    asset_id: str,
    document_id: str,
    *,
    dry_run: bool,
) -> str:
    if dry_run:
        return "dry_run"
    payload = {
        "asset_id": asset_id,
        "document_id": document_id,
        "link_type": "primary",
        "extraction_method": "manual",
        "extraction_confidence": 0.85,
        "is_material": True,
        "verified_by_pass2": False,
    }
    try:
        client._rest(
            "POST", "asset_documents",
            json_body=payload,
            prefer="return=minimal",
        )
        return "inserted"
    except SupabaseError as exc:
        if exc.status == 409 or "23505" in (exc.body or ""):
            return "dedup"
        raise


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

@dataclass
class Stats:
    targets_seen: int = 0
    skipped_domain: int = 0
    skipped_classify: int = 0
    fetched_ok: int = 0
    fetch_errors: int = 0
    docs_written: int = 0
    docs_dedup: int = 0
    links_inserted: int = 0
    links_dedup: int = 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="ingest_fda_event_citation_urls")
    p.add_argument("--limit", type=int, default=100,
                   help="Max citation_urls to fetch per run")
    p.add_argument("--dry-run", action="store_true",
                   help="Classify + enumerate, do not fetch or write")
    p.add_argument("--insert-primary-links", action="store_true", default=True,
                   help="After writing the document, insert an "
                        "asset_documents(link_type=primary, "
                        "extraction_method=manual) row to fire the orchestrator. "
                        "Defaults to True. Pass --no-insert-primary-links to "
                        "only seed documents without linking.")
    p.add_argument("--no-insert-primary-links", dest="insert_primary_links",
                   action="store_false")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    client = SupabaseClient()
    writer = DocumentWriter(client=client)
    session = requests.Session()
    stats = Stats()

    targets = find_citation_targets(client, limit=args.limit)
    stats.targets_seen = len(targets)
    logger.info("Found %d unique (asset, citation_url) targets", len(targets))

    t0 = time.time()
    for tgt in targets:
        classified = classify_url(tgt.citation_url)
        if classified is None:
            host = urlparse(tgt.citation_url).hostname or ""
            if host in SKIP_DOMAINS:
                stats.skipped_domain += 1
            else:
                stats.skipped_classify += 1
            logger.debug("skip %s %s host=%s",
                         tgt.ticker, tgt.citation_url[:60], host)
            continue

        if args.dry_run:
            logger.info("DRY %s %s → source=%s doc_type=%s",
                        tgt.ticker, tgt.citation_url[:70],
                        classified[0], classified[1])
            continue

        fetched = fetch_and_extract(tgt.citation_url, session=session)
        if fetched.error:
            stats.fetch_errors += 1
            logger.info("fetch_err %s %s: %s",
                        tgt.ticker, tgt.citation_url[:70], fetched.error)
            continue
        stats.fetched_ok += 1

        doc_id = write_doc_from_citation(tgt, classified, fetched, writer)
        if not doc_id:
            stats.fetch_errors += 1
            continue
        # write_document is idempotent — we can't tell new vs dedup without an
        # extra round trip; charge dedup conservatively (logged in writer).
        stats.docs_written += 1

        if args.insert_primary_links:
            r = link_doc_to_asset(client, tgt.asset_id, doc_id,
                                  dry_run=False)
            if r == "inserted":
                stats.links_inserted += 1
            elif r == "dedup":
                stats.links_dedup += 1
        logger.info(
            "ok %s source=%s title=%r url=%s",
            tgt.ticker, classified[0],
            (fetched.title or "")[:60], tgt.citation_url[:70],
        )

    logger.info(
        "citation_url ingest summary: targets=%d skipped_domain=%d "
        "skipped_classify=%d fetched_ok=%d fetch_errors=%d docs_written=%d "
        "links_inserted=%d links_dedup=%d elapsed_s=%.1f",
        stats.targets_seen, stats.skipped_domain, stats.skipped_classify,
        stats.fetched_ok, stats.fetch_errors, stats.docs_written,
        stats.links_inserted, stats.links_dedup, time.time() - t0,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
