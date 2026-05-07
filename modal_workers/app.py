"""
Modal app definition for Conan v2.

Surface (Phase 3 — full scanner fleet):
  - `rubric_apply_caps`  — web endpoint RPC'd by the reactor edge function on every
    signals.INSERT to apply auto-caps without porting rubric logic to TypeScript.
  - `health`             — trivial GET for dashboard + smoke tests.
  - 19 scanner functions — each as `<name>_once` (on-demand callable).
  - 3 dispatcher crons   — `dispatch_3h`, `dispatch_release_times`, `dispatch_weekly`.
    `dispatch_release_times` fires at 06/08/13/17/21 UTC and reads
    `scanners.scheduled_hour_utc` from the registry to decide which daily
    scanners to spawn at each tick. Per-scanner isolation preserved (each
    scanner runs in its own container with its own timeout).

NOT hosted here (by design, spec.md §7.4 revised 2026-04-20):
  - `thesis_writer`      — runs as a Claude skill under Pedro's account via a Cowork
    scheduled task (see `.claude/skills/thesis_writer.md`). Modal doesn't draft theses.
  - `candidate_aging`    — same pattern; a separate skill.

Scheduled dispatch (2026-04-22 release-time amendment, spec.md §7):
  - 3h:      edgar_filing_monitor (SEC filings land throughout US day)
  - weekly:  takeover_candidate_scanner (Mon 12:00 UTC)
  - daily release-time buckets — queried from registry per tick:
      06 UTC  EU pre-open           (lse_rns, esma_short, bse_nse)
      08 UTC  APAC post-close       (asx, tdnet, hkex, kind)
      13 UTC  US pre-open / Americas (fda_pdufa, cvm, sedar_plus, bmv) + fetchers
      17 UTC  US midday              (congressional_trading)
      21 UTC  US post-close          (sec_enforcement, courtlistener)

Only rows with `status='operational'` fire; paused rows are skipped inside
`_dispatch`. Adding a new scanner = INSERT row with a `scheduled_hour_utc`;
no code redeploy needed for timing tweaks.

Secret requirements (populate via `modal secret create scanner-secrets ...`):
  - SEC_USER_AGENT          — required by edgar, fda_pdufa, takeover_candidate,
                              sec_enforcement. Must be a valid contact string.
  - COURTLISTENER_TOKEN     — optional; courtlistener emits auth_required without it.
  - OPENDART_KEY            — optional; kind_scanner emits auth_required without it.
  - OPENFIGI_API_KEY        — optional; openfigi_resolver falls back to anonymous tier.

Deploy:   modal deploy modal_workers/app.py
Status:   modal app list
Logs:     modal app logs conan-v2
Trigger:  modal run modal_workers/app.py::<scanner_name>_once
"""

from __future__ import annotations

from typing import List, Optional

import modal
from fastapi import Header, HTTPException

app = modal.App("conan-v2")

# Base image — one image for the whole fleet. Modal caches aggressively, so a shared
# image is simpler than per-scanner slim images.
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "fastapi[standard]",
        "pydantic>=2",
        "requests>=2.31",
        "beautifulsoup4>=4.12",   # congressional_trading, esma_short_scanner
        "openpyxl>=3.1",          # esma_short_scanner (FCA xlsx)
        "yfinance>=0.2",          # sedar_plus_scanner, asx_scanner (ticker→mcap proxies)
        "reportlab>=4.0",         # reporting_weekly (PDF render)
    )
    .add_local_python_source("modal_workers")
)

# Secrets — populate via Modal Dashboard or `modal secret create`.
scanner_secrets = modal.Secret.from_name("scanner-secrets")       # SEC_USER_AGENT, OPENFIGI_API_KEY, COURTLISTENER_TOKEN, OPENDART_KEY
supabase_secrets = modal.Secret.from_name("supabase-secrets")     # SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
compute_auth_secrets = modal.Secret.from_name("compute-auth")     # CONAN_COMPUTE_SECRET — shared with Supabase internal_config.compute_secret
# anthropic-secrets intentionally NOT referenced here. thesis_writer + candidate_aging
# run as Claude skills via Cowork scheduled tasks, not as Modal functions.


# ----------------------------------------------------------------------
# rubric_apply_caps — called by the reactor edge function on every signals.INSERT.
# Wraps the Python apply_auto_caps so no rubric logic is ported to TypeScript.
# ----------------------------------------------------------------------

@app.function(image=image, timeout=10)
@modal.fastapi_endpoint(method="POST", label="rubric-apply-caps")
def rubric_apply_caps(payload: dict) -> dict:
    from modal_workers.shared.rubric_engine import apply_auto_caps, compute_demotion_reason
    signal = payload.get("signal") or {}
    dimensions = payload.get("dimensions") or {}
    profile = payload["profile"]
    band = payload["band"]
    new_band, caps = apply_auto_caps(signal, dimensions, profile, band)
    return {
        "band": new_band,
        "auto_caps_triggered": caps,
        "demotion_reason": compute_demotion_reason(caps),
    }


# ----------------------------------------------------------------------
# health — trivial liveness check.
#
# 2026-05-08 — demoted from `@modal.fastapi_endpoint` (label='health',
# URL https://marazuela--health.modal.run) to a plain Modal function to
# free one of the workspace's 8 free-tier `fastapi_endpoint` slots so
# conan-v3-orchestrator's `compute-v3` multiplex can deploy. No code in
# this repo or the Cowork skills called the HTTP endpoint — it was a
# manual smoke test only ("doubles as smoke test after deploy" per its
# original comment). Manual smoke now: `modal run conan-v2::health`.
# (The local engine `health_check.py` is unrelated; it never hit this
# endpoint.)
# ----------------------------------------------------------------------

