"""
BMV/BIVA scanner — Modal port of tools/bmv_scanner.py.

Preserved from v1 (byte-equivalent where relevant):
  - BIVA public JSON endpoint (https://www.biva.mx/emisoras/eventos-relevantes)
    with browser User-Agent headers. No auth.
  - Full 20-pattern Spanish-language classifier: merger_announcement,
    acquisition, tender_offer (OPA/OPC), spinoff, delisting, change_of_control,
    major_shareholder_change, auditor_change, board_resignation,
    board_appointment, insolvency, going_concern, trading_suspension,
    regulatory_investigation, litigation_event, profit_warning, impairment,
    rating_downgrade, rating_watch_negative, plus material_event_generic
    fallback for emisora-section filings.
  - v1 boilerplate regex (afirma calificacion, asamblea anual, etc.) kept
    inline; additionally routed through shared is_boilerplate("BMV", headline).
  - Epoch-ms → UTC datetime conversion on `fechaPublicacion`.
  - 14-day lookback window, page_size=500.
  - litigation_event → scoring_profile "litigation" via registry
    signal_type_profile_map.

Deviations from v1:
  - No OUT_FILE; signals returned via ScannerResult.
  - No HttpClient dependency — plain `requests` with v1 BIVA_HEADERS.
  - source_content_hash now carries "sha256:<64hex>" prefix for spec.md §3.4
    convergence classification parity.
  - EntityHints emitted so the resolver can walk ticker+XMEX / id_empresa_biva /
    name cascades. v1's raw_data dict preserved verbatim in raw_payload.

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from modal_workers.shared.boilerplate_filters import is_boilerplate
from modal_workers.shared.scanner_base import Signal, ScannerResult
from modal_workers.shared.supabase_client import EntityHints, ScannerConfig

NAME = "bmv_scanner"

# ---------------------------------------------------------------------------
# Constants (verbatim from v1)
# ---------------------------------------------------------------------------

BIVA_API = "https://www.biva.mx/emisoras/eventos-relevantes"
BIVA_ROOT = "https://www.biva.mx"
LOOKBACK_DAYS = 14
PAGE_SIZE = 500
REQUEST_TIMEOUT = 25

BIVA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
    "Referer": "https://www.biva.mx/en/web/portal/eventos-relevantes",
}

# (regex, (signal_type, scoring_profile, thesis_direction))
HIGH_SIGNAL_PATTERNS: List[Tuple[re.Pattern, Tuple[str, str, str]]] = [
    # M&A / tender offers
    (re.compile(r"(?i)\b(fusi[óo]n|merger)\b", re.I),
     ("merger_announcement", "merger_arb", "unknown")),
    (re.compile(r"(?i)\badquisici[óo]n\b", re.I),
     ("acquisition", "merger_arb", "long")),
    (re.compile(r"(?i)oferta\s+p[úu]blica\s+de\s+adquisici[óo]n|\bOPA\b", re.I),
     ("tender_offer", "merger_arb", "long")),
    (re.compile(r"(?i)oferta\s+p[úu]blica\s+de\s+compra|\bOPC\b", re.I),
     ("tender_offer", "merger_arb", "long")),
    (re.compile(r"(?i)\bescisi[óo]n\b", re.I),
     ("spinoff", "merger_arb", "unknown")),
    (re.compile(r"(?i)desliste|cancelaci[óo]n\s+de\s+inscripci[óo]n|delisting", re.I),
     ("delisting", "merger_arb", "long")),
    # Governance
    (re.compile(r"(?i)cambio\s+de\s+control|nuevo\s+accionista\s+de\s+control", re.I),
     ("change_of_control", "activist_governance", "neutral")),
    (re.compile(r"(?i)accionista\s+mayoritario|participaci[óo]n\s+relevante", re.I),
     ("major_shareholder_change", "activist_governance", "neutral")),
    (re.compile(r"(?i)(renuncia|remoci[óo]n|destituci[óo]n).*(auditor|comisario)", re.I),
     ("auditor_change", "activist_governance", "short")),
    (re.compile(r"(?i)(renuncia|remoci[óo]n).*(consejero|director\s+independiente|director\s+general)", re.I),
     ("board_resignation", "activist_governance", "short")),
    (re.compile(r"(?i)nombramiento.*(consejo\s+de\s+administraci[óo]n|comit[ée])", re.I),
     ("board_appointment", "activist_governance", "neutral")),
    # Distress
    (re.compile(r"(?i)concurso\s+mercantil|reestructura\s+financiera|insolvencia", re.I),
     ("insolvency", "activist_governance", "short")),
    (re.compile(r"(?i)going\s+concern|negocio\s+en\s+marcha|duda\s+sobre\s+la\s+continuidad", re.I),
     ("going_concern", "activist_governance", "short")),
    (re.compile(r"(?i)suspensi[óo]n\s+de\s+(cotizaci[óo]n|operaciones)", re.I),
     ("trading_suspension", "activist_governance", "short")),
    # Regulatory / litigation
    (re.compile(r"(?i)investigaci[óo]n|sanci[óo]n|multa|cnbv\s+inici[óa]", re.I),
     ("regulatory_investigation", "activist_governance", "short")),
    (re.compile(r"(?i)demanda|litigio|resoluci[óo]n\s+judicial|amparo", re.I),
     ("litigation_event", "litigation", "short")),
    # Earnings shocks
    (re.compile(r"(?i)(advertencia|alerta).*(resultados|utilidad|p[ée]rdida)", re.I),
     ("profit_warning", "activist_governance", "short")),
    (re.compile(r"(?i)deterioro|impairment|prdida\s+extraordinaria", re.I),
     ("impairment", "activist_governance", "short")),
    # Rating agency actions (seccion=calificadora; covers HR Ratings / Fitch MX / Moody's MX)
    (re.compile(r"(?i)(baja|rebaja|downgrade|revisi[óo]n\s+a\s+la\s+baja).*calificaci[óo]n", re.I),
     ("rating_downgrade", "activist_governance", "short")),
    (re.compile(r"(?i)calificaci[óo]n.*perspectiva\s+(negativa|en\s+revisi[óo]n)", re.I),
     ("rating_watch_negative", "activist_governance", "short")),
]

# Inline v1 boilerplate patterns (kept so the classifier behaves identically when
# is_boilerplate's BMV pack lacks coverage for e.g. "Afirma calificación").
BOILERPLATE_PATTERNS: List[re.Pattern] = [
    re.compile(r"(?i)^\s*afirma\s+calificaci", re.I),
    re.compile(r"(?i)asamblea\s+(ordinaria|anual)\s+de\s+accionistas", re.I),
    re.compile(r"(?i)convocatoria\s+a\s+asamblea", re.I),
    re.compile(r"(?i)pago\s+de\s+dividendos?", re.I),
    re.compile(r"(?i)aviso\s+legal|aviso\s+de\s+ejercicio", re.I),
    re.compile(r"(?i)reporte\s+(trimestral|anual|de\s+sustentabilidad)", re.I),
    re.compile(r"(?i)estados\s+financieros", re.I),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _epoch_ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except Exception:
        return None


def _within_window(rec: Dict[str, Any], days: int) -> bool:
    dt = _epoch_ms_to_dt(rec.get("fechaPublicacion"))
    if dt is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return dt >= cutoff


def _is_v1_boilerplate(tipo_doc: str) -> bool:
    return any(p.search(tipo_doc or "") for p in BOILERPLATE_PATTERNS)


def _classify(tipo_doc: str, seccion: str) -> Optional[Tuple[str, str, str]]:
    if _is_v1_boilerplate(tipo_doc):
        return None
    for pat, result in HIGH_SIGNAL_PATTERNS:
        if pat.search(tipo_doc or ""):
            return result
    # emisora section: unmatched-but-non-boilerplate is still potentially material.
    # calificadora: conservatively drop unmatched (affirmations, etc.).
    if seccion == "emisora" and tipo_doc and len(tipo_doc.strip()) > 10:
        return ("material_event_generic", "activist_governance", "unknown")
    return None


def _pick_pdf_url(archivos: List[Dict[str, Any]]) -> Optional[str]:
    if not archivos:
        return None
    pdf_url: Optional[str] = None
    for a in archivos:
        if (a.get("extension") or "").upper() == "PDF":
            pdf_url = a.get("url")
            break
    if not pdf_url:
        pdf_url = archivos[0].get("url")
    if pdf_url and pdf_url.startswith("/"):
        pdf_url = BIVA_ROOT + pdf_url
    return pdf_url


def _fetch_biva(size: int = PAGE_SIZE) -> Tuple[List[Dict[str, Any]], str]:
    url = f"{BIVA_API}?size={size}"
    try:
        r = requests.get(url, headers=BIVA_HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return [], f"api fetch failed: {type(e).__name__}: {e}"
    if not isinstance(data, dict):
        return [], f"unexpected payload shape: {type(data).__name__}"
    content = data.get("content") or []
    return content, ""


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------

def _build_signal(rec: Dict[str, Any], scan_date: datetime) -> Optional[Signal]:
    clave = (rec.get("clave") or "").strip()
    tipo_doc = (rec.get("tipoDocumento") or "").strip()
    seccion = (rec.get("seccion") or "").strip()
    if not clave or not tipo_doc:
        return None

    # Shared BMV boilerplate pack (runs on the headline = "{CLAVE}: {tipo_doc}").
    headline = f"{clave}: {tipo_doc[:140]}"
    if is_boilerplate("BMV", headline):
        return None

    cls = _classify(tipo_doc, seccion)
    if cls is None:
        return None
    signal_type, profile, direction = cls

    id_doc = str(rec.get("idDocumento") or "")
    id_emp = rec.get("idEmpresa")
    fecha_ms = rec.get("fechaPublicacion")
    source_date = _epoch_ms_to_dt(fecha_ms) or scan_date

    pdf_url = _pick_pdf_url(rec.get("archivosXbrl") or [])

    source_content_hash = (
        f"sha256:{hashlib.sha256(f'{clave}|{tipo_doc}|{fecha_ms or 0}'.encode()).hexdigest()}"
    )
    signal_id = hashlib.sha256(f"biva:{clave}:{id_doc}".encode()).hexdigest()[:32]

    # raw_payload mirrors v1's raw_data dict verbatim (same keys, same shape).
    raw_payload: Dict[str, Any] = {
        "clave": clave,
        "id_documento": id_doc,
        "id_empresa": id_emp,
        "fecha_publicacion_ms": fecha_ms,
        "seccion": seccion,
        "doc_type": rec.get("docType"),
        "nombre_archivo": rec.get("nombreArchivo"),
        "tipo_documento": tipo_doc,
        # Denormalised fields carried for the rubric / reactor:
        "headline": headline,
        "summary": tipo_doc,
        "filing_url": pdf_url,
        "company_name_en": None,
        "ticker": clave,
        "mic": "XMEX",
    }

    entity_hints = EntityHints(
        ticker=clave,
        mic="XMEX",  # BMV canonical MX MIC; BIVA issuers dual-list
        id_empresa_biva=str(id_emp) if id_emp is not None else None,
        name=clave,  # BIVA API omits long names; fall back to ticker for name_normalized lookup
        country="MX",
    )

    return Signal(
        signal_id=signal_id,
        source_content_hash=source_content_hash,
        source_date=source_date,
        scan_date=scan_date,
        signal_type=signal_type,
        raw_payload=raw_payload,
        source_url=pdf_url,
        entity_hints=entity_hints,
        scoring_profile=profile,
        thesis_direction=direction,
    )


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    scan_date = datetime.now(timezone.utc)
    scan_start = time.time()
    budget = max(10, cfg.timeout_soft_s - 5)

    records, err = _fetch_biva(PAGE_SIZE)
    if err:
        return ScannerResult(
            scanner=NAME,
            status="error",
            signals=[],
            warnings=[err],
            fetched_records=0,
            error=err,
        )

    warnings: List[str] = []
    signals: List[Signal] = []
    seen_hashes: set[str] = set()
    in_window_records = 0

    for rec in records:
        if time.time() - scan_start > budget:
            warnings.append(f"wall-clock budget ({budget}s) exceeded during record loop")
            break
        if not _within_window(rec, LOOKBACK_DAYS):
            continue
        in_window_records += 1
        sig = _build_signal(rec, scan_date)
        if sig is None:
            continue
        if sig.source_content_hash in seen_hashes:
            continue
        seen_hashes.add(sig.source_content_hash)
        signals.append(sig)

    status = "partial" if warnings else "ok"

    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=signals,
        warnings=warnings,
        fetched_records=len(records),
    )
