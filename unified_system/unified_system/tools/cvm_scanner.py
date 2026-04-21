"""
CVM Scanner - Brazil Comissão de Valores Mobiliários filings.

Promoted from stub to operational on 2026-04-16 (S2f).

Data source: CVM Open Data Portal (dados.cvm.gov.br). We download the
annual IPE (Informações Periódicas e Eventuais) zip file for the
current year, parse its CSV, filter to the last N days, and classify
by Categoria + Tipo + Assunto (free-text subject line in Portuguese).

URL: https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{YYYY}.zip

Zip contains a single latin-1 CSV delimited by ';' with fields:
  CNPJ_Companhia, Nome_Companhia, Codigo_CVM, Data_Referencia,
  Categoria, Tipo, Especie, Assunto, Data_Entrega, Tipo_Apresentacao,
  Protocolo_Entrega, Versao, Link_Download

We only classify high-signal categories:
  - Fato Relevante                         (Material Fact)
  - Comunicação sobre Transação entre Partes Relacionadas  (RPT)
  - Comunicado ao Mercado                  (with subtype filtering)
  - Informações de Companhias em Recuperação Judicial     (Judicial Recovery)

Signal-type mapping (via Portuguese regex on Assunto + Tipo):
  OPA / Oferta Pública de Aquisição               -> tender_offer (long)
  Aquisição/Alienação de Participação Acionária   -> major_shareholder_change (neutral)
  Mudança de Auditor                              -> auditor_change (short)
  Conversão de Registro / Cancelamento de Registro -> delisting (short)
  Recuperação Judicial                             -> judicial_recovery (short)
  Destituição / nomeação do conselho               -> board_shakeup (neutral)
  Memorando de Entendimento                        -> mou_signed (long)
  Leilão                                           -> auction_result (neutral)
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))
try:
    from http_client import HttpClient  # type: ignore
except Exception:
    HttpClient = None

NAME = "cvm_scanner"
REPO = Path(__file__).parent.parent
OUT_FILE = REPO / "signals" / f"{NAME}_output.json"
OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

IPE_ZIP_FMT = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip"
LOOKBACK_DAYS = 7  # CVM has weekend gaps; 7d keeps recent activity

# Categories we even consider (everything else is routine/boilerplate)
TARGET_CATEGORIES = {
    "Fato Relevante",
    "Comunicação sobre Transação entre Partes Relacionadas",
    "Comunicado ao Mercado",
    "Informações de Companhias em Recuperação Judicial ou Extrajudicial",
}

# Categoria|Tipo combinations we drop even within target cats
BOILERPLATE_TIPO = {
    "Outros Comunicados Não Considerados Fatos Relevantes",
    "Apresentações a analistas/agentes do mercado",
    "Contas demonstrativas mensais",
    "Esclarecimentos sobre questionamentos da CVM/B3",  # usually brief, low-signal
}

# (pattern, (signal_type, scoring_profile, thesis_direction))
# Portuguese regex against Assunto + Tipo combined
HIGH_SIGNAL_PATTERNS = [
    # Tender offers / takeovers
    (re.compile(r"(?i)oferta\s+p[uú]blica\s+de\s+aquisi[cç][aã]o|opa\b"),
     ("tender_offer", "merger_arb", "long")),
    (re.compile(r"(?i)fus[aã]o|incorpora[cç][aã]o\s+de\s+a[cç][oõ]es"),
     ("merger_announcement", "merger_arb", "unknown")),
    (re.compile(r"(?i)cis[aã]o"),
     ("spinoff", "merger_arb", "unknown")),

    # Ownership / control
    (re.compile(r"(?i)aquisi[cç][aã]o.*participa[cç][aã]o|aliena[cç][aã]o.*participa[cç][aã]o|"
                r"art\.?\s*12.*instr.*cvm"),
     ("major_shareholder_change", "activist_governance", "neutral")),
    (re.compile(r"(?i)acordo\s+de\s+acionistas"),
     ("shareholder_agreement", "activist_governance", "neutral")),

    # Governance red flags
    (re.compile(r"(?i)mudan[cç]a\s+de\s+auditor"),
     ("auditor_change", "activist_governance", "short")),
    (re.compile(r"(?i)ren[uú]ncia.*conselho|ren[uú]ncia.*diretor"),
     ("board_resignation", "activist_governance", "short")),
    (re.compile(r"(?i)destitui[cç][aã]o.*(?:conselho|diretor|administrador|estatut[aá]rio)"),
     ("board_shakeup", "activist_governance", "neutral")),
    (re.compile(r"(?i)investiga[cç][aã]o|aus[eê]ncia\s+de\s+conformidade|non[\-\s]?compliance"),
     ("regulatory_investigation", "activist_governance", "short")),

    # Distress
    (re.compile(r"(?i)recupera[cç][aã]o\s+judicial|fal[eê]ncia"),
     ("judicial_recovery", "activist_governance", "short")),
    (re.compile(r"(?i)adiamento.*divulga[cç][aã]o|atraso.*demonstra[cç][oõ]es"),
     ("earnings_delay", "activist_governance", "short")),
    (re.compile(r"(?i)convers[aã]o\s+de\s+registro|cancelamento\s+de\s+registro"),
     ("delisting", "activist_governance", "short")),

    # Positive / neutral structural
    (re.compile(r"(?i)memorando\s+de\s+entendimento|mou\b"),
     ("mou_signed", "activist_governance", "long")),
    (re.compile(r"(?i)leil[aã]o"),
     ("auction_result", "activist_governance", "neutral")),
    (re.compile(r"(?i)partes\s+relacionadas"),
     ("related_party_transaction", "activist_governance", "neutral")),

    # Litigation
    (re.compile(r"(?i)a[cç][aã]o\s+judicial|senten[cç]a|senten[cç]a.*proced[eê]ncia|"
                r"condena[cç][aã]o"),
     ("litigation_event", "litigation", "short")),
]


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sig_id(cnpj: str, protocolo: str) -> str:
    return hashlib.sha256(f"cvm:{cnpj}:{protocolo}".encode()).hexdigest()[:32]


def _content_hash(cnpj: str, categoria: str, tipo: str, assunto: str, data_entrega: str) -> str:
    return hashlib.sha256(
        f"{cnpj}|{categoria}|{tipo}|{assunto[:120]}|{data_entrega}".encode()
    ).hexdigest()[:16]


def _parse_date(s: str) -> str:
    """'2026-04-14' (assumed BRT) -> ISO-8601 UTC start-of-day.

    CVM only gives date-level precision in the dataset. We approximate
    to 00:00 BRT (UTC-3) = 03:00 UTC same day.
    """
    if not s:
        return ""
    try:
        dt_local = datetime.strptime(s, "%Y-%m-%d")
        # BRT = UTC-3
        dt_utc = dt_local + timedelta(hours=3)
        return dt_utc.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def _classify(categoria: str, tipo: str, assunto: str) -> Optional[tuple]:
    if categoria not in TARGET_CATEGORIES:
        return None
    if tipo and tipo.strip() in BOILERPLATE_TIPO:
        return None
    haystack = f"{tipo or ''}  {assunto or ''}"
    for pat, result in HIGH_SIGNAL_PATTERNS:
        if pat.search(haystack):
            return result
    # Unclassified Fato Relevante with non-empty Assunto is still signal —
    # the category itself carries information even if the subject is generic.
    if categoria == "Fato Relevante" and assunto and len(assunto.strip()) > 3:
        return ("material_fact_generic", "activist_governance", "unknown")
    if categoria == "Informações de Companhias em Recuperação Judicial ou Extrajudicial":
        return ("judicial_recovery_update", "activist_governance", "short")
    return None


def _record_to_signal(rec: Dict[str, str]) -> Optional[Dict[str, Any]]:
    categoria = (rec.get("Categoria") or "").strip()
    tipo = (rec.get("Tipo") or "").strip()
    assunto = (rec.get("Assunto") or "").strip()
    cls = _classify(categoria, tipo, assunto)
    if cls is None:
        return None
    signal_type, profile, direction = cls
    cnpj = (rec.get("CNPJ_Companhia") or "").strip()
    nome = (rec.get("Nome_Companhia") or "").strip()
    cvm_code = (rec.get("Codigo_CVM") or "").strip()
    data_entrega = (rec.get("Data_Entrega") or "").strip()
    protocolo = (rec.get("Protocolo_Entrega") or "").strip()
    link = rec.get("Link_Download") or ""

    return {
        "signal_id": _sig_id(cnpj, protocolo),
        "source_content_hash": _content_hash(cnpj, categoria, tipo, assunto, data_entrega),
        "scanner_source": NAME,
        "upstream_scanner": NAME,
        "scoring_profile": profile,
        "signal_type": signal_type,
        "thesis_direction": direction,
        "ticker": None,  # CVM dataset has no ticker; resolver can map via CNPJ or Codigo_CVM
        "mic": "BVMF",
        "figi": None,
        "issuer_figi": None,
        "cnpj": cnpj,
        "codigo_cvm": cvm_code,
        "company_name_en": nome,  # Portuguese name; frontend can i18n
        "protocolo": protocolo,
        "filing_url": link or None,
        "scan_date": _iso(),
        "source_date": _parse_date(data_entrega) or _iso(),
        "headline": (f"{cvm_code or cnpj} {nome}: "
                     f"{categoria}{' / ' + tipo if tipo else ''}{' - ' + assunto[:120] if assunto else ''}")[:240],
        "summary": assunto or f"{categoria} / {tipo}",
        "raw_data": {
            "cnpj": cnpj,
            "codigo_cvm": cvm_code,
            "categoria": categoria,
            "tipo": tipo,
            "especie": rec.get("Especie"),
            "data_entrega_brt": data_entrega,
            "protocolo": protocolo,
            "versao": rec.get("Versao"),
        },
    }


def _fetch_ipe_zip(client, year: int) -> tuple:
    url = IPE_ZIP_FMT.format(year=year)
    try:
        r = client.get(url, timeout_s=30)
        r.raise_for_status()
        return r.content, ""
    except Exception as e:
        return None, f"fetch failed: {type(e).__name__}: {e}"


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
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=LOOKBACK_DAYS)
    content, err = _fetch_ipe_zip(client, today.year)
    if err:
        # Fall back to previous year at year boundary
        if today.month == 1 and today.day < 15:
            content, err = _fetch_ipe_zip(client, today.year - 1)
        if err:
            return {
                "scanner": NAME,
                "ran_at_utc": _iso(),
                "status": "error",
                "signals": [],
                "error": err,
            }

    try:
        z = zipfile.ZipFile(io.BytesIO(content))
        csv_names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            return {
                "scanner": NAME,
                "ran_at_utc": _iso(),
                "status": "error",
                "signals": [],
                "error": "no csv in IPE zip",
            }
        with z.open(csv_names[0]) as f:
            text = f.read().decode("latin-1")
    except Exception as e:
        return {
            "scanner": NAME,
            "ran_at_utc": _iso(),
            "status": "error",
            "signals": [],
            "error": f"zip parse failed: {type(e).__name__}: {e}",
        }

    records = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    total_rows = len(records)

    # Filter to recent + target categories
    recent_records: List[Dict[str, str]] = []
    for rec in records:
        de = rec.get("Data_Entrega") or ""
        if not de:
            continue
        try:
            d = datetime.strptime(de, "%Y-%m-%d").date()
        except Exception:
            continue
        if d < cutoff:
            continue
        if (rec.get("Categoria") or "").strip() not in TARGET_CATEGORIES:
            continue
        recent_records.append(rec)

    signals: List[Dict[str, Any]] = []
    seen = set()
    skipped_boilerplate = 0
    skipped_unmatched = 0
    for rec in recent_records:
        sig = _record_to_signal(rec)
        if sig is None:
            tipo = (rec.get("Tipo") or "").strip()
            if tipo in BOILERPLATE_TIPO:
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
        "fetched_records": total_rows,
        "target_cat_records_in_window": len(recent_records),
        "unique_signals": len(signals),
        "skipped_boilerplate": skipped_boilerplate,
        "skipped_unmatched": skipped_unmatched,
        "lookback_days": LOOKBACK_DAYS,
    }


def main():
    result = scan()
    tmp = OUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    os.replace(tmp, OUT_FILE)
    print(json.dumps({
        "signals": len(result["signals"]),
        "scanner": NAME,
        "status": result["status"],
        "fetched": result.get("fetched_records", 0),
        "in_window": result.get("target_cat_records_in_window", 0),
    }))


if __name__ == "__main__":
    main()

# --- END OF FILE ---