@app.function(image=image, timeout=10)
def health() -> dict:
    from modal_workers.shared.rubric_engine import WEIGHTS
    return {
        "status": "ok",
        "app": "conan-v2",
        "profiles": sorted(WEIGHTS.keys()),
    }


# ==========================================================================
# Skill compute endpoints — called by Cowork-scheduled skills (signal_resolver,
# thesis_writer, candidate_aging, coverage_auditor) via pg_net-backed Postgres
# RPCs (supabase/migrations/*_compute_rpcs.sql). Introduced 2026-04-22 when the
# Cowork Linux sandbox stopped starting, breaking every skill that shelled out
# to `python3 -c` or `curl`. Skills now call `rpc_<name>` via the Supabase MCP,
# the RPC POSTs here via pg_net, we invoke the pure Python helpers, response
# flows back to the skill as jsonb.
#
# Auth: every endpoint requires `x-conan-compute-secret` matching
# `CONAN_COMPUTE_SECRET` from the `compute-auth` Modal secret. The Supabase
# side reads the same value from `public.internal_config` (key='compute_secret')
# and injects it via `_conan_modal_post`. Rotating the secret requires both
# sides updated in lockstep.
# ==========================================================================

ALLOWED_STORAGE_BUCKETS = frozenset({"reports", "candidates"})
MAX_STORAGE_CONTENT_BYTES = 5 * 1024 * 1024  # 5 MiB; coverage reports + candidate dossiers are <200 KB in practice.


def _verify_compute_secret(provided: Optional[str]) -> None:
    """Raise HTTPException if `provided` doesn't match CONAN_COMPUTE_SECRET.

    401 on bad/missing header, 500 on server misconfiguration. Constant-time
    compare so an attacker can't learn the prefix byte-by-byte.
    """
    import hmac
    import os
    expected = os.environ.get("CONAN_COMPUTE_SECRET", "")
    if not expected:
        raise HTTPException(
            status_code=500,
            detail={"error": "server misconfiguration: CONAN_COMPUTE_SECRET not set"},
        )
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid or missing x-conan-compute-secret"},
        )


def _validate_storage_upload(payload: dict) -> None:
    """Guard storage_upload_endpoint: bucket allowlist, path sanity, size cap.

    Raises HTTPException(400) on invalid bucket/path/content shape and
    HTTPException(413) on oversize content. Size check is O(1) against the
    encoded length; a 5 MiB cap is well above any legitimate caller.
    """
    bucket = payload.get("bucket")
    if bucket not in ALLOWED_STORAGE_BUCKETS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "bucket not allowed",
                "bucket": bucket,
                "allowed": sorted(ALLOWED_STORAGE_BUCKETS),
            },
        )
    path = payload.get("path")
    if not isinstance(path, str) or not path:
        raise HTTPException(status_code=400, detail={"error": "path must be a non-empty string"})
    if path.startswith("/") or ".." in path.split("/"):
        raise HTTPException(status_code=400, detail={"error": "invalid path", "path": path})
    content = payload.get("content")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail={"error": "content must be a string"})
    size = len(content.encode("utf-8"))
    if size > MAX_STORAGE_CONTENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"error": "content too large", "size_bytes": size, "max_bytes": MAX_STORAGE_CONTENT_BYTES},
        )


@app.function(image=image, timeout=30, secrets=[compute_auth_secrets])
@modal.fastapi_endpoint(method="POST", label="rescore-with-dims")
def rescore_with_dims_endpoint(
    payload: dict,
    x_conan_compute_secret: Optional[str] = Header(default=None),
) -> dict:
    """Wraps `modal_workers.shared.rubric_engine.rescore_with_dims` for signal_resolver.

    payload:  {scoring_profile, raw_payload, dims, provenance?}
    returns:  {scoring_profile, dimensions, dimensions_with_provenance, score, band, auto_caps_triggered}
    """
    _verify_compute_secret(x_conan_compute_secret)
    from modal_workers.shared.rubric_engine import rescore_with_dims
    return rescore_with_dims(
        scoring_profile=payload["scoring_profile"],
        raw_payload=payload.get("raw_payload") or {},
        dims=payload["dims"],
        provenance=payload.get("provenance", "ai_resolved"),
    )


@app.function(image=image, timeout=30, secrets=[compute_auth_secrets])
@modal.fastapi_endpoint(method="POST", label="assess-thesis")
def assess_thesis_endpoint(
    payload: dict,
    x_conan_compute_secret: Optional[str] = Header(default=None),
) -> dict:
    """Wraps `modal_workers.shared.candidate_gate.assess_thesis_v2` for thesis_writer + signal_resolver.

    payload:  {thesis: {...}}
    returns:  {ok: bool, reasons: [str]}
    """
    _verify_compute_secret(x_conan_compute_secret)
    from modal_workers.shared.candidate_gate import assess_thesis_v2
    ok, reasons = assess_thesis_v2(payload.get("thesis"))
    return {"ok": ok, "reasons": reasons}


@app.function(image=image, timeout=30, secrets=[compute_auth_secrets])
@modal.fastapi_endpoint(method="POST", label="render-candidate-markdown")
def render_candidate_markdown_endpoint(
    payload: dict,
    x_conan_compute_secret: Optional[str] = Header(default=None),
) -> dict:
    """Wraps `modal_workers.shared.candidate_gate.render_candidate_markdown_v2` for thesis_writer.

    payload:  {signal, thesis, band, scoring_profile, entity?}
    returns:  {markdown: str}
    """
    _verify_compute_secret(x_conan_compute_secret)
    from modal_workers.shared.candidate_gate import render_candidate_markdown_v2
    md = render_candidate_markdown_v2(
        payload.get("signal") or {},
        payload.get("thesis") or {},
        band=payload["band"],
        scoring_profile=payload.get("scoring_profile"),
        entity=payload.get("entity"),
    )
    return {"markdown": md}


