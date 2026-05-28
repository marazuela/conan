"""FDA Complete Response Letter (CRL) transparency-ledger fetcher.

Two-stage source pull:

  1. Manifest (cheap, ~1 MB). openFDA publishes its CRL transparency dataset
     index at `api.fda.gov/download.json`
        -> results.transparency.crl.partitions[0].file
     That partition is a ZIPped JSON document with `meta` + `results[]`.
     Each result has the well-structured fields documented at
     `/apis/transparency/crl/searchable-fields/`:
        letter_date, letter_year, letter_type, approval_status,
        application_number[], file_name, company_name,
        approver_*, company_rep, company_address, text.

  2. PDFs (heavy, ~218 MB total). The actual letter PDFs are published as two
     ZIPs on `download.open.fda.gov`:
        approved_CRLs.zip   — letters whose application was later approved
        unapproved_CRLs.zip — letters whose application remained pending/
                              withdrawn at export time.
     Each ZIP holds one PDF per CRL. The manifest's `approval_status`
     field tells us which ZIP a given CRL is in; the manifest's `file_name`
     tells us the exact PDF entry inside that ZIP.

Why both stages
---------------
openFDA's `/drug/drugsfda` endpoint does NOT expose CRL as a submission
status (verified 2026-05-27: `submissions.submission_status:RL` returns
HTTP 404; documented codes are only AP, TA, plus pre-1998 historical).
Sponsors who receive a CRL typically re-file as a new submission, and the
original rejection vanishes from drugsfda. The transparency ledger is
therefore the only authoritative FDA-published source of CRL events.

The JSON manifest is structurally clean but has gaps the PDFs fill:
  - drug name (brand + generic): only present in the PDF body — JSON has
    `text` = the PDF's extracted prose, but no structured `drug_name` field.
  - indication: same situation. The PDF body usually contains a "for the
    treatment of …" phrase that the manifest does not extract.

pdfplumber on each PDF extracts what the JSON cannot, while the JSON
manifest supplies the well-structured fields (catalyst date, sponsor,
application number, NDA/BLA type, letter type) reliably. Older filings
(e.g. `761355_2024_Orig1s000OtherActionLtrs.pdf`) do NOT encode the CRL
date in the filename, so going PDF-only would silently lose dates for
roughly half the corpus.

Pairs with `scripts/curate_crl_from_edgar.py`, which mines CRL disclosures
from EDGAR 8-Ks. That source catches sponsor-disclosed events quickly but
misses CRLs the sponsor downplays. The transparency table is ground truth.
When both yield a row for the same application_number whose dates disagree
by more than 30 days, an operator_flag is opened and the transparency-table
date is preferred.

What this DOES
--------------
1. Fetch the openFDA download index (auth'd via shared openfda_client) and
   resolve the current partition URL + export_date.
2. Download the partition ZIP, parse the JSON manifest, filter to
   `letter_type == 'COMPLETE RESPONSE'`. Other letter types in the feed
   (REFUSAL TO FILE, TENTATIVE APPROVAL, PROVISIONAL DETERMINATION,
   RESCIND COMPLETE RESPONSE) are intentionally NOT emitted: they aren't
   the rejection set the eval harness needs, and conflating them would
   corrupt the materiality adjudicator's negative class.
3. Download both CRL PDF ZIPs (approved + unapproved). They are cached in
   memory for the run.
4. For each filtered manifest record:
     a. Locate the PDF entry by `file_name` in the correct ZIP (selected
        by `approval_status`).
     b. Parse the PDF body with pdfplumber (first ~3 pages — page 1 is
        usually a cover sheet, body text starts page 2).
     c. Regex-extract drug name + indication from body text.
     d. Resolve application_number to fda_assets via direct match on
        fda_assets.application_number, then fallback on
        fda_asset_resolution_aliases (alias_type='application_number').
        Ticker is left NULL when both miss — catalyst_universe accepts that.
     e. Upsert one catalyst_universe row per CRL with
          profile='binary_catalyst'
          catalyst_type='fda_crl'
          material_outcome='negative'      # admitted post migration
                                           # 20260613009000
        raw_payload carries the structured manifest fields plus the
        PDF-extracted drug name + indication.
5. Cross-validate against EDGAR-derived CRL rows in eval_harness
   (realized_outcome='crl', realized_outcome_data.source='edgar_8k'). When
   the same application has an EDGAR row whose date disagrees with the
   transparency-table date by more than 30 days, open an operator_flag
   (source='manual', kind='fda_crl_date_disagreement', severity='warn').

What this DOES NOT
------------------
- Create fda_assets rows for unmatched application_numbers. Silent asset
  creation pollutes the v3 watchlist (same reasoning as edgar_8k_pdufa,
  fed_register_adcom).
- Cover pre-2007 CRLs. The transparency dataset begins ~2007; older CRLs
  aren't in this source and are accepted as a gap.

Run locally:
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... [OPENFDA_API_KEY=...] \\
    python3 -m modal_workers.fetchers.universe.fda_crl_transparency --apply
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError  # noqa: E402
from modal_workers.shared.emissions_ledger import upsert_catalyst_universe_row  # noqa: E402
from modal_workers.shared.openfda_client import (  # noqa: E402
    openfda_auth_params,
    openfda_url,
)

logger = logging.getLogger("fda_crl_transparency")

SOURCE_FEED = "openfda_transparency_crl"
EDGAR_CRL_SOURCE_TAG = "edgar_8k"  # set by curate_crl_from_edgar in realized_outcome_data.source
DATE_DISAGREEMENT_THRESHOLD_DAYS = 30

APPROVED_PDF_ZIP_URL = "https://download.open.fda.gov/approved_CRLs.zip"
UNAPPROVED_PDF_ZIP_URL = "https://download.open.fda.gov/unapproved_CRLs.zip"
PDF_FETCH_TIMEOUT_S = 300

EMITTED_LETTER_TYPES = frozenset({"COMPLETE RESPONSE"})

# Application-number regex on PDF body / manifest strings. openFDA records
# write "BLA 761365" or "NDA 212479"; older PDFs sometimes use "BLA761365"
# without a space.
_APP_NUM_RE = re.compile(r"\b(NDA|BLA|ANDA)\s*(\d{6})\b", re.IGNORECASE)
_APP_NUM_DIGITS = re.compile(r"\b(\d{6})\b")

# Body-text regexes used by `_extract_drug_and_indication`. Anchored on FDA
# CRL boilerplate so we don't snag agency section headers ("Drugs and
# Biologics", "Center for Drug Evaluation and Research") or trial references
# ("Study 206"). Both are best-effort; ~40-60% recall in practice. False
# positives are filtered downstream by `_looks_like_drug_name`.
_DRUG_AFTER_FOR_RE = re.compile(
    r"(?:biologics\s+license\s+application|new\s+drug\s+application|"
    r"section\s+351\(a\)\s+of\s+the\s+Public\s+Health\s+Service\s+Act|"
    r"section\s+505\(b\)(?:\(\d\))?\s+of\s+the\s+Federal\s+Food)"
    r"[^.]{0,200}?"
    r"\bfor\s+(?P<drug>[A-Za-z0-9][A-Za-z0-9\-' ]{2,60}?)"
    r"(?=[.,;]|\s+dated|\s+Injection|\s+Oral|\s+Tablet|\s+Capsule|\s+is\b|\s+Solution|\s+in\b)",
    re.IGNORECASE | re.DOTALL,
)
_INDICATION_RE = re.compile(
    r"(?:for\s+the\s+)?treatment\s+of\s+(?P<indication>[a-z][a-zA-Z\- ,]{5,80}?)"
    r"(?=[.,;]|\s+in\s+adult|\s+in\s+patient|\s+with\s+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def fetch(
    client: SupabaseClient,
    *,
    start_date: Optional[date] = None,  # noqa: ARG001 — accepted for parity with _run_fetcher
    end_date: Optional[date] = None,    # noqa: ARG001
    dry_run: bool = False,
    pdf_sample_limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Pull the openFDA transparency CRL manifest, download both PDF ZIPs,
    parse each CRL PDF with pdfplumber, and upsert one catalyst_universe row
    per CRL.

    start_date / end_date are accepted for compatibility with `_run_fetcher`
    in modal_workers/app.py but are not used: the transparency feed is a
    monolithic snapshot and the upsert helper's dedup key handles
    re-firing without writes.

    pdf_sample_limit caps the number of PDFs actually parsed — useful in
    dry-run for smoke testing without spending 3-4 minutes on the full
    ~430-PDF sweep. None means "parse everything".
    """
    session = _session()

    try:
        partition = _resolve_partition_url(session)
    except (requests.RequestException, KeyError) as exc:
        return _empty_result({
            "stage": "index",
            "error": f"{type(exc).__name__}: {exc}"[:400],
        })
    if partition is None:
        return _empty_result({
            "stage": "index",
            "error": "transparency.crl partition missing from download.json",
        })

    try:
        manifest = _download_manifest_json(session, partition["file"])
    except (requests.RequestException, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        return _empty_result({
            "stage": "manifest_download",
            "error": f"{type(exc).__name__}: {exc}"[:400],
        })

    # Filter manifest BEFORE downloading PDFs so we know which ZIPs to fetch
    # (and to avoid loading the larger ZIP when no rows reference it).
    eligible: List[Tuple[Dict[str, Any], date, str, Optional[str]]] = []
    skipped_other_letter_type = 0
    skipped_no_date = 0
    skipped_no_application = 0
    needs_approved = False
    needs_unapproved = False

    for record in manifest:
        letter_type = (record.get("letter_type") or "").strip().upper()
        if letter_type not in EMITTED_LETTER_TYPES:
            skipped_other_letter_type += 1
            continue

        catalyst_d = _parse_letter_date(record.get("letter_date"))
        if catalyst_d is None:
            skipped_no_date += 1
            continue

        app_number, app_type = _extract_application_number(record)
        if app_number is None:
            skipped_no_application += 1
            continue

        approval_status = (record.get("approval_status") or "").strip().lower()
        if approval_status == "approved":
            needs_approved = True
        elif approval_status == "unapproved":
            needs_unapproved = True

        eligible.append((record, catalyst_d, app_number, app_type))

    # Apply optional sample cap (for dry-run smoke).
    if pdf_sample_limit is not None and len(eligible) > pdf_sample_limit:
        eligible = eligible[:pdf_sample_limit]

    pdf_zips: Dict[str, zipfile.ZipFile] = {}
    errors: List[Dict[str, Any]] = []
    if needs_approved or eligible:  # default-fetch when partition lists rows
        try:
            pdf_zips["approved"] = _download_pdf_zip(session, APPROVED_PDF_ZIP_URL)
        except (requests.RequestException, zipfile.BadZipFile) as exc:
            errors.append({"stage": "approved_zip_download",
                           "error": f"{type(exc).__name__}: {exc}"[:400]})
    if needs_unapproved:
        try:
            pdf_zips["unapproved"] = _download_pdf_zip(session, UNAPPROVED_PDF_ZIP_URL)
        except (requests.RequestException, zipfile.BadZipFile) as exc:
            errors.append({"stage": "unapproved_zip_download",
                           "error": f"{type(exc).__name__}: {exc}"[:400]})

    fetched = 0
    upserted = 0
    duplicate_within_run = 0
    pdf_parse_errors = 0
    pdf_missing = 0
    asset_resolved = 0
    drug_extracted = 0
    indication_extracted = 0
    disagreements_flagged = 0
    seen_within_run: set[Tuple[str, str]] = set()
    edgar_index = _build_edgar_crl_index(client) if not dry_run else {}

    for record, catalyst_d, app_number, app_type in eligible:
        fetched += 1
        dedup_key = (app_number, catalyst_d.isoformat())
        if dedup_key in seen_within_run:
            duplicate_within_run += 1
            continue
        seen_within_run.add(dedup_key)

        sponsor_name = record.get("company_name") or None
        file_name = record.get("file_name") or ""
        approval_status = (record.get("approval_status") or "").strip().lower()
        zip_bucket = "approved" if approval_status == "approved" else "unapproved"

        drug_name: Optional[str] = None
        indication: Optional[str] = None
        zip_handle = pdf_zips.get(zip_bucket)
        if zip_handle is None:
            pdf_missing += 1
        else:
            try:
                drug_name, indication = _parse_pdf_body(zip_handle, file_name)
            except (KeyError, zipfile.BadZipFile) as exc:
                pdf_missing += 1
                logger.debug("PDF %s not found in %s: %s", file_name, zip_bucket, exc)
            except Exception as exc:  # noqa: BLE001 — pdfplumber raises a wide variety
                pdf_parse_errors += 1
                logger.debug("pdfplumber failed on %s: %s", file_name, exc)

        if drug_name:
            drug_extracted += 1
        if indication:
            indication_extracted += 1

        ticker: Optional[str] = None
        entity_id: Optional[str] = None
        if not dry_run:
            asset = _resolve_asset_by_application_number(client, app_number)
            if asset:
                ticker = asset.get("ticker")
                entity_id = asset.get("entity_id")
                asset_resolved += 1

        raw_payload = {
            "application_number": app_number,
            "application_type": app_type,
            "file_name": file_name,
            "letter_type": "COMPLETE RESPONSE",
            "letter_year": record.get("letter_year"),
            "approval_status": record.get("approval_status"),
            "approver_name": record.get("approver_name"),
            "approver_title": record.get("approver_title"),
            "approver_center": record.get("approver_center"),
            "company_name": sponsor_name,
            "company_rep": record.get("company_rep"),
            "drug_name": drug_name,
            "indication": indication,
            "pdf_bucket": zip_bucket,
            "partition_export_date": partition.get("export_date"),
        }

        if dry_run:
            upserted += 1
            logger.info(
                "[dry-run] CRL %s app=%s sponsor=%r drug=%r indication=%r",
                catalyst_d.isoformat(), app_number, sponsor_name, drug_name, indication,
            )
            continue

        try:
            upsert_catalyst_universe_row(
                client,
                profile="binary_catalyst",
                catalyst_type="fda_crl",
                catalyst_date=catalyst_d,
                source_feed=SOURCE_FEED,
                ticker=ticker,
                entity_id=entity_id,
                material_outcome="negative",
                source_url=_drugsfda_application_url(app_number),
                raw_payload=raw_payload,
            )
            upserted += 1
        except (SupabaseError, ValueError) as exc:
            errors.append({
                "application_number": app_number,
                "catalyst_date": catalyst_d.isoformat(),
                "error": str(exc)[:400],
            })
            continue

        for edgar_d, harness_id in edgar_index.get(app_number, []):
            delta = abs((catalyst_d - edgar_d).days)
            if delta > DATE_DISAGREEMENT_THRESHOLD_DAYS:
                try:
                    _open_date_disagreement_flag(
                        client,
                        application_number=app_number,
                        transparency_date=catalyst_d,
                        edgar_date=edgar_d,
                        delta_days=delta,
                        eval_harness_id=harness_id,
                        sponsor_name=sponsor_name,
                    )
                    disagreements_flagged += 1
                except SupabaseError as exc:
                    errors.append({
                        "application_number": app_number,
                        "stage": "operator_flag",
                        "error": str(exc)[:400],
                    })

    return {
        "fetched": fetched,
        "upserted": upserted,
        "skipped_other_letter_type": skipped_other_letter_type,
        "skipped_no_date": skipped_no_date,
        "skipped_no_application": skipped_no_application,
        "duplicate_within_run": duplicate_within_run,
        "pdf_parse_errors": pdf_parse_errors,
        "pdf_missing": pdf_missing,
        "drug_extracted": drug_extracted,
        "indication_extracted": indication_extracted,
        "asset_resolved": asset_resolved,
        "disagreements_flagged": disagreements_flagged,
        "errors": errors,
        "partition": {
            "file": partition.get("file"),
            "export_date": partition.get("export_date"),
            "records_in_partition": partition.get("records"),
            "approved_zip_loaded": "approved" in pdf_zips,
            "unapproved_zip_loaded": "unapproved" in pdf_zips,
        },
    }


# ---------------------------------------------------------------------------
# Index + downloads
# ---------------------------------------------------------------------------

def _resolve_partition_url(session: requests.Session) -> Optional[Dict[str, Any]]:
    """Walk download.json -> results.transparency.crl.partitions[0]."""
    r = session.get(
        openfda_url("download.json"),
        params=openfda_auth_params(),
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    crl_block = (((body.get("results") or {}).get("transparency") or {}).get("crl")) or {}
    partitions = crl_block.get("partitions") or []
    if not partitions:
        return None
    partition = dict(partitions[0])
    partition["export_date"] = crl_block.get("export_date")
    return partition


def _download_manifest_json(session: requests.Session, url: str) -> List[Dict[str, Any]]:
    r = session.get(url, timeout=60)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        names = z.namelist()
        if not names:
            return []
        with z.open(names[0]) as fh:
            body = json.load(fh)
    return body.get("results") or []


def _download_pdf_zip(session: requests.Session, url: str) -> zipfile.ZipFile:
    """Download a CRL PDF ZIP and return it as an in-memory ZipFile.

    Total payload at present scale: ~172 MB (approved) + ~46 MB (unapproved).
    Held in memory for the duration of the run; released when the Modal
    container exits.
    """
    r = session.get(url, timeout=PDF_FETCH_TIMEOUT_S, stream=True)
    r.raise_for_status()
    buf = io.BytesIO()
    for chunk in r.iter_content(chunk_size=1024 * 1024):
        if chunk:
            buf.write(chunk)
    buf.seek(0)
    return zipfile.ZipFile(buf)


# ---------------------------------------------------------------------------
# PDF parsing
# ---------------------------------------------------------------------------

def _parse_pdf_body(
    zip_handle: zipfile.ZipFile, file_name: str
) -> Tuple[Optional[str], Optional[str]]:
    """Open `file_name` inside `zip_handle`, parse first 3 pages via
    pdfplumber, and regex-extract (drug_name, indication). Either may be None.

    pdfplumber is imported lazily so module import doesn't depend on it; that
    keeps the CLI runnable when pdfplumber is not installed for ad-hoc
    inspection.
    """
    import pdfplumber  # noqa: WPS433 — lazy import keeps module import cheap.

    raw_pdf = zip_handle.read(file_name)
    with pdfplumber.open(io.BytesIO(raw_pdf)) as pdf:
        chunks: List[str] = []
        for page in pdf.pages[:3]:
            chunks.append(page.extract_text() or "")
        text = "\n".join(chunks)

    return _extract_drug_and_indication(text)


def _extract_drug_and_indication(text: str) -> Tuple[Optional[str], Optional[str]]:
    drug: Optional[str] = None
    indication: Optional[str] = None
    if not text:
        return drug, indication

    for m in _DRUG_AFTER_FOR_RE.finditer(text):
        candidate = m.group("drug").strip(" ,.")
        # Strip trailing connective words ("Bortezomib for", "X and").
        candidate = _TRAILING_NOISE_RE.sub("", candidate).strip()
        if _looks_like_drug_name(candidate):
            drug = candidate
            break

    m = _INDICATION_RE.search(text)
    if m:
        ind_raw = m.group("indication").strip(" ,.").lower()
        if 5 <= len(ind_raw) <= 80:
            indication = ind_raw

    return drug, indication


_DRUG_NAME_NOISE = {
    "the", "this", "that", "your", "these", "their", "you", "us",
    "review", "approval", "complete", "response", "letter", "letters",
    "submission", "application", "amendment", "amendments",
    "section", "subsection", "indication", "indications", "treatment",
    "drugs", "drug", "biologics", "biologic",   # FDA section header noise
    "study", "studies", "trial", "trials",      # trial-reference noise
    "use", "approval", "approved", "evaluation",
    "injection", "tablets", "capsules", "solution",  # dosage forms alone
}


_TRAILING_NOISE_RE = re.compile(r"\s+(for|and|or|in|of|with|to|as)\s*$", re.IGNORECASE)


def _looks_like_drug_name(candidate: str) -> bool:
    if not candidate or len(candidate) < 3 or len(candidate) > 60:
        return False
    first_word = candidate.split()[0].lower()
    if first_word in _DRUG_NAME_NOISE:
        return False
    # Reject pure dates and pure numbers.
    if re.fullmatch(r"[0-9 ,.\-]+", candidate):
        return False
    # Reject candidates carrying FDA redaction markers — they were truncated
    # to a partial phrase by the regex.
    if "(b)" in candidate or "(4)" in candidate or "(5)" in candidate:
        return False
    return True


# ---------------------------------------------------------------------------
# Field extraction (manifest)
# ---------------------------------------------------------------------------

def _parse_letter_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _extract_application_number(record: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Return (digits, type) where type is 'NDA' / 'BLA' / 'ANDA' or None."""
    candidates: List[str] = []
    raw_app = record.get("application_number")
    if isinstance(raw_app, list):
        candidates.extend(s for s in raw_app if isinstance(s, str))
    elif isinstance(raw_app, str):
        candidates.append(raw_app)

    for cand in candidates:
        m = _APP_NUM_RE.search(cand)
        if m:
            return m.group(2), m.group(1).upper()
        m2 = _APP_NUM_DIGITS.search(cand)
        if m2:
            return m2.group(1), None

    file_name = record.get("file_name") or ""
    m3 = re.search(r"\b(NDA|BLA|ANDA)\s*(\d{6})\b", file_name, re.IGNORECASE)
    if m3:
        return m3.group(2), m3.group(1).upper()
    m4 = _APP_NUM_DIGITS.search(file_name)
    if m4:
        return m4.group(1), None

    return None, None


def _drugsfda_application_url(application_number: Optional[str]) -> Optional[str]:
    if not application_number:
        return None
    return (
        f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm"
        f"?event=overview.process&ApplNo={application_number}"
    )


# ---------------------------------------------------------------------------
# Asset resolution
# ---------------------------------------------------------------------------

_ASSET_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}


def _resolve_asset_by_application_number(
    client: SupabaseClient, application_number: str
) -> Optional[Dict[str, Any]]:
    if application_number in _ASSET_CACHE:
        return _ASSET_CACHE[application_number]

    try:
        rows = client._rest(
            "GET", "fda_assets",
            params={
                "application_number": f"eq.{application_number}",
                "select": "id,ticker,entity_id",
                "limit": "1",
            },
        ) or []
    except SupabaseError:
        rows = []
    if rows:
        _ASSET_CACHE[application_number] = rows[0]
        return rows[0]

    try:
        aliases = client._rest(
            "GET", "fda_asset_resolution_aliases",
            params={
                "alias_type": "eq.application_number",
                "alias_value": f"eq.{application_number}",
                "asset_id": "not.is.null",
                "select": "asset_id",
                "order": "confidence.desc,created_at.desc",
                "limit": "1",
            },
        ) or []
    except SupabaseError:
        aliases = []
    if aliases:
        asset_id = aliases[0]["asset_id"]
        try:
            rows = client._rest(
                "GET", "fda_assets",
                params={"id": f"eq.{asset_id}", "select": "id,ticker,entity_id", "limit": "1"},
            ) or []
        except SupabaseError:
            rows = []
        if rows:
            _ASSET_CACHE[application_number] = rows[0]
            return rows[0]

    _ASSET_CACHE[application_number] = None
    return None


# ---------------------------------------------------------------------------
# Cross-source disagreement detection
# ---------------------------------------------------------------------------

def _build_edgar_crl_index(client: SupabaseClient) -> Dict[str, List[Tuple[date, str]]]:
    """Index every EDGAR-derived CRL row in eval_harness by application_number."""
    try:
        rows = client._rest(
            "GET", "eval_harness",
            params={
                "select": "id,realized_outcome_data",
                "realized_outcome": "eq.crl",
                "realized_outcome_data->>source": f"eq.{EDGAR_CRL_SOURCE_TAG}",
                "limit": "5000",
            },
        ) or []
    except SupabaseError:
        return {}

    index: Dict[str, List[Tuple[date, str]]] = {}
    for r in rows:
        data = r.get("realized_outcome_data") or {}
        app = data.get("application_number_extracted")
        if not app:
            continue
        raw_d = data.get("approval_or_crl_date")
        if not raw_d:
            continue
        try:
            crl_d = datetime.strptime(raw_d, "%Y-%m-%d").date()
        except ValueError:
            continue
        index.setdefault(str(app), []).append((crl_d, r["id"]))
    return index


def _open_date_disagreement_flag(
    client: SupabaseClient,
    *,
    application_number: str,
    transparency_date: date,
    edgar_date: date,
    delta_days: int,
    eval_harness_id: str,
    sponsor_name: Optional[str],
) -> None:
    """Open (or refresh) an operator_flag for a >30d CRL date disagreement."""
    title = (
        f"FDA CRL date disagreement on application {application_number}: "
        f"transparency={transparency_date.isoformat()} "
        f"edgar={edgar_date.isoformat()} (Δ{delta_days}d)"
    )
    body = (
        "openFDA transparency ledger and EDGAR 8-K mining produced CRL rows "
        f"for application_number={application_number} whose letter dates "
        f"differ by {delta_days} days (threshold "
        f"{DATE_DISAGREEMENT_THRESHOLD_DAYS}d). Prefer the transparency-table "
        "date when reconciling — it is the FDA's own ledger. The EDGAR row "
        "likely captured an 8-K filing date later than the actual CRL receipt, "
        "or extracted the wrong application_number."
    )
    flag = {
        "severity": "warn",
        "source": "manual",
        "kind": "fda_crl_date_disagreement",
        "title": title,
        "body": body,
        "evidence": {
            "application_number": application_number,
            "transparency_date": transparency_date.isoformat(),
            "edgar_date": edgar_date.isoformat(),
            "delta_days": delta_days,
            "eval_harness_id": eval_harness_id,
            "sponsor_name": sponsor_name,
            "source_feed": SOURCE_FEED,
            "edgar_curator_script": "modal_workers/scripts/curate_crl_from_edgar.py",
            "threshold_days": DATE_DISAGREEMENT_THRESHOLD_DAYS,
        },
    }
    client._rest_with_retry(
        "POST", "operator_flags",
        json_body=flag,
        prefer="resolution=ignore-duplicates,return=minimal",
    )


# ---------------------------------------------------------------------------
# HTTP session + helpers
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Conan/1.0 (FDA orchestrator; https://github.com/marazuela/conan)",
        "Accept": "application/json, application/zip;q=0.9, */*;q=0.5",
    })
    return s


def _empty_result(error: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fetched": 0,
        "upserted": 0,
        "skipped_other_letter_type": 0,
        "skipped_no_date": 0,
        "skipped_no_application": 0,
        "duplicate_within_run": 0,
        "pdf_parse_errors": 0,
        "pdf_missing": 0,
        "drug_extracted": 0,
        "indication_extracted": 0,
        "asset_resolved": 0,
        "disagreements_flagged": 0,
        "errors": [error],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(prog="fda_crl_transparency")
    parser.add_argument("--apply", action="store_true",
                        help="Write to Supabase. Default dry-run.")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--pdf-sample-limit", type=int, default=None,
                        help="Smoke-test: parse only first N PDFs.")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(levelname)s %(name)s %(message)s")

    client = SupabaseClient() if args.apply else _DryClient()
    result = fetch(
        client,
        dry_run=not args.apply,
        pdf_sample_limit=args.pdf_sample_limit,
    )
    for k in (
        "partition",
        "fetched", "upserted",
        "skipped_other_letter_type", "skipped_no_date", "skipped_no_application",
        "duplicate_within_run",
        "pdf_parse_errors", "pdf_missing",
        "drug_extracted", "indication_extracted",
        "asset_resolved", "disagreements_flagged",
    ):
        if k in result:
            print(f"{k}: {result[k]}")
    errs = result.get("errors") or []
    if errs:
        print(f"errors: {len(errs)}")
        for err in errs[:5]:
            print(f"  - {err}")
    return 0


class _DryClient:
    """Stub for --no-apply runs. Returns no rows so resolution paths
    short-circuit cleanly."""

    def _rest(self, *_args, **_kwargs) -> List[Any]:
        return []

    def _rest_with_retry(self, *_args, **_kwargs) -> List[Any]:
        return []


if __name__ == "__main__":
    raise SystemExit(main())
