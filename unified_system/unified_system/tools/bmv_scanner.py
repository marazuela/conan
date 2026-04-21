"""
BMV/BIVA Scanner — Mexico corporate eventos relevantes.

Promoted from stub to operational on 2026-04-16 (S2g).

Data source: BIVA's (Bolsa Institucional de Valores) public JSON API.
  https://www.biva.mx/emisoras/eventos-relevantes?size=N

BIVA is Mexico's alternative exchange. Unlike BMV (the primary exchange),
BIVA exposes a clean JSON endpoint without WAF gating. BMV's
eventos-relevantes pages are heavily client-rendered and require per-issuer
enumeration (BOLSA-XXXX-CGEN_CAPIT URLs) — not practical for a scanner.

Coverage: Mexican issuers must disclose material events ("eventos
relevantes") simultaneously on BMV and BIVA per CNBV rules, so BIVA
effectively mirrors the BMV disclosure stream. The dataset also includes
rating-agency (calificadora) announcements with their own `seccion` marker.

Signal-type mapping (on Spanish `tipoDocumento` field):
  Fusión / adquisición / OPA                 -> merger_arb
  Oferta pública / OPC                        -> merger_arb (long)
  Escisión                                    -> merger_arb
  Cambio de control / accionista mayoritario  -> activist_governance
  Renuncia de auditor / cambio de auditor     -> activist_governance (short)
  Renuncia de consejero / director            -> activist_governance
  Concurso mercantil / reestructura           -> activist_governance (short)
  Desliste / cancelación de inscripción       -> merger_arb
  Investigación / multa / sanción             -> activist_governance (short)
  Litigio / demanda / resolución judicial     -> litigation (short)
  Advertencia de resultados / pérdida         -> activist_governance (short)
  Rebaja de calificación (calificadora)       -> activist_governance (short)

Skips rating-agency boilerplate "Afirma" messages — only rating
downgrades/reviews are actionable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))
try:
    from http_client import HttpClient  # type: ignore
except Exception:
    HttpClient = None

NAME = "bmv_scanner"
REPO = Path(__file__).parent.parent
OUT_FILE = REPO / "signals" / f"{NAME}_output.json"
OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

BIVA_API = "https://www.biva.mx/emisoras/eventos-relevantes"
BIVA_ROOT = "https://www.biva.mx"
LOOKBACK_DAYS = 14  # BIVA returns only ~15 most recent; wider window catches them all
PAGE_SIZE = 500     # server caps at its own ceiling, but we try

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

HIGH_SIGNAL_PATTERNS = [
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
    # Rating agency actions (seccion=calificadora)
    (re.compile(r"(?i)(baja|rebaja|downgrade|revisi[óo]n\s+a\s+la\s+baja).*calificaci[óo]n", re.I),
     ("rating_downgrade", "activist_governance", "short")),
    (re.compile(r"(?i)calificaci[óo]n.*perspectiva\s+(negativa|en\s+revisi[óo]n)", re.I),
     ("rating_watch_negative", "activist_governance", "short")),
]

BOILERPLATE_PATTERNS = [
    re.compile(r"(?i)^\s*afirma\s+calificaci", re.I),  # "AM Best Afirma..." rating affirmation
    re.compile(r"(?i)asamblea\s+(ordinaria|anual)\s+de\s+accionistas", re.I),
    re.compile(r"(?i)convocatoria\s+a\s+asamblea", re.I),
    re.compile(r"(?i)pago\s+de\s+dividendos?", re.I),
    re.compile(r"(?i)aviso\s+legal|aviso\s+de\s+ejercicio", re.I),
    re.compile(r"(?i)reporte\s+(trimestral|anual|de\s+sustentabilidad)", re.I),
    re.compile(r"(?i)estados\s+financieros", re.I),
]


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sig_id(clave: str, id_doc: str) -> str:
    return hashlib.sha256(f"biva:{clave}:{id_doc}".encode()).hexdigest()[:32]


def _content_hash(clave: str, tipo_doc: str, fecha_ms: int) -> str:
    return hashlib.sha256(f"{clave}|{tipo_doc}|{fecha_ms}".encode()).hexdigest()[:16]


def _epoch_ms_to_iso(ms: Optional[int]) -> str:
    if not ms:
        return ""
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def _classify(tipo_doc: str, seccion: str) -> Optional[tuple]:
    for pat in BOILERPLATE_PATTERNS:
        if pat.search(tipo_doc or ""):
            return None
    for pat, result in HIGH_SIGNAL_PATTERNS:
        if pat.search(tipo_doc or ""):
            return result
    # For emisora section, unmatched but non-boilerplate is still potentially material.
    # Conservative: drop unmatched from calificadora; keep emisora generic material fact.
    if seccion == "emisora" and tipo_doc and len(tipo_doc.strip()) > 10:
        return ("material_event_generic", "activist_governance", "unknown")
    return None


def _record_to_signal(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    clave = (rec.get("clave") or "").strip()
    tipo_doc = (rec.get("tipoDocumento") or "").strip()
    seccion = (rec.get("seccion") or "").strip()
    if not clave or not tipo_doc:
        return None
    cls = _classify(tipo_doc, seccion)
    if cls is None:
        return None
    signal_type, profile, direction = cls
    id_doc = str(rec.get("idDocumento") or "")
    id_emp = rec.get("idEmpresa")
    fecha_ms = rec.get("fechaPublicacion")
    archivos = rec.get("archivosXbrl") or []
    # Prefer PDF URL
    pdf_url = None
    for a in archivos:
        if (a.get("extension") or "").upper() == "PDF":
            pdf_url = a.get("url")
            break
    if not pdf_url and archivos:
        pdf_url = archivos[0].get("url")
    if pdf_url and pdf_url.startswith("/"):
        pdf_url = BIVA_ROOT + pdf_url

    return {
        "signal_id": _sig_id(clave, id_doc),
        "source_content_hash": _content_hash(clave, tipo_doc, fecha_ms or 0),
        "scanner_source": NAME,
        "upstream_scanner": NAME,
        "scoring_profile": profile,
        "signal_type": signal_type,
        "thesis_direction": direction,
        "ticker": clave,
        "mic": "XMEX",  # BMV MIC (BIVA is BIVA but issuers dual-list; XMEX is the canonical MX MIC)
        "figi": None,
        "issuer_figi": None,
        "id_empresa_biva": id_emp,
        "company_name_en": None,  # BIVA API doesn't return long name; ticker-only
        "id_documento": id_doc,
        "seccion": seccion,
        "filing_url": pdf_url,
        "scan_date": _iso(),
        "source_date": _epoch_ms_to_iso(fecha_ms) or _iso(),
        "headline": f"{clave}: {tipo_doc[:140]}",
        "summary": tipo_doc,
        "raw_data": {
            "clave": clave,
            "id_documento": id_doc,
            "id_empresa": id_emp,
            "fecha_publicacion_ms": fecha_ms,
            "seccion": seccion,
            "doc_type": rec.get("docType"),
            "nombre_archivo": rec.get("nombreArchivo"),
            "tipo_documento": tipo_doc,
        },
    }


def _fetch_biva(client, size: int = PAGE_SIZE):
    url = f"{BIVA_API}?size={size}"
    try:
        r = client.get(url, timeout_s=25, headers=BIVA_HEADERS)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return [], f"api fetch failed: {type(e).__name__}: {e}"
    if not isinstance(data, dict):
        return [], f"unexpected payload shape: {type(data).__name__}"
    content = data.get("content") or []
    return content, ""


def _within_window(rec: Dict[str, Any], days: int) -> bool:
    fecha_ms = rec.get("fechaPublicacion")
    if not fecha_ms:
        return False
    try:
        dt = datetime.fromtimestamp(fecha_ms / 1000, tz=timezone.utc)
    except Exception:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return dt >= cutoff


def scan() -> Dict[str, Any]:
    if HttpClient is None:
        return {
            "scanner": NAME,
            "ran_at_utc": _iso(),
            "status": "error",
            "signals": [],
            "error": "http_client module not importable",
        }

    client = HttpClient()
    records, err = _fetch_biva(client, PAGE_SIZE)
    if err:
        return {
            "scanner": NAME,
            "ran_at_utc": _iso(),
            "status": "error",
            "signals": [],
            "error": err,
        }

    signals: List[Dict[str, Any]] = []
    seen = set()
    skipped_boilerplate = 0
    skipped_unmatched = 0
    skipped_out_of_window = 0
    in_window_records = 0
    for rec in records:
        if not _within_window(rec, LOOKBACK_DAYS):
            skipped_out_of_window += 1
            continue
        in_window_records += 1
        sig = _record_to_signal(rec)
        if sig is None:
            tipo_doc = rec.get("tipoDocumento") or ""
            if any(p.search(tipo_doc) for p in BOILERPLATE_PATTERNS):
                skipped_boilerplate += 1
            else:
                skipped_unmatched += 1
            continue
        h = sig["source_content_hash"]
        if h in seen:
            continue
        seen.add(h)
        signals.append(sig)

    return {
        "scanner": NAME,
        "ran_at_utc": _iso(),
        "status": "ok",
        "signals": signals,
        "fetched_records": len(records),
        "in_window_records": in_window_records,
        "unique_signals": len(signals),
        "skipped_boilerplate": skipped_boilerplate,
        "skipped_unmatched": skipped_unmatched,
        "skipped_out_of_window": skipped_out_of_window,
        "lookback_days": LOOKBACK_DAYS,
    }


def main():
    result = scan()
    tmp = OUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    os.replace(tmp, OUT_FILE)
    print(json.dumps({
        "signals": len(result["signals"]),
        "scanner": NAME,
        "status": result["status"],
        "fetched": result.get("fetched_records", 0),
        "in_window": result.get("in_window_records", 0),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()

# --- END OF FILE ---