@app.function(image=image, timeout=30, secrets=[compute_auth_secrets])
@modal.fastapi_endpoint(method="POST", label="regex-check")
def regex_check_endpoint(
    payload: dict,
    x_conan_compute_secret: Optional[str] = Header(default=None),
) -> dict:
    """Python `re.search` for candidate_aging step 6 integrity check.

    Case-insensitive by default UNLESS the pattern already embeds an inline flag
    group in its first 5 chars (e.g. `(?i)`, `(?im)`, `^(?i)`). Matches the
    exact rule candidate_aging.md step 6 used in bash before the sandbox outage.

    payload:  {pattern: str, text: str}
    returns:  {matched: bool, match: str | null}
    """
    _verify_compute_secret(x_conan_compute_secret)
    import re
    pattern = payload["pattern"]
    text = payload["text"]
    flags = 0 if "(?" in pattern[:5] else re.IGNORECASE
    m = re.search(pattern, text, flags)
    return {"matched": m is not None, "match": m.group(0) if m else None}


@app.function(image=image, timeout=45, secrets=[scanner_secrets, supabase_secrets, compute_auth_secrets])
@modal.fastapi_endpoint(method="POST", label="edgar-fetch")
def edgar_fetch_endpoint(
    payload: dict,
    x_conan_compute_secret: Optional[str] = Header(default=None),
) -> dict:
    """Multi-kind compute-fetch endpoint for signal_resolver.

    Hosts two "kinds" under one fastapi_endpoint to stay within Modal's
    per-app web-endpoint cap (8 on the current plan). Dispatch happens on
    `payload.kind`; default is 'edgar_fetch' for backward-compat with the
    existing rpc_edgar_fetch RPC (which doesn't send `kind`).

    The URL is kept as `edgar-fetch` despite the expanded scope because
    rotating the label would break in-flight rpc_edgar_fetch calls during
    deploy. The internal_config entries for each kind point at this one URL.

    kind='edgar_fetch' (default):
      Fetch an SEC/EDGAR URL with the SEC_USER_AGENT header. Required because
      WebFetch's default UA is 403'd by sec.gov under SEC's fair-access policy.
      payload:  {url: str, max_bytes?: int (default 2_000_000)}
      returns:  {status, content, content_type, final_url, truncated}

    kind='market_snapshot':
      Fetch a live yfinance snapshot (mcap, ADV, valuation cushion). Used by
      the litigation profile's financial_materiality dim because
      entities.market_cap_usd has no writer (100% NULL).
      payload:  {ticker: str, mic?: str}
      returns:  load_market_snapshot result dict, always with source_liveness
    """
    _verify_compute_secret(x_conan_compute_secret)

    kind = payload.get("kind") or "edgar_fetch"

    if kind == "edgar_fetch":
        return _do_edgar_fetch(payload)
    if kind == "market_snapshot":
        return _do_market_snapshot(payload)
    raise ValueError(f"edgar_fetch_endpoint: unknown kind {kind!r}")


