"""
CVM scanner — Modal port of tools/cvm_scanner.py.

Data source: Brazilian Comissão de Valores Mobiliários Open Data Portal
(dados.cvm.gov.br). Downloads the annual IPE (Informações Periódicas e
Eventuais) zip file for the current year, parses its latin-1 ;-delimited CSV,
filters to the last N days, and classifies by Categoria + Tipo + Assunto
(Portuguese free-text subject line).

Preserved from v1 (byte-equivalent where relevant):
  - TARGET_CATEGORIES whitelist (Fato Relevante, RPT, Comunicado ao Mercado,
    Recuperação Judicial).
  - BOILERPLATE_TIPO drop-list (board minutes, JCP notices, monthly accounts,
    CVM/B3 clarifications).
  - Full 18-pattern Portuguese HIGH_SIGNAL_PATTERNS classifier (tender_offer,
    merger_announcement, spinoff, major_shareholder_change,
    shareholder_agreement, auditor_change, board_resignation, board_shakeup,
    regulatory_investigation, judicial_recovery, earnings_delay, delisting,
    mou_signed, auction_result, related_party_transaction, litigation_event,
    plus material_fact_generic + judicial_recovery_update fallbacks).
  - Categoria + Tipo boilerplate skip.
  - Zip parsing: zipfile.ZipFile(io.BytesIO(bytes)), single CSV, latin-1,
    ';'-delimited, csv.DictReader.
  - 7-day lookback window (weekend gaps).
  - Year-boundary fallback: if current-year fetch fails early in January, try
    previous year.
  - Data_Entrega (date-level) parsed as BRT (UTC-3) → UTC start-of-day.

Deviations from v1:
  - No OUT_FILE; signals returned via ScannerResult.
  - No HttpClient dependency — plain `requests` with short CVM-friendly
    User-Agent.
  - Daily zip cache via SupabaseClient.read_cache("cvm", "ipe_{year}.zip"),
    keyed by a sidecar `ipe_{year}.meta` containing today's UTC date. On hit
    we skip the network call (warm runtime: ~2s). On miss we download +
    repopulate the cache (cold runtime: ~15s download + ~5s parse). Tens-of-MB
    zip size makes this a meaningful budget win under cfg.timeout_soft_s=60.
  - source_content_hash carries the spec.md §3.4 "sha256:<64hex>" prefix;
    derived from Protocolo_Entrega + Versao + Data_Entrega (stable per
    filing version — supports amendments via Versao bump).
  - EntityHints emitted with codigo_cvm + cnpj + Portuguese company name so
    the resolver walks codigo_cvm → cnpj → name cascade. issuer_figi is None
    (CVM dataset has no ticker/FIGI) — party_resolver downstream handles it.
  - Shared is_boilerplate("CVM", headline) pass layered on top of v1's
    BOILERPLATE_TIPO for extra coverage (board minutes, JCP edicts,
    convocation edicts).
  - Wall-clock budget-guard on cfg.timeout_soft_s.

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from modal_workers.shared.boilerplate_filters import is_boilerplate
from modal_workers.shared.scanner_base import Signal, ScannerResult
from modal_workers.shared.supabase_client import EntityHints, ScannerConfig, SupabaseClient

NAME = "cvm_scanner"

# ---------------------------------------------------------------------------
# Constants (verbatim from v1)
# ---------------------------------------------------------------------------

IPE_ZIP_FMT = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip"
LOOKBACK_DAYS = 7               # CVM has weekend gaps; 7d keeps recent activity
REQUEST_TIMEOUT = 30            # the zip is tens of MB

USER_AGENT = "Conan/2.0 (scanner=cvm_scanner; contact=ops@conan)"

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
HIGH_SIGNAL_PATTERNS: List[Tuple[re.Pattern, Tuple[str, str, str]]] = [
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_source_date(s: str) -> Optional[datetime]:
    """'YYYY-MM-DD' (assumed BRT 00:00) -> UTC datetime.

    CVM dataset is date-precision only; we approximate to start-of-day BRT
    (UTC-3) → 03:00 UTC same date.
    """
    if not s:
        return None
    try:
        dt_local = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None
    dt_utc = dt_local + timedelta(hours=3)
    return dt_utc.replace(tzinfo=timezone.utc)


def _classify(categoria: str, tipo: str, assunto: str) -> Optional[Tuple[str, str, str]]:
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


def _headline(cvm_code: str, cnpj: str, nome: str, categoria: str,
              tipo: str, assunto: str) -> str:
    return (f"{cvm_code or cnpj} {nome}: "
            f"{categoria}{' / ' + tipo if tipo else ''}"
            f"{' - ' + assunto[:120] if assunto else ''}")[:240]


# ---------------------------------------------------------------------------
# Fetch + cache
# ---------------------------------------------------------------------------

def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _fetch_ipe_zip(year: int) -> Tuple[Optional[bytes], str]:
    url = IPE_ZIP_FMT.format(year=year)
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT},
                         timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.content, ""
    except Exception as e:  # noqa: BLE001
        return None, f"fetch failed (year={year}): {type(e).__name__}: {e}"


def _cache_get(client: SupabaseClient, year: int) -> Optional[bytes]:
    """Return cached zip bytes if `ipe_{year}.meta` says today's date; else None."""
    meta = client.read_cache("cvm", f"ipe_{year}.meta")
    if meta is None:
        return None
    try:
        cached_date = meta.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if cached_date != _today_iso():
        return None
    return client.read_cache("cvm", f"ipe_{year}.zip")