def _do_edgar_fetch(payload: dict) -> dict:
    import os
    import urllib.parse
    import urllib.request
    import urllib.error

    url = payload.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("edgar_fetch: `url` is required and must be a string")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"edgar_fetch: unsupported scheme {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not (host == "sec.gov" or host.endswith(".sec.gov")):
        raise ValueError(f"edgar_fetch: only sec.gov hosts allowed; got {host!r}")

    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        raise RuntimeError(
            "edgar_fetch: SEC_USER_AGENT env var missing — scanner-secrets not attached")

    max_bytes = int(payload.get("max_bytes") or 2_000_000)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml,text/plain,*/*",
            "Accept-Encoding": "identity",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read(max_bytes + 1)
            truncated = len(raw) > max_bytes
            if truncated:
                raw = raw[:max_bytes]
            content_type = resp.headers.get("Content-Type", "") or ""
            charset = "utf-8"
            if "charset=" in content_type.lower():
                charset = content_type.split("charset=")[-1].split(";")[0].strip() or "utf-8"
            content = raw.decode(charset, errors="replace")
            return {
                "status": resp.status,
                "content": content,
                "content_type": content_type,
                "final_url": resp.url,
                "truncated": truncated,
            }
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(2000).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"edgar_fetch: HTTP {e.code} from {url} — {body[:500]}")


def _do_market_snapshot(payload: dict) -> dict:
    from modal_workers.shared.market_snapshot import load_market_snapshot
    from modal_workers.shared.supabase_client import SupabaseClient
    ticker = payload.get("ticker")
    if not isinstance(ticker, str) or not ticker.strip():
        raise ValueError("market_snapshot: `ticker` is required and must be a non-empty string")
    mic = payload.get("mic")
    if mic is not None and not isinstance(mic, str):
        raise ValueError("market_snapshot: `mic` must be a string or omitted")
    snapshot = load_market_snapshot(ticker.strip(), mic=mic, client=SupabaseClient())
    return snapshot or {"source_liveness": "unavailable", "ticker": ticker}


@app.function(image=image, timeout=60, secrets=[supabase_secrets, compute_auth_secrets])
@modal.fastapi_endpoint(method="POST", label="storage-upload")
def storage_upload_endpoint(
    payload: dict,
    x_conan_compute_secret: Optional[str] = Header(default=None),
) -> dict:
    """PUT a string payload to Supabase Storage with service-role auth.

    Callers (2026-04-22):
      - coverage_auditor: {bucket: "reports", path: "coverage/<iso_week>.md", content, content_type: "text/markdown"}
      - thesis_writer:    {bucket: "candidates", path: "<YYYY>/<MM>/<ticker>_<signal_id>.md", content, content_type: "text/markdown"}

    payload:  {bucket, path, content, content_type?}
    returns:  {uploaded: true, bucket, path, size_bytes}
    """
    _verify_compute_secret(x_conan_compute_secret)
    _validate_storage_upload(payload)

    import os
    import requests

    bucket = payload["bucket"]
    path = payload["path"]
    content = payload["content"]
    content_type = payload.get("content_type", "text/markdown")

    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    url = f"{os.environ['SUPABASE_URL']}/storage/v1/object/{bucket}/{path}"
    body = content.encode("utf-8")

    # v2 (2026-04-22) — surface upstream Storage errors instead of raise_for_status.
    r = requests.put(
        url,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": content_type,
            "x-upsert": "true",
        },
        data=body,
        timeout=30,
    )
    if not r.ok:
        raise HTTPException(
            status_code=502,
            detail={
                "version": "v2",
                "upstream_status": r.status_code,
                "upstream_body": r.text[:1000],
                "url": url,
            },
        )
    return {
        "uploaded": True,
        "bucket": bucket,
        "path": path,
        "size_bytes": len(body),
    }


# ----------------------------------------------------------------------
# Scanner runner — each `_once` wraps run_scanner(name). Lazy import so image
# build doesn't need the full module graph resolved.
# ----------------------------------------------------------------------

def _run(scanner_name: str) -> dict:
    from importlib import import_module
    from modal_workers.shared.scanner_base import run_scanner, result_to_dict
    scan = getattr(import_module(f"modal_workers.scanners.{scanner_name}"), "scan")
    result = run_scanner(scanner_name, scan)
    return result_to_dict(result)


def _run_fetcher(fetcher_module: str, *, days_back: int = 7) -> dict:
    """Runner for catalyst_universe fetchers (modal_workers/fetchers/universe/*).

    Fetchers don't use scanner_base/scanner_runs — they write directly to
    catalyst_universe. Contract: `fetch(client, *, start_date, end_date) -> dict`.
    """
    from datetime import date, timedelta
    from importlib import import_module
    from modal_workers.shared.supabase_client import SupabaseClient

    mod = import_module(f"modal_workers.fetchers.universe.{fetcher_module}")
    end = date.today()
    start = end - timedelta(days=days_back)
    return mod.fetch(SupabaseClient(), start_date=start, end_date=end)


# ==========================================================================
# 17 on-demand scanner functions (not scheduled — fired by dispatchers below
# or via `modal run modal_workers/app.py::<name>_once`).
# Timeouts match scanners.timeout_hard_s in the registry.
# ==========================================================================

# --- 3h cadence ---

@app.function(image=image, timeout=180, secrets=[scanner_secrets, supabase_secrets])
def edgar_filing_monitor_once() -> dict:
    # Flagship EDGAR now runs budgeted full coverage by default (not one rotating
    # category only), with issuer filtering, market-cap triage, retries, and
    # structured telemetry. Give it room above the soft budget so it can finish
    # filing-type coverage and persist after_insert state safely.
    return _run("edgar_filing_monitor")

@app.function(image=image, timeout=240, secrets=[scanner_secrets, supabase_secrets])
def fda_pdufa_pipeline_once() -> dict:
    # Soft budget bumped 60→120s + Modal hard timeout 120→240s. Two recent runs
    # (2026-04-28, 2026-04-30) hit "wall-clock budget exceeded during signal build"
    # at the 60s soft mark, dropping watchlist tail entries silently. With ~45-entry
    # watchlists + per-entry OpenFIGI + market-snapshot lookups, 60s is too tight on
    # cold caches. Registry timeout_soft_s/hard_s updated to match.
    return _run("fda_pdufa_pipeline")


@app.function(image=image, timeout=600, secrets=[scanner_secrets, supabase_secrets])
def fda_signal_bridge_once() -> dict:
    # Iterates pending fda_regulatory_events, calls process_event +
    # upsert_feature_snapshot per row. Mode (shadow / shadow_with_emit /
    # operational) is read from scanners.config.mode at run time. Polygon
    # providers are best-effort: missing POLYGON_API_KEY leaves market+options
    # None and degrades to fair_probability-only scoring with Immediate gated off.
    return _run("fda_signal_bridge")

@app.function(image=image, timeout=120, secrets=[scanner_secrets, supabase_secrets])
def lse_rns_scanner_once() -> dict:
    return _run("lse_rns_scanner")

@app.function(image=image, timeout=120, secrets=[scanner_secrets, supabase_secrets])
def tdnet_scanner_once() -> dict:
    return _run("tdnet_scanner")

@app.function(image=image, timeout=240, secrets=[scanner_secrets, supabase_secrets])
def asx_scanner_once() -> dict:
    # asx needs 240s (not 120): per-ticker Markit concurrent fetch across the
    # rotation chunk routinely exceeds 90s on cold cache. Registry updated to match.
    return _run("asx_scanner")


# --- daily cadence ---

@app.function(image=image, timeout=1200, secrets=[scanner_secrets, supabase_secrets])
def esma_short_scanner_once() -> dict:
    # 4 regulators × xlsx/csv fetch + ISIN dedup + OpenFIGI resolve + per-signal
    # entity resolution. Budget history: 120→240→480→1200s.
    # The 480s budget assumed ~80 emitted signals/run, but cold-start emits ~2000+
    # (every holder+ISIN with pct ≥ 0.5). scanner_base's per-signal resolve_or_create_entity
    # loop does 1-3 DB round trips × 2233 positions = ~400-500s in EU-West → eu-west-3.
    # 1200s covers cold-start; warm runs (only |change_pct| ≥ 0.2 positions emit) finish
    # in <120s. Registry timeout_soft_s/hard_s also bumped. Bulk-resolve refactor in
    # scanner_base is the real long-term fix.
    return _run("esma_short_scanner")

@app.function(image=image, timeout=180, secrets=[scanner_secrets, supabase_secrets])
def congressional_trading_once() -> dict:
    # 20 pages × 1s polite delay + BS4 parse + OpenFIGI per ticker. 120s
    # insufficient; bumped to 180s (registry also updated).
    return _run("congressional_trading")

@app.function(image=image, timeout=120, secrets=[scanner_secrets, supabase_secrets])
def sedar_plus_scanner_once() -> dict:
    return _run("sedar_plus_scanner")

@app.function(image=image, timeout=120, secrets=[scanner_secrets, supabase_secrets])
def hkex_scanner_once() -> dict:
    return _run("hkex_scanner")

@app.function(image=image, timeout=120, secrets=[scanner_secrets, supabase_secrets])
def kind_scanner_once() -> dict:
    return _run("kind_scanner")

@app.function(image=image, timeout=120, secrets=[scanner_secrets, supabase_secrets])
def bse_nse_scanner_once() -> dict:
    return _run("bse_nse_scanner")

@app.function(image=image, timeout=120, secrets=[scanner_secrets, supabase_secrets])
def cvm_scanner_once() -> dict:
    return _run("cvm_scanner")

@app.function(image=image, timeout=60, secrets=[scanner_secrets, supabase_secrets])
def bmv_scanner_once() -> dict:
    return _run("bmv_scanner")

@app.function(image=image, timeout=120, secrets=[scanner_secrets, supabase_secrets])
def courtlistener_scanner_once() -> dict:
    return _run("courtlistener_scanner")

@app.function(image=image, timeout=60, secrets=[scanner_secrets, supabase_secrets])
def sec_enforcement_scanner_once() -> dict:
    return _run("sec_enforcement_scanner")

@app.function(image=image, timeout=240, secrets=[scanner_secrets, supabase_secrets])
def insider_form4_scanner_once() -> dict:
    # Per-filing XML fetch: EFTS list (1 call) + primary_doc.xml fetch per hit
    # (~500 cap). At 9 req/s SEC ceiling with connection pooling, cold runs land
    # in ~80-120s; keep timeout at 240s for variance. Registry timeout_hard_s
    # matches.
    return _run("insider_form4_scanner")

@app.function(image=image, timeout=180, secrets=[scanner_secrets, supabase_secrets])
def delaware_chancery_scanner_once() -> dict:
    return _run("delaware_chancery_scanner")


# --- weekly cadence ---

@app.function(image=image, timeout=300, secrets=[scanner_secrets, supabase_secrets])
def takeover_candidate_scanner_once() -> dict:
    # Multi-pattern EDGAR merge across 45d PE-filer + 60d review/streamlined
    # windows + post-edge disqualification lookups. 180s insufficient on cold
    # caches; bumped to 300s (registry also updated).
    return _run("takeover_candidate_scanner")

@app.function(image=image, timeout=180, secrets=[scanner_secrets, supabase_secrets])
def pre_phase3_readout_scanner_once() -> dict:
    return _run("pre_phase3_readout_scanner")


# --- v3 RAG corpus ingest (signal-less; daily registry-driven, deep on Sun) ---

@app.function(image=image, timeout=900, secrets=[scanner_secrets, supabase_secrets])
def openfda_corpus_ingest_once() -> dict:
    # Registry-driven via dispatch_release_times: scanners.scheduled_hour_utc=6
    # routes this to the 06 UTC bucket. The scanner auto-switches to a 180d
    # deep sweep on Sundays so a single registry row covers both shallow
    # daily ingest and the weekly backfill catch-up. timeout=900s sized for
    # the 180d sweep (shallow 30d runs in <60s).
    return _run("openfda_corpus_ingest")


@app.function(image=image, timeout=900, secrets=[scanner_secrets, supabase_secrets])
def openfda_corpus_ingest_deep() -> dict:
    # Manual override entry point — sets OPENFDA_INGEST_MODE=deep so a non-Sunday
    # ad-hoc run still triggers the 180d sweep. Not on a schedule; invoke via
    # `modal run modal_workers/app.py::openfda_corpus_ingest_deep`.
    import os
    os.environ["OPENFDA_INGEST_MODE"] = "deep"
    return _run("openfda_corpus_ingest")


# ==========================================================================
# Catalyst-universe fetchers (Phase 1b of the accuracy feedback loop).
# Populate catalyst_universe with independent-truth catalyst events, which
# the coverage_auditor (Cowork weekly skill) joins against emissions_ledger
# to identify recall gaps.
# ==========================================================================

@app.function(image=image, timeout=300, secrets=[scanner_secrets, supabase_secrets])
def fda_adcomm_pdufa_once() -> dict:
    """openFDA drugsfda AP submissions → catalyst_universe (fda_approval).
    Default 7-day look-back; scheduled via dispatch_release_times 13 UTC bucket."""
    return _run_fetcher("fda_adcomm_pdufa", days_back=7)


@app.function(image=image, timeout=300, secrets=[scanner_secrets, supabase_secrets])
def sec_8k_mna_once() -> dict:
    """EDGAR 8-K items 1.01 / 2.01 → catalyst_universe (mna_announce / mna_close).
    Requires SEC_USER_AGENT from scanner-secrets."""
    return _run_fetcher("sec_8k_mna", days_back=7)


# ==========================================================================
# reporting_weekly — spec §7.3 + §7.7 integrity sweep. Sunday 12:00 UTC cron.
#   1. SQL RPC `reporting_integrity_sweep()` (migration 23) — UPSERTs
#      operator_flags for orphan alerts, stuck-active candidates, stuck-drafting
#      thesis_jobs.
#   2. Render single-page executive PDF (candidates + weekly stats).
#   3. Upload to reports/<yyyy>/<mm>/<date>_executive_summary.pdf.
# ==========================================================================

@app.function(image=image, schedule=modal.Cron("0 12 * * 0"), timeout=300,
              secrets=[supabase_secrets])
def reporting_weekly_cron() -> dict:
    """Sunday 12:00 UTC weekly report + integrity sweep."""
    from modal_workers.reporting import reporting_weekly
    return reporting_weekly()


@app.function(image=image, timeout=300, secrets=[supabase_secrets])
def reporting_weekly_once() -> dict:
    """On-demand equivalent; same work as the cron, callable manually via
    `modal.Function.from_name('conan-v2', 'reporting_weekly_once').remote()`."""
    from modal_workers.reporting import reporting_weekly
    return reporting_weekly()


# ==========================================================================
# 3 dispatcher crons — the only scheduled functions in the app. Each spawns
# the `_once` variants of its bucket in parallel so per-scanner isolation
# (container, timeout) is preserved. Dispatcher returns as soon as all spawns
# are queued; spawned functions run independently.
# ==========================================================================

_DEFAULT_SCANNERS_3H: List[str] = [
    "edgar_filing_monitor",
]
_DEFAULT_SCANNERS_WEEKLY: List[str] = [
    "takeover_candidate_scanner",
]


def _load_cadence_names(cadence: str, fallback: List[str]) -> tuple[List[str], Optional[str]]:
    """Resolve operational scanner names for a cadence from the registry, falling
    back to a hardcoded list if the registry lookup fails. Returns (names, error).

    Registry is the source of truth — adding/removing a 3h or weekly scanner is a
    single UPDATE on public.scanners, no redeploy. The hardcoded fallback only
    fires if the registry GET errors (network, auth) so a transient failure
    doesn't leave the cron empty.
    """
    try:
        from modal_workers.shared.supabase_client import SupabaseClient
        names = SupabaseClient().load_operational_names_by_cadence(cadence)
        return names, None
    except Exception as e:  # noqa: BLE001
        return list(fallback), f"{type(e).__name__}: {e}"

# Catalyst-universe fetchers are NOT registry-backed scanners — they have no
# row in public.scanners, so _dispatch_by_hour can't pick them up. Hardcoded
# to the 13 UTC (US pre-open) bucket alongside registry-driven daily scanners.
# Fold in here so dispatch_release_times fires them at the right tick.
_FETCHERS_AT_HOUR: dict[int, List[str]] = {
    13: ["fda_adcomm_pdufa", "sec_8k_mna"],
}

# Registry-backed scanners that need a SECOND firing within the same day on top
# of their `scanners.scheduled_hour_utc` primary slot. Used for catalysts whose
# freshness window is shorter than 24h. Code-level (not schema) because the
# need is currently exactly one scanner; promote to a column when ≥3 scanners
# need it.
#
# fda_pdufa_pipeline: post-Phase-2 the scanner detects CRLs from 8-K filings
# that typically land 13–22 UTC (US business hours). Once-daily-at-13 misses
# afternoon CRLs by ~23h, gating the short-thesis email by a full day. A 21
# UTC secondary slot (post-close US) captures the 13:01–21:00 window with
# <1h latency.
_SCANNERS_SECONDARY_HOUR: dict[int, List[str]] = {
    21: ["fda_pdufa_pipeline"],
}


def _load_dispatch_statuses(names: List[str]) -> tuple[dict[str, str], Optional[str]]:
    if not names:
        return {}, None
    try:
        from modal_workers.shared.supabase_client import SupabaseClient
        return SupabaseClient().load_scanner_statuses(names), None
    except Exception as e:  # noqa: BLE001 — status gating should not block the bucket
        return {}, f"{type(e).__name__}: {e}"


def _dispatch(names: List[str]) -> dict:
    """Spawn the `_once` variant of each scanner in `names`. Returns a summary
    envelope; spawned functions run concurrently in their own containers.

    Pre-flight: sweeps orphaned `scanner_runs.status='running'` rows (Modal hard-
    timeouts leave these behind when a container is killed before the scanner can
    call close_scanner_run). Threshold 1200s = 20 min, comfortably above the longest
    hard_timeout (takeover_candidate at 300s) while still catching real orphans on
    the same day. Sweep failures don't block spawning — just logged in `errors`.
    """
    import sys
    me = sys.modules[__name__]

    reaped: List[dict] = []
    reaper_error: Optional[str] = None
    try:
        from modal_workers.shared.supabase_client import SupabaseClient
        reaped = SupabaseClient().reap_orphan_runs(max_age_seconds=1200)
    except Exception as e:  # noqa: BLE001 — reaper is advisory; don't block dispatch
        reaper_error = f"{type(e).__name__}: {e}"

    statuses, status_lookup_error = _load_dispatch_statuses(names)
    spawned = []
    skipped = []
    errors = []
    for name in names:
        status = statuses.get(name)
        if status is not None and status != "operational":
            skipped.append({
                "scanner": name,
                "status": status,
                "reason": f"registry status={status}",
            })
            continue
        fn = getattr(me, f"{name}_once", None)
        if fn is None:
            errors.append({"scanner": name, "error": "function not found"})
            continue
        try:
            call = fn.spawn()
            spawned.append({"scanner": name, "call_id": getattr(call, "object_id", None)})
        except Exception as e:
            errors.append({"scanner": name, "error": str(e)})
    envelope = {"spawned": spawned, "skipped": skipped, "errors": errors, "count": len(spawned),
                "reaped_orphan_runs": len(reaped)}
    if reaped:
        envelope["reaped_sample"] = reaped[:5]
    if reaper_error:
        envelope["reaper_error"] = reaper_error
    if status_lookup_error:
        envelope["status_lookup_error"] = status_lookup_error
    return envelope


@app.function(image=image, schedule=modal.Period(hours=3), timeout=60,
              secrets=[scanner_secrets, supabase_secrets])
def dispatch_3h() -> dict:
    names, registry_error = _load_cadence_names("3h", _DEFAULT_SCANNERS_3H)
    envelope = _dispatch(names)
    if registry_error:
        envelope["registry_error"] = registry_error
    return envelope


@app.function(image=image, schedule=modal.Cron("0 6,8,13,17,21 * * *"), timeout=60,
              secrets=[scanner_secrets, supabase_secrets])
def dispatch_release_times() -> dict:
    """Release-time-aware daily dispatcher. Fires at 06/08/13/17/21 UTC and
    spawns only scanners whose `scheduled_hour_utc` matches the current hour
    (NULL rows default to the 13 UTC bucket). Registry-driven — retiming a
    scanner is a single UPDATE on public.scanners, no redeploy.
    """
    from datetime import datetime, timezone
    from modal_workers.shared.supabase_client import SupabaseClient

    hour = datetime.now(timezone.utc).hour
    try:
        registry_names = SupabaseClient().load_operational_daily_names_for_hour(hour)
    except Exception as e:  # noqa: BLE001 — if the registry lookup fails, fetchers still fire
        registry_names = []
        registry_error = f"{type(e).__name__}: {e}"
    else:
        registry_error = None

    names = (
        list(registry_names)
        + _FETCHERS_AT_HOUR.get(hour, [])
        + _SCANNERS_SECONDARY_HOUR.get(hour, [])
    )
    # De-dupe in case a scanner accidentally appears twice (registry primary +
    # secondary at the same hour, or fetcher overlap).
    seen: set[str] = set()
    deduped: List[str] = []
    for n in names:
        if n in seen:
            continue
        seen.add(n)
        deduped.append(n)
    envelope = _dispatch(deduped)
    envelope["hour_utc"] = hour
    if registry_error:
        envelope["registry_error"] = registry_error
    return envelope


@app.function(image=image, schedule=modal.Cron("0 12 * * 1"), timeout=60,
              secrets=[scanner_secrets, supabase_secrets])
def dispatch_weekly() -> dict:
    names, registry_error = _load_cadence_names("weekly", _DEFAULT_SCANNERS_WEEKLY)
    envelope = _dispatch(names)
    if registry_error:
        envelope["registry_error"] = registry_error
    return envelope


# ==========================================================================
# Observability dispatcher (spec §7.6). One cron slot covers all sweeps
# to stay under Modal's 5-cron plan limit.
#
#   Every 6h at :15 UTC (02,08,14,20):  scanner_probe  (§7.6.2)
#   Every 6h at :15 UTC (02,08,14,20):  pre_edge_monitor (deterministic lifecycle guard)
#   02:15 UTC window also runs:         translation_health (§7.6.1)
#   02:15 UTC window also runs:         convergence_qa (§7.6.3)
#   02:15 UTC window also runs:         legal_enrichment / biotech_enrichment sweeps
#   Sun 02:15 UTC window also:          litigation_baselines_refresh (§7.6.4)
#
# Each writes to `operator_flags`. No Claude calls — all mechanical.
#
# Cron history: shipped as "15 */6 * * *" which fires hours 0/6/12/18 UTC. The
# `if now.hour == 2` branch below (the 02:15 window) then never triggered, so
# translation_health / convergence_qa / litigation_baselines_refresh were dead
# in production. 2026-04-21 fix: pin the hour list explicitly so 02 is in it.
# ==========================================================================