def _cache_put(client: SupabaseClient, year: int, content: bytes) -> None:
    try:
        client.write_cache("cvm", f"ipe_{year}.zip", content,
                           content_type="application/zip")
        client.write_cache("cvm", f"ipe_{year}.meta",
                           _today_iso().encode("utf-8"),
                           content_type="text/plain")
    except Exception:  # noqa: BLE001 — best effort
        pass


def _load_zip(client: SupabaseClient, year: int) -> Tuple[Optional[bytes], str, bool]:
    """Return (content, error_string, cache_hit). Empty error means success."""
    cached = _cache_get(client, year)
    if cached:
        return cached, "", True
    content, err = _fetch_ipe_zip(year)
    if err:
        return None, err, False
    _cache_put(client, year, content)
    return content, "", False


def _parse_zip_to_records(content: bytes) -> Tuple[List[Dict[str, str]], str]:
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
        csv_names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            return [], "no csv in IPE zip"
        with z.open(csv_names[0]) as f:
            text = f.read().decode("latin-1")
    except Exception as e:  # noqa: BLE001
        return [], f"zip parse failed: {type(e).__name__}: {e}"
    records = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    return records, ""


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------

def _build_signal(rec: Dict[str, str], scan_date: datetime) -> Optional[Signal]:
    categoria = (rec.get("Categoria") or "").strip()
    tipo = (rec.get("Tipo") or "").strip()
    assunto = (rec.get("Assunto") or "").strip()
    cnpj = (rec.get("CNPJ_Companhia") or "").strip()
    nome = (rec.get("Nome_Companhia") or "").strip()
    cvm_code = (rec.get("Codigo_CVM") or "").strip()
    data_entrega = (rec.get("Data_Entrega") or "").strip()
    data_referencia = (rec.get("Data_Referencia") or "").strip()
    protocolo = (rec.get("Protocolo_Entrega") or "").strip()
    versao = (rec.get("Versao") or "").strip()
    link = rec.get("Link_Download") or ""

    headline = _headline(cvm_code, cnpj, nome, categoria, tipo, assunto)

    # Shared CVM boilerplate pack (board-minutes / JCP / convocation edicts).
    if is_boilerplate("CVM", headline):
        return None

    cls = _classify(categoria, tipo, assunto)
    if cls is None:
        return None
    signal_type, profile, direction = cls

    # source_content_hash: stable per filing version, bumps on amendment via Versao.
    source_content_hash = (
        f"sha256:{hashlib.sha256(f'{protocolo}|{versao}|{data_entrega}'.encode()).hexdigest()}"
    )
    # signal_id: deterministic on (cnpj, protocolo) so re-runs dedup cleanly.
    signal_id = hashlib.sha256(f"cvm:{cnpj}:{protocolo}".encode()).hexdigest()[:32]

    # Prefer Data_Entrega (submission date); fall back to Data_Referencia; then scan_date.
    source_date = (
        _parse_source_date(data_entrega)
        or _parse_source_date(data_referencia)
        or scan_date
    )

    raw_payload: Dict[str, Any] = {
        "cnpj": cnpj,
        "codigo_cvm": cvm_code,
        "nome_companhia": nome,
        "categoria": categoria,
        "tipo": tipo,
        "especie": rec.get("Especie"),
        "assunto": assunto,
        "data_entrega_brt": data_entrega,
        "data_referencia_brt": data_referencia,
        "tipo_apresentacao": rec.get("Tipo_Apresentacao"),
        "protocolo": protocolo,
        "versao": versao,
        "link_download": link or None,
        # Denormalised fields for rubric / reactor / dashboard:
        "headline": headline,
        "summary": assunto or f"{categoria} / {tipo}",
        "filing_url": link or None,
        "company_name_en": nome,
        "ticker": None,       # CVM dataset has no ticker
        "mic": "BVMF",        # Brazilian B3 MIC; downstream resolver may refine
    }

    entity_hints = EntityHints(
        codigo_cvm=cvm_code or None,
        cnpj=cnpj or None,
        name=nome or None,
        country="BR",
    )

    return Signal(
        signal_id=signal_id,
        source_content_hash=source_content_hash,
        source_date=source_date,
        scan_date=scan_date,
        signal_type=signal_type,
        raw_payload=raw_payload,
        source_url=link or None,
        entity_hints=entity_hints,
        scoring_profile=profile,
        thesis_direction=direction,
    )


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    client = SupabaseClient()
    scan_date = datetime.now(timezone.utc)
    scan_start = time.time()
    budget = max(10, cfg.timeout_soft_s - 5)

    today = scan_date.date()
    cutoff = today - timedelta(days=LOOKBACK_DAYS)

    warnings: List[str] = []

    # --- Fetch (cached-by-day) ---
    content, err, cache_hit = _load_zip(client, today.year)
    if err and today.month == 1 and today.day < 15:
        # Year-boundary fallback: try previous year when January early-days fetch fails.
        warnings.append(err)
        content, err, cache_hit = _load_zip(client, today.year - 1)
    if err:
        return ScannerResult(
            scanner=NAME,
            status="error",
            signals=[],
            warnings=warnings + [err],
            fetched_records=0,
            error=err,
        )
    # cache_hit intentionally not surfaced as a warning — it's the happy path.
    _ = cache_hit

    # --- Parse ---
    records, parse_err = _parse_zip_to_records(content or b"")
    if parse_err:
        return ScannerResult(
            scanner=NAME,
            status="error",
            signals=[],
            warnings=warnings + [parse_err],
            fetched_records=0,
            error=parse_err,
        )

    total_rows = len(records)

    # --- Window + category prefilter ---
    recent_records: List[Dict[str, str]] = []
    for rec in records:
        if time.time() - scan_start > budget:
            warnings.append(f"wall-clock budget ({budget}s) exceeded during prefilter")
            break
        de = (rec.get("Data_Entrega") or "").strip()
        if not de:
            continue
        try:
            d = datetime.strptime(de, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff:
            continue
        if (rec.get("Categoria") or "").strip() not in TARGET_CATEGORIES:
            continue
        recent_records.append(rec)

    # --- Build signals ---
    signals: List[Signal] = []
    seen_hashes: set[str] = set()
    for rec in recent_records:
        if time.time() - scan_start > budget:
            warnings.append(f"wall-clock budget ({budget}s) exceeded during signal build")
            break
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
        fetched_records=total_rows,
    )