@app.function(image=image, schedule=modal.Cron("15 2,8,14,20 * * *"), timeout=600,
              secrets=[scanner_secrets, supabase_secrets])
def dispatch_observability() -> dict:
    from datetime import datetime, timezone
    from modal_workers.biotech_enricher import biotech_enrichment_sweep
    from modal_workers.observability import (
        convergence_qa, edgar_runtime_health, litigation_baselines_refresh, orphan_convergence_sweeper,
        precision_auditor, provisional_convergence_audit, scanner_probe, thesis_jobs_sla_sweeper,
        timing_auditor, translation_health,
    )
    from modal_workers.legal_enricher import legal_enrichment_sweep
    from modal_workers.pre_edge_monitor import pre_edge_monitor
    now = datetime.now(timezone.utc)
    results: dict = {"utc": now.isoformat(), "ran": []}

    # Always: scanner_probe (spec §7.6.2 every-6h cadence).
    try:
        results["scanner_probe"] = scanner_probe()
        results["ran"].append("scanner_probe")
    except Exception as e:
        results["scanner_probe_error"] = str(e)

    # Always: EDGAR-specific degradation rule for repeated budget-bound or
    # zero-coverage runs on the highest-priority source.
    try:
        results["edgar_runtime_health"] = edgar_runtime_health()
        results["ran"].append("edgar_runtime_health")
    except Exception as e:
        results["edgar_runtime_health_error"] = str(e)

    # Always: heal signals dropped by webhook burst (idempotent reactor replay).
    try:
        results["orphan_convergence_sweeper"] = orphan_convergence_sweeper()
        results["ran"].append("orphan_convergence_sweeper")
    except Exception as e:
        results["orphan_convergence_sweeper_error"] = str(e)

    # Always: provisional-row invariant audit — catches reactor-gate regressions
    # that let a provisional row get convergence-stamped. Does NOT auto-fix.
    try:
        results["provisional_convergence_audit"] = provisional_convergence_audit()
        results["ran"].append("provisional_convergence_audit")
    except Exception as e:
        results["provisional_convergence_audit_error"] = str(e)

    # Always: thesis_jobs SLA breach detection + auto-reset for stuck `scoring` rows.
    try:
        results["thesis_jobs_sla_sweeper"] = thesis_jobs_sla_sweeper()
        results["ran"].append("thesis_jobs_sla_sweeper")
    except Exception as e:
        results["thesis_jobs_sla_sweeper_error"] = str(e)

    # Always: deterministic pre-edge lifecycle guard.
    try:
        results["pre_edge_monitor"] = pre_edge_monitor()
        results["ran"].append("pre_edge_monitor")
    except Exception as e:
        results["pre_edge_monitor_error"] = str(e)

    # 20:00-20:59 UTC window: daily price tracker (spawned via dispatcher to
    # stay under Modal's 5-cron plan limit; previously had its own @modal.Cron
    # at 23:30 UTC, but that pushed the app to 6 schedules and blocked deploy).
    # 20:15 UTC ≈ 16:15 ET — late-session, US daily close not yet final, so
    # yfinance prev-day data is still the previous trading day; the price
    # tracker reads `realized_move_*` over a window already settled.
    if now.hour == 20:
        import sys
        me = sys.modules[__name__]
        try:
            results["evaluate_ticker_movement"] = me.evaluate_ticker_movement.spawn().object_id
            results["ran"].append("evaluate_ticker_movement")
        except Exception as e:
            results["evaluate_ticker_movement_error"] = str(e)

    # 02:00-02:59 UTC window (the :15 run): daily sweeps.
    if now.hour == 2:
        try:
            results["translation_health"] = translation_health()
            results["ran"].append("translation_health")
        except Exception as e:
            results["translation_health_error"] = str(e)
        try:
            results["convergence_qa"] = convergence_qa()
            results["ran"].append("convergence_qa")
        except Exception as e:
            results["convergence_qa_error"] = str(e)
        try:
            results["legal_enrichment_sweep"] = legal_enrichment_sweep()
            results["ran"].append("legal_enrichment_sweep")
        except Exception as e:
            results["legal_enrichment_sweep_error"] = str(e)
        try:
            results["biotech_enrichment_sweep"] = biotech_enrichment_sweep()
            results["ran"].append("biotech_enrichment_sweep")
        except Exception as e:
            results["biotech_enrichment_sweep_error"] = str(e)
        # Sunday: litigation baselines + Phase 1d precision/timing auditors.
        if now.weekday() == 6:  # Sunday
            try:
                results["litigation_baselines_refresh"] = litigation_baselines_refresh()
                results["ran"].append("litigation_baselines_refresh")
            except Exception as e:
                results["litigation_baselines_refresh_error"] = str(e)
            try:
                results["precision_auditor"] = precision_auditor()
                results["ran"].append("precision_auditor")
            except Exception as e:
                results["precision_auditor_error"] = str(e)
            try:
                results["timing_auditor"] = timing_auditor()
                results["ran"].append("timing_auditor")
            except Exception as e:
                results["timing_auditor_error"] = str(e)

    return results


# ==========================================================================
# On-demand observability entry points (for manual triggers via `modal run`).
# ==========================================================================

@app.function(image=image, timeout=180, secrets=[supabase_secrets])
def translation_health_once() -> dict:
    from modal_workers.observability import translation_health
    return translation_health()


@app.function(image=image, timeout=180, secrets=[supabase_secrets])
def scanner_probe_once() -> dict:
    from modal_workers.observability import scanner_probe
    return scanner_probe()


@app.function(image=image, timeout=180, secrets=[supabase_secrets])
def edgar_runtime_health_once() -> dict:
    from modal_workers.observability import edgar_runtime_health
    return edgar_runtime_health()


@app.function(image=image, timeout=240, secrets=[supabase_secrets])
def convergence_qa_once() -> dict:
    from modal_workers.observability import convergence_qa
    return convergence_qa()


@app.function(image=image, timeout=240, secrets=[supabase_secrets])
def pre_edge_monitor_once() -> dict:
    from modal_workers.pre_edge_monitor import pre_edge_monitor
    return pre_edge_monitor()


@app.function(image=image, timeout=240, secrets=[supabase_secrets])
def legal_enrichment_once() -> dict:
    from modal_workers.legal_enricher import legal_enrichment_sweep
    return legal_enrichment_sweep()


@app.function(image=image, timeout=240, secrets=[supabase_secrets])
def biotech_enrichment_once() -> dict:
    from modal_workers.biotech_enricher import biotech_enrichment_sweep
    return biotech_enrichment_sweep()


@app.function(image=image, timeout=300, secrets=[supabase_secrets])
def litigation_baselines_refresh_once() -> dict:
    from modal_workers.observability import litigation_baselines_refresh
    return litigation_baselines_refresh()




@app.function(image=image, timeout=600, secrets=[supabase_secrets])
def orphan_convergence_sweeper_once() -> dict:
    from modal_workers.observability import orphan_convergence_sweeper
    return orphan_convergence_sweeper()


@app.function(image=image, timeout=300, secrets=[supabase_secrets])
def precision_auditor_once() -> dict:
    from modal_workers.observability import precision_auditor
    return precision_auditor()


@app.function(image=image, timeout=300, secrets=[supabase_secrets])
def timing_auditor_once() -> dict:
    from modal_workers.observability import timing_auditor
    return timing_auditor()


# ==========================================================================
# Daily price tracker — feeds signal_price_snapshots and the
# outcomes.realized_move_{1d,7d,30d} columns the timing/precision auditors
# read. Cron pinned to 23:30 UTC (~18:30 ET) so the US daily close is settled
# in yfinance before we fetch.
# ==========================================================================

@app.function(image=image, timeout=1800,
              secrets=[scanner_secrets, supabase_secrets])
def evaluate_ticker_movement() -> dict:
    # Spawned daily by `dispatch_observability` at the 20:15 UTC tick — schedule
    # was lifted off this function to stay under Modal's 5-cron plan limit.
    # Manual runs available via `evaluate_ticker_movement_once`.
    from modal_workers.evaluators.price_tracker import run_price_tracker
    return run_price_tracker()


@app.function(image=image, timeout=1800, secrets=[scanner_secrets, supabase_secrets])
def evaluate_ticker_movement_once() -> dict:
    from modal_workers.evaluators.price_tracker import run_price_tracker
    return run_price_tracker()


